import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.config import settings
from app.parser import build_rule_eval_document
from app.rules import resolve_pointer
from app.schemas import (
    AlertAuditEvent,
    AlertRecord,
    AlertStatus,
    NotificationDecision,
    OutboxRecord,
    OutboxStatus,
    ParsedLog,
    RuleMatch,
    StepExecutionRecord,
)

_DEFAULT_GROUP_BY = ["/service", "/level", "/message"]


def _utcnow_iso(at: datetime | None = None) -> str:
    dt = at or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_group_key(
    message: dict[str, Any],
    rule_dict: dict[str, Any],
) -> tuple[str, str]:
    """Build a human-readable group key and stable fingerprint for deduplication."""
    raw_fields = rule_dict.get("group_by")
    if isinstance(raw_fields, list) and raw_fields:
        fields = [str(f) for f in raw_fields if isinstance(f, str) and f.startswith("/")]
    else:
        fields = list(_DEFAULT_GROUP_BY)

    parts: list[str] = []
    for path in fields:
        try:
            val = resolve_pointer(message, path)
            # Treat missing sentinel (non-JSON-serializable object) as None
            if type(val) is object:
                val = None
        except ValueError:
            val = None
        key_name = path.strip("/").replace("/", ".")
        parts.append(f"{key_name}={val if val is not None else '-'}")

    group_key = " | ".join(parts) if parts else "global"
    rule_id = str(rule_dict.get("id") or "rule")
    fingerprint = hashlib.sha256(f"{rule_id}::{group_key}".encode("utf-8")).hexdigest()
    return group_key, fingerprint


class AlertStore:
    """SQLite-backed alert store supporting deduplication, grouping, and ack/resolve lifecycle."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or settings.alerts_db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    status TEXT NOT NULL,
                    service TEXT,
                    group_key TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    message TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 1,
                    dedup_window_seconds INTEGER NOT NULL DEFAULT 300,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    acknowledged_by TEXT,
                    acknowledged_at TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    notifications_json TEXT NOT NULL DEFAULT '[]',
                    sample_raw TEXT NOT NULL DEFAULT '',
                    sample_metadata_json TEXT NOT NULL DEFAULT '{}',
                    history_json TEXT NOT NULL DEFAULT '[]',
                    step_executions_json TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            # Migrate existing DBs that predate step_executions_json
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(alerts);").fetchall()
            }
            if "step_executions_json" not in cols:
                conn.execute(
                    "ALTER TABLE alerts ADD COLUMN step_executions_json TEXT NOT NULL DEFAULT '[]';"
                )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_alerts_fp_status
                ON alerts (fingerprint, status, last_seen);
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    id TEXT PRIMARY KEY,
                    alert_id TEXT NOT NULL,
                    rule_id TEXT NOT NULL,
                    rule_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target TEXT NOT NULL,
                    service TEXT,
                    log_summary TEXT NOT NULL,
                    working_hours_only INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 5,
                    next_retry_at TEXT NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_outbox_status_retry
                ON notification_outbox (status, next_retry_at);
                """
            )

    def _row_to_record(self, row: sqlite3.Row) -> AlertRecord:
        notifications = [
            NotificationDecision.model_validate(item)
            for item in json.loads(row["notifications_json"] or "[]")
        ]
        history = [
            AlertAuditEvent.model_validate(item)
            for item in json.loads(row["history_json"] or "[]")
        ]
        step_executions = [
            StepExecutionRecord.model_validate(item)
            for item in json.loads(row["step_executions_json"] or "[]")
        ]
        return AlertRecord(
            id=row["id"],
            rule_id=row["rule_id"],
            rule_name=row["rule_name"],
            severity=row["severity"],
            status=row["status"],
            service=row["service"],
            group_key=row["group_key"],
            summary=row["summary"],
            message=row["message"],
            count=row["count"],
            dedup_window_seconds=row["dedup_window_seconds"],
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            acknowledged_by=row["acknowledged_by"],
            acknowledged_at=row["acknowledged_at"],
            resolved_by=row["resolved_by"],
            resolved_at=row["resolved_at"],
            notifications=notifications,
            sample_raw=row["sample_raw"],
            sample_metadata=json.loads(row["sample_metadata_json"] or "{}"),
            history=history,
            step_executions=step_executions,
        )

    def record_match(
        self,
        parsed_log: ParsedLog,
        rule_match: RuleMatch,
        rule_dict: dict[str, Any],
        notifications: list[NotificationDecision],
        at: datetime | None = None,
    ) -> tuple[AlertRecord, bool]:
        """Record a rule match, deduplicating within `dedup_window_seconds`.

        Returns `(alert_record, should_notify)`:
        - `should_notify=True` when a new alert is created (first occurrence in window).
        - `should_notify=False` when merged into an active open/acked alert within the window.
        """
        now_dt = at or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = _utcnow_iso(now_dt)

        raw_window = rule_dict.get("dedup_window_seconds")
        window_seconds = (
            int(raw_window)
            if isinstance(raw_window, (int, float)) and raw_window >= 0
            else settings.default_dedup_window_seconds
        )

        msg_dict = build_rule_eval_document(parsed_log)
        group_key, fingerprint = compute_group_key(msg_dict, rule_dict)

        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM alerts
                WHERE fingerprint = ? AND status IN ('open', 'acked')
                ORDER BY last_seen DESC
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()

            if row is not None and window_seconds > 0:
                last_seen_dt = _parse_iso(row["last_seen"])
                if now_dt - last_seen_dt <= timedelta(seconds=window_seconds):
                    new_count = int(row["count"]) + 1
                    conn.execute(
                        """
                        UPDATE alerts
                        SET count = ?, last_seen = ?, summary = ?, message = ?,
                            sample_raw = ?, sample_metadata_json = ?
                        WHERE id = ?
                        """,
                        (
                            new_count,
                            now_iso,
                            parsed_log.summary or parsed_log.message,
                            parsed_log.message,
                            parsed_log.raw,
                            json.dumps(parsed_log.metadata),
                            row["id"],
                        ),
                    )
                    updated = conn.execute(
                        "SELECT * FROM alerts WHERE id = ?", (row["id"],)
                    ).fetchone()
                    conn.execute("COMMIT")
                    return self._row_to_record(updated), False

            alert_id = f"alrt_{uuid.uuid4().hex[:10]}"
            history = [
                AlertAuditEvent(
                    action="opened",
                    actor="rule-engine",
                    at=now_iso,
                    note=f"Matched rule '{rule_match.name}' ({group_key})",
                ).model_dump()
            ]
            conn.execute(
                """
                INSERT INTO alerts (
                    id, fingerprint, rule_id, rule_name, severity, status,
                    service, group_key, summary, message, count, dedup_window_seconds,
                    first_seen, last_seen, notifications_json, sample_raw,
                    sample_metadata_json, history_json
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    fingerprint,
                    rule_match.id,
                    rule_match.name,
                    rule_match.severity,
                    parsed_log.service,
                    group_key,
                    parsed_log.summary or parsed_log.message,
                    parsed_log.message,
                    window_seconds,
                    now_iso,
                    now_iso,
                    json.dumps([n.model_dump() for n in notifications]),
                    parsed_log.raw,
                    json.dumps(parsed_log.metadata),
                    json.dumps(history),
                ),
            )
            created = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            conn.execute("COMMIT")
            return self._row_to_record(created), True
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def update_notifications(
        self,
        alert_id: str,
        notifications: list[NotificationDecision],
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE alerts SET notifications_json = ? WHERE id = ?",
                (json.dumps([n.model_dump() for n in notifications]), alert_id),
            )

    def list_alerts(
        self,
        status: AlertStatus | None = None,
        service: str | None = None,
        rule_id: str | None = None,
        limit: int = 100,
    ) -> list[AlertRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if service:
            clauses.append("LOWER(service) = LOWER(?)")
            params.append(service)
        if rule_id:
            clauses.append("rule_id = ?")
            params.append(rule_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM alerts {where} ORDER BY last_seen DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def get_alert(self, alert_id: str) -> AlertRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def transition(
        self,
        alert_id: str,
        action: str,
        actor: str = "oncall",
        note: str | None = None,
        at: datetime | None = None,
    ) -> AlertRecord | None:
        now_iso = _utcnow_iso(at)
        clean_actor = actor.strip() or "oncall"

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            if row is None:
                return None

            history = json.loads(row["history_json"] or "[]")
            if action == "ack":
                new_status = "acked"
                history.append(
                    AlertAuditEvent(
                        action="acked",
                        actor=clean_actor,
                        at=now_iso,
                        note=note,
                    ).model_dump()
                )
                conn.execute(
                    """
                    UPDATE alerts
                    SET status = ?, acknowledged_by = ?, acknowledged_at = ?, history_json = ?
                    WHERE id = ?
                    """,
                    (new_status, clean_actor, now_iso, json.dumps(history), alert_id),
                )
            elif action == "resolve":
                new_status = "resolved"
                history.append(
                    AlertAuditEvent(
                        action="resolved",
                        actor=clean_actor,
                        at=now_iso,
                        note=note,
                    ).model_dump()
                )
                conn.execute(
                    """
                    UPDATE alerts
                    SET status = ?, resolved_by = ?, resolved_at = ?, history_json = ?
                    WHERE id = ?
                    """,
                    (new_status, clean_actor, now_iso, json.dumps(history), alert_id),
                )
            elif action == "reopen":
                new_status = "open"
                history.append(
                    AlertAuditEvent(
                        action="reopened",
                        actor=clean_actor,
                        at=now_iso,
                        note=note,
                    ).model_dump()
                )
                conn.execute(
                    """
                    UPDATE alerts
                    SET status = ?, resolved_by = NULL, resolved_at = NULL, history_json = ?
                    WHERE id = ?
                    """,
                    (new_status, json.dumps(history), alert_id),
                )
            else:
                raise ValueError(f"Unsupported alert transition: {action}")

            updated = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            return self._row_to_record(updated)

    def record_step_execution(
        self,
        alert_id: str,
        execution: StepExecutionRecord,
    ) -> AlertRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            if row is None:
                return None

            executions = json.loads(row["step_executions_json"] or "[]")
            executions.append(execution.model_dump())

            history = json.loads(row["history_json"] or "[]")
            approval_tag = " (approved)" if execution.approved else ""
            history.append(
                AlertAuditEvent(
                    action="step_executed",
                    actor=execution.actor,
                    at=execution.executed_at,
                    note=f"Step #{execution.step_index + 1} [{execution.action}]{approval_tag}: {execution.output}",
                ).model_dump()
            )
            conn.execute(
                """
                UPDATE alerts
                SET step_executions_json = ?, history_json = ?
                WHERE id = ?
                """,
                (json.dumps(executions), json.dumps(history), alert_id),
            )
            updated = conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
            return self._row_to_record(updated)

    # ------------------------------------------------------------------
    # Durable notification outbox (retry with exponential backoff + DLQ)
    # ------------------------------------------------------------------

    @staticmethod
    def _row_to_outbox(row: sqlite3.Row) -> OutboxRecord:
        return OutboxRecord(
            id=row["id"],
            alert_id=row["alert_id"],
            rule_id=row["rule_id"],
            rule_name=row["rule_name"],
            severity=row["severity"],
            channel=row["channel"],
            target=row["target"],
            service=row["service"],
            log_summary=row["log_summary"],
            working_hours_only=bool(row["working_hours_only"]),
            status=row["status"],
            attempts=int(row["attempts"]),
            max_attempts=int(row["max_attempts"]),
            next_retry_at=row["next_retry_at"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def enqueue_failed_notifications(
        self,
        alert_id: str,
        decisions: list[NotificationDecision],
        log_summary: str,
        service: str | None = None,
        max_attempts: int | None = None,
        base_delay_seconds: float | None = None,
        at: datetime | None = None,
    ) -> list[OutboxRecord]:
        """Persist failed notification deliveries to the outbox for backoff retries."""
        failed = [d for d in decisions if d.status == "failed"]
        if not failed:
            return []

        now_dt = at or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = _utcnow_iso(now_dt)

        effective_max = (
            max_attempts if max_attempts is not None else settings.notification_max_attempts
        )
        effective_delay = (
            base_delay_seconds
            if base_delay_seconds is not None
            else settings.notification_retry_base_seconds
        )
        next_retry_iso = _utcnow_iso(now_dt + timedelta(seconds=max(0.0, effective_delay)))
        initial_status: OutboxStatus = "dead_letter" if effective_max <= 1 else "pending"

        created_records: list[OutboxRecord] = []
        with self._connect() as conn:
            for decision in failed:
                outbox_id = f"out_{uuid.uuid4().hex[:10]}"
                conn.execute(
                    """
                    INSERT INTO notification_outbox (
                        id, alert_id, rule_id, rule_name, severity, channel, target,
                        service, log_summary, working_hours_only, status, attempts,
                        max_attempts, next_retry_at, last_error, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                    """,
                    (
                        outbox_id,
                        alert_id,
                        decision.rule_id,
                        decision.rule_name,
                        decision.severity,
                        decision.channel,
                        decision.target,
                        service,
                        log_summary,
                        1 if decision.working_hours_only else 0,
                        initial_status,
                        effective_max,
                        next_retry_iso,
                        decision.reason,
                        now_iso,
                        now_iso,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
                ).fetchone()
                if row is not None:
                    created_records.append(self._row_to_outbox(row))
        return created_records

    def claim_due_outbox(
        self,
        limit: int = 25,
        at: datetime | None = None,
    ) -> list[OutboxRecord]:
        """Return pending outbox items whose `next_retry_at` is due."""
        now_iso = _utcnow_iso(at)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM notification_outbox
                WHERE status = 'pending' AND next_retry_at <= ?
                ORDER BY next_retry_at ASC
                LIMIT ?
                """,
                (now_iso, limit),
            ).fetchall()
        return [self._row_to_outbox(r) for r in rows]

    def mark_outbox_sent(
        self,
        outbox_id: str,
        reason: str,
        at: datetime | None = None,
    ) -> OutboxRecord | None:
        """Mark an outbox item sent and update the parent alert's notification status."""
        now_iso = _utcnow_iso(at)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                return None

            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'sent', attempts = attempts + 1, last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, outbox_id),
            )

            alert_row = conn.execute(
                "SELECT notifications_json FROM alerts WHERE id = ?",
                (row["alert_id"],),
            ).fetchone()
            if alert_row is not None:
                notifs = json.loads(alert_row["notifications_json"] or "[]")
                changed = False
                for item in notifs:
                    if (
                        item.get("rule_id") == row["rule_id"]
                        and item.get("channel") == row["channel"]
                        and item.get("target") == row["target"]
                        and item.get("status") == "failed"
                    ):
                        item["status"] = "sent"
                        item["reason"] = f"{reason} (after retry)"
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE alerts SET notifications_json = ? WHERE id = ?",
                        (json.dumps(notifs), row["alert_id"]),
                    )

            updated = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            return self._row_to_outbox(updated) if updated else None

    def mark_outbox_retry_or_dlq(
        self,
        outbox_id: str,
        error: str,
        base_delay_seconds: float | None = None,
        max_delay_seconds: float | None = None,
        at: datetime | None = None,
    ) -> tuple[OutboxRecord | None, bool]:
        """Record a failed retry attempt; schedule exponential backoff or move to DLQ.

        Returns `(record, dead_lettered)`.
        """
        now_dt = at or datetime.now(timezone.utc)
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)
        now_iso = _utcnow_iso(now_dt)

        base = (
            base_delay_seconds
            if base_delay_seconds is not None
            else settings.notification_retry_base_seconds
        )
        max_delay = (
            max_delay_seconds
            if max_delay_seconds is not None
            else settings.notification_retry_max_seconds
        )

        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                return None, False

            next_attempts = int(row["attempts"]) + 1
            max_attempts = int(row["max_attempts"])

            if next_attempts >= max_attempts:
                conn.execute(
                    """
                    UPDATE notification_outbox
                    SET status = 'dead_letter', attempts = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (next_attempts, error, now_iso, outbox_id),
                )
                updated = conn.execute(
                    "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
                ).fetchone()
                return (self._row_to_outbox(updated) if updated else None), True

            # Exponential backoff: base * 2^(attempts - 1), capped at max_delay
            delay = min(max_delay, max(0.0, base) * (2 ** max(0, next_attempts - 1)))
            next_retry_iso = _utcnow_iso(now_dt + timedelta(seconds=delay))
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'pending', attempts = ?, next_retry_at = ?, last_error = ?, updated_at = ?
                WHERE id = ?
                """,
                (next_attempts, next_retry_iso, error, now_iso, outbox_id),
            )
            updated = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            return (self._row_to_outbox(updated) if updated else None), False

    def list_outbox(
        self,
        status: OutboxStatus | None = None,
        alert_id: str | None = None,
        limit: int = 100,
    ) -> list[OutboxRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if alert_id:
            clauses.append("alert_id = ?")
            params.append(alert_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM notification_outbox {where} ORDER BY updated_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_outbox(r) for r in rows]

    def requeue_outbox(
        self,
        outbox_id: str,
        at: datetime | None = None,
    ) -> OutboxRecord | None:
        """Reset a dead-lettered or pending outbox item for immediate retry."""
        now_iso = _utcnow_iso(at)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                """
                UPDATE notification_outbox
                SET status = 'pending', attempts = 0, next_retry_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now_iso, now_iso, outbox_id),
            )
            updated = conn.execute(
                "SELECT * FROM notification_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
            return self._row_to_outbox(updated) if updated else None
