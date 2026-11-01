"""Mock provider, request logger record shape, dashboard metrics, config sanity."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml
from fastapi.testclient import TestClient

from dashboard import metrics
from gateway import request_logger
from mock_provider import app as mock

ROOT = Path(__file__).resolve().parent.parent


# ---------- mock provider ----------
def test_mock_failure_switch(monkeypatch):
    monkeypatch.setattr(mock, "LATENCY_S", 0)
    c = TestClient(mock.app)
    c.post("/admin/failures", json={"openai": False, "anthropic": False})
    payload = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
    ok = c.post("/openai/v1/chat/completions", json=payload)
    assert ok.status_code == 200 and ok.json()["usage"]["completion_tokens"] > 0
    c.post("/admin/failures", json={"openai": True})
    assert c.post("/openai/v1/chat/completions", json=payload).status_code == 503
    assert c.post("/anthropic/v1/chat/completions", json=payload).status_code == 200
    assert c.post("/admin/failures", json={"azure": True}).status_code == 400
    c.post("/admin/failures", json={"openai": False})


def test_initial_failures_env_parsing():
    assert mock.initial_failures(" OpenAI , ") == {"openai": True, "anthropic": False}
    assert mock.initial_failures(None) == {"openai": False, "anthropic": False}


# ---------- request logger ----------
def test_build_record_failure_has_no_prompt():
    kwargs = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "SECRET PROMPT"}],
        "litellm_params": {"model_info": {"provider_label": "openai (mock)"}},
        "standard_logging_object": {
            "model_group": "gpt-4o-mini", "model": "openai/gpt-4o-mini",
            "metadata": {"user_api_key_team_alias": "platform-eng", "user_api_key_alias": "k1"},
            "response_cost": 0, "error_str": "503 forced failure",
            "endTime": datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp(),
        },
    }
    rec = request_logger.build_record(kwargs, "failure", 0.61)
    assert rec["provider"] == "openai (mock)" and rec["team"] == "platform-eng"
    assert rec["status"] == "failure" and "503" in rec["error"]
    assert "SECRET PROMPT" not in json.dumps(rec)


def test_gateway_rejection_is_not_a_provider_failure():
    kwargs = {"litellm_params": {}, "standard_logging_object": {
        "model_group": "claude-haiku", "custom_llm_provider": "openai",
        "metadata": {"user_api_key_team_alias": "support-bot"},
        "error_str": "Budget has been exceeded! Team=x"}}
    rec = request_logger.build_record(kwargs, "failure", 0.004)
    assert rec["status"] == "rejected" and rec["provider"] == "gateway"


def test_provider_label_falls_back_to_api_base():
    assert request_logger.provider_label({}, "http://mock:8001/anthropic/v1", "openai") == "anthropic"
    assert request_logger.provider_label(None, None, "openai") == "openai"


def test_write_record_appends_jsonl(tmp_path):
    p = tmp_path / "logs" / "r.jsonl"
    request_logger.write_record({"a": 1}, p)
    request_logger.write_record({"a": 2}, p)
    assert [json.loads(x)["a"] for x in p.read_text().splitlines()] == [1, 2]


# ---------- dashboard metrics ----------
def test_team_budgets_flags_blocked_team():
    teams = pd.DataFrame({"team_id": ["a", "b"], "team_alias": ["platform", "support"],
                          "spend": [0.2, 0.0012], "max_budget": [1.0, 0.001]})
    out = metrics.team_budgets(teams).set_index("team_alias")
    assert out.loc["support", "blocked"] and not out.loc["platform", "blocked"]
    assert out.loc["platform", "budget_used_pct"] == 20.0


def test_provider_failure_rates_excludes_cache_hits(tmp_path):
    rows = [
        {"status": "success", "provider": "openai", "cache_hit": False},
        {"status": "failure", "provider": "openai", "cache_hit": False},
        {"status": "success", "provider": "openai", "cache_hit": True},
        {"status": "success", "provider": "anthropic", "cache_hit": False},
        {"status": "rejected", "provider": "gateway", "cache_hit": False, "team": "support",
         "error": "Budget has been exceeded!"},
    ]
    p = tmp_path / "r.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n{broken line")
    log = metrics.load_request_log(p)
    rates = metrics.provider_failure_rates(log).set_index("provider")
    assert rates.loc["openai", "attempts"] == 2 and rates.loc["openai", "failure_rate_pct"] == 50.0
    assert rates.loc["anthropic", "failure_rate_pct"] == 0.0
    assert "gateway" not in rates.index
    assert metrics.cache_hit_rate(log) == round(100 / 3, 1)
    rej = metrics.gateway_rejections(log)
    assert rej.to_dict("records") == [{"team": "support", "reason": "budget exceeded", "count": 1}]


def test_cost_by_model_and_team():
    spend = pd.DataFrame({"request_id": ["1", "2", "3"], "model": ["m", "m", "c"],
                          "model_group": ["gpt-4o-mini", "gpt-4o-mini", ""],
                          "total_tokens": [10, 20, 5], "spend": [0.1, 0.2, 0.05], "team_id": ["a", "gone", None]})
    by_model = metrics.cost_by_model(spend).set_index("model_group")
    assert by_model.loc["gpt-4o-mini", "requests"] == 2
    assert "c" in by_model.index  # empty model_group falls back to model
    teams = pd.DataFrame({"team_id": ["a"], "team_alias": ["platform"]})
    by_team = metrics.spend_by_team(spend, teams).set_index("team_alias")
    assert round(by_team.loc["platform", "spend"], 3) == 0.1
    assert round(by_team.loc["(deleted team)", "spend"], 3) == 0.2 and "(no team)" in by_team.index


def test_empty_inputs_do_not_crash(tmp_path):
    log = metrics.load_request_log(tmp_path / "missing.jsonl")
    assert metrics.provider_failure_rates(log).empty
    assert metrics.cache_hit_rate(log) is None


# ---------- config ----------
def _load(name):
    return yaml.safe_load((ROOT / "gateway" / name).read_text())


def test_local_and_docker_configs_differ_only_in_cache():
    docker, local = _load("config.yaml"), _load("config.local.yaml")
    assert docker["litellm_settings"]["cache_params"]["type"] == "redis"
    assert local["litellm_settings"]["cache_params"]["type"] == "local"
    for cfg in (docker, local):
        cfg["litellm_settings"].pop("cache_params")
    assert docker == local


def test_no_secrets_in_config():
    for name in ("config.yaml", "config.local.yaml"):
        cfg = _load(name)
        assert cfg["general_settings"]["master_key"].startswith("os.environ/")
        for m in cfg["model_list"]:
            assert m["litellm_params"]["api_key"].startswith("os.environ/")


def test_fallback_targets_exist():
    cfg = _load("config.yaml")
    names = {m["model_name"] for m in cfg["model_list"]}
    for rule in cfg["router_settings"]["fallbacks"]:
        for src, targets in rule.items():
            assert src in names and set(targets) <= names


def test_litellm_pinned_to_reviewed_version():
    lock = (ROOT / "requirements.lock").read_text()
    assert "litellm==1.106.5 \\" in lock
    assert "litellm==1.107.0" not in lock
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "ghcr.io/berriai/litellm:v1.106.5@sha256:" in compose
