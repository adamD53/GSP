import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import notifications as notif_mod
from app.alerts import AlertStore
from app.config import settings
from app.main import create_app
from app.notifications import process_outbox_once
from app.parser import HybridLogParser
from app.schemas import NotificationDecision, ParsedLog
from app.worker import process_raw_message


class FakeProducer:
    def __init__(self) -> None:
        self.sent: dict[str, list[bytes]] = defaultdict(list)

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        self.sent[topic or "default"].append(payload)


class DummyLLM:
    async def parse(self, raw_log: str) -> ParsedLog:
        raise AssertionError("LLM should not be called for JSON test input")

    async def close(self) -> None:
        return None


def test_failed_notification_enqueued_and_recovered_on_retry(
    tmp_path: Path, monkeypatch
) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "fatal-email",
                    "name": "Fatal Email",
                    "severity": "critical",
                    "enabled": True,
                    "notify": [{"channel": "email", "to": "oncall@example.com"}],
                    "field": "/level",
                    "operator": "eq",
                    "value": "FATAL",
                }
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(settings, "smtp_host", "smtp.test.local")
    monkeypatch.setattr(settings, "notification_retry_base_seconds", 10.0)
    monkeypatch.setattr(settings, "notification_max_attempts", 3)

    fail_smtp = True

    def fake_smtp(_host: str, _port: int, _msg) -> None:
        if fail_smtp:
            raise ConnectionRefusedError("SMTP server down")

    monkeypatch.setattr(notif_mod, "_send_smtp_sync", fake_smtp)

    store = AlertStore(tmp_path / "alerts.db")
    producer = FakeProducer()
    parser = HybridLogParser(llm_parser=DummyLLM())
    raw_json = json.dumps(
        {
            "timestamp": "2026-11-21T07:00:00Z",
            "level": "FATAL",
            "service": "payments-api",
            "message": "database unreachable",
        }
    )

    ok = asyncio.run(
        process_raw_message(
            raw_text=raw_json,
            parser=parser,
            broker=producer,
            parsed_topic="parsed-logs",
            dead_letter_topic="dead-letter-logs",
            rules_file=str(rules_path),
            alert_store=store,
        )
    )
    assert ok is True

    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].notifications[0].status == "failed"

    outbox = store.list_outbox(status="pending")
    assert len(outbox) == 1
    assert outbox[0].alert_id == alerts[0].id
    assert outbox[0].attempts == 1
    assert "SMTP server down" in (outbox[0].last_error or "")

    # Channel recovers -> run retry pass after backoff window elapses
    fail_smtp = False
    future = datetime.now(timezone.utc) + timedelta(seconds=30)
    stats = asyncio.run(process_outbox_once(store, broker=producer, at=future))
    assert stats == {"processed": 1, "sent": 1, "retried": 0, "dead_lettered": 0}

    sent_items = store.list_outbox(status="sent")
    assert len(sent_items) == 1
    assert sent_items[0].attempts == 2

    # Parent alert notification status updated to sent
    refreshed = store.get_alert(alerts[0].id)
    assert refreshed is not None
    assert refreshed.notifications[0].status == "sent"
    assert "after retry" in refreshed.notifications[0].reason


def test_outbox_exponential_backoff_and_dlq_publish(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", "smtp.test.local")

    def always_fail(_host: str, _port: int, _msg) -> None:
        raise TimeoutError("SMTP timeout")

    monkeypatch.setattr(notif_mod, "_send_smtp_sync", always_fail)

    store = AlertStore(tmp_path / "alerts.db")
    producer = FakeProducer()
    t0 = datetime(2026, 11, 21, 8, 0, 0, tzinfo=timezone.utc)

    decision = NotificationDecision(
        rule_id="r1",
        rule_name="Rule 1",
        severity="critical",
        channel="email",
        target="ops@example.com",
        working_hours_only=False,
        status="failed",
        reason="SMTP error: SMTP timeout",
    )
    enqueued = store.enqueue_failed_notifications(
        alert_id="alrt_test123",
        decisions=[decision],
        log_summary="core service down",
        service="core",
        max_attempts=3,
        base_delay_seconds=10.0,
        at=t0,
    )
    assert len(enqueued) == 1
    assert enqueued[0].next_retry_at == (t0 + timedelta(seconds=10)).isoformat()

    # Before next_retry_at -> nothing due
    s0 = asyncio.run(
        process_outbox_once(store, broker=producer, base_delay_seconds=10.0, at=t0 + timedelta(seconds=5))
    )
    assert s0["processed"] == 0

    # Attempt #2 fails -> exponential backoff = 10 * 2^(2-1) = 20s
    t1 = t0 + timedelta(seconds=11)
    s1 = asyncio.run(
        process_outbox_once(store, broker=producer, base_delay_seconds=10.0, at=t1)
    )
    assert s1 == {"processed": 1, "sent": 0, "retried": 1, "dead_lettered": 0}
    pending = store.list_outbox(status="pending")
    assert len(pending) == 1
    assert pending[0].attempts == 2
    assert pending[0].next_retry_at == (t1 + timedelta(seconds=20)).isoformat()

    # Attempt #3 fails -> reaches max_attempts=3 -> moves to dead_letter + Kafka DLQ
    t2 = t1 + timedelta(seconds=21)
    s2 = asyncio.run(
        process_outbox_once(
            store,
            broker=producer,
            dead_letter_topic="notification-dead-letter",
            base_delay_seconds=10.0,
            at=t2,
        )
    )
    assert s2 == {"processed": 1, "sent": 0, "retried": 0, "dead_lettered": 1}
    dlq_rows = store.list_outbox(status="dead_letter")
    assert len(dlq_rows) == 1
    assert dlq_rows[0].attempts == 3
    assert len(producer.sent["notification-dead-letter"]) == 1
    dlq_msg = json.loads(producer.sent["notification-dead-letter"][0].decode("utf-8"))
    assert dlq_msg["alert_id"] == "alrt_test123"
    assert dlq_msg["status"] == "dead_letter"


def test_outbox_api_list_requeue_and_process(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "smtp_host", None)
    store = AlertStore(tmp_path / "alerts.db")
    producer = FakeProducer()

    decision = NotificationDecision(
        rule_id="r1",
        rule_name="Rule 1",
        severity="high",
        channel="email",
        target="ops@example.com",
        working_hours_only=False,
        status="failed",
        reason="SMTP error: down",
    )
    enqueued = store.enqueue_failed_notifications(
        alert_id="alrt_api1",
        decisions=[decision],
        log_summary="alert body",
        service="api",
        max_attempts=1,  # immediately dead_letter
    )
    outbox_id = enqueued[0].id
    assert enqueued[0].status == "dead_letter"

    app = create_app(
        broker=producer,  # type: ignore[arg-type]
        alert_store=store,
    )
    with TestClient(app) as client:
        res = client.get("/notifications/outbox?status=dead_letter")
        assert res.status_code == 200
        assert len(res.json()) == 1

        requeue = client.post(f"/notifications/outbox/{outbox_id}/retry")
        assert requeue.status_code == 200
        assert requeue.json()["status"] == "pending"
        assert requeue.json()["attempts"] == 0

        proc = client.post("/notifications/outbox/process")
        assert proc.status_code == 200
        assert proc.json()["sent"] == 1

        sent = client.get("/notifications/outbox?status=sent")
        assert sent.status_code == 200
        assert len(sent.json()) == 1
