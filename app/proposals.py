import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
import yaml
from pydantic import BaseModel, Field

from app.config import settings
from app.schemas import (
    AlertRecord,
    ProposalTargetType,
    ProposedFix,
    RunbookDefinition,
)

logger = logging.getLogger("log_proposals")


class _LLMRunbookDraft(BaseModel):
    title: str = Field(description="Short title for this runbook suggestion.")
    rationale: str = Field(description="One-sentence reason why this runbook helps.")
    runbook_name: str = Field(description="Human-readable runbook name.")
    yaml_content: str = Field(
        description=(
            "Valid YAML runbook with top-level keys 'runbook' and 'steps' "
            "(each step has check, action, approval: none|required)."
        )
    )


class _LLMRulePatchDraft(BaseModel):
    title: str = Field(description="Short title for this rule tuning suggestion.")
    rationale: str = Field(description="One-sentence reason why this rule change helps.")
    dedup_window_seconds: int | None = Field(
        default=None,
        description="Suggested deduplication window in seconds, or null if unchanged.",
    )
    description: str | None = Field(
        default=None,
        description="Suggested rule description, or null if unchanged.",
    )
    severity: Literal["critical", "high", "medium", "low"] | None = Field(
        default=None,
        description="Suggested severity, or null if unchanged.",
    )


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "alert"


def _choose_target_type(
    alert: AlertRecord,
    requested: Literal["auto", "runbook", "rule"],
) -> ProposalTargetType:
    if requested in ("runbook", "rule"):
        return requested
    if alert.count >= 5 and not alert.step_executions:
        return "rule"
    return "runbook"


def _build_heuristic_runbook_proposal(
    alert: AlertRecord,
    existing_runbooks: list[RunbookDefinition],
) -> ProposedFix:
    existing = next((rb for rb in existing_runbooks if alert.rule_id in rb.rule_ids), None)
    service = alert.service or "target-service"
    rb_id = existing.id if existing else f"rb-{_slugify(alert.rule_id)}"
    rb_name = existing.name if existing else f"{alert.rule_name} Remediation"

    steps: list[dict[str, Any]] = []
    if alert.step_executions:
        for exec_rec in alert.step_executions:
            steps.append(
                {
                    "check": exec_rec.label,
                    "action": exec_rec.action,
                    "approval": "required" if exec_rec.approved else "none",
                }
            )
    else:
        steps.extend(
            [
                {
                    "check": "Verify {{service}} health endpoint is responsive",
                    "action": "http_get",
                    "url": "http://{{service}}.internal/health",
                    "approval": "none",
                },
                {
                    "check": f"Restart {{{{service}}}} or retry failed operation for '{alert.rule_name}'",
                    "action": "restart_service",
                    "approval": "required",
                },
                {
                    "if": "alert persists after remediation",
                    "check": "Escalate {{service}} incident to on-call owner",
                    "action": "escalate",
                    "team": f"{_slugify(service)}-oncall",
                    "approval": "required",
                },
            ]
        )

    resolution_notes = [
        ev.note for ev in alert.history if ev.action in ("acked", "resolved") and ev.note
    ]
    if resolution_notes:
        steps.append(
            {
                "check": f"Confirm resolution note: {resolution_notes[-1]}",
                "action": "open_ticket",
                "approval": "none",
            }
        )

    yaml_doc = {
        "runbook": rb_id,
        "matches": {"rule_id": alert.rule_id, "service": service},
        "steps": steps,
    }
    yaml_content = yaml.safe_dump(yaml_doc, sort_keys=False)

    services = sorted(set([*(existing.services if existing else []), *([alert.service] if alert.service else [])]))
    rule_ids = sorted(set([*(existing.rule_ids if existing else []), alert.rule_id]))

    proposed_rb = RunbookDefinition(
        id=rb_id,
        name=rb_name,
        format="yaml",
        rule_ids=rule_ids,
        services=services,
        description=f"Suggested from alert {alert.id} ({alert.rule_name}).",
        content=yaml_content,
    )

    action_word = "Update" if existing else "Create"
    rationale = (
        f"Captured {len(alert.step_executions)} executed step(s) from alert {alert.id} into {rb_id}."
        if alert.step_executions
        else f"Suggested health-check, restart, and escalation playbook for {service}."
    )
    return ProposedFix(
        id=f"prop_{uuid.uuid4().hex[:10]}",
        alert_id=alert.id,
        target_type="runbook",
        target_id=rb_id,
        title=f"{action_word} runbook '{rb_name}'",
        rationale=rationale,
        source="heuristic",
        proposed_runbook=proposed_rb,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _build_heuristic_rule_proposal(
    alert: AlertRecord,
    rules: list[dict[str, Any]],
) -> ProposedFix:
    target_rule = next((r for r in rules if r.get("id") == alert.rule_id), None)
    current_window = int(
        (target_rule.get("dedup_window_seconds") if target_rule else None)
        or alert.dedup_window_seconds
        or 300
    )
    suggested_window = min(900, max(current_window * 2, 600))
    patch: dict[str, Any] = {
        "dedup_window_seconds": suggested_window,
    }
    if target_rule and not target_rule.get("group_by"):
        patch["group_by"] = ["/service", "/level"]

    return ProposedFix(
        id=f"prop_{uuid.uuid4().hex[:10]}",
        alert_id=alert.id,
        target_type="rule",
        target_id=alert.rule_id,
        title=f"Reduce alert noise on '{alert.rule_name}'",
        rationale=(
            f"Alert {alert.id} fired {alert.count}x within {current_window}s; "
            f"increasing dedup_window_seconds to {suggested_window}s reduces paging fatigue."
        ),
        source="heuristic",
        proposed_rule_patch=patch,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


async def _try_llm_proposal(
    alert: AlertRecord,
    target_type: ProposalTargetType,
    existing_runbooks: list[RunbookDefinition],
    rules: list[dict[str, Any]],
    ollama_base_url: str,
    ollama_model: str,
    client: httpx.AsyncClient | None = None,
) -> ProposedFix | None:
    context = {
        "alert_id": alert.id,
        "rule_id": alert.rule_id,
        "rule_name": alert.rule_name,
        "severity": alert.severity,
        "status": alert.status,
        "service": alert.service,
        "count": alert.count,
        "dedup_window_seconds": alert.dedup_window_seconds,
        "summary": alert.summary,
        "message": alert.message,
        "sample_metadata": alert.sample_metadata,
        "step_executions": [s.model_dump() for s in alert.step_executions],
        "history": [h.model_dump() for h in alert.history[-6:]],
    }

    owns_client = client is None
    http_client = client or httpx.AsyncClient(
        timeout=settings.proposal_llm_timeout_seconds
    )
    try:
        if target_type == "runbook":
            existing = next((rb for rb in existing_runbooks if alert.rule_id in rb.rule_ids), None)
            rb_id = existing.id if existing else f"rb-{_slugify(alert.rule_id)}"
            payload = {
                "model": ollama_model,
                "stream": False,
                "format": _LLMRunbookDraft.model_json_schema(),
                "options": {"temperature": 0.2, "num_predict": 220},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "You are an SRE assistant. Draft a concise YAML runbook suggestion "
                            "from the alert and its history. Return ONLY valid JSON matching the schema."
                        ),
                    },
                    {"role": "user", "content": json.dumps(context, ensure_ascii=False)[:2500]},
                ],
            }
            resp = await http_client.post(f"{ollama_base_url.rstrip('/')}/api/chat", json=payload)
            resp.raise_for_status()
            draft = _LLMRunbookDraft.model_validate_json(resp.json()["message"]["content"])
            parsed_yaml = yaml.safe_load(draft.yaml_content)
            if not isinstance(parsed_yaml, dict) or not isinstance(parsed_yaml.get("steps"), list):
                return None

            services = sorted(set([*(existing.services if existing else []), *([alert.service] if alert.service else [])]))
            rule_ids = sorted(set([*(existing.rule_ids if existing else []), alert.rule_id]))
            proposed_rb = RunbookDefinition(
                id=rb_id,
                name=draft.runbook_name.strip() or f"{alert.rule_name} Remediation",
                format="yaml",
                rule_ids=rule_ids,
                services=services,
                description=draft.rationale.strip(),
                content=draft.yaml_content,
            )
            return ProposedFix(
                id=f"prop_{uuid.uuid4().hex[:10]}",
                alert_id=alert.id,
                target_type="runbook",
                target_id=rb_id,
                title=draft.title.strip() or f"Update runbook '{proposed_rb.name}'",
                rationale=draft.rationale.strip(),
                source="llm",
                proposed_runbook=proposed_rb,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

        target_rule = next((r for r in rules if r.get("id") == alert.rule_id), None)
        if target_rule is None:
            return None
        payload = {
            "model": ollama_model,
            "stream": False,
            "format": _LLMRulePatchDraft.model_json_schema(),
            "options": {"temperature": 0.2, "num_predict": 160},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an SRE assistant. Suggest a safe rule tuning patch "
                        "(dedup_window_seconds, description, or severity) to reduce alert noise "
                        "or clarify triage. Return ONLY valid JSON matching the schema."
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {"alert": context, "current_rule": target_rule}, ensure_ascii=False
                    )[:2500],
                },
            ],
        }
        resp = await http_client.post(f"{ollama_base_url.rstrip('/')}/api/chat", json=payload)
        resp.raise_for_status()
        draft = _LLMRulePatchDraft.model_validate_json(resp.json()["message"]["content"])
        patch: dict[str, Any] = {}
        if draft.dedup_window_seconds is not None and draft.dedup_window_seconds >= 0:
            patch["dedup_window_seconds"] = int(draft.dedup_window_seconds)
        if draft.description:
            patch["description"] = draft.description.strip()
        if draft.severity:
            patch["severity"] = draft.severity
        if not patch:
            return None

        return ProposedFix(
            id=f"prop_{uuid.uuid4().hex[:10]}",
            alert_id=alert.id,
            target_type="rule",
            target_id=alert.rule_id,
            title=draft.title.strip() or f"Tune rule '{alert.rule_name}'",
            rationale=draft.rationale.strip(),
            source="llm",
            proposed_rule_patch=patch,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception as exc:
        logger.info("LLM proposal fallback to heuristic (%s)", exc)
        return None
    finally:
        if owns_client:
            await http_client.aclose()


async def generate_proposed_fix(
    alert: AlertRecord,
    rules: list[dict[str, Any]],
    runbooks: list[RunbookDefinition],
    target_type: Literal["auto", "runbook", "rule"] = "auto",
    ollama_base_url: str | None = None,
    ollama_model: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> ProposedFix:
    """Generate a read-only runbook or rule enhancement suggestion from alert history."""
    resolved_target = _choose_target_type(alert, target_type)
    base_url = ollama_base_url if ollama_base_url is not None else settings.ollama_base_url
    model = ollama_model if ollama_model is not None else settings.ollama_model

    if base_url:
        llm_proposal = await _try_llm_proposal(
            alert=alert,
            target_type=resolved_target,
            existing_runbooks=runbooks,
            rules=rules,
            ollama_base_url=base_url,
            ollama_model=model,
            client=client,
        )
        if llm_proposal is not None:
            return llm_proposal

    if resolved_target == "rule":
        return _build_heuristic_rule_proposal(alert, rules)
    return _build_heuristic_runbook_proposal(alert, runbooks)
