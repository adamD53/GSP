import asyncio
import json
from collections import defaultdict
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.ollama_client import OllamaLogParser
from app.rules import check_message, evaluate_message, validate_rules
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
