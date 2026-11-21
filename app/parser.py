import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Protocol

import yaml

from app.rules import _MISSING, resolve_pointer
from app.schemas import LogLevel, ParsedLog, ParseMode, SchemaFieldMapping

logger = logging.getLogger("log_parser")

_LEVEL_ALIASES: dict[str, LogLevel] = {
    "fatal": "FATAL",
    "critical": "FATAL",
    "crit": "FATAL",
    "emerg": "FATAL",
    "emergency": "FATAL",
    "alert": "FATAL",
    "sev1": "FATAL",
    "p0": "FATAL",
    "p1": "FATAL",
    "error": "ERROR",
    "err": "ERROR",
    "fail": "ERROR",
    "failed": "ERROR",
    "sev2": "ERROR",
    "warn": "WARN",
    "warning": "WARN",
    "sev3": "WARN",
    "info": "INFO",
    "information": "INFO",
    "notice": "INFO",
    "ok": "INFO",
    "debug": "DEBUG",
    "trace": "DEBUG",
    "verbose": "DEBUG",
}

_TIMESTAMP_POINTERS = (
    "/timestamp",
    "/ts",
    "/startsAt",
    "/time",
    "/@timestamp",
    "/created_at",
    "/createdAt",
    "/meta/ts",
    "/meta/timestamp",
    "/header/timestamp",
)

_LEVEL_POINTERS = (
    "/level",
    "/severity",
    "/labels/severity",
    "/log_level",
    "/lvl",
    "/priority",
    "/meta/severity",
    "/payload/severity",
)

_SERVICE_POINTERS = (
    "/service",
    "/labels/service",
    "/meta/producer",
    "/producer",
    "/workflowName",
    "/app",
    "/component",
    "/source",
    "/logger",
    "/host",
    "/payload/service",
)

_MESSAGE_POINTERS = (
    "/message",
    "/msg",
    "/error/message",
    "/annotations/summary",
    "/annotations/description",
    "/summary",
    "/payload/reason",
    "/reason",
    "/alertname",
    "/type",
)

_CANONICAL_TOP_KEYS = {"timestamp", "level", "service", "message", "summary"}


class LLMParserProtocol(Protocol):
    async def parse(self, raw_log: str) -> ParsedLog: ...

    async def close(self) -> None: ...


class SchemaLearnerProtocol(LLMParserProtocol, Protocol):
    async def infer_schema_mapping(
        self, sample_doc: dict[str, Any]
    ) -> SchemaFieldMapping: ...


def normalize_level(value: Any) -> LogLevel:
    if value is None or value is _MISSING:
        return "UNKNOWN"
    text = str(value).strip().lower()
    if text in _LEVEL_ALIASES:
        return _LEVEL_ALIASES[text]
    upper = text.upper()
    if upper in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
        return upper  # type: ignore[return-value]
    return "UNKNOWN"


def normalize_timestamp(value: Any) -> str | None:
    if value is None or value is _MISSING:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Heuristic: > 1e11 is epoch milliseconds, otherwise epoch seconds.
        seconds = value / 1000.0 if abs(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OSError, OverflowError, ValueError):
            return str(value)
    text = str(value).strip()
    return text or None


def _first_present(doc: dict[str, Any], pointers: tuple[str, ...]) -> tuple[Any, str | None]:
    for ptr in pointers:
        val = resolve_pointer(doc, ptr)
        if val is not _MISSING and val is not None and val != "":
            return val, ptr
    return None, None


def _collect_leaf_paths(obj: Any, prefix: str = "", out: list[str] | None = None) -> list[str]:
    if out is None:
        out = []
    if isinstance(obj, dict):
        for key in sorted(obj.keys()):
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            _collect_leaf_paths(obj[key], f"{prefix}/{escaped}", out)
    elif isinstance(obj, list):
        if obj and isinstance(obj[0], dict):
            _collect_leaf_paths(obj[0], f"{prefix}/0", out)
        else:
            out.append(f"{prefix}[]")
    else:
        out.append(prefix or "/")
    return out


def compute_schema_fingerprint(doc: dict[str, Any]) -> str:
    """Stable hash of the sorted leaf JSON Pointer paths of a structured document."""
    paths = _collect_leaf_paths(doc)
    signature = "|".join(paths)
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:24]


def _extract_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """Preserve all non-canonical fields (and nested objects) for rules and runbooks."""
    meta: dict[str, Any] = {}
    existing_meta = doc.get("metadata")
    if isinstance(existing_meta, dict):
        meta.update(existing_meta)
    for k, v in doc.items():
        if k in _CANONICAL_TOP_KEYS or k == "metadata":
            continue
        meta[k] = v
    return meta


def _build_summary(
    doc: dict[str, Any],
    service: str | None,
    message: str,
) -> str:
    explicit = doc.get("summary")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    alertname = doc.get("alertname")
    if isinstance(alertname, str) and alertname.strip():
        prefix = f"{alertname} on {service}" if service else alertname
        return f"{prefix}: {message}" if message != alertname else prefix
    if service:
        return f"{service}: {message}"
    return message


def _extract_with_heuristics(
    doc: dict[str, Any],
    raw_log: str,
    parse_mode: ParseMode,
) -> tuple[ParsedLog, int]:
    """Deterministically extract canonical fields from a structured dict.

    Returns `(ParsedLog, hits)` where `hits` is how many canonical fields were
    recognized without falling back to defaults.
    """
    ts_raw, ts_ptr = _first_present(doc, _TIMESTAMP_POINTERS)
    lvl_raw, lvl_ptr = _first_present(doc, _LEVEL_POINTERS)
    svc_raw, svc_ptr = _first_present(doc, _SERVICE_POINTERS)
    msg_raw, msg_ptr = _first_present(doc, _MESSAGE_POINTERS)

    timestamp = normalize_timestamp(ts_raw)
    level = normalize_level(lvl_raw)

    # Infer ERROR if no explicit severity field but event type/reason signals failure.
    if level == "UNKNOWN":
        type_hint = str(
            doc.get("type")
            or (doc.get("meta") or {}).get("schema", "")
            if isinstance(doc.get("meta"), dict)
            else doc.get("type") or ""
        ).lower()
        if any(tok in type_hint for tok in ("failed", "error", "fault")):
            level = "ERROR"
        elif any(tok in type_hint for tok in ("rejected", "breach", "warn")):
            level = "WARN"

    service = str(svc_raw).strip() if svc_raw is not None else None
    if service == "":
        service = None

    if msg_raw is not None:
        message = str(msg_raw).strip()
    else:
        message = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))[:300]

    hits = sum(
        (
            1 if ts_ptr is not None else 0,
            1 if (lvl_ptr is not None and level != "UNKNOWN") else 0,
            1 if svc_ptr is not None else 0,
            1 if msg_ptr is not None else 0,
        )
    )

    parsed = ParsedLog(
        timestamp=timestamp,
        level=level,
        service=service,
        message=message,
        summary=_build_summary(doc, service, message),
        metadata=_extract_metadata(doc),
        raw=raw_log,
        event=doc,
        parse_mode=parse_mode,
    )
    return parsed, hits


def _safe_pointer(doc: dict[str, Any], ptr: str | None) -> Any:
    if not ptr or not isinstance(ptr, str) or not ptr.startswith("/"):
        return None
    val = resolve_pointer(doc, ptr)
    return None if val is _MISSING else val


def _apply_schema_mapping(
    doc: dict[str, Any],
    raw_log: str,
    mapping: SchemaFieldMapping,
    fallback: ParsedLog,
) -> ParsedLog:
    ts_val = normalize_timestamp(_safe_pointer(doc, mapping.timestamp_ptr))
    lvl_val = normalize_level(_safe_pointer(doc, mapping.level_ptr))
    svc_raw = _safe_pointer(doc, mapping.service_ptr)
    msg_raw = _safe_pointer(doc, mapping.message_ptr)

    service = str(svc_raw).strip() if svc_raw is not None and str(svc_raw).strip() else fallback.service
    message = str(msg_raw).strip() if msg_raw is not None and str(msg_raw).strip() else fallback.message
    level: LogLevel = lvl_val if lvl_val != "UNKNOWN" else fallback.level
    timestamp = ts_val if ts_val is not None else fallback.timestamp

    return ParsedLog(
        timestamp=timestamp,
        level=level,
        service=service,
        message=message,
        summary=_build_summary(doc, service, message),
        metadata=_extract_metadata(doc),
        raw=raw_log,
        event=doc,
        parse_mode="schema_cache",
    )


def _try_parse_structured(raw_log: str) -> tuple[dict[str, Any] | None, ParseMode | None]:
    stripped = raw_log.strip()
    if not stripped:
        return None, None

    # 1. Fast JSON object detection
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed, "fast_json"
        except json.JSONDecodeError:
            pass

    # 2. Fast multi-line YAML mapping detection (e.g. key: value lines)
    if "\n" in stripped and ":" in stripped and not stripped.startswith("<"):
        try:
            parsed = yaml.safe_load(stripped)
            if isinstance(parsed, dict) and len(parsed) >= 2:
                # Ensure all keys are strings so arbitrary text isn't misread as YAML.
                if all(isinstance(k, str) for k in parsed.keys()):
                    return parsed, "fast_yaml"
        except yaml.YAMLError:
            pass

    return None, None


def build_rule_eval_document(parsed: ParsedLog) -> dict[str, Any]:
    """Build the document evaluated by JSON-Pointer rules.

    Exposes both canonical ParsedLog paths (`/level`, `/service`, `/metadata/...`,
    `/event/...`) and top-level original event paths (`/error/code`,
    `/payload/notional`, `/labels/severity`) with original unmodified types.
    """
    canonical = parsed.model_dump()
    if isinstance(parsed.event, dict):
        return {**parsed.event, **canonical}
    return canonical


class HybridLogParser:
    """Fast-path structured parser with schema-mapping cache and Ollama LLM fallback."""

    def __init__(
        self,
        llm_parser: LLMParserProtocol,
        enable_fast_parser: bool = True,
        enable_schema_learning: bool = True,
        max_cached_schemas: int = 512,
    ) -> None:
        self.llm_parser = llm_parser
        self.enable_fast_parser = enable_fast_parser
        self.enable_schema_learning = enable_schema_learning
        self.max_cached_schemas = max_cached_schemas
        self._schema_cache: OrderedDict[str, SchemaFieldMapping] = OrderedDict()
        self._schema_locks: dict[str, asyncio.Lock] = {}

    @property
    def cached_schema_count(self) -> int:
        return len(self._schema_cache)

    async def close(self) -> None:
        await self.llm_parser.close()

    def _put_cache(self, fingerprint: str, mapping: SchemaFieldMapping) -> None:
        self._schema_cache[fingerprint] = mapping
        self._schema_cache.move_to_end(fingerprint)
        while len(self._schema_cache) > self.max_cached_schemas:
            self._schema_cache.popitem(last=False)

    async def parse(self, raw_log: str) -> ParsedLog:
        if self.enable_fast_parser:
            doc, mode = _try_parse_structured(raw_log)
            if doc is not None and mode is not None:
                heuristic_log, hits = _extract_with_heuristics(doc, raw_log, mode)
                if hits >= 2 or not self.enable_schema_learning:
                    return heuristic_log

                # Alien/unseen JSON schema: check cache or learn field mapping once.
                fp = compute_schema_fingerprint(doc)
                cached = self._schema_cache.get(fp)
                if cached is not None:
                    self._schema_cache.move_to_end(fp)
                    return _apply_schema_mapping(doc, raw_log, cached, heuristic_log)

                infer_fn = getattr(self.llm_parser, "infer_schema_mapping", None)
                if callable(infer_fn):
                    lock = self._schema_locks.setdefault(fp, asyncio.Lock())
                    async with lock:
                        cached = self._schema_cache.get(fp)
                        if cached is not None:
                            return _apply_schema_mapping(doc, raw_log, cached, heuristic_log)
                        try:
                            mapping = await infer_fn(doc)
                            self._put_cache(fp, mapping)
                            logger.info(
                                "Learned schema mapping %s -> ts=%s level=%s svc=%s msg=%s",
                                fp,
                                mapping.timestamp_ptr,
                                mapping.level_ptr,
                                mapping.service_ptr,
                                mapping.message_ptr,
                            )
                            return _apply_schema_mapping(doc, raw_log, mapping, heuristic_log)
                        except Exception as exc:
                            logger.warning(
                                "Schema learning failed for %s, using heuristic fast path: %s",
                                fp,
                                exc,
                            )
                            return heuristic_log

                return heuristic_log

        # Unstructured plain text / syslog / nginx -> LLM parser
        return await self.llm_parser.parse(raw_log)
