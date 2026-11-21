import json
import operator
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from app.notifications import (
    WorkingHoursConfig,
    normalize_notify_spec,
    resolve_notifications_for_rules,
)
from app.schemas import NotificationDecision, RuleMatch, RuleSeverity

_MISSING = object()
_NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
_VALID_SEVERITIES: set[str] = {"critical", "high", "medium", "low"}

# Simple mtime-based cache so the worker picks up rule edits without restart.
_RULES_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _op_in(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (list, tuple, set)):
        return actual in expected
    if isinstance(expected, str) and isinstance(actual, str):
        return actual in expected
    return False


def _op_not_in(actual: Any, expected: Any) -> bool:
    if isinstance(expected, (list, tuple, set)):
        return actual not in expected
    if isinstance(expected, str) and isinstance(actual, str):
        return actual not in expected
    return False


def _op_contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str) and isinstance(expected, str):
        return expected.lower() in actual.lower()
    if isinstance(actual, (list, tuple, set)):
        return expected in actual
    return False


def _op_regex(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, str) or actual is None:
        return False
    try:
        return re.search(expected, str(actual)) is not None
    except re.error:
        return False


COMPARISONS = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
    "in": _op_in,
    "not_in": _op_not_in,
    "contains": _op_contains,
    "regex": _op_regex,
    "exists": lambda actual, expected: (actual is not _MISSING) is bool(expected),
}


def resolve_pointer(message: Any, path: str) -> Any:
    """Resolve a JSON Pointer path (e.g. /metadata/latency_ms) against a dict/list."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Field must use a path such as /error/code")

    actual = message
    for raw_key in path[1:].split("/"):
        key = raw_key.replace("~1", "/").replace("~0", "~")
        if isinstance(actual, dict):
            actual = actual.get(key, _MISSING)
        elif isinstance(actual, list) and key.isdigit():
            idx = int(key)
            actual = actual[idx] if 0 <= idx < len(actual) else _MISSING
        else:
            return _MISSING

        if actual is _MISSING:
            return _MISSING

    return actual


def _unwrap_condition(rule: dict[str, Any]) -> dict[str, Any]:
    if "condition" in rule and isinstance(rule["condition"], dict):
        return rule["condition"]
    return rule


def validate_condition(condition: Any) -> None:
    """Validate a single condition node (all/any/not or field/operator/value)."""
    if not isinstance(condition, dict):
        raise ValueError("Rule condition must be a JSON object")

    if "all" in condition:
        children = condition["all"]
        if not isinstance(children, list) or not children:
            raise ValueError("'all' must be a non-empty list of conditions")
        for child in children:
            validate_condition(child)
        return

    if "any" in condition:
        children = condition["any"]
        if not isinstance(children, list) or not children:
            raise ValueError("'any' must be a non-empty list of conditions")
        for child in children:
            validate_condition(child)
        return

    if "not" in condition:
        validate_condition(condition["not"])
        return

    path = condition.get("field")
    if not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("Field must use a path such as /error/code")

    op = condition.get("operator")
    if op not in COMPARISONS:
        raise ValueError(f"Unsupported operator: {op}")

    if "value" not in condition:
        raise ValueError("Condition must specify 'value'")


def validate_rules(rules: Any) -> list[dict[str, Any]]:
    """Normalize (single dict or list) and validate all rules in a rulebook."""
    if isinstance(rules, dict):
        rule_list = [rules]
    elif isinstance(rules, list):
        rule_list = rules
    else:
        raise ValueError("Rulebook must be a JSON object or list of rule objects")

    for idx, rule in enumerate(rule_list):
        if not isinstance(rule, dict):
            raise ValueError(f"Rule at index {idx} must be a JSON object")
        severity = rule.get("severity")
        if severity is not None and severity not in _VALID_SEVERITIES:
            raise ValueError(
                f"Invalid severity '{severity}' at rule index {idx}; "
                f"expected one of {sorted(_VALID_SEVERITIES)}"
            )
        if "notify" in rule and rule["notify"] is not None:
            raw_notify = rule["notify"]
            notify_items = raw_notify if isinstance(raw_notify, list) else [raw_notify]
            for item in notify_items:
                normalize_notify_spec(item)
        validate_condition(_unwrap_condition(rule))

    return rule_list


def matches_condition(message: Any, condition: dict[str, Any]) -> bool:
    """Evaluate a condition tree against a message dict."""
    if "all" in condition:
        children = condition["all"]
        if not isinstance(children, list) or not children:
            return False
        return all(matches_condition(message, child) for child in children)

    if "any" in condition:
        children = condition["any"]
        if not isinstance(children, list) or not children:
            return False
        return any(matches_condition(message, child) for child in children)

    if "not" in condition:
        return not matches_condition(message, condition["not"])

    path = condition["field"]
    actual = resolve_pointer(message, path)

    op = condition["operator"]
    if op not in COMPARISONS:
        raise ValueError(f"Unsupported operator: {op}")

    expected = condition["value"]

    if op == "exists":
        return COMPARISONS["exists"](actual, expected)

    if actual is _MISSING:
        return False

    if op in _NUMERIC_OPS:
        if not _is_number(actual) or not _is_number(expected):
            return False

    return bool(COMPARISONS[op](actual, expected))


def evaluate_message(
    message: dict[str, Any],
    rules: dict[str, Any] | list[dict[str, Any]],
) -> list[RuleMatch]:
    """Return all enabled rules that match the given message."""
    rule_list = [rules] if isinstance(rules, dict) else list(rules)
    matched: list[RuleMatch] = []

    for idx, rule in enumerate(rule_list):
        if rule.get("enabled", True) is False:
            continue
        cond = _unwrap_condition(rule)
        if matches_condition(message, cond):
            severity = rule.get("severity", "medium")
            if severity not in _VALID_SEVERITIES:
                severity = "medium"
            matched.append(
                RuleMatch(
                    id=str(rule.get("id") or f"rule-{idx + 1}"),
                    name=str(rule.get("name") or f"Rule {idx + 1}"),
                    severity=severity,  # type: ignore[arg-type]
                )
            )

    return matched


def load_rules(rules_file: str | Path = "rules.json") -> list[dict[str, Any]]:
    path = Path(rules_file)
    if not path.exists():
        return []

    mtime = path.stat().st_mtime
    cache_key = str(path.resolve())
    cached = _RULES_CACHE.get(cache_key)
    if cached and cached[0] == mtime:
        return cached[1]

    with path.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)

    normalized = validate_rules(raw)
    _RULES_CACHE[cache_key] = (mtime, normalized)
    return normalized


def save_rules(
    rules: Any,
    rules_file: str | Path = "rules.json",
) -> list[dict[str, Any]]:
    normalized = validate_rules(rules)
    path = Path(rules_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(normalized, fh, indent=2)
        fh.write("\n")

    cache_key = str(path.resolve())
    _RULES_CACHE[cache_key] = (path.stat().st_mtime, normalized)
    return normalized


def evaluate_and_route(
    message: dict[str, Any],
    rules: dict[str, Any] | list[dict[str, Any]],
    at: datetime | None = None,
    working_hours: WorkingHoursConfig | None = None,
) -> tuple[list[RuleMatch], list[NotificationDecision]]:
    """Evaluate rules and resolve notification routing decisions for matches."""
    rule_list = [rules] if isinstance(rules, dict) else list(rules)
    matched: list[RuleMatch] = []
    matched_raw_rules: list[dict[str, Any]] = []

    for idx, rule in enumerate(rule_list):
        if rule.get("enabled", True) is False:
            continue
        cond = _unwrap_condition(rule)
        if matches_condition(message, cond):
            severity = rule.get("severity", "medium")
            if severity not in _VALID_SEVERITIES:
                severity = "medium"
            matched.append(
                RuleMatch(
                    id=str(rule.get("id") or f"rule-{idx + 1}"),
                    name=str(rule.get("name") or f"Rule {idx + 1}"),
                    severity=severity,  # type: ignore[arg-type]
                )
            )
            matched_raw_rules.append(rule)

    decisions = resolve_notifications_for_rules(
        matched_raw_rules,
        at=at,
        working_hours=working_hours,
    )
    return matched, decisions


def evaluate_and_route_file(
    message: dict[str, Any],
    rules_file: str | Path = "rules.json",
    at: datetime | None = None,
    working_hours: WorkingHoursConfig | None = None,
) -> tuple[list[RuleMatch], list[NotificationDecision]]:
    rules = load_rules(rules_file)
    if not rules:
        return [], []
    return evaluate_and_route(message, rules, at=at, working_hours=working_hours)


def evaluate_message_file(
    message: dict[str, Any],
    rules_file: str | Path = "rules.json",
) -> list[RuleMatch]:
    rules = load_rules(rules_file)
    if not rules:
        return []
    return evaluate_message(message, rules)


def check_message(
    message: dict[str, Any],
    rules_file: str | Path = "rules.json",
) -> bool:
    """Return True if any enabled rule in rules_file matches message."""
    with Path(rules_file).open("r", encoding="utf-8") as fh:
        rules = json.load(fh)
    normalized = validate_rules(rules)
    return len(evaluate_message(message, normalized)) > 0
