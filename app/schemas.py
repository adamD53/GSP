from typing import Any, Literal

from pydantic import BaseModel, Field

LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN"]
RuleSeverity = Literal["critical", "high", "medium", "low"]
NotificationChannel = Literal["email", "sms"]
NotificationStatus = Literal["sent", "routed", "suppressed", "failed"]
OutboxStatus = Literal["pending", "sent", "dead_letter"]
AlertStatus = Literal["open", "acked", "resolved"]
RunbookFormat = Literal["yaml", "markdown", "text"]
ApprovalMode = Literal["none", "required"]
ParseMode = Literal["fast_json", "fast_yaml", "schema_cache", "llm"]


class RunbookStep(BaseModel):
    index: int
    label: str
    action: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    approval: ApprovalMode = "none"
    condition_note: str | None = None


class RunbookDefinition(BaseModel):
    id: str
    name: str
    format: RunbookFormat = "yaml"
    rule_ids: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    description: str = ""
    content: str


class ResolvedRunbook(BaseModel):
    id: str
    name: str
    format: RunbookFormat
    description: str = ""
    rendered_content: str
    steps: list[RunbookStep] = Field(default_factory=list)


class StepExecutionRecord(BaseModel):
    step_index: int
    runbook_id: str
    action: str
    label: str
    status: Literal["executed", "failed"] = "executed"
    actor: str
    approved: bool = False
    executed_at: str
    output: str
    note: str | None = None


class AlertAuditEvent(BaseModel):
    action: Literal[
        "opened",
        "deduplicated",
        "acked",
        "resolved",
        "reopened",
        "step_executed",
    ]
    actor: str = Field(default="system")
    at: str
    note: str | None = None


class AlertRecord(BaseModel):
    id: str
    rule_id: str
    rule_name: str
    severity: RuleSeverity = "medium"
    status: AlertStatus = "open"
    service: str | None = None
    group_key: str
    summary: str
    message: str
    count: int = 1
    dedup_window_seconds: int = 300
    first_seen: str
    last_seen: str
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    notifications: list["NotificationDecision"] = Field(default_factory=list)
    sample_raw: str = ""
    sample_metadata: dict[str, Any] = Field(default_factory=dict)
    history: list[AlertAuditEvent] = Field(default_factory=list)
    runbook: ResolvedRunbook | None = None
    step_executions: list[StepExecutionRecord] = Field(default_factory=list)


class NotificationDecision(BaseModel):
    rule_id: str = Field(description="Rule that triggered this notification.")
    rule_name: str = Field(description="Human-readable rule name.")
    severity: RuleSeverity = Field(default="medium")
    channel: NotificationChannel = Field(description="Delivery channel: email or sms.")
    target: str = Field(description="Recipient email address or phone number.")
    working_hours_only: bool = Field(
        default=False,
        description="Whether this route is restricted to working hours.",
    )
    status: NotificationStatus = Field(
        description="sent (live provider), routed (simulated), suppressed (off-hours), or failed."
    )
    reason: str = Field(description="Human-readable routing decision explanation.")


class OutboxRecord(BaseModel):
    id: str
    alert_id: str
    rule_id: str
    rule_name: str
    severity: RuleSeverity = "medium"
    channel: NotificationChannel
    target: str
    service: str | None = None
    log_summary: str
    working_hours_only: bool = False
    status: OutboxStatus = "pending"
    attempts: int = 0
    max_attempts: int = 5
    next_retry_at: str
    last_error: str | None = None
    created_at: str
    updated_at: str


class RuleMatch(BaseModel):
    id: str = Field(description="Rule identifier.")
    name: str = Field(description="Human-readable rule name.")
    severity: RuleSeverity = Field(default="medium", description="Alert severity.")


class SchemaFieldMapping(BaseModel):
    timestamp_ptr: str | None = Field(
        default=None,
        description="JSON Pointer (e.g. /meta/ts) to the event timestamp, or null.",
    )
    level_ptr: str | None = Field(
        default=None,
        description="JSON Pointer (e.g. /labels/severity) to the severity/level, or null.",
    )
    service_ptr: str | None = Field(
        default=None,
        description="JSON Pointer (e.g. /labels/service) to the service/producer name, or null.",
    )
    message_ptr: str | None = Field(
        default=None,
        description="JSON Pointer (e.g. /error/message) to the human-readable message, or null.",
    )


class LLMExtractedLog(BaseModel):
    timestamp: str | None = Field(
        default=None,
        description="Timestamp extracted from the log line, or null if absent.",
    )
    level: LogLevel = Field(
        default="UNKNOWN",
        description="Normalized severity level.",
    )
    service: str | None = Field(
        default=None,
        description="Service or component name if present, otherwise null.",
    )
    message: str = Field(
        description="Core log message text.",
    )
    summary: str = Field(
        description="Short one-sentence summary of what happened.",
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Any extra key-value fields found in the log.",
    )


class ParsedLog(LLMExtractedLog):
    raw: str = Field(description="Original unmodified raw log string.")
    event: dict[str, Any] | None = Field(
        default=None,
        description="Original structured JSON/YAML document when parsed via fast path.",
    )
    parse_mode: ParseMode = Field(
        default="llm",
        description="Parser path used: fast_json, fast_yaml, schema_cache, or llm.",
    )
    matched_rules: list[RuleMatch] = Field(
        default_factory=list,
        description="Rulebook rules matched against this parsed log.",
    )
    notifications: list[NotificationDecision] = Field(
        default_factory=list,
        description="Resolved notification routes (email, sms, working-hours suppression).",
    )
    alert: bool = Field(
        default=False,
        description="True when one or more rulebook rules matched.",
    )
