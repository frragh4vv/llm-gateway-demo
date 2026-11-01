"""End-to-end demo of the LLM gateway.

What it shows, in order:
  1. Ops creates two teams, each with its own approved models and budget.
  2. Ops issues one virtual key per team (devs never see provider keys).
  3. A team cannot call a model it is not approved for.
  4. Identical requests are served from cache.
  5. A provider outage is absorbed by automatic fallback.
  6. A team that burns its budget gets rejected.
  7. (--real) The same virtual key reaches real free-tier providers, with a real fallback chain.

Usage:  python scripts/demo.py            (reads .env for LITELLM_MASTER_KEY)
        python scripts/demo.py --real     (also call Gemini / Groq / Mistral; needs their keys in .env)
        python scripts/demo.py --keep     (do not delete demo teams from earlier runs)
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx

try:
    from dotenv import load_dotenv
except ImportError:  # optional
    load_dotenv = None


@dataclass(frozen=True)
class TeamSpec:
    alias: str
    models: list[str]
    max_budget: float  # USD
    key_alias: str


# Real free-tier models (gateway/config*.yaml). Fallback chain: gemini-flash -> groq-gpt-oss -> mistral-7b.
REAL_MODELS = ["gemini-flash", "groq-gpt-oss", "mistral-7b"]
RETIRED_MODEL = "gemini-retired"  # always fails upstream; falls back to groq-gpt-oss

TEAMS = [
    TeamSpec("platform-eng", ["gpt-4o-mini", "gpt-4o", "claude-haiku"] + REAL_MODELS + [RETIRED_MODEL],
             1.00, "platform-eng-dev"),
    # Tiny budget so the demo can show a team being blocked after a few calls.
    TeamSpec("support-bot", ["claude-haiku"], 0.001, "support-bot-prod"),
]
DEMO_TAG = "llm-gateway-demo"


def is_budget_error(status_code: int, body: dict | str) -> bool:
    """LiteLLM rejects over-budget keys/teams with a 4xx (422 in 1.106.x) and a budget message."""
    if status_code not in (400, 401, 403, 422, 429):
        return False
    text = body if isinstance(body, str) else str(body.get("error", body))
    return "budget" in text.lower() and "exceed" in text.lower()


def is_model_denied(status_code: int, body: dict | str) -> bool:
    if status_code not in (400, 401, 403):
        return False
    text = (body if isinstance(body, str) else str(body.get("error", body))).lower()
    return "not allowed" in text or "not available for this api key" in text


def served_by(headers: httpx.Headers | dict, body: dict) -> str:
    """Which upstream actually answered. The mock embeds its provider in the reply."""
    content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
    if content.startswith("[") and "]" in content:
        return content[1:content.index("]")]
    return str(body.get("model", "unknown"))


def deployment(r: httpx.Response) -> str:
    """The gateway deployment that answered (model_info.id in the config)."""
    return r.headers.get("x-litellm-model-id") or str(r.json().get("model", "unknown"))


def mask(key: str) -> str:
    return f"{key[:6]}...{key[-4:]}" if len(key) > 12 else "***"


@dataclass
class DemoResult:
    keys: dict[str, str] = field(default_factory=dict)
    denied: bool = False
    cache_speedup: float | None = None
    fallback_served_by: str | None = None
    budget_blocked_after: int | None = None
    real_calls: dict[str, str] = field(default_factory=dict)
    real_fallback_served_by: str | None = None


class GatewayDemo:
    def __init__(self, gateway: httpx.Client, mock: httpx.Client, master_key: str, log=print):
        self.gw = gateway
        self.mock = mock
        self.admin = {"Authorization": f"Bearer {master_key}"}
        self.log = log

    # ---------- ops (master key) ----------
    def wait_until_ready(self, timeout_s: float = 120) -> None:
        deadline = time.time() + timeout_s
        while True:
            try:
                if self.gw.get("/health/liveliness").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise RuntimeError("gateway did not become ready")
            time.sleep(2)

    def delete_old_demo_teams(self) -> None:
        r = self.gw.get("/team/list", headers=self.admin)
        r.raise_for_status()
        aliases = {t.alias for t in TEAMS}
        old = [t["team_id"] for t in r.json() if t.get("team_alias") in aliases]
        if old:
            self.gw.post("/team/delete", headers=self.admin, json={"team_ids": old}).raise_for_status()
            self.log(f"  removed {len(old)} team(s) from a previous run")

    def create_team(self, spec: TeamSpec) -> str:
        r = self.gw.post("/team/new", headers=self.admin, json={
            "team_alias": spec.alias,
            "models": spec.models,
            "max_budget": spec.max_budget,
            "metadata": {"created_by": DEMO_TAG},
        })
        r.raise_for_status()
        return r.json()["team_id"]

    def create_key(self, team_id: str, spec: TeamSpec) -> str:
        r = self.gw.post("/key/generate", headers=self.admin, json={
            "team_id": team_id,
            "key_alias": spec.key_alias,
            "metadata": {"created_by": DEMO_TAG},
        })
        r.raise_for_status()
        return r.json()["key"]

    def set_failure(self, provider: str, enabled: bool) -> None:
        self.mock.post("/admin/failures", json={provider: enabled}).raise_for_status()

    # ---------- dev (virtual key) ----------
    def chat(self, key: str, model: str, prompt: str, **extra) -> httpx.Response:
        return self.gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                            json={"model": model, "messages": [{"role": "user", "content": prompt}], **extra})

    # ---------- the story ----------
    def run_real(self, key: str, res: DemoResult) -> None:
        self.log("\n6) Real providers through the same virtual key (free-tier keys live only on the gateway)")
        question = "In one sentence: why put an LLM gateway in front of provider APIs?"
        for model in REAL_MODELS:
            t0 = time.perf_counter()
            # Gemini 2.5 and gpt-oss spend part of max_tokens on reasoning: keep it low and the
            # budget generous. drop_params strips reasoning_effort for models without it.
            r = self.chat(key, model, f"{question} ({uuid.uuid4().hex[:4]})",
                          max_tokens=800, reasoning_effort="low")
            dt = time.perf_counter() - t0
            if r.status_code != 200:
                res.real_calls[model] = f"HTTP {r.status_code}"
                self.log(f"  {model:<13} -> HTTP {r.status_code} {_short(r)}")
                continue
            body = r.json()
            text = (body["choices"][0]["message"]["content"] or "").strip().replace("\n", " ")
            cost = float(r.headers.get("x-litellm-response-cost") or 0)
            res.real_calls[model] = deployment(r)
            self.log(f"  {model:<13} -> served by {deployment(r)} in {dt:.1f}s, cost ${cost:.6f}")
            self.log(f"                   \"{text[:140]}\"")
        self.log(f"\n7) Real fallback: call {RETIRED_MODEL}, a model Google no longer serves")
        r = self.chat(key, RETIRED_MODEL, f"Say hi in five words. {uuid.uuid4().hex[:4]}", max_tokens=400)
        if r.status_code == 200:
            res.real_fallback_served_by = deployment(r)
            self.log(f"  Gemini returned an error -> answered by {res.real_fallback_served_by}")
        else:
            self.log(f"  -> HTTP {r.status_code} {_short(r)}")

    def run(self, keep: bool = False, real: bool = False) -> DemoResult:
        res = DemoResult()
        self.log("Waiting for gateway...")
        self.wait_until_ready()
        self.set_failure("openai", False)
        self.set_failure("anthropic", False)

        self.log("\n1) Ops creates teams and virtual keys")
        if not keep:
            self.delete_old_demo_teams()
        for spec in TEAMS:
            team_id = self.create_team(spec)
            key = self.create_key(team_id, spec)
            res.keys[spec.alias] = key
            self.log(f"  team {spec.alias:<13} models={spec.models} budget=${spec.max_budget} key={mask(key)}")
        platform, support = res.keys["platform-eng"], res.keys["support-bot"]

        self.log("\n2) Model access: support-bot asks for gpt-4o (not approved)")
        r = self.chat(support, "gpt-4o", "hello")
        res.denied = is_model_denied(r.status_code, _json(r))
        self.log(f"  -> HTTP {r.status_code} {'(denied as expected)' if res.denied else _short(r)}")

        self.log("\n3) Caching: platform-eng sends the same prompt twice")
        # Warm-up call (different prompt) so a cold proxy does not inflate the comparison.
        self.chat(platform, "gpt-4o-mini", f"warm-up {uuid.uuid4().hex[:6]}").raise_for_status()
        prompt = f"Summarise our refund policy in one line. run={uuid.uuid4().hex[:6]}"
        t0 = time.perf_counter(); r1 = self.chat(platform, "gpt-4o-mini", prompt); t1 = time.perf_counter()
        r2 = self.chat(platform, "gpt-4o-mini", prompt); t2 = time.perf_counter()
        r1.raise_for_status(); r2.raise_for_status()
        first, second = t1 - t0, t2 - t1
        res.cache_speedup = round(first / second, 1) if second > 0 else None
        self.log(f"  first {first:.2f}s, second {second:.2f}s (x{res.cache_speedup} faster, served from cache)")

        self.log("\n4) Fallback: OpenAI goes down, platform-eng keeps calling gpt-4o-mini")
        self.set_failure("openai", True)
        try:
            r = self.chat(platform, "gpt-4o-mini", f"Classify this ticket: {uuid.uuid4().hex[:6]}")
            r.raise_for_status()
            res.fallback_served_by = served_by(r.headers, r.json())
            self.log(f"  -> HTTP {r.status_code}, answered by: {res.fallback_served_by}")
        finally:
            self.set_failure("openai", False)

        self.log("\n5) Budget: support-bot sends traffic until its $%.3f budget runs out" % TEAMS[1].max_budget)
        for i in range(1, 41):
            r = self.chat(support, "claude-haiku", f"Draft a reply to ticket #{i} {uuid.uuid4().hex[:6]}")
            if is_budget_error(r.status_code, _json(r)):
                res.budget_blocked_after = i - 1
                self.log(f"  request {i}: HTTP {r.status_code} budget exceeded -> blocked")
                break
            r.raise_for_status()
            self.log(f"  request {i}: ok")
            time.sleep(0.3)  # let the proxy update cached team spend
        else:
            self.log("  budget never tripped (check proxy_batch_write_at / team budget)")

        if real:
            self.run_real(platform, res)

        self.log("\nDone. Open the dashboard to see spend, budgets, failures and recent requests.")
        return res


def _json(r: httpx.Response) -> dict | str:
    try:
        return r.json()
    except ValueError:
        return r.text


def _short(r: httpx.Response) -> str:
    return str(_json(r))[:160]


def main(argv: list[str] | None = None) -> int:
    # Real model replies contain characters a Windows cp1252 console cannot print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if load_dotenv:
        load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gateway", default=os.getenv("GATEWAY_URL", "http://localhost:4000"))
    ap.add_argument("--mock", default=os.getenv("MOCK_ADMIN_URL", "http://localhost:8001"))
    ap.add_argument("--keep", action="store_true", help="keep demo teams from earlier runs")
    ap.add_argument("--real", action="store_true", help="also call the real free-tier providers")
    args = ap.parse_args(argv)
    master_key = os.getenv("LITELLM_MASTER_KEY")
    if not master_key:
        print("LITELLM_MASTER_KEY is not set (copy .env.example to .env)", file=sys.stderr)
        return 2
    with httpx.Client(base_url=args.gateway, timeout=120) as gw, httpx.Client(base_url=args.mock, timeout=10) as mock:
        res = GatewayDemo(gw, mock, master_key).run(keep=args.keep, real=args.real)
    ok = res.denied and res.fallback_served_by and "anthropic" in res.fallback_served_by \
        and res.budget_blocked_after is not None
    print("\nSummary:", "all scenarios passed" if ok else "some scenarios did not behave as expected")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
