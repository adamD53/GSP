import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from app.schemas import (
    AlertRecord,
    ApprovalMode,
    ResolvedRunbook,
    RunbookDefinition,
    RunbookStep,
    StepExecutionRecord,
)

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-/]+)\s*\}\}")
_RUNBOOKS_CACHE: dict[str, tuple[float, list[RunbookDefinition]]] = {}


def _lookup_context(context: dict[str, Any], key_path: str) -> Any:
    """Resolve dotted or slash-separated keys like `metadata.order_id` or `workflowId`."""
    cleaned = key_path.strip().lstrip("/")
    segments = [s for s in re.split(r"[./]", cleaned) if s]
    current: Any = context
    for seg in segments:
        if isinstance(current, dict) and seg in current:
            current = current[seg]
        else:
            # Also try looking directly inside metadata for convenience (e.g. {{order_id}})
            meta = context.get("metadata")
            if len(segments) == 1 and isinstance(meta, dict) and seg in meta:
                return meta[seg]
            return None
    return current


def interpolate_template(text: str, context: dict[str, Any]) -> str:
    """Replace {{var}} placeholders using alert/log context; keep readable fallback."""
    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        val = _lookup_context(context, expr)
        if val is None or val == "":
            return f"<{expr}>"
        return str(val)

    return _TEMPLATE_RE.sub(_replace, text)


def _interpolate_obj(obj: Any, context: dict[str, Any]) -> Any:
    if isinstance(obj, str):
        return interpolate_template(obj, context)
    if isinstance(obj, dict):
        return {k: _interpolate_obj(v, context) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_obj(v, context) for v in obj]
    return obj


def _parse_yaml_steps(rendered_yaml: str) -> list[RunbookStep]:
    try:
        doc = yaml.safe_load(rendered_yaml)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    raw_steps = doc.get("steps")
    if not isinstance(raw_steps, list):
        return []

    steps: list[RunbookStep] = []
    for idx, item in enumerate(raw_steps):
        if not isinstance(item, dict):
            steps.append(RunbookStep(index=idx, label=str(item)))
            continue
        action = str(item["action"]).strip() if item.get("action") else None
        raw_approval = str(item.get("approval", "none")).strip().lower()
        approval: ApprovalMode = "required" if raw_approval == "required" else "none"
        cond_note = str(item["if"]) if item.get("if") else None
        label = str(item.get("check") or item.get("label") or action or f"Step {idx + 1}")
        params = {
            k: v
            for k, v in item.items()
            if k not in {"check", "label", "action", "approval", "if"}
        }
        steps.append(
            RunbookStep(
                index=idx,
                label=label,
                action=action,
                params=params,
                approval=approval,
                condition_note=cond_note,
            )
        )
    return steps


def _infer_action_from_text(line: str) -> tuple[str | None, ApprovalMode]:
    lower = line.lower()
    approval: ApprovalMode = (
        "required"
        if any(tok in lower for tok in ("needs approval", "requires approval", "approval required"))
        else "none"
    )
    if "fail" in lower and "over" in lower:
        return "failover_region", "required"
    if "restart" in lower:
        return "restart_service", approval
    if "retry" in lower:
        return "retry_workflow", approval
    if "escalat" in lower or "call the" in lower:
        return "escalate", "required"
    if "health" in lower or "probe" in lower:
        return "http_get", approval
    if "ticket" in lower:
        return "open_ticket", approval
    return None, approval


def _parse_markdown_or_text_steps(rendered: str) -> list[RunbookStep]:
    lines = [ln.strip() for ln in rendered.splitlines() if ln.strip()]
    numbered_or_bullet = [
        re.sub(r"^(\d+[.)]|[-*])\s+", "", ln)
        for ln in lines
        if re.match(r"^(\d+[.)]|[-*])\s+", ln)
    ]
    candidates = numbered_or_bullet if numbered_or_bullet else [
        s.strip()
        for s in re.split(r"(?<=[.!?])\s+", " ".join(lines))
        if s.strip() and not s.strip().startswith("#")
    ]

    steps: list[RunbookStep] = []
    for idx, text in enumerate(candidates):
        action, approval = _infer_action_from_text(text)
        steps.append(
            RunbookStep(
                index=idx,
                label=text,
                action=action,
                params={},
                approval=approval,
            )
        )
    return steps


def resolve_runbook_for_alert(
    alert: AlertRecord,
    runbooks: list[RunbookDefinition],
) -> ResolvedRunbook | None:
    """Find the best matching runbook for an alert and interpolate its steps."""
    matched: RunbookDefinition | None = None

    # 1. Exact rule_id match first
    for rb in runbooks:
        if alert.rule_id in rb.rule_ids:
            matched = rb
            break

    # 2. Fallback: service name match
    if matched is None and alert.service:
        svc_lower = alert.service.lower()
        for rb in runbooks:
            if any(s.lower() == svc_lower for s in rb.services):
                matched = rb
                break

    if matched is None:
        return None

    context: dict[str, Any] = {
        "alert_id": alert.id,
        "rule_id": alert.rule_id,
        "rule_name": alert.rule_name,
        "severity": alert.severity,
        "service": alert.service or "unknown-service",
        "summary": alert.summary,
        "message": alert.message,
        "count": alert.count,
        "group_key": alert.group_key,
        "metadata": alert.sample_metadata or {},
    }
    # Promote top-level metadata keys for shorthand {{workflowId}} / {{order_id}}
    for k, v in (alert.sample_metadata or {}).items():
        context.setdefault(k, v)

    rendered = interpolate_template(matched.content, context)
    if matched.format == "yaml":
        steps = _parse_yaml_steps(rendered)
    else:
        steps = _parse_markdown_or_text_steps(rendered)

    return ResolvedRunbook(
        id=matched.id,
        name=matched.name,
        format=matched.format,
        description=matched.description,
        rendered_content=rendered,
        steps=steps,
    )


# Pluggable simulated action executors (adding a new action = 1 registry entry)
ActionHandler = Callable[[RunbookStep, AlertRecord], str]


def _sim_http_get(step: RunbookStep, alert: AlertRecord) -> str:
    url = step.params.get("url") or f"http://{alert.service or 'service'}.internal/health"
    return f"HTTP GET {url} -> 200 OK (simulated health probe, 18ms)"


def _sim_retry_workflow(step: RunbookStep, alert: AlertRecord) -> str:
    wf = step.params.get("workflowId") or alert.sample_metadata.get("order_id") or alert.id
    return f"Queued workflow retry for '{wf}' (simulated execution accepted)"


def _sim_restart_service(step: RunbookStep, alert: AlertRecord) -> str:
    svc = step.params.get("service") or alert.service or "target-service"
    return f"Rolling restart triggered for '{svc}' (simulated: 2/2 pods healthy)"


def _sim_failover_region(step: RunbookStep, alert: AlertRecord) -> str:
    svc = alert.service or "service"
    return f"Traffic failover executed for '{svc}' -> secondary region active (simulated)"


def _sim_escalate(step: RunbookStep, alert: AlertRecord) -> str:
    team = step.params.get("team") or "oncall-escalation"
    return f"Escalated alert {alert.id} to team '{team}' (simulated page sent)"


def _sim_open_ticket(step: RunbookStep, alert: AlertRecord) -> str:
    return f"Created incident ticket INC-{alert.id[-4:].upper()} for '{alert.rule_name}' (simulated)"


def _sim_webhook(step: RunbookStep, alert: AlertRecord) -> str:
    url = step.params.get("url") or "https://hooks.internal/ops"
    return f"POST {url} with alert {alert.id} -> 202 Accepted (simulated)"


ACTION_EXECUTORS: dict[str, ActionHandler] = {
    "http_get": _sim_http_get,
    "retry_workflow": _sim_retry_workflow,
    "restart_service": _sim_restart_service,
    "failover_region": _sim_failover_region,
    "escalate": _sim_escalate,
    "open_ticket": _sim_open_ticket,
    "webhook": _sim_webhook,
}


def execute_runbook_step(
    runbook: ResolvedRunbook,
    step_index: int,
    alert: AlertRecord,
    actor: str = "oncall",
    approved: bool = False,
    note: str | None = None,
) -> StepExecutionRecord:
    """Execute a runbook step, enforcing human approval when `approval == 'required'`."""
    if step_index < 0 or step_index >= len(runbook.steps):
        raise ValueError(f"Step index {step_index} out of range")

    step = runbook.steps[step_index]
    if step.approval == "required" and not approved:
        raise PermissionError(
            f"Step {step_index} ('{step.label}') requires explicit human approval."
        )

    action_name = step.action or "manual_check"
    handler = ACTION_EXECUTORS.get(action_name)
    if handler is not None:
        output = handler(step, alert)
    else:
        output = f"Completed '{action_name}': {step.label} (simulated)"

    return StepExecutionRecord(
        step_index=step_index,
        runbook_id=runbook.id,
        action=action_name,
        label=step.label,
        status="executed",
        actor=actor.strip() or "oncall",
        approved=approved,
        executed_at=datetime.now(timezone.utc).isoformat(),
        output=output,
        note=note,
    )


def load_runbooks(
    runbooks_file: str | Path = "runbooks.json",
) -> list[RunbookDefinition]:
    path = Path(runbooks_file)
    if not path.exists():
        return []

    mtime = path.stat().st_mtime
    cache_key = str(path.resolve())
    cached = _RUNBOOKS_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    if not isinstance(raw, list):
        raise ValueError("Runbooks file must contain a JSON array")

    defs = [RunbookDefinition.model_validate(item) for item in raw]
    _RUNBOOKS_CACHE[cache_key] = (mtime, defs)
    return defs


def save_runbooks(
    runbooks: list[RunbookDefinition],
    runbooks_file: str | Path = "runbooks.json",
) -> list[RunbookDefinition]:
    path = Path(runbooks_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump([rb.model_dump() for rb in runbooks], fh, indent=2)
        fh.write("\n")

    cache_key = str(path.resolve())
    _RUNBOOKS_CACHE[cache_key] = (path.stat().st_mtime, runbooks)
    return runbooks
