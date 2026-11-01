"""Demo script logic against fake gateway + mock-provider transports (no network)."""

import json

import httpx
import pytest

from scripts import demo


def test_is_budget_error_matches_litellm_message():
    body = {"error": {"message": "Budget has been exceeded! Team=abc Current cost: 0.0012, Max budget: 0.001",
                      "type": "budget_exceeded"}}
    assert demo.is_budget_error(400, body)
    assert demo.is_budget_error(422, body)  # what litellm 1.106.5 actually returns
    assert not demo.is_budget_error(200, body)
    assert not demo.is_budget_error(400, {"error": {"message": "bad request"}})


def test_is_model_denied():
    body = {"error": {"message": "team not allowed to access model. This team can only access models=['claude-haiku']"}}
    assert demo.is_model_denied(401, body)
    real = {"error": {"message": "The requested model 'gpt-4o' is not available for this API key, "
                                 "or the model name is invalid."}}
    assert demo.is_model_denied(403, real)
    assert not demo.is_model_denied(500, body)


def test_served_by_reads_mock_tag():
    body = {"model": "claude-3-5-haiku",
            "choices": [{"message": {"content": "[anthropic mock / claude-3-5-haiku] You said: hi"}}]}
    assert demo.served_by({}, body) == "anthropic mock / claude-3-5-haiku"
    assert demo.served_by({}, {"model": "x", "choices": [{"message": {"content": "plain"}}]}) == "x"


def test_mask_never_shows_full_key():
    key = "sk-abcdefghijklmnopqrstuvwxyz"
    assert key not in demo.mask(key)
    assert demo.mask("short") == "***"


class FakeGateway:
    """Minimal stand-in for the LiteLLM proxy: teams, keys, model ACLs, budgets, fallback."""

    def __init__(self, mock_state, cost_per_call=0.0004):
        self.mock_state = mock_state
        self.teams, self.keys, self.cache = {}, {}, {}
        self.cost = cost_per_call

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        body = json.loads(req.content) if req.content else {}
        if path == "/health/liveliness":
            return httpx.Response(200, json="ok")
        if path == "/team/list":
            return httpx.Response(200, json=[{"team_id": k, "team_alias": v["alias"]} for k, v in self.teams.items()])
        if path == "/team/delete":
            for t in body["team_ids"]:
                self.teams.pop(t)
            return httpx.Response(200, json={})
        if path == "/team/new":
            tid = f"team-{len(self.teams)}"
            self.teams[tid] = {"alias": body["team_alias"], "models": body["models"],
                               "max_budget": body["max_budget"], "spend": 0.0}
            return httpx.Response(200, json={"team_id": tid})
        if path == "/key/generate":
            key = f"sk-virtual-{len(self.keys)}-xxxxxxxx"
            self.keys[key] = body["team_id"]
            return httpx.Response(200, json={"key": key})
        if path == "/v1/chat/completions":
            team = self.teams[self.keys[req.headers["authorization"].split()[1]]]
            model, prompt = body["model"], body["messages"][0]["content"]
            if model not in team["models"]:
                return httpx.Response(401, json={"error": {"message": "team not allowed to access model"}})
            if team["spend"] >= team["max_budget"]:
                return httpx.Response(400, json={"error": {"message": "Budget has been exceeded!"}})
            if prompt in self.cache:
                return httpx.Response(200, json=self.cache[prompt])
            provider = "anthropic" if model == "claude-haiku" or self.mock_state["openai"] else "openai"
            team["spend"] += self.cost
            resp = {"model": model, "choices": [{"message": {"content": f"[{provider} mock / {model}] ok"}}]}
            self.cache[prompt] = resp
            return httpx.Response(200, json=resp)
        return httpx.Response(404)


def fake_mock(state):
    def handler(req):
        state.update(json.loads(req.content))
        return httpx.Response(200, json=state)
    return handler


@pytest.fixture
def clients():
    state = {"openai": False, "anthropic": False}
    gw = FakeGateway(state)
    with httpx.Client(transport=httpx.MockTransport(gw), base_url="http://gw") as g, \
            httpx.Client(transport=httpx.MockTransport(fake_mock(state)), base_url="http://mock") as m:
        yield g, m, gw, state


def test_full_story(clients, monkeypatch):
    g, m, gw, state = clients
    monkeypatch.setattr(demo.time, "sleep", lambda s: None)
    res = demo.GatewayDemo(g, m, "sk-master", log=lambda *a: None).run()

    assert set(res.keys) == {"platform-eng", "support-bot"}
    assert res.denied
    assert res.fallback_served_by.startswith("anthropic")
    # $0.001 budget at $0.0004/call -> 3 calls succeed, 4th is blocked.
    assert res.budget_blocked_after == 3
    # failure switch is always reset, even after the fallback step
    assert state["openai"] is False


def test_rerun_replaces_old_demo_teams(clients, monkeypatch):
    g, m, gw, _ = clients
    monkeypatch.setattr(demo.time, "sleep", lambda s: None)
    quiet = demo.GatewayDemo(g, m, "sk-master", log=lambda *a: None)
    quiet.run()
    quiet.run()
    assert sorted(t["alias"] for t in gw.teams.values()) == ["platform-eng", "support-bot"]


def test_main_requires_master_key(monkeypatch):
    monkeypatch.setattr(demo, "load_dotenv", None)
    monkeypatch.delenv("LITELLM_MASTER_KEY", raising=False)
    assert demo.main([]) == 2
