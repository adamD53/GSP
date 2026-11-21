import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ollama_client import OllamaLogParser
from app.rules import check_message, evaluate_and_route, evaluate_message, validate_rules
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


def test_check_message_single_rule_and_json_pointer_escaping(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "field": "/meta/a~1b/c~0d",
                "operator": "eq",
                "value": 42,
            }
        ),
        encoding="utf-8",
    )

    assert check_message({"meta": {"a/b": {"c~d": 42}}}, rules_path) is True
    assert check_message({"meta": {"a/b": {"c~d": 41}}}, rules_path) is False
    assert check_message({"meta": {}}, rules_path) is False


def test_combinators_all_any_not_and_numeric_safety(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "high-latency-err",
                    "name": "High Latency Error",
                    "severity": "critical",
                    "all": [
                        {"field": "/level", "operator": "in", "value": ["ERROR", "FATAL"]},
                        {"field": "/metadata/latency_ms", "operator": "gt", "value": 3000},
                    ],
                },
                {
                    "id": "disabled-rule",
                    "name": "Disabled",
                    "enabled": False,
                    "field": "/level",
                    "operator": "eq",
                    "value": "INFO",
                },
            ]
        ),
        encoding="utf-8",
    )

    assert (
        check_message({"level": "ERROR", "metadata": {"latency_ms": 5021}}, rules_path)
        is True
    )
    # Boolean should not be treated as a number for numeric comparisons.
    assert (
        check_message({"level": "ERROR", "metadata": {"latency_ms": True}}, rules_path)
        is False
    )
    # String should not match numeric gt.
    assert (
        check_message({"level": "ERROR", "metadata": {"latency_ms": "5021"}}, rules_path)
        is False
    )
    # Disabled rule must not match.
    assert check_message({"level": "INFO"}, rules_path) is False


def test_in_operator_safe_on_non_iterable() -> None:
    matches = evaluate_message(
        {"level": "ERROR"},
        [{"field": "/level", "operator": "in", "value": 123}],
    )
    assert matches == []


def test_extra_operators_contains_regex_exists_list_index() -> None:
    rules = [
        {
            "id": "r1",
            "name": "Contains + List Index",
            "severity": "high",
            "all": [
                {"field": "/message", "operator": "contains", "value": "timeout"},
                {"field": "/tags/0", "operator": "regex", "value": "^prod-"},
                {"field": "/metadata/order_id", "operator": "exists", "value": True},
            ],
        }
    ]
    msg = {
        "message": "Payment gateway TIMEOUT occurred",
        "tags": ["prod-eu-1"],
        "metadata": {"order_id": "ord_1"},
    }
    matches = evaluate_message(msg, rules)
    assert len(matches) == 1
    assert matches[0].id == "r1"
    assert matches[0].severity == "high"


def test_validate_rules_rejects_bad_path_or_operator() -> None:
    with pytest.raises(ValueError, match="Field must use a path"):
        validate_rules({"field": "level", "operator": "eq", "value": "ERROR"})

    with pytest.raises(ValueError, match="Unsupported operator"):
        validate_rules({"field": "/level", "operator": "approx", "value": "ERROR"})


def test_rules_api_crud_and_evaluate(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    broker = FakeBroker()
    app = create_app(broker=broker, rules_file=str(rules_path))

    sample_rules = [
        {
            "id": "err-rule",
            "name": "Error Alert",
            "severity": "high",
            "enabled": True,
            "field": "/level",
            "operator": "eq",
            "value": "ERROR",
        }
    ]

    with TestClient(app) as client:
        assert client.get("/rules").json() == []

        put_resp = client.put("/rules", json=sample_rules)
        assert put_resp.status_code == 200
        assert put_resp.json() == sample_rules

        get_resp = client.get("/rules")
        assert get_resp.status_code == 200
        assert get_resp.json() == sample_rules

        eval_hit = client.post(
            "/rules/evaluate",
            json={"message": {"level": "ERROR", "service": "api"}},
        )
        assert eval_hit.status_code == 200
        assert eval_hit.json()["matched"] is True
        assert eval_hit.json()["matched_rules"][0]["id"] == "err-rule"

        eval_miss = client.post(
            "/rules/evaluate",
            json={"message": {"level": "INFO", "service": "api"}},
        )
        assert eval_miss.status_code == 200
        assert eval_miss.json()["matched"] is False

        bad_put = client.put(
            "/rules",
            json=[{"field": "no-leading-slash", "operator": "eq", "value": 1}],
        )
        assert bad_put.status_code == 400


def test_worker_attaches_matched_rules(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "checkout-slow-error",
                    "name": "Slow Checkout Error",
                    "severity": "critical",
                    "all": [
                        {"field": "/service", "operator": "eq", "value": "checkout-api"},
                        {"field": "/metadata/latency_ms", "operator": "gt", "value": 3000},
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    llm_json = json.dumps(
        {
            "timestamp": "2026-11-21T03:00:00Z",
            "level": "ERROR",
            "service": "checkout-api",
            "message": "payment gateway timeout",
            "summary": "Checkout timed out talking to payment gateway.",
            "metadata": {"latency_ms": 5021},
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": llm_json}})

    async def _run() -> FakeBroker:
        broker = FakeBroker()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            parser = OllamaLogParser("http://ollama:11434", "qwen2.5:1.5b", client=client)
            ok = await process_raw_message(
                raw_text="raw checkout timeout",
                parser=parser,
                broker=broker,
                parsed_topic="parsed-logs",
                dead_letter_topic="dead-letter-logs",
                rules_file=str(rules_path),
            )
            assert ok is True
        return broker

    broker = asyncio.run(_run())
    saved = json.loads(broker.messages["parsed-logs"][0].decode("utf-8"))
    assert saved["alert"] is True
    assert len(saved["matched_rules"]) == 1
    assert saved["matched_rules"][0]["id"] == "checkout-slow-error"
    assert saved["matched_rules"][0]["severity"] == "critical"


def test_notification_routing_email_sms_and_working_hours() -> None:
    rules = [
        {
            "id": "ops-alert",
            "name": "Ops Alert",
            "severity": "high",
            "notify": [
                {"channel": "email", "to": "ops@example.com"},
                {"channel": "sms", "to": "+15551112222", "working_hours_only": False},
                {"channel": "sms", "to": "+15559998888", "working_hours_only": True},
                "sms_working_hours",
            ],
            "field": "/level",
            "operator": "eq",
            "value": "ERROR",
        }
    ]
    msg = {"level": "ERROR"}

    # Tuesday 14:00 UTC -> inside default 09:00-17:00 Mon-Fri UTC
    during_work = datetime(2026, 11, 24, 14, 0, tzinfo=timezone.utc)
    _, decisions_work = evaluate_and_route(msg, rules, at=during_work)
    assert len(decisions_work) == 4
    assert all(d.status == "routed" for d in decisions_work)

    # Tuesday 03:00 UTC -> outside working hours
    off_hours = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    _, decisions_off = evaluate_and_route(msg, rules, at=off_hours)
    assert decisions_off[0].channel == "email" and decisions_off[0].status == "routed"
    assert decisions_off[1].channel == "sms" and decisions_off[1].status == "routed"
    assert decisions_off[2].channel == "sms" and decisions_off[2].status == "suppressed"
    assert decisions_off[3].channel == "sms" and decisions_off[3].status == "suppressed"

    # Saturday 14:00 UTC -> weekend (outside working days)
    weekend = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)
    _, decisions_wknd = evaluate_and_route(msg, rules, at=weekend)
    assert decisions_wknd[0].status == "routed"
    assert decisions_wknd[1].status == "routed"
    assert decisions_wknd[2].status == "suppressed"
    assert decisions_wknd[3].status == "suppressed"


def test_rules_evaluate_api_respects_at_timestamp(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "r-sms",
                    "name": "SMS Work Hours",
                    "severity": "high",
                    "notify": [
                        {"channel": "email", "to": "team@example.com"},
                        "sms_working_hours",
                    ],
                    "field": "/level",
                    "operator": "eq",
                    "value": "ERROR",
                }
            ]
        ),
        encoding="utf-8",
    )

    broker = FakeBroker()
    app = create_app(broker=broker, rules_file=str(rules_path))

    with TestClient(app) as client:
        wh_resp = client.get("/rules/working-hours")
        assert wh_resp.status_code == 200
        assert "description" in wh_resp.json()

        work_resp = client.post(
            "/rules/evaluate",
            json={
                "message": {"level": "ERROR"},
                "at": "2026-11-24T14:00:00Z",
            },
        )
        assert work_resp.status_code == 200
        work_data = work_resp.json()
        assert work_data["within_working_hours"] is True
        assert [n["status"] for n in work_data["notifications"]] == ["routed", "routed"]

        night_resp = client.post(
            "/rules/evaluate",
            json={
                "message": {"level": "ERROR"},
                "at": "2026-11-24T02:00:00Z",
            },
        )
        assert night_resp.status_code == 200
        night_data = night_resp.json()
        assert night_data["within_working_hours"] is False
        assert [n["status"] for n in night_data["notifications"]] == [
            "routed",
            "suppressed",
        ]

        bad_channel = client.put(
            "/rules",
            json=[
                {
                    "field": "/level",
                    "operator": "eq",
                    "value": "ERROR",
                    "notify": ["carrier_pigeon"],
                }
            ],
        )
        assert bad_channel.status_code == 400

