"""Pure data shaping for the ops console (no web framework, no DB), so it is testable."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd


def load_request_log(path: Path, limit: int = 5000) -> pd.DataFrame:
    """Read the gateway's JSONL request log; skip partial or corrupt lines."""
    columns = ["ts", "status", "model_group", "deployment", "provider", "team",
               "key_alias", "cost_usd", "total_tokens", "cache_hit", "latency_s", "error"]
    if not path.exists():
        return pd.DataFrame(columns=columns)
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    df = pd.DataFrame(rows[-limit:], columns=columns)
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df


def team_budgets(teams: pd.DataFrame) -> pd.DataFrame:
    """Spend vs budget per team. Expects team_alias, spend, max_budget."""
    out = teams[["team_alias", "spend", "max_budget"]].copy()
    out["spend"] = out["spend"].fillna(0.0).astype(float)
    budget = out["max_budget"].astype(float)
    out["budget_used_pct"] = (out["spend"] / budget * 100).where(budget > 0).round(1)
    out["blocked"] = (budget > 0) & (out["spend"] >= budget)
    return out.sort_values("spend", ascending=False).reset_index(drop=True)


def cost_by_model(spend_logs: pd.DataFrame) -> pd.DataFrame:
    """Requests, tokens and USD per model group, from LiteLLM_SpendLogs rows."""
    if spend_logs.empty:
        return pd.DataFrame(columns=["model_group", "requests", "total_tokens", "spend"])
    df = spend_logs.copy()
    df["model_group"] = df["model_group"].replace("", pd.NA).fillna(df["model"])
    return (
        df.groupby("model_group", dropna=False)
        .agg(requests=("request_id", "count"),
             total_tokens=("total_tokens", "sum"),
             spend=("spend", "sum"))
        .reset_index()
        .sort_values("spend", ascending=False)
        .reset_index(drop=True)
    )


def spend_by_team(spend_logs: pd.DataFrame, teams: pd.DataFrame) -> pd.DataFrame:
    if spend_logs.empty:
        return pd.DataFrame(columns=["team_alias", "requests", "spend"])
    names = dict(zip(teams["team_id"], teams["team_alias"]))
    df = spend_logs.copy()
    # Spend logs outlive teams: re-running the demo deletes and recreates its teams.
    df["team_alias"] = df["team_id"].map(names).where(df["team_id"].isna() | df["team_id"].isin(names),
                                                      "(deleted team)").fillna("(no team)")
    return (
        df.groupby("team_alias")
        .agg(requests=("request_id", "count"), spend=("spend", "sum"))
        .reset_index()
        .sort_values("spend", ascending=False)
        .reset_index(drop=True)
    )


def provider_failure_rates(log: pd.DataFrame) -> pd.DataFrame:
    """Attempts, failures and failure rate per upstream provider.

    Cache hits and gateway rejections (budget, model ACL) never reach a provider,
    so they are excluded.
    """
    cols = ["provider", "attempts", "failures", "failure_rate_pct"]
    if log.empty:
        return pd.DataFrame(columns=cols)
    df = log[~log["cache_hit"].fillna(False).astype(bool) & log["status"].ne("rejected")]
    if df.empty:
        return pd.DataFrame(columns=cols)
    out = (
        df.assign(failed=df["status"].eq("failure"))
        .groupby("provider")
        .agg(attempts=("status", "count"), failures=("failed", "sum"))
        .reset_index()
    )
    out["failure_rate_pct"] = (out["failures"] / out["attempts"] * 100).round(1)
    return out[cols].sort_values("failure_rate_pct", ascending=False).reset_index(drop=True)


def gateway_rejections(log: pd.DataFrame) -> pd.DataFrame:
    """Requests the gateway refused before any provider call, per team and reason."""
    cols = ["team", "reason", "count"]
    if log.empty or not log["status"].eq("rejected").any():
        return pd.DataFrame(columns=cols)
    df = log[log["status"] == "rejected"].copy()
    err = df["error"].fillna("").str.lower()
    df["reason"] = "other"
    df.loc[err.str.contains("budget"), "reason"] = "budget exceeded"
    df.loc[err.str.contains("not available for this api key|not allowed"), "reason"] = "model not approved"
    return (df.groupby(["team", "reason"], dropna=False).size().rename("count")
            .reset_index().sort_values("count", ascending=False).reset_index(drop=True))


def cache_hit_rate(log: pd.DataFrame) -> float | None:
    ok = log[log["status"] == "success"] if not log.empty else log
    if ok.empty:
        return None
    return round(float(ok["cache_hit"].fillna(False).astype(bool).mean() * 100), 1)


SPEND_SQL = """
SELECT request_id, "startTime" AS start_time, model, model_group, model_id, spend,
       total_tokens, cache_hit, team_id, status,
       metadata->>'user_api_key_alias' AS key_alias
FROM "LiteLLM_SpendLogs"
ORDER BY "startTime" DESC
LIMIT 5000
"""


def requests_per_minute(spend_logs: pd.DataFrame, minutes: int = 60, now: pd.Timestamp | None = None) -> list[dict]:
    """Request count per minute for the last `minutes`, zero-filled (for the sparkline)."""
    now = (now or pd.Timestamp.now(tz="UTC")).floor("min")
    idx = pd.date_range(end=now, periods=minutes, freq="min")
    if spend_logs.empty:
        counts = pd.Series(0, index=idx)
    else:
        ts = pd.to_datetime(spend_logs["start_time"], utc=True, errors="coerce").dt.floor("min")
        counts = ts.value_counts().reindex(idx, fill_value=0)
    return [{"t": t.isoformat(), "n": int(n)} for t, n in counts.items()]


def records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe list of dicts (NaN/NaT -> None, timestamps -> ISO)."""
    if df.empty:
        return []
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            # LiteLLM stores naive UTC timestamps; tag them so browsers do not read them as local time.
            if out[col].dt.tz is None:
                out[col] = out[col].dt.tz_localize("UTC")
            out[col] = out[col].map(lambda v: v.isoformat() if pd.notna(v) else None)
    out = out.astype(object).where(pd.notna(out), None)
    return out.to_dict("records")


def overview_payload(teams: pd.DataFrame, spend: pd.DataFrame, log: pd.DataFrame) -> dict:
    """Everything the console's overview page shows, in one JSON object."""
    teams = teams.copy()
    for col in ("spend", "max_budget"):
        teams[col] = pd.to_numeric(teams[col], errors="coerce")
    spend = spend.copy()
    spend["spend"] = pd.to_numeric(spend["spend"], errors="coerce").fillna(0.0)
    budgets = team_budgets(teams) if not teams.empty else pd.DataFrame()
    names = dict(zip(teams["team_id"], teams["team_alias"])) if not teams.empty else {}
    recent = spend.head(40).assign(
        team=lambda d: d["team_id"].map(names),
        cache_hit=lambda d: d["cache_hit"].astype(str).str.lower().eq("true"),
    )
    errors = log[log["status"] != "success"].tail(15).iloc[::-1] if not log.empty else log
    upstream = log[log["status"].isin(["success", "failure"])] if not log.empty else log
    return {
        "kpis": {
            "total_spend": round(float(spend["spend"].sum()), 6),
            "requests": int(len(spend)),
            "teams": int(len(teams)),
            "teams_over_budget": int(budgets["blocked"].sum()) if not budgets.empty else 0,
            "cache_hit_rate": cache_hit_rate(log),
            "upstream_failure_rate": round(float(upstream["status"].eq("failure").mean() * 100), 1)
            if not upstream.empty else None,
            "blocked_requests": int(log["status"].eq("rejected").sum()) if not log.empty else 0,
        },
        "budgets": records(budgets),
        "by_model": records(cost_by_model(spend)),
        "by_team": records(spend_by_team(spend, teams)),
        "providers": records(provider_failure_rates(log)),
        "rejections": records(gateway_rejections(log)),
        "timeline": requests_per_minute(spend),
        "recent": records(recent[["start_time", "team", "key_alias", "model_group", "model_id",
                                  "status", "spend", "total_tokens", "cache_hit"]]),
        "errors": records(errors[["ts", "team", "model_group", "provider", "status", "error"]])
        if not errors.empty else [],
    }
