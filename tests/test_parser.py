import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from app.alerts import AlertStore
from app.main import create_app
from app.ollama_client import OllamaLogParser
from app.parser import HybridLogParser, build_rule_eval_document, prepare_rule_eval_target
from app.schemas import ParsedLog, SchemaFieldMapping
from app.worker import process_batch, process_raw_message


class CountingFakeLLM:
    def __init__(self, fail_infer: bool = False) -> None:
        self.parse_calls = 0
        self.infer_calls = 0
        self.fail_infer = fail_infer

    async def parse(self, raw_log: str) -> ParsedLog:
        self.parse_calls += 1
        return ParsedLog(
            timestamp="2026-11-21T07:00:00Z",
            level="ERROR",
            service="plain-svc",
            message=raw_log,
            summary=f"LLM parsed: {raw_log[:40]}",
            metadata={"from_llm": True},
            raw=raw_log,
            parse_mode="llm",
        )

    async def infer_schema_mapping(self, sample_doc: dict[str, Any]) -> SchemaFieldMapping:
        self.infer_calls += 1
        if self.fail_infer:
            raise RuntimeError("ollama timeout")
        return SchemaFieldMapping(
            timestamp_ptr="/hdr/created",
            level_ptr="/body/prio",
            service_ptr="/hdr/app_id",
            message_ptr="/body/detail",
        )

    async def close(self) -> None:
        return None


class FakeProducer:
    def __init__(self) -> None:
        self.sent: dict[str, list[bytes]] = defaultdict(list)

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        self.sent[topic or "default"].append(payload)


def test_fast_json_skips_llm_for_appendix_a_schemas() -> None:
    llm = CountingFakeLLM()
    parser = HybridLogParser(llm_parser=llm)

    workflow_event = json.dumps(
        {
            "type": "WORKFLOW_FAILED",
            "workflowId": "WF-20261023-00042",
            "workflowName": "order-settlement",
            "step": "validate-counterparty",
            "severity": "ERROR",
            "environment": "prod",
            "timestamp": "2026-10-23T09:14:02.331Z",
            "error": {
                "code": "TIMEOUT",
                "message": "Counterparty service did not respond in 5000 ms",
            },
        }
    )
    alertmanager_event = json.dumps(
        {
            "alertname": "HighErrorRate",
            "labels": {
                "service": "payments-api",
                "region": "eu-west",
                "severity": "critical",
            },
            "annotations": {"summary": "Error rate 12% over the last 5 minutes"},
            "startsAt": "2026-10-23T09:13:00Z",
            "status": "firing",
        }
    )
    business_event = json.dumps(
        {
            "meta": {
                "schema": "trade.rejected.v2",
                "producer": "trade-gateway",
                "ts": 1792833245000,
            },
            "payload": {
                "tradeId": "T-998877",
                "desk": "rates",
                "notional": 25000000,
                "currency": "EUR",
                "reason": "LIMIT_BREACH",
            },
        }
    )

    async def _run() -> tuple[ParsedLog, ParsedLog, ParsedLog]:
        p1 = await parser.parse(workflow_event)
        p2 = await parser.parse(alertmanager_event)
        p3 = await parser.parse(business_event)
        return p1, p2, p3

    p1, p2, p3 = asyncio.run(_run())

    assert llm.parse_calls == 0
    assert llm.infer_calls == 0

    assert p1.parse_mode == "fast_json"
    assert p1.level == "ERROR"
    assert p1.service == "order-settlement"
    assert p1.message == "Counterparty service did not respond in 5000 ms"
    assert p1.event is not None and p1.event["error"]["code"] == "TIMEOUT"

    assert p2.parse_mode == "fast_json"
    assert p2.level == "FATAL"
    assert p2.service == "payments-api"
    assert p2.message == "Error rate 12% over the last 5 minutes"

    assert p3.parse_mode == "fast_json"
    assert p3.level == "WARN"
    assert p3.service == "trade-gateway"
    assert p3.message == "LIMIT_BREACH"
    assert p3.event is not None and p3.event["payload"]["notional"] == 25000000


def test_fast_yaml_skips_llm() -> None:
    llm = CountingFakeLLM()
    parser = HybridLogParser(llm_parser=llm)
    yaml_payload = (
        "timestamp: 2026-11-21T03:00:00Z\n"
        "level: WARN\n"
        "service: inventory-sync\n"
        "message: low stock threshold reached\n"
        "sku: GPU-4090\n"
        "remaining: 3"
    )
    parsed = asyncio.run(parser.parse(yaml_payload))
    assert llm.parse_calls == 0
    assert parsed.parse_mode == "fast_yaml"
    assert parsed.service == "inventory-sync"
    assert parsed.level == "WARN"
    assert parsed.metadata["remaining"] == 3


def test_unknown_json_schema_learns_once_and_caches() -> None:
    llm = CountingFakeLLM()
    parser = HybridLogParser(llm_parser=llm)

    msg1 = json.dumps(
        {
            "hdr": {"app_id": "settlement-v3", "created": "2026-11-21T07:00:00Z"},
            "body": {"prio": "sev1", "detail": "queue overflow", "depth": 9900},
        }
    )
    msg2 = json.dumps(
        {
            "hdr": {"app_id": "settlement-v3", "created": "2026-11-21T07:01:00Z"},
            "body": {"prio": "sev2", "detail": "retry budget exhausted", "depth": 12000},
        }
    )

    async def _run() -> tuple[ParsedLog, ParsedLog]:
        r1 = await parser.parse(msg1)
        r2 = await parser.parse(msg2)
        return r1, r2

    r1, r2 = asyncio.run(_run())
    assert llm.parse_calls == 0
    assert llm.infer_calls == 1
    assert parser.cached_schema_count == 1
    assert r1.parse_mode == "schema_cache"
    assert r1.level == "FATAL"
    assert r1.service == "settlement-v3"
    assert r1.message == "queue overflow"
    assert r2.parse_mode == "schema_cache"
    assert r2.level == "ERROR"
    assert r2.message == "retry budget exhausted"


def test_schema_learning_failure_falls_back_gracefully() -> None:
    llm = CountingFakeLLM(fail_infer=True)
    parser = HybridLogParser(llm_parser=llm)
    alien = json.dumps({"x_a": 1, "x_b": "boom"})

    parsed = asyncio.run(parser.parse(alien))
    assert llm.infer_calls == 1
    assert llm.parse_calls == 0
    assert parsed.parse_mode == "fast_json"
    assert parsed.event == {"x_a": 1, "x_b": "boom"}


def test_unstructured_plain_text_uses_llm() -> None:
    llm = CountingFakeLLM()
    parser = HybridLogParser(llm_parser=llm)
    raw = "<34>Nov 21 07:00:00 auth-02 sshd[123]: Failed password for admin"

    parsed = asyncio.run(parser.parse(raw))
    assert llm.parse_calls == 1
    assert parsed.parse_mode == "llm"


def test_rules_evaluate_original_nested_fields_and_batch_dedups(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "team-rates",
                    "name": "Team Rates High Notional",
                    "severity": "high",
                    "enabled": True,
                    "dedup_window_seconds": 300,
                    "group_by": ["/payload/desk", "/payload/currency"],
                    "notify": ["email"],
                    "all": [
                        {"field": "/meta/schema", "operator": "regex", "value": r"^trade\.rejected\."},
                        {"field": "/payload/desk", "operator": "eq", "value": "rates"},
                        {"field": "/payload/notional", "operator": "gt", "value": 10000000},
                    ],
                },
                {
                    "id": "broken-rule",
                    "name": "Broken Rule",
                    "severity": "low",
                    "enabled": True,
                    "field": "/payload/desk",
                    "operator": "eq",
                    "value": "equities",
                },
            ]
        ),
        encoding="utf-8",
    )

    llm = CountingFakeLLM()
    parser = HybridLogParser(llm_parser=llm)
    producer = FakeProducer()
    store = AlertStore(tmp_path / "alerts.db")

    event_payload = json.dumps(
        {
            "meta": {"schema": "trade.rejected.v2", "producer": "trade-gateway", "ts": 1792833245000},
            "payload": {
                "tradeId": "T-998877",
                "desk": "rates",
                "notional": 25000000,
                "currency": "EUR",
                "reason": "LIMIT_BREACH",
            },
        }
    )

    results = asyncio.run(
        process_batch(
            raw_messages=[event_payload] * 5,
            parser=parser,
            broker=producer,
            parsed_topic="parsed-logs",
            dead_letter_topic="dead-letter-logs",
            concurrency=5,
            rules_file=str(rules_path),
            alert_store=store,
        )
    )

    assert results == [True] * 5
    assert llm.parse_calls == 0
    assert len(producer.sent["parsed-logs"]) == 5
    alerts = store.list_alerts()
    assert len(alerts) == 1
    assert alerts[0].rule_id == "team-rates"
    assert alerts[0].count == 5
    assert alerts[0].group_key == "payload.desk=rates | payload.currency=EUR"


def test_build_rule_eval_document_exposes_both_canonical_and_event_paths() -> None:
    parsed = ParsedLog(
        timestamp="2026-10-23T09:14:02Z",
        level="ERROR",
        service="order-settlement",
        message="timeout",
        summary="order-settlement: timeout",
        metadata={"workflowId": "WF-1"},
        raw="{}",
        event={"workflowName": "order-settlement", "error": {"code": "TIMEOUT"}},
        parse_mode="fast_json",
    )
    doc = build_rule_eval_document(parsed)
    assert doc["level"] == "ERROR"
    assert doc["metadata"]["workflowId"] == "WF-1"
    assert doc["error"]["code"] == "TIMEOUT"
    assert doc["event"]["error"]["code"] == "TIMEOUT"


def test_ollama_parser_preserves_original_json_event_types(tmp_path: Path) -> None:
    raw_event = json.dumps(
        {
            "meta": {"schema": "trade.rejected.v2"},
            "payload": {"notional": 25000000, "currency": "EUR"},
        }
    )
    # Simulate an LLM that retypes notional to string and drops nested payload.
    llm_json = json.dumps(
        {
            "timestamp": None,
            "level": "WARN",
            "service": "trade-gateway",
            "message": "trade rejected",
            "summary": "Trade rejected.",
            "metadata": {"notional": "25000000"},
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": llm_json}})

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "rates-notional",
                    "name": "Rates Notional",
                    "severity": "high",
                    "enabled": True,
                    "field": "/event/payload/notional",
                    "operator": "gt",
                    "value": 10000000,
                }
            ]
        ),
        encoding="utf-8",
    )

    async def _run() -> tuple[ParsedLog, FakeProducer]:
        producer = FakeProducer()
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            parser = OllamaLogParser("http://ollama:11434", "qwen2.5:1.5b", client=client)
            parsed = await parser.parse(raw_event)
            await process_raw_message(
                raw_text=raw_event,
                parser=parser,
                broker=producer,
                parsed_topic="parsed-logs",
                dead_letter_topic="dead-letter-logs",
                rules_file=str(rules_path),
            )
            return parsed, producer

    parsed, producer = asyncio.run(_run())
    assert parsed.event is not None
    assert parsed.event["payload"]["notional"] == 25000000
    saved = json.loads(producer.sent["parsed-logs"][0].decode("utf-8"))
    assert saved["alert"] is True
    assert saved["matched_rules"][0]["id"] == "rates-notional"


def test_rules_evaluate_api_supports_event_and_raw_paths(tmp_path: Path) -> None:
    class _DummyBroker:
        async def start(self) -> None: ...
        async def stop(self) -> None: ...

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            [
                {
                    "id": "event-path-rule",
                    "name": "Event Path Rule",
                    "severity": "high",
                    "enabled": True,
                    "field": "/event/payload/notional",
                    "operator": "gt",
                    "value": 10000000,
                }
            ]
        ),
        encoding="utf-8",
    )
    app = create_app(
        broker=_DummyBroker(),  # type: ignore[arg-type]
        rules_file=str(rules_path),
        alert_store=AlertStore(tmp_path / "alerts.db"),
    )
    with TestClient(app) as client:
        # Raw event without wrapper still matches /event/payload/notional
        resp = client.post(
            "/rules/evaluate",
            json={"message": {"payload": {"notional": 25000000}}},
        )
        assert resp.status_code == 200
        assert resp.json()["matched"] is True

        # ParsedLog dict with `event` matches top-level /payload/notional too
        target = prepare_rule_eval_target(
            {"level": "WARN", "event": {"payload": {"notional": 25000000}}}
        )
        assert target["payload"]["notional"] == 25000000
        assert target["event"]["payload"]["notional"] == 25000000
