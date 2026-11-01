"""Tiny OpenAI-compatible mock LLM provider.

It stands in for two upstream providers so the gateway can be demoed offline:

    /openai/v1/chat/completions      -> pretends to be OpenAI
    /anthropic/v1/chat/completions   -> pretends to be Anthropic (OpenAI wire format)

A "force failure" switch makes one provider return HTTP 503, which is how the
demo shows automatic fallback and the per-provider failure rate on the dashboard.

    GET  /admin/failures                  -> {"openai": false, "anthropic": false}
    POST /admin/failures {"openai": true} -> openai now fails every request
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

PROVIDERS = ("openai", "anthropic")
LATENCY_S = float(os.getenv("MOCK_LATENCY_S", "0.6"))


def initial_failures(env_value: str | None) -> dict[str, bool]:
    """Parse MOCK_FAIL_PROVIDERS ("openai,anthropic") into the failure map."""
    wanted = {p.strip().lower() for p in (env_value or "").split(",") if p.strip()}
    return {p: p in wanted for p in PROVIDERS}


def estimate_tokens(text: str) -> int:
    # Rough 4-chars-per-token estimate; good enough for cost tracking in a demo.
    return max(1, len(text) // 4)


def build_completion(provider: str, model: str, messages: list[dict]) -> dict:
    prompt = " ".join(str(m.get("content", "")) for m in messages)
    last = str(messages[-1].get("content", "")) if messages else ""
    answer = f"[{provider} mock / {model}] You said: {last[:200]}"
    prompt_tokens = estimate_tokens(prompt)
    completion_tokens = estimate_tokens(answer) + 40  # pad so spend is visible
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


app = FastAPI(title="Mock LLM provider")
app.state.failures = initial_failures(os.getenv("MOCK_FAIL_PROVIDERS"))
app.state.calls = {p: 0 for p in PROVIDERS}


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "failures": app.state.failures, "calls": app.state.calls}


@app.get("/admin/failures")
async def get_failures() -> dict:
    return app.state.failures


@app.post("/admin/failures")
async def set_failures(body: dict) -> dict:
    for provider, enabled in body.items():
        if provider not in PROVIDERS:
            raise HTTPException(400, f"unknown provider {provider!r}")
        app.state.failures[provider] = bool(enabled)
    return app.state.failures


@app.get("/{provider}/v1/models")
async def list_models(provider: str) -> dict:
    return {"object": "list", "data": []}


@app.post("/{provider}/v1/chat/completions")
async def chat_completions(provider: str, request: Request):
    if provider not in PROVIDERS:
        raise HTTPException(404, "unknown provider")
    app.state.calls[provider] += 1
    body = await request.json()
    await asyncio.sleep(LATENCY_S)
    if app.state.failures[provider]:
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "message": f"{provider} mock is down (forced failure)",
                    "type": "service_unavailable",
                    "code": "forced_failure",
                }
            },
        )
    if body.get("stream"):
        raise HTTPException(400, "mock provider does not support streaming")
    return build_completion(provider, body.get("model", "unknown"), body.get("messages", []))
