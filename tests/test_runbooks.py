import json
from collections import defaultdict
from pathlib import Path

from fastapi.testclient import TestClient

from app.alerts import AlertStore
from app.main import create_app
from app.runbooks import resolve_runbook_for_alert
from app.schemas import ParsedLog, RuleMatch, RunbookDefinition


class FakeBroker:
    def __init__(self) -> None:
        self.messages: dict[str, list[bytes]] = defaultdict(list)

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_raw(self, payload: bytes, topic: str | None = None) -> None:
        self.messages[topic or "raw-logs"].append(payload)


def _make_alert(store: AlertStore, rule_id: str, service: str, metadata: dict):
    log = ParsedLog(
        timestamp="2026-11-21T10:00:00Z",
        level="ERROR",
        service=service,
        message="upstream timeout",
        summary=f"{service} timed out",
        metadata=metadata,
        raw="raw",
    )
    match = RuleMatch(id=rule_id, name=f"Rule {rule_id}", severity="high")
    alert, _ = store.record_match(log, match, {"id": rule_id}, [])
    return alert


def test_yaml_markdown_and_text_runbook_interpolation(tmp_path: Path) -> None:
    store = AlertStore(tmp_path / "alerts.db")

    yaml_rb = RunbookDefinition(
        id="rb-yaml",
        name="Settlement Timeout",
        format="yaml",
        rule_ids=["settlement-rule"],
        content=(
            "runbook: settlement-timeout\n"
            "steps:\n"
            '  - check: "Check {{service}} health"\n'
            "    action: http_get\n"
            '    url: "http://{{service}}/health"\n'
            "    approval: none\n"
            '  - check: "Retry {{workflowId}}"\n'
            "    action: retry_workflow\n"
            '    workflowId: "{{workflowId}}"\n'
            "    approval: none\n"
            '  - if: "retry fails twice"\n'
            '    check: "Escalate to support"\n'
            "    action: escalate\n"
            "    team: counterparty-support\n"
            "    approval: required\n"
        ),
    )
    md_rb = RunbookDefinition(
        id="rb-md",
        name="Payments Playbook",
        format="markdown",
        services=["payments-api"],
        content=(
            "# HighErrorRate on {{service}}\n"
            "1. Check dashboard for {{service}}.\n"
            "2. Fail traffic over to secondary region (needs approval from team lead).\n"
        ),
    )
    txt_rb = RunbookDefinition(
        id="rb-txt",
        name="Pricing Cache Note",
        format="text",
        services=["pricing-engine"],
        content=(
            "When {{service}} latency exceeds 1s, restart the {{service}} instance "
            "and escalate if latency remains high."
        ),
    )
    all_rbs = [yaml_rb, md_rb, txt_rb]

    # YAML match by rule_id
    a1 = _make_alert(store, "settlement-rule", "order-settlement", {"workflowId": "WF-42"})
    r1 = resolve_runbook_for_alert(a1, all_rbs)
    assert r1 is not None
    assert len(r1.steps) == 3
    assert r1.steps[0].params["url"] == "http://order-settlement/health"
    assert r1.steps[1].params["workflowId"] == "WF-42"
    assert r1.steps[2].approval == "required"

    # Markdown match by service
    a2 = _make_alert(store, "other-rule", "payments-api", {})
    r2 = resolve_runbook_for_alert(a2, all_rbs)
    assert r2 is not None
    assert r2.format == "markdown"
    assert "payments-api" in r2.steps[0].label
    assert r2.steps[1].approval == "required"
    assert r2.steps[1].action == "failover_region"

    # Free-text match by service
    a3 = _make_alert(store, "other-rule", "pricing-engine", {})
    r3 = resolve_runbook_for_alert(a3, all_rbs)
    assert r3 is not None
    assert r3.format == "text"
    assert "pricing-engine" in r3.rendered_content


def test_runbook_approval_gate_and_crud_api(tmp_path: Path) -> None:
    runbooks_path = tmp_path / "runbooks.json"
    runbooks_path.write_text(
        json.dumps(
            [
                {
                    "id": "rb-test",
                    "name": "Checkout Remediation",
                    "format": "yaml",
                    "rule_ids": ["high-latency-error"],
                    "services": ["checkout-api"],
                    "content": (
                        "steps:\n"
                        '  - check: "Probe {{service}}"\n'
                        "    action: http_get\n"
                        "    approval: none\n"
                        '  - check: "Escalate {{metadata.order_id}}"\n'
                        "    action: escalate\n"
                        "    team: payments-oncall\n"
                        "    approval: required\n"
                    ),
                }
            ]
        ),
        encoding="utf-8",
    )

    store = AlertStore(tmp_path / "alerts.db")
    alert = _make_alert(
        store,
        "high-latency-error",
        "checkout-api",
        {"order_id": "ord_98421"},
    )
    app = create_app(
        broker=FakeBroker(),
        rules_file=str(tmp_path / "rules.json"),
        alert_store=store,
        runbooks_file=str(runbooks_path),
    )

    with TestClient(app) as client:
        # Alert includes resolved runbook
        fetched = client.get(f"/alerts/{alert.id}").json()
        assert fetched["runbook"]["id"] == "rb-test"
        assert fetched["runbook"]["steps"][1]["label"] == "Escalate ord_98421"

        # Step 0 (approval: none) executes without approval
        s0 = client.post(
            f"/alerts/{alert.id}/steps/0/execute",
            json={"actor": "anna", "approved": False},
        )
        assert s0.status_code == 200
        assert len(s0.json()["step_executions"]) == 1
        assert "200 OK" in s0.json()["step_executions"][0]["output"]

        # Step 1 (approval: required) rejected without approval
        s1_denied = client.post(
            f"/alerts/{alert.id}/steps/1/execute",
            json={"actor": "anna", "approved": False},
        )
        assert s1_denied.status_code == 403

        # Step 1 succeeds with explicit approval
        s1_ok = client.post(
            f"/alerts/{alert.id}/steps/1/execute",
            json={"actor": "anna", "approved": True},
        )
        assert s1_ok.status_code == 200
        data = s1_ok.json()
        assert len(data["step_executions"]) == 2
        assert data["step_executions"][1]["approved"] is True
        assert "payments-oncall" in data["step_executions"][1]["output"]
        assert any(ev["action"] == "step_executed" for ev in data["history"])

        # Runbook CRUD
        created = client.post(
            "/runbooks",
            json={
                "id": "rb-new",
                "name": "New Wiki Note",
                "format": "text",
                "services": ["billing-worker"],
                "content": "Restart {{service}} when queue stalls.",
            },
        )
        assert created.status_code == 201
        assert len(created.json()) == 2

        deleted = client.delete("/runbooks/rb-new")
        assert deleted.status_code == 200
        assert len(deleted.json()) == 1
