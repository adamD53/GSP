import json
from typing import Any

import httpx

from app.schemas import LLMExtractedLog, ParsedLog, SchemaFieldMapping

SYSTEM_PROMPT = (
    "You are a log parser. Extract structured fields from the raw log line. "
    "Return ONLY valid JSON matching the provided schema. "
    "Do not invent timestamps or service names that are not present in the log."
)

SCHEMA_MAPPING_PROMPT = (
    "You map unknown JSON event schemas to canonical log fields using RFC 6901 JSON Pointers "
    "(starting with '/', e.g. '/hdr/app_id' or '/body/detail'). "
    "Return ONLY valid JSON matching the provided schema. Use null if a field does not exist."
)


class OllamaLogParser:
    """Calls Ollama's /api/chat endpoint with JSON Schema structured outputs."""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def parse(self, raw_log: str) -> ParsedLog:
        payload = {
            "model": self.model,
            "stream": False,
            "format": LLMExtractedLog.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": raw_log},
            ],
        }
        response = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        response.raise_for_status()
        body = response.json()
        content = body["message"]["content"]
        extracted = LLMExtractedLog.model_validate_json(content)
        return ParsedLog(**extracted.model_dump(), raw=raw_log, parse_mode="llm")

    async def infer_schema_mapping(self, sample_doc: dict[str, Any]) -> SchemaFieldMapping:
        payload = {
            "model": self.model,
            "stream": False,
            "format": SchemaFieldMapping.model_json_schema(),
            "options": {"temperature": 0},
            "messages": [
                {"role": "system", "content": SCHEMA_MAPPING_PROMPT},
                {"role": "user", "content": json.dumps(sample_doc, ensure_ascii=False)[:2000]},
            ],
        }
        response = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        response.raise_for_status()
        body = response.json()
        content = body["message"]["content"]
        return SchemaFieldMapping.model_validate_json(content)
