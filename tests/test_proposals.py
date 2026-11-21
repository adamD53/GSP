import asyncio
import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.alerts import AlertStore
from app.config import settings
from app.main import create_app
from app.proposals import generate_proposed_fix
from app.schemas import ParsedLog, RuleMatch, StepExecutionRecord


class _DummyBroker:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _seed_alert(store: AlertStore, rule_id: str = "high-latency", count: int = 1):
    parsed = ParsedLog(
        timestamp="2026-11-21T07:00:00Z",
        level="ERROR",
        service="checkout-api",
        message="payment gateway timeout",
        summary="checkout-api: payment gateway timeout",
        metadata={"order_id": "ord_42", "latency_ms": 5400},
        raw="{}",
    )
    match = RuleMatch(id=rule_id, name="High Latency", severity="high")
    rule_dict = {
        "id": rule_id,
        "name": "High Latency",
        "severity": "high",
        "dedup_window_seconds": 300,
        "group_by": ["/service"],
        "field": "/level",
        "operator": "eq",
        "value": "ERROR",
    }
    rec = None
    for _ in range(count):
        rec, _ = store.record_match(parsed, match, rule_dict, [])
    assert rec is not None
    return rec, rule_dict


def test_llm_runbook_proposal_drafting(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")
    alert, rule_dict = _seed_alert(store)

    llm_draft = json.dumps(
        {
            "title": "Add checkout-api timeout runbook",
            "rationale": "Standardizes health probe and retry steps for checkout timeouts.",
            "runbook_name": "Checkout Timeout Playbook",
            "yaml_content": (
                "runbook: rb-checkout-timeout\n"
                "steps:\n"
                "  - check: 'Probe checkout-api health'\n"
                "    action: http_get\n"
                "    approval: none\n"
            ),
        }
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": llm_draft}})

    async def _run():
        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            return await generate_proposed_fix(
                alert=alert,
                rules=[rule_dict],
                runbooks=[],
                target_type="runbook",
                ollama_base_url="http://ollama:11434",
                ollama_model="qwen2.5:1.5b",
                client=client,
            )

    proposal = asyncio.run(_run())
    assert proposal.source == "llm"
    assert proposal.target_type == "runbook"
    assert proposal.proposed_runbook is not None
    assert proposal.proposed_runbook.name == "Checkout Timeout Playbook"


def test_propose_fix_endpoint_is_read_only(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "ollama_base_url", "")
    store = AlertStore(tmp_path / "alerts.db")
    alert, rule_dict = _seed_alert(store, count=6)
    store.record_step_execution(
        alert.id,
        StepExecutionRecord(
            step_index=0,
            runbook_id="rb-old",
            action="restart_service",
            label="Restart checkout-api pods",
            actor="alice",
            approved=True,
            executed_at="2026-11-21T07:05:00Z",
            output="2/2 pods healthy",
        ),
    )

    rules_path = tmp_path / "rules.json"
    rules_path.write_text(json.dumps([rule_dict]), encoding="utf-8")
    runbooks_path = tmp_path / "runbooks.json"
    runbooks_path.write_text("[]", encoding="utf-8")

    app = create_app(
        broker=_DummyBroker(),  # type: ignore[arg-type]
        rules_file=str(rules_path),
        runbooks_file=str(runbooks_path),
        alert_store=store,
    )
    with TestClient(app) as client:
        res = client.post(f"/alerts/{alert.id}/propose-fix", json={"target_type": "runbook"})
        assert res.status_code == 200
        prop = res.json()
        assert prop["source"] == "heuristic"
        assert "Restart checkout-api pods" in prop["proposed_runbook"]["content"]
        # Read-only: neither rules.json nor runbooks.json modified
        assert json.loads(runbooks_path.read_text()) == []
        assert json.loads(rules_path.read_text())[0]["dedup_window_seconds"] == 300
