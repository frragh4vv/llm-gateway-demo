"""Ops console: one web app for everything the ops team does with the gateway.

The browser never holds the master key. Ops logs in once with it; the server
checks it, issues an HttpOnly session cookie, and makes every LiteLLM admin call
itself. Provider API keys typed into the console go straight to LiteLLM's
credential store (encrypted with LITELLM_SALT_KEY) and are never sent back.

Run: uvicorn console.server:app --port 8080
"""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from console import metrics

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:4000")
MOCK_URL = os.environ.get("MOCK_ADMIN_URL", "http://localhost:8001")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
REQUEST_LOG_PATH = Path(os.environ.get("REQUEST_LOG_PATH", "logs/requests.jsonl"))
SESSION_TTL_S = 8 * 3600
COOKIE = "gw_console"
STATIC = Path(__file__).resolve().parent / "static"

# Providers the "add credential" form offers, mapped to LiteLLM's provider prefix.
PROVIDERS = {
    "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Google Gemini",
    "groq": "Groq", "mistral": "Mistral", "azure": "Azure OpenAI", "cohere": "Cohere",
}

app = FastAPI(title="LLM Gateway console", docs_url=None, redoc_url=None)
_sessions: dict[str, float] = {}  # token -> expiry (in memory: restarting logs everyone out)


def master_key() -> str:
    key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not key:
        raise HTTPException(500, "LITELLM_MASTER_KEY is not set on the console server")
    return key


# ---------- auth ----------
def require_session(request: Request) -> None:
    token = request.cookies.get(COOKIE, "")
    expiry = _sessions.get(token)
    if not expiry or expiry < time.time():
        _sessions.pop(token, None)
        raise HTTPException(401, "not signed in")
    # Cookie is SameSite=Strict; a custom header on writes is a second CSRF guard.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-console") != "1":
        raise HTTPException(403, "missing x-console header")


class Login(BaseModel):
    master_key: str


# Demo build: ops can sign in with a short console password (default "123")
# instead of pasting the master key. The master key itself still never leaves the server.
DEMO_PASSWORD = os.environ.get("CONSOLE_PASSWORD", "123")


@app.post("/api/login")
def login(body: Login, response: Response) -> dict:
    given = body.master_key.encode()
    ok = secrets.compare_digest(given, master_key().encode()) or (
        bool(DEMO_PASSWORD) and secrets.compare_digest(given, DEMO_PASSWORD.encode()))
    if not ok:
        time.sleep(0.5)  # slow down guessing
        raise HTTPException(401, "wrong password")
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + SESSION_TTL_S
    response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=SESSION_TTL_S)
    return {"ok": True}


@app.post("/api/logout")
def logout(request: Request, response: Response) -> dict:
    _sessions.pop(request.cookies.get(COOKIE, ""), None)
    response.delete_cookie(COOKIE)
    return {"ok": True}


@app.get("/api/session")
def session(request: Request) -> dict:
    expiry = _sessions.get(request.cookies.get(COOKIE, ""), 0)
    return {"signed_in": expiry > time.time()}


# ---------- gateway helper ----------
async def gw(method: str, path: str, **kwargs: Any) -> Any:
    headers = {"Authorization": f"Bearer {master_key()}"}
    try:
        async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=30) as client:
            r = await client.request(method, path, headers=headers, **kwargs)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"gateway unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise HTTPException(r.status_code, _error_text(r))
    return r.json() if r.content else {}


def _error_text(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text[:300]
    err = body.get("error", body.get("detail", body))
    if isinstance(err, dict):
        err = err.get("message") or err.get("error") or err
    return str(err)[:300]


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [])}


auth = [Depends(require_session)]


# ---------- status + overview ----------
@app.get("/api/status", dependencies=auth)
async def status() -> dict:
    out: dict[str, Any] = {"gateway": False, "database": False, "mock": None}
    async with httpx.AsyncClient(timeout=3) as client:
        try:
            out["gateway"] = (await client.get(f"{GATEWAY_URL}/health/liveliness")).status_code == 200
        except httpx.HTTPError:
            pass
        try:
            out["mock"] = (await client.get(f"{MOCK_URL}/admin/failures")).json()
        except (httpx.HTTPError, ValueError):
            pass
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as conn:
            conn.execute("SELECT 1")
        out["database"] = True
    except Exception:
        pass
    return out


def _query(sql: str) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])


@app.get("/api/overview", dependencies=auth)
def overview() -> dict:
    teams = _query('SELECT team_id, team_alias, spend, max_budget, blocked FROM "LiteLLM_TeamTable"')
    spend = _query(metrics.SPEND_SQL)
    log = metrics.load_request_log(REQUEST_LOG_PATH)
    return metrics.overview_payload(teams, spend, log)


@app.get("/api/logs", dependencies=auth)
def logs(limit: int = 200) -> list[dict]:
    log = metrics.load_request_log(REQUEST_LOG_PATH)
    return metrics.records(log.tail(min(limit, 1000)).iloc[::-1])


# ---------- teams ----------
class TeamIn(BaseModel):
    team_alias: str = Field(min_length=1, max_length=64)
    models: list[str] = []
    max_budget: float | None = Field(default=None, ge=0)


@app.get("/api/teams", dependencies=auth)
async def list_teams() -> list[dict]:
    teams = await gw("GET", "/team/list")
    keep = ("team_id", "team_alias", "models", "spend", "max_budget", "blocked", "created_at")
    return [{k: t.get(k) for k in keep} | {"keys": len(t.get("keys") or [])} for t in teams]


@app.post("/api/teams", dependencies=auth)
async def create_team(body: TeamIn) -> dict:
    return await gw("POST", "/team/new", json=_clean(body.model_dump()) | {"metadata": {"created_by": "console"}})


@app.patch("/api/teams/{team_id}", dependencies=auth)
async def update_team(team_id: str, body: TeamIn) -> dict:
    return await gw("POST", "/team/update", json={"team_id": team_id, **body.model_dump()})


@app.post("/api/teams/{team_id}/{action}", dependencies=auth)
async def block_team(team_id: str, action: str) -> dict:
    if action not in ("block", "unblock"):
        raise HTTPException(404)
    return await gw("POST", f"/team/{action}", json={"team_id": team_id})


@app.delete("/api/teams/{team_id}", dependencies=auth)
async def delete_team(team_id: str) -> dict:
    return await gw("POST", "/team/delete", json={"team_ids": [team_id]})


# ---------- virtual keys ----------
class KeyIn(BaseModel):
    key_alias: str = Field(min_length=1, max_length=64)
    team_id: str | None = None
    models: list[str] = []
    max_budget: float | None = Field(default=None, ge=0)
    duration: str | None = Field(default=None, pattern=r"^\d+[smhd]$")  # e.g. 30d


class KeyUpdate(BaseModel):
    key_alias: str | None = None
    models: list[str] | None = None
    max_budget: float | None = Field(default=None, ge=0)


@app.get("/api/keys", dependencies=auth)
async def list_keys() -> list[dict]:
    data = await gw("GET", "/key/list", params={"return_full_object": "true", "size": 100})
    teams = {t["team_id"]: t.get("team_alias") for t in await gw("GET", "/team/list")}
    keep = ("token", "key_alias", "key_name", "team_id", "models", "spend",
            "max_budget", "blocked", "expires", "created_at", "last_active")
    # key_name is LiteLLM's masked display form (sk-...abcd); the full key is never listed.
    return [{k: key.get(k) for k in keep} | {"team_alias": teams.get(key.get("team_id"))}
            for key in data.get("keys", [])
            if not key.get("is_session_token") and key.get("key_alias") != "litellm-dashboard"]


@app.post("/api/keys", dependencies=auth)
async def create_key(body: KeyIn) -> dict:
    created = await gw("POST", "/key/generate", json=_clean(body.model_dump()) | {"metadata": {"created_by": "console"}})
    # The full key is returned exactly once, here. The console does not store it.
    return {"key": created["key"], "token": created.get("token"), "key_alias": created.get("key_alias")}


@app.patch("/api/keys/{token}", dependencies=auth)
async def update_key(token: str, body: KeyUpdate) -> dict:
    return await gw("POST", "/key/update", json={"key": token, **body.model_dump(exclude_none=True)})


@app.post("/api/keys/{token}/{action}", dependencies=auth)
async def block_key(token: str, action: str) -> dict:
    if action not in ("block", "unblock"):
        raise HTTPException(404)
    return await gw("POST", f"/key/{action}", json={"key": token})


@app.delete("/api/keys/{token}", dependencies=auth)
async def delete_key(token: str) -> dict:
    return await gw("POST", "/key/delete", json={"keys": [token]})


# ---------- provider credentials + models ----------
class CredentialIn(BaseModel):
    credential_name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    provider: str
    api_key: str = Field(min_length=8)
    api_base: str | None = None


class ModelIn(BaseModel):
    model_name: str = Field(min_length=1, max_length=64)  # what dev teams ask for
    provider_model: str = Field(min_length=1)             # e.g. gemini-2.5-flash
    credential_name: str


@app.get("/api/providers", dependencies=auth)
def providers() -> dict:
    return PROVIDERS


@app.get("/api/credentials", dependencies=auth)
async def list_credentials() -> list[dict]:
    data = await gw("GET", "/credentials")
    # Only names and metadata leave the server. Values (even masked) are dropped.
    return [{"credential_name": c.get("credential_name"),
             "provider": (c.get("credential_info") or {}).get("custom_llm_provider"),
             "created_at": (c.get("credential_info") or {}).get("created_at")}
            for c in data.get("credentials", [])]


@app.post("/api/credentials", dependencies=auth)
async def create_credential(body: CredentialIn) -> dict:
    if body.provider not in PROVIDERS:
        raise HTTPException(400, "unknown provider")
    values = _clean({"api_key": body.api_key, "api_base": body.api_base})
    await gw("POST", "/credentials", json={
        "credential_name": body.credential_name,
        "credential_values": values,
        "credential_info": {"custom_llm_provider": body.provider, "created_by": "console"},
    })
    return {"ok": True, "credential_name": body.credential_name}


@app.delete("/api/credentials/{name}", dependencies=auth)
async def delete_credential(name: str) -> dict:
    return await gw("DELETE", f"/credentials/{name}")


@app.get("/api/models", dependencies=auth)
async def list_models() -> list[dict]:
    data = await gw("GET", "/v2/model/info")
    out = []
    for m in data.get("data", []):
        info, params = m.get("model_info") or {}, m.get("litellm_params") or {}
        out.append({
            "id": info.get("id"),
            "model_name": m.get("model_name"),
            "provider_model": params.get("model"),
            "provider": info.get("provider_label") or params.get("custom_llm_provider")
                        or str(params.get("model", "")).split("/")[0],
            "credential": params.get("litellm_credential_name"),
            "editable": bool(info.get("db_model")),  # config-file models are read-only
            "input_cost_per_m": _per_million(info.get("input_cost_per_token") or params.get("input_cost_per_token")),
            "output_cost_per_m": _per_million(info.get("output_cost_per_token") or params.get("output_cost_per_token")),
        })
    return out


def _per_million(v: Any) -> float | None:
    return round(float(v) * 1_000_000, 4) if v else None


@app.post("/api/models", dependencies=auth)
async def create_model(body: ModelIn) -> dict:
    creds = {c["credential_name"]: c for c in await list_credentials()}
    cred = creds.get(body.credential_name)
    if not cred:
        raise HTTPException(400, "unknown credential")
    provider = cred["provider"]
    model = body.provider_model if body.provider_model.startswith(f"{provider}/") else f"{provider}/{body.provider_model}"
    created = await gw("POST", "/model/new", json={
        "model_name": body.model_name,
        "litellm_params": {"model": model, "litellm_credential_name": body.credential_name},
        "model_info": {"provider_label": PROVIDERS.get(provider, provider).lower()},
    })
    # LiteLLM echoes litellm_params (encrypted); none of that goes to the browser.
    return {"ok": True, "model_id": created.get("model_id"), "model_name": body.model_name}


@app.delete("/api/models/{model_id}", dependencies=auth)
async def delete_model(model_id: str) -> dict:
    return await gw("POST", "/model/delete", json={"id": model_id})


# ---------- playground + mock switch ----------
class PlaygroundIn(BaseModel):
    key: str = Field(min_length=1)
    model: str
    prompt: str = Field(min_length=1, max_length=4000)
    team_id: str | None = None


# Demo shortcut: typing the demo password ("123") as the virtual key makes the console mint
# a 1-hour key for the chosen team, so the playground still goes through that team's
# model ACL, budget and cache exactly like a real dev-team key would.
_demo_keys: dict[str, tuple[str, float]] = {}


async def _demo_key(team_id: str | None) -> str:
    slot = team_id or "_none"
    cached = _demo_keys.get(slot)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    body: dict[str, Any] = {"duration": "1h", "key_alias": f"playground-demo-{slot}"[:64],
                            "metadata": {"created_by": "console-playground"}}
    if team_id:
        body["team_id"] = team_id
    created = await gw("POST", "/key/generate", json=body)
    _demo_keys[slot] = (created["key"], time.time() + 3600)
    return created["key"]


@app.post("/api/playground", dependencies=auth)
async def playground(body: PlaygroundIn) -> JSONResponse:
    """Call the gateway exactly like a dev team would: with a virtual key, not the master key."""
    key = body.key
    if DEMO_PASSWORD and secrets.compare_digest(key.encode(), DEMO_PASSWORD.encode()):
        key = await _demo_key(body.team_id)
    t0 = time.perf_counter()
    async with httpx.AsyncClient(base_url=GATEWAY_URL, timeout=120) as client:
        r = await client.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                              json={"model": body.model, "max_tokens": 800, "reasoning_effort": "low",
                                    "messages": [{"role": "user", "content": body.prompt}]})
    latency = round(time.perf_counter() - t0, 2)
    if r.status_code >= 400:
        return JSONResponse({"ok": False, "status": r.status_code, "error": _error_text(r), "latency_s": latency})
    data = r.json()
    return JSONResponse({
        "ok": True, "status": r.status_code, "latency_s": latency,
        "content": data["choices"][0]["message"].get("content") or "",
        "served_by": r.headers.get("x-litellm-model-id"),
        "cost_usd": float(r.headers.get("x-litellm-response-cost") or 0),
        "cache_hit": r.headers.get("x-litellm-cache-key") is not None and latency < 0.3,
        "attempted_fallbacks": int(r.headers.get("x-litellm-attempted-fallbacks") or 0),
        "usage": data.get("usage"),
    })


@app.post("/api/mock/failures", dependencies=auth)
async def mock_failures(body: dict[str, bool]) -> dict:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.post(f"{MOCK_URL}/admin/failures", json=body)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:200])
    return r.json()


# ---------- static frontend ----------
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})
