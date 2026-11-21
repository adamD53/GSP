from typing import Any, Literal

from pydantic import BaseModel, Field

LogLevel = Literal["DEBUG", "INFO", "WARN", "ERROR", "FATAL", "UNKNOWN"]


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
