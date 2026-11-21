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
