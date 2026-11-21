import asyncio
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.alerts import AlertStore
from app.main import create_app
from app.ollama_client import OllamaLogParser
from app.schemas import NotificationDecision, ParsedLog, RuleMatch
from app.worker import process_raw_message


class FakeBroker:
    def __init__(self) -> None:
        self.messages: dict[str, list[bytes]] = defaultdict(list)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        self.messages[topic or "raw-logs"].append(payload)


def _sample_log(
    service: str = "pricing-engine",
    message: str = "cache miss spike",
    metadata: dict | None = None,
) -> ParsedLog:
    return ParsedLog(
        timestamp="2026-11-21T10:00:00Z",
        level="ERROR",
        service=service,
        message=message,
        summary=f"{service}: {message}",
        metadata=metadata or {},
        raw=f"ERROR {service} {message}",
    )


def test_deduplication_and_grouping_by_field(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    rule_dict = {
        "id": "pricing-warn",
        "name": "Pricing Engine Alert",
        "severity": "high",
        "dedup_window_seconds": 300,
        "group_by": ["/service", "/metadata/instrument"],
    }
    match = RuleMatch(id="pricing-warn", name="Pricing Engine Alert", severity="high")
    notif = [
        NotificationDecision(
            rule_id="pricing-warn",
            rule_name="Pricing Engine Alert",
            severity="high",
            channel="email",
            target="pricing@example.com",
            status="routed",
            reason="Routed via email",
        )
    ]

    t0 = datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)
    # 5 identical EURUSD alerts within 2 minutes -> 1 alert with count=5
    first_alert, notify_1 = store.record_match(
        _sample_log(metadata={"instrument": "EURUSD"}), match, rule_dict, notif, at=t0
    )
    assert notify_1 is True
    assert first_alert.count == 1

    for i in range(1, 5):
        merged, should_notify = store.record_match(
            _sample_log(metadata={"instrument": "EURUSD"}),
            match,
            rule_dict,
            notif,
            at=t0 + timedelta(seconds=i * 20),
        )
        assert should_notify is False
        assert merged.id == first_alert.id
        assert merged.count == i + 1

    # Different instrument -> separate alert
    gbp_alert, notify_gbp = store.record_match(
        _sample_log(metadata={"instrument": "GBPUSD"}),
        match,
        rule_dict,
        notif,
        at=t0 + timedelta(seconds=30),
    )
    assert notify_gbp is True
    assert gbp_alert.id != first_alert.id
    assert gbp_alert.count == 1

    # Outside 300s window -> opens a new alert
    later_alert, notify_later = store.record_match(
        _sample_log(metadata={"instrument": "EURUSD"}),
        match,
        rule_dict,
        notif,
        at=t0 + timedelta(seconds=600),
    )
    assert notify_later is True
    assert later_alert.id != first_alert.id


def test_alert_lifecycle_api_ack_resolve_reopen(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    rule_dict = {"id": "r1", "name": "DB Timeout", "severity": "critical"}
    match = RuleMatch(id="r1", name="DB Timeout", severity="critical")
    alert, _ = store.record_match(_sample_log(service="db-migrator"), match, rule_dict, [])

    broker = FakeBroker()
    app = create_app(broker=broker, rules_file=str(tmp_path / "rules.json"), alert_store=store)

    with TestClient(app) as client:
        listed = client.get("/alerts?status=open").json()
        assert len(listed) == 1
        assert listed[0]["id"] == alert.id
        assert listed[0]["status"] == "open"

        acked = client.post(
            f"/alerts/{alert.id}/ack",
            json={"actor": "anna", "note": "Investigating DB pool"},
        )
        assert acked.status_code == 200
        assert acked.json()["status"] == "acked"
        assert acked.json()["acknowledged_by"] == "anna"

        resolved = client.post(
            f"/alerts/{alert.id}/resolve",
            json={"actor": "anna", "note": "Restarted pooler"},
        )
        assert resolved.status_code == 200
        assert resolved.json()["status"] == "resolved"
        assert resolved.json()["resolved_by"] == "anna"
        assert len(resolved.json()["history"]) == 3

        reopened = client.post(f"/alerts/{alert.id}/reopen", json={"actor": "bob"})
        assert reopened.status_code == 200
        assert reopened.json()["status"] == "open"
        assert reopened.json()["resolved_by"] is None

        # Once resolved, a new matching log opens a brand-new alert
        client.post(f"/alerts/{alert.id}/resolve", json={"actor": "bob"})
        fresh, should_notify = store.record_match(
            _sample_log(service="db-migrator"), match, rule_dict, []
        )
        assert should_notify is True
        assert fresh.id != alert.id


def test_worker_deduplicates_notifications(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "err-rule",
                    "name": "Error Rule",
                    "severity": "high",
                    "dedup_window_seconds": 300,
                    "notify": [{"channel": "email", "to": "team@example.com"}],
                    "field": "/level",
                    "operator": "eq",
                    "value": "ERROR",
                }
            ]
        ),
        encoding="utf-8",
    )
    store = AlertStore(tmp_path / "alerts.db")
    llm_json = json.dumps(
        {
            "timestamp": "2026-11-21T10:00:00Z",
            "level": "ERROR",
            "service": "checkout-api",
            "message": "gateway timeout",
            "summary": "Gateway timed out.",
            "metadata": {},
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": llm_json}})

    async def _run() -> FakeBroker:
        broker = FakeBroker()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            parser = OllamaLogParser("http://ollama:11434", "qwen2.5:1.5b", client=client)
            await process_raw_message(
                "raw-1", parser, broker, "parsed-logs", "dlq", str(rules_path), store
            )
            await process_raw_message(
                "raw-2", parser, broker, "parsed-logs", "dlq", str(rules_path), store
            )
        return broker

    broker = asyncio.run(_run())
    first_log = json.loads(broker.messages["parsed-logs"][0].decode("utf-8"))
    second_log = json.loads(broker.messages["parsed-logs"][1].decode("utf-8"))

    assert first_log["notifications"][0]["status"] == "routed"
    assert second_log["notifications"][0]["status"] == "suppressed"
    assert "Deduplicated" in second_log["notifications"][0]["reason"]

    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].count == 2
