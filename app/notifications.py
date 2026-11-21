import asyncio
import json
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, time, timezone
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.config import settings
from app.schemas import NotificationChannel, NotificationDecision, RuleSeverity

if TYPE_CHECKING:
    from app.alerts import AlertStore

logger = logging.getLogger("log_notifications")

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_VALID_CHANNELS: set[str] = {"email", "sms", "sms_working_hours"}


@dataclass(frozen=True)
class WorkingHoursConfig:
    start: time
    end: time
    days: tuple[int, ...]
    tz_name: str

    def describe(self) -> str:
        day_str = ",".join(_DAY_NAMES[d] for d in self.days if 0 <= d <= 6)
        return (
            f"{self.start.strftime('%H:%M')}–{self.end.strftime('%H:%M')} "
            f"{day_str} {self.tz_name}"
        )


def _parse_hhmm(value: str) -> time:
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Invalid HH:MM time format: {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    return time(hour=hour, minute=minute)


def get_default_working_hours() -> WorkingHoursConfig:
    days = tuple(
        int(d.strip())
        for d in settings.working_days.split(",")
        if d.strip().isdigit() and 0 <= int(d.strip()) <= 6
    )
    return WorkingHoursConfig(
        start=_parse_hhmm(settings.working_hours_start),
        end=_parse_hhmm(settings.working_hours_end),
        days=days or (0, 1, 2, 3, 4),
        tz_name=settings.working_hours_timezone or "UTC",
    )


def parse_working_hours_override(
    override: dict[str, Any] | None,
    base: WorkingHoursConfig | None = None,
) -> WorkingHoursConfig:
    default = base or get_default_working_hours()
    if not override:
        return default

    start = _parse_hhmm(override["start"]) if "start" in override else default.start
    end = _parse_hhmm(override["end"]) if "end" in override else default.end
    raw_days = override.get("days")
    if isinstance(raw_days, list):
        days = tuple(int(d) for d in raw_days if isinstance(d, int) and 0 <= d <= 6)
    else:
        days = default.days
    tz_name = str(override.get("timezone") or default.tz_name)
    return WorkingHoursConfig(start=start, end=end, days=days, tz_name=tz_name)


def is_within_working_hours(
    at: datetime | None = None,
    config: WorkingHoursConfig | None = None,
) -> bool:
    """Return True if `at` falls inside the configured working days and hours."""
    cfg = config or get_default_working_hours()
    dt = at or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    try:
        tz = ZoneInfo(cfg.tz_name)
    except ZoneInfoNotFoundError:
        tz = timezone.utc

    local_dt = dt.astimezone(tz)
    if local_dt.weekday() not in cfg.days:
        return False

    current_time = local_dt.time().replace(tzinfo=None)
    if cfg.start <= cfg.end:
        return cfg.start <= current_time < cfg.end
    # Overnight window (e.g. 22:00 -> 06:00)
    return current_time >= cfg.start or current_time < cfg.end


def normalize_notify_spec(raw_item: Any) -> dict[str, Any]:
    """Validate and normalize a single `notify` entry (string shorthand or dict)."""
    if isinstance(raw_item, str):
        key = raw_item.strip().lower()
        if key not in _VALID_CHANNELS:
            raise ValueError(
                f"Unsupported notification channel '{raw_item}'; "
                f"expected one of {sorted(_VALID_CHANNELS)}"
            )
        if key == "sms_working_hours":
            return {
                "channel": "sms",
                "to": settings.default_alert_sms,
                "working_hours_only": True,
            }
        return {
            "channel": key,
            "to": (
                settings.default_alert_email
                if key == "email"
                else settings.default_alert_sms
            ),
            "working_hours_only": False,
        }

    if not isinstance(raw_item, dict):
        raise ValueError("Each 'notify' entry must be a string or JSON object")

    raw_channel = str(raw_item.get("channel", "")).strip().lower()
    if raw_channel not in _VALID_CHANNELS:
        raise ValueError(
            f"Unsupported notification channel '{raw_channel}'; "
            f"expected one of {sorted(_VALID_CHANNELS)}"
        )

    working_hours_only = bool(raw_item.get("working_hours_only", False))
    channel: NotificationChannel
    if raw_channel == "sms_working_hours":
        channel = "sms"
        working_hours_only = True
    else:
        channel = raw_channel  # type: ignore[assignment]

    default_target = (
        settings.default_alert_email
        if channel == "email"
        else settings.default_alert_sms
    )
    target = str(raw_item.get("to") or default_target).strip()

    wh_override = raw_item.get("working_hours")
    if wh_override is not None:
        if not isinstance(wh_override, dict):
            raise ValueError("'working_hours' override must be a JSON object")
        parse_working_hours_override(wh_override)

    return {
        "channel": channel,
        "to": target,
        "working_hours_only": working_hours_only,
        "working_hours": wh_override,
    }


def resolve_notifications_for_rules(
    matched_rules: list[dict[str, Any]],
    at: datetime | None = None,
    working_hours: WorkingHoursConfig | None = None,
) -> list[NotificationDecision]:
    """Resolve notification routes for all matched rule dicts at timestamp `at`."""
    base_wh = working_hours or get_default_working_hours()
    decisions: list[NotificationDecision] = []

    for idx, rule in enumerate(matched_rules):
        raw_notify = rule.get("notify")
        if not raw_notify:
            continue
        notify_items = raw_notify if isinstance(raw_notify, list) else [raw_notify]

        rule_id = str(rule.get("id") or f"rule-{idx + 1}")
        rule_name = str(rule.get("name") or f"Rule {idx + 1}")
        severity: RuleSeverity = rule.get("severity", "medium")

        for item in notify_items:
            spec = normalize_notify_spec(item)
            channel: NotificationChannel = spec["channel"]
            target: str = spec["to"]
            wh_only: bool = spec["working_hours_only"]
            item_wh = parse_working_hours_override(spec.get("working_hours"), base_wh)

            if wh_only and not is_within_working_hours(at=at, config=item_wh):
                decisions.append(
                    NotificationDecision(
                        rule_id=rule_id,
                        rule_name=rule_name,
                        severity=severity,
                        channel=channel,
                        target=target,
                        working_hours_only=True,
                        status="suppressed",
                        reason=f"Suppressed: outside working hours ({item_wh.describe()})",
                    )
                )
            else:
                reason = (
                    f"Routed via {channel} (within working hours {item_wh.describe()})"
                    if wh_only
                    else f"Routed via {channel}"
                )
                decisions.append(
                    NotificationDecision(
                        rule_id=rule_id,
                        rule_name=rule_name,
                        severity=severity,
                        channel=channel,
                        target=target,
                        working_hours_only=wh_only,
                        status="routed",
                        reason=reason,
                    )
                )

    return decisions


def _send_smtp_sync(host: str, port: int, msg: EmailMessage) -> None:
    with smtplib.SMTP(host, port, timeout=5) as s:
        s.send_message(msg)


async def dispatch_notifications(
    decisions: list[NotificationDecision],
    log_summary: str,
    service: str | None = None,
) -> list[NotificationDecision]:
    """Optionally send live email/SMS when SMTP_HOST or SMS_WEBHOOK_URL are set."""
    results: list[NotificationDecision] = []

    for decision in decisions:
        if decision.status != "routed":
            results.append(decision)
            continue

        if decision.channel == "email" and settings.smtp_host:
            try:
                msg = EmailMessage()
                msg["Subject"] = f"[{decision.severity.upper()}] {decision.rule_name}"
                msg["From"] = settings.smtp_from
                msg["To"] = decision.target
                msg.set_content(
                    f"Rule: {decision.rule_name} ({decision.rule_id})\n"
                    f"Service: {service or 'unknown'}\n"
                    f"Summary: {log_summary}\n"
                )
                await asyncio.to_thread(
                    _send_smtp_sync, settings.smtp_host, settings.smtp_port, msg
                )
                results.append(
                    decision.model_copy(
                        update={"status": "sent", "reason": f"Sent via SMTP to {decision.target}"}
                    )
                )
            except Exception as exc:
                logger.warning("SMTP dispatch failed: %s", exc)
                results.append(
                    decision.model_copy(
                        update={"status": "failed", "reason": f"SMTP error: {exc}"}
                    )
                )
            continue

        if decision.channel == "sms" and settings.sms_webhook_url:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        settings.sms_webhook_url,
                        json={
                            "to": decision.target,
                            "rule_id": decision.rule_id,
                            "rule_name": decision.rule_name,
                            "severity": decision.severity,
                            "service": service,
                            "message": log_summary,
                        },
                    )
                    resp.raise_for_status()
                results.append(
                    decision.model_copy(
                        update={"status": "sent", "reason": f"Sent SMS via webhook to {decision.target}"}
                    )
                )
            except Exception as exc:
                logger.warning("SMS webhook dispatch failed: %s", exc)
                results.append(
                    decision.model_copy(
                        update={"status": "failed", "reason": f"SMS webhook error: {exc}"}
                    )
                )
            continue

        # No live provider configured -> keep as routed (logged & persisted in ParsedLog)
        logger.info(
            "Notification routed: channel=%s target=%s rule=%s",
            decision.channel,
            decision.target,
            decision.rule_id,
        )
        results.append(decision)

    return results


async def process_outbox_once(
    alert_store: "AlertStore",
    broker: Any | None = None,
    dead_letter_topic: str | None = None,
    base_delay_seconds: float | None = None,
    max_delay_seconds: float | None = None,
    limit: int = 25,
    at: datetime | None = None,
) -> dict[str, int]:
    """Process due notification outbox items with exponential backoff and DLQ."""
    due = alert_store.claim_due_outbox(limit=limit, at=at)
    stats = {"processed": 0, "sent": 0, "retried": 0, "dead_lettered": 0}
    dlq_topic = dead_letter_topic or settings.notification_dead_letter_topic

    for rec in due:
        stats["processed"] += 1
        decision = NotificationDecision(
            rule_id=rec.rule_id,
            rule_name=rec.rule_name,
            severity=rec.severity,
            channel=rec.channel,
            target=rec.target,
            working_hours_only=rec.working_hours_only,
            status="routed",
            reason=f"Outbox retry attempt #{rec.attempts + 1}",
        )
        dispatched = await dispatch_notifications(
            [decision],
            log_summary=rec.log_summary,
            service=rec.service,
        )
        result = dispatched[0]

        if result.status in ("sent", "routed"):
            alert_store.mark_outbox_sent(rec.id, result.reason, at=at)
            stats["sent"] += 1
            logger.info(
                "Outbox %s delivered on attempt #%d (%s -> %s)",
                rec.id,
                rec.attempts + 1,
                rec.channel,
                rec.target,
            )
            continue

        updated, dead_lettered = alert_store.mark_outbox_retry_or_dlq(
            rec.id,
            error=result.reason,
            base_delay_seconds=base_delay_seconds,
            max_delay_seconds=max_delay_seconds,
            at=at,
        )
        if dead_lettered:
            stats["dead_lettered"] += 1
            logger.error(
                "Outbox %s exhausted %d attempts -> dead_letter (%s)",
                rec.id,
                rec.max_attempts,
                result.reason,
            )
            if broker is not None and updated is not None:
                try:
                    payload = json.dumps(updated.model_dump()).encode("utf-8")
                    await broker.send_raw(payload, topic=dlq_topic)
                except Exception as dlq_exc:
                    logger.warning(
                        "Failed to publish outbox %s to Kafka DLQ '%s': %s",
                        rec.id,
                        dlq_topic,
                        dlq_exc,
                    )
        else:
            stats["retried"] += 1
            logger.warning(
                "Outbox %s attempt #%d failed (%s); next retry at %s",
                rec.id,
                updated.attempts if updated else rec.attempts + 1,
                result.reason,
                updated.next_retry_at if updated else "unknown",
            )

    return stats
