"""Master ops dashboard for the LLM gateway.

Reads LiteLLM's own Postgres tables (teams, spend logs) plus the gateway's JSONL
request log (which also records failed provider attempts). Read-only.

Run: streamlit run dashboard/app.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd
import psycopg
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import metrics  # noqa: E402

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REQUEST_LOG_PATH = Path(os.environ.get("REQUEST_LOG_PATH", "logs/requests.jsonl"))
REFRESH_S = int(os.environ.get("DASHBOARD_REFRESH_S", "5"))

TEAMS_SQL = 'SELECT team_id, team_alias, spend, max_budget, models FROM "LiteLLM_TeamTable"'
SPEND_SQL = """
SELECT request_id, "startTime" AS start_time, model, model_group, api_base, spend,
       total_tokens, cache_hit, team_id, status,
       metadata->>'user_api_key_alias' AS key_alias
FROM "LiteLLM_SpendLogs"
ORDER BY "startTime" DESC
LIMIT 5000
"""


def query(sql: str) -> pd.DataFrame:
    with psycopg.connect(DATABASE_URL, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [c.name for c in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


st.set_page_config(page_title="LLM Gateway Ops", layout="wide")
st.title("LLM Gateway: ops dashboard")
st.caption("Reconstruction demo. Data: LiteLLM Postgres tables + gateway request log. "
           f"Auto-refreshes every {REFRESH_S}s.")


@st.fragment(run_every=REFRESH_S)
def render() -> None:
    try:
        teams = query(TEAMS_SQL)
        spend = query(SPEND_SQL)
    except Exception as exc:  # DB not up yet, or tables not migrated
        st.warning(f"Database not reachable yet: {exc}")
        return
    for col in ("spend", "max_budget"):
        teams[col] = pd.to_numeric(teams[col], errors="coerce")
    spend["spend"] = pd.to_numeric(spend["spend"], errors="coerce").fillna(0.0)
    log = metrics.load_request_log(REQUEST_LOG_PATH)

    budgets = metrics.team_budgets(teams) if not teams.empty else pd.DataFrame()
    failures = metrics.provider_failure_rates(log)
    hit_rate = metrics.cache_hit_rate(log)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total spend (USD)", f"${spend['spend'].sum():.4f}")
    c2.metric("Requests logged", len(spend))
    c3.metric("Teams over budget", int(budgets["blocked"].sum()) if not budgets.empty else 0)
    c4.metric("Cache hit rate", f"{hit_rate}%" if hit_rate is not None else "n/a")

    left, right = st.columns(2)
    with left:
        st.subheader("Budget used per team")
        if budgets.empty:
            st.info("No teams yet. Run scripts/demo.py.")
        else:
            st.dataframe(
                budgets,
                hide_index=True,
                column_config={
                    "spend": st.column_config.NumberColumn("spend ($)", format="%.5f"),
                    "max_budget": st.column_config.NumberColumn("budget ($)", format="%.4f"),
                    "budget_used_pct": st.column_config.ProgressColumn(
                        "budget used", min_value=0, max_value=100, format="%.0f%%"),
                },
            )
        st.subheader("Spend per team")
        by_team = metrics.spend_by_team(spend, teams)
        if not by_team.empty:
            st.bar_chart(by_team, x="team_alias", y="spend", horizontal=True)
    with right:
        st.subheader("Cost per model")
        by_model = metrics.cost_by_model(spend)
        st.dataframe(by_model, hide_index=True,
                     column_config={"spend": st.column_config.NumberColumn("spend ($)", format="%.5f")})
        st.subheader("Failure rate per provider")
        st.caption("Every upstream attempt, including ones a fallback rescued. Cache hits excluded.")
        st.dataframe(failures, hide_index=True)
        st.subheader("Blocked by the gateway")
        st.caption("Refused before reaching any provider: over budget or model not approved for the team.")
        st.dataframe(metrics.gateway_rejections(log), hide_index=True)

    st.subheader("Recent requests")
    names = dict(zip(teams["team_id"], teams["team_alias"])) if not teams.empty else {}
    recent = spend.head(50).assign(team=lambda d: d["team_id"].map(names),
                                   cache_hit=lambda d: d["cache_hit"].astype(str).str.lower().eq("true"))
    st.dataframe(
        recent[["start_time", "team", "key_alias", "model_group", "model", "status",
                "spend", "total_tokens", "cache_hit"]],
        hide_index=True,
    )
    st.subheader("Recent provider errors")
    errs = log[log["status"] == "failure"].tail(20).iloc[::-1] if not log.empty else log
    st.dataframe(errs[["ts", "team", "model_group", "provider", "error"]] if not errs.empty else errs,
                 hide_index=True)


render()
