from typing import Any, Literal

from pydantic import BaseModel, Field

LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN"]
RuleSeverity = Literal["critical", "high", "medium", "low"]
NotificationChannel = Literal["email", "sms"]
NotificationStatus = Literal["sent", "routed", "suppressed", "failed"]


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


class RuleMatch(BaseModel):
    id: str = Field(description="Rule identifier.")
    name: str = Field(description="Human-readable rule name.")
    severity: RuleSeverity = Field(default="medium", description="Alert severity.")


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
