"""LiteLLM custom callback: one JSON line per upstream attempt.

LiteLLM already writes successful requests to its LiteLLM_SpendLogs table. This
logger adds what the ops dashboard needs on top of that: every *failed* attempt
against a provider, including attempts that were rescued by a fallback. That is
what makes "failure rate per provider" possible.

Loaded from config via `callbacks: request_logger.request_logger`.
Prompts and responses are never written here, only metadata.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # litellm is only present inside the proxy process
    from litellm.integrations.custom_logger import CustomLogger
except ImportError:  # pragma: no cover - lets tests import build_record without litellm
    CustomLogger = object  # type: ignore[misc,assignment]

LOG_PATH = Path(os.getenv("REQUEST_LOG_PATH", "logs/requests.jsonl"))
_lock = threading.Lock()


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    return str(value)


def provider_label(model_info: dict | None, api_base: str | None, fallback: str | None) -> str:
    """Human provider name: model_info.provider_label, else guessed from api_base."""
    if model_info and model_info.get("provider_label"):
        return str(model_info["provider_label"])
    base = (api_base or "").lower()
    for name in ("anthropic", "openai", "azure", "bedrock", "gemini", "mistral"):
        if name in base:
            return name
    return fallback or "unknown"


def build_record(kwargs: dict, status: str, latency_s: float | None) -> dict:
    """Turn LiteLLM callback kwargs into a flat, prompt-free log record."""
    slo = kwargs.get("standard_logging_object") or {}
    meta = slo.get("metadata") or {}
    litellm_params = kwargs.get("litellm_params") or {}
    model_info = litellm_params.get("model_info") or {}
    api_base = slo.get("api_base") or litellm_params.get("api_base")
    error = slo.get("error_str")
    if status == "failure" and not error:
        exc = kwargs.get("exception")
        error = str(exc) if exc else "unknown error"
    # A failure with no deployment picked never reached a provider: the gateway itself
    # refused it (budget, model ACL, bad key). Keep it out of provider failure rates.
    rejected = status == "failure" and not model_info and not api_base
    return {
        "ts": _iso(slo.get("endTime")) or datetime.now(timezone.utc).isoformat(),
        "status": "rejected" if rejected else status,
        "model_group": slo.get("model_group") or kwargs.get("model"),
        "deployment": slo.get("model") or kwargs.get("model"),
        "provider": "gateway" if rejected else provider_label(model_info, api_base, slo.get("custom_llm_provider")),
        "team": meta.get("user_api_key_team_alias") or meta.get("user_api_key_team_id"),
        "key_alias": meta.get("user_api_key_alias"),
        "cost_usd": float(slo.get("response_cost") or 0.0),
        "total_tokens": int(slo.get("total_tokens") or 0),
        "cache_hit": bool(slo.get("cache_hit")),
        "latency_s": round(latency_s, 3) if latency_s is not None else None,
        "error": (error or "")[:300] or None,
    }


def write_record(record: dict, path: Path = LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, default=str)
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def _latency(start: Any, end: Any) -> float | None:
    try:
        return (end - start).total_seconds()
    except Exception:
        return None


class RequestLogger(CustomLogger):  # type: ignore[misc]
    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        write_record(build_record(kwargs, "success", _latency(start_time, end_time)))

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        write_record(build_record(kwargs, "failure", _latency(start_time, end_time)))


request_logger = RequestLogger()
