"""Ops console server: auth, master-key handling, and what it sends to / returns from LiteLLM."""

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from console import metrics, server

MASTER = "sk-test-master-key-123"


@pytest.fixture
def calls(monkeypatch):
    """Replace the LiteLLM admin client with a recorder that returns canned data."""
    monkeypatch.setenv("LITELLM_MASTER_KEY", MASTER)
    server._sessions.clear()
    log = []

    async def fake_gw(method, path, **kw):
        log.append((method, path, kw.get("json"), kw.get("params")))
        if path == "/credentials" and method == "GET":
            return {"credentials": [{"credential_name": "openai-prod",
                                     "credential_values": {"api_key": "sk-REALSECRET****"},
                                     "credential_info": {"custom_llm_provider": "openai"}}]}
        if path == "/key/generate":
            return {"key": "sk-new-virtual-key", "token": "hash123", "key_alias": kw["json"]["key_alias"]}
        if path == "/team/list":
            return [{"team_id": "t1", "team_alias": "search"}]
        if path == "/key/list":
            return {"keys": [{"token": "h1", "key_alias": "a", "key_name": "sk-...abcd", "team_id": "t1"},
                             {"token": "h2", "key_alias": "litellm-dashboard"}]}
        return {"ok": True}

    monkeypatch.setattr(server, "gw", fake_gw)
    return log


def signed_in(client):
    assert client.post("/api/login", json={"master_key": MASTER}).status_code == 200
    return client


def test_api_requires_login(calls):
    c = TestClient(server.app)
    assert c.get("/api/teams").status_code == 401
    assert c.get("/api/session").json() == {"signed_in": False}


def test_wrong_master_key_rejected(calls, monkeypatch):
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    c = TestClient(server.app)
    assert c.post("/api/login", json={"master_key": "sk-wrong"}).status_code == 401


def test_login_sets_httponly_cookie_and_never_echoes_key(calls):
    c = TestClient(server.app)
    r = c.post("/api/login", json={"master_key": MASTER})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert MASTER not in r.text and MASTER.lower() not in cookie


def test_writes_need_csrf_header(calls):
    c = signed_in(TestClient(server.app))
    body = {"team_alias": "search", "models": ["gpt-4o-mini"], "max_budget": 5}
    assert c.post("/api/teams", json=body).status_code == 403
    assert c.post("/api/teams", json=body, headers={"x-console": "1"}).status_code == 200
    method, path, sent, _ = calls[-1]
    assert (method, path) == ("POST", "/team/new") and sent["team_alias"] == "search"


def test_credentials_list_drops_values(calls):
    c = signed_in(TestClient(server.app))
    r = c.get("/api/credentials")
    assert r.json() == [{"credential_name": "openai-prod", "provider": "openai", "created_at": None}]
    assert "REALSECRET" not in r.text


def test_store_credential_sends_key_to_gateway_only(calls):
    c = signed_in(TestClient(server.app))
    r = c.post("/api/credentials", headers={"x-console": "1"},
               json={"credential_name": "groq-main", "provider": "groq", "api_key": "gsk_abcdefgh123"})
    assert r.status_code == 200 and "gsk_" not in r.text
    _, path, sent, _ = calls[-1]
    assert path == "/credentials" and sent["credential_values"] == {"api_key": "gsk_abcdefgh123"}
    bad = c.post("/api/credentials", headers={"x-console": "1"},
                 json={"credential_name": "x", "provider": "not-a-provider", "api_key": "12345678"})
    assert bad.status_code == 400


def test_add_model_prefixes_provider_from_credential(calls):
    c = signed_in(TestClient(server.app))
    r = c.post("/api/models", headers={"x-console": "1"},
               json={"model_name": "fast-chat", "provider_model": "gpt-4o-mini", "credential_name": "openai-prod"})
    assert r.status_code == 200
    _, path, sent, _ = calls[-1]
    assert path == "/model/new"
    assert sent["litellm_params"] == {"model": "openai/gpt-4o-mini", "litellm_credential_name": "openai-prod"}


def test_generate_key_returns_it_once_and_list_hides_ui_session_keys(calls):
    c = signed_in(TestClient(server.app))
    r = c.post("/api/keys", headers={"x-console": "1"}, json={"key_alias": "search-prod", "team_id": "t1"})
    assert r.json()["key"] == "sk-new-virtual-key"
    keys = c.get("/api/keys").json()
    assert [(k["key_alias"], k["team_alias"]) for k in keys] == [("a", "search")]


def test_key_duration_validated(calls):
    c = signed_in(TestClient(server.app))
    r = c.post("/api/keys", headers={"x-console": "1"}, json={"key_alias": "x", "duration": "forever"})
    assert r.status_code == 422


def test_overview_payload_shape():
    teams = pd.DataFrame({"team_id": ["a"], "team_alias": ["platform"], "spend": [0.5], "max_budget": [1.0],
                          "blocked": [False]})
    spend = pd.DataFrame({"request_id": ["1", "2"], "start_time": pd.to_datetime(["2026-11-01T10:00Z"] * 2),
                          "model": ["m", "m"], "model_group": ["gpt-4o-mini"] * 2, "model_id": ["x", "x"],
                          "spend": [0.1, 0.2], "total_tokens": [10, 20], "cache_hit": ["True", ""],
                          "team_id": ["a", "a"], "status": ["success", "success"], "key_alias": ["k", "k"]})
    log = pd.DataFrame([{"ts": pd.Timestamp("2026-11-01T10:00Z"), "status": "failure", "provider": "openai",
                         "cache_hit": False, "team": "platform", "model_group": "gpt-4o-mini", "error": "503"}],
                       columns=["ts", "status", "model_group", "deployment", "provider", "team", "key_alias",
                                "cost_usd", "total_tokens", "cache_hit", "latency_s", "error"])
    out = metrics.overview_payload(teams, spend, log)
    assert out["kpis"]["requests"] == 2 and out["kpis"]["upstream_failure_rate"] == 100.0
    assert out["budgets"][0]["budget_used_pct"] == 50.0
    assert out["recent"][0]["cache_hit"] is True
    naive = metrics.records(pd.DataFrame({"t": pd.to_datetime(["2026-11-01 10:00:00"])}))
    assert naive[0]["t"].endswith("+00:00")
    assert len(out["timeline"]) == 60
    import json
    json.dumps(out)  # must be JSON-serialisable (no NaN/Timestamp objects)


def test_requests_per_minute_zero_fills():
    now = pd.Timestamp("2026-11-01T10:05Z")
    df = pd.DataFrame({"start_time": ["2026-11-01T10:05:10Z", "2026-11-01T10:05:50Z", "2026-11-01T10:03:00Z"]})
    series = metrics.requests_per_minute(df, minutes=4, now=now)
    assert [p["n"] for p in series] == [0, 1, 0, 2]


def test_demo_password_signs_in(calls):
    c = TestClient(server.app)
    assert c.post("/api/login", json={"master_key": "123"}).status_code == 200
    assert c.get("/api/session").json() == {"signed_in": True}


def test_demo_key_mints_team_key(calls, monkeypatch):
    server._demo_keys.clear()
    minted = __import__("asyncio").run(server._demo_key("t1"))
    assert minted == "sk-new-virtual-key"
    gen = [c for c in calls if c[1] == "/key/generate"][0][2]
    assert gen["team_id"] == "t1" and gen["duration"] == "1h"
