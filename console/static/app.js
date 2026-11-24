// Gateway Ops console. Vanilla JS, no build step. Every call goes to the console
// server (/api/*), which holds the master key; this page never sees it after login.
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const usd = (v, d = 4) => (v == null ? "—" : "$" + Number(v).toFixed(d));
const num = (v) => (v == null ? "—" : Number(v).toLocaleString());
const when = (iso) => {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
};

// ---------- API ----------
class ApiError extends Error {}
async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: "same-origin",
    headers: { "content-type": "application/json", "x-console": "1" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401 && path !== "/login") { showLogin(); throw new ApiError("signed out"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const d = data?.detail;
    throw new ApiError(typeof d === "string" ? d : Array.isArray(d) ? d.map((x) => `${x.loc?.at(-1)}: ${x.msg}`).join("; ") : `HTTP ${res.status}`);
  }
  return data;
}

function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toasts").append(el);
  setTimeout(() => el.remove(), kind === "err" ? 6000 : 3000);
}

// ---------- drawer ----------
function openDrawer(title, html, onMount) {
  $("#drawer-title").textContent = title;
  $("#drawer-body").innerHTML = html;
  $("#scrim").hidden = false;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  onMount?.($("#drawer-body"));
  setTimeout(() => $("#drawer-body input, #drawer-body select, #drawer-body textarea")?.focus(), 250);
}
function closeDrawer() {
  $("#scrim").hidden = true;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
}
$("#drawer-close").onclick = closeDrawer;
$("#scrim").onclick = closeDrawer;
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

function formData(form) {
  const out = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") { (out[el.name] ||= []); if (el.checked) out[el.name].push(el.value); continue; }
    if (el.type === "number") { out[el.name] = el.value === "" ? null : Number(el.value); continue; }
    out[el.name] = el.value.trim() === "" ? null : el.value.trim();
  }
  return out;
}

async function submitting(form, fn) {
  const btn = form.querySelector("[type=submit]");
  const err = form.querySelector(".form-error");
  btn.disabled = true; if (err) err.textContent = "";
  try { await fn(); } catch (e) { if (err) err.textContent = e.message; else toast(e.message, "err"); }
  finally { btn.disabled = false; }
}

const modelChecks = (models, selected = []) =>
  models.length
    ? `<div class="checks">${[...new Set(models)].map((m) => `
        <label class="check"><input type="checkbox" name="models" value="${esc(m)}" ${selected.includes(m) ? "checked" : ""}><span>${esc(m)}</span></label>`).join("")}</div>`
    : `<small>No models yet. Add one under Providers &amp; models.</small>`;

const chips = (list, empty = "all models") =>
  list?.length ? `<div class="chips">${list.map((m) => `<span class="chip">${esc(m)}</span>`).join("")}</div>` : `<span class="chip">${empty}</span>`;

// ---------- charts ----------
// Requests per minute, last hour: one series, faint dashed grid, round ticks above the max.
function lineChart(points, width = 720) {
  // viewBox matches the drawn width so axis text is never stretched
  const w = Math.max(280, Math.round(width)), h = 272, padL = 34, padB = 22, padT = 8;
  const peak = Math.max(0, ...points.map((p) => p.n));
  // four whole-number gridlines above the peak: step from 1, 2, 3, 5, 10, 20, 25, 50, ...
  const nice = [1, 2, 3, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000];
  const step = nice.find((s) => s * 4 >= peak) ?? Math.ceil(peak / 4000) * 1000;
  const top = step * 4;
  const ticks = []; for (let v = 0; v <= top + 1e-9; v += top / 4) ticks.push(v);
  const iw = w - padL, ih = h - padB - padT;
  const x = (i) => padL + (i / Math.max(1, points.length - 1)) * iw;
  const y = (n) => padT + ih - (n / top) * ih;
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.n).toFixed(1)}`).join("");
  const lbl = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const xt = [0, 15, 30, 45, points.length - 1].filter((i) => points[i]);
  const last = points.at(-1);
  return `<svg class="chart" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" role="img" aria-label="Requests per minute over the last hour, peak ${peak}">
    ${ticks.map((v) => `<line class="grid-line" x1="${padL}" x2="${w}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${padL - 8}" y="${y(v) + 4}" text-anchor="end">${Math.round(v)}</text>`).join("")}
    ${xt.map((i) => `<text class="axis" x="${x(i)}" y="${h - 4}" text-anchor="${i === 0 ? "start" : i === points.length - 1 ? "end" : "middle"}">${i === points.length - 1 ? "now" : lbl(points[i].t)}</text>`).join("")}
    <path class="area" d="${line}L${x(points.length - 1)},${y(0)}L${padL},${y(0)}Z"/>
    <path class="series" d="${line}"/>
    ${last ? `<circle class="now" cx="${x(points.length - 1)}" cy="${y(last.n)}" r="3"/>` : ""}
  </svg>`;
}

const chartWidth = () => {
  const view = $("#view").clientWidth || 900;
  const wide = matchMedia("(min-width: 1201px)").matches;
  const pad = innerWidth <= 720 ? 34 : 46;
  return (wide ? (view - 24) * 8 / 12 : view) - pad;
};

function meter(pct) {
  if (pct == null) return `<div class="meter none" role="img" aria-label="No budget limit"><i></i></div>`;
  const w = Math.min(100, Math.max(0, pct));
  return `<div class="meter ${pct >= 90 ? "hot" : ""}" role="img" aria-label="${pct}% of budget used"><i style="width:${w.toFixed(1)}%"></i></div>`;
}

function bars(rows, label, value, fmt) {
  if (!rows.length) return `<div class="empty"><b>No traffic yet</b>Run scripts/demo.py or use the Playground.</div>`;
  const max = Math.max(...rows.map((r) => Number(r[value]) || 0), 1e-9);
  return rows.map((r) => `
    <div class="bar-row"><span class="name" title="${esc(r[label])}">${esc(r[label])}</span>
      <div class="bar"><i style="width:${(((Number(r[value]) || 0) / max) * 100).toFixed(1)}%"></i></div>
      <span class="val">${fmt(r)}</span></div>`).join("");
}

// ---------- pages ----------
const pages = {};
let refreshTimer = null;

pages.overview = {
  sub: "Live traffic through the gateway · refreshes every 5s",
  title: "Overview",
  async render(view) {
    const [o, status] = await Promise.all([api("/overview"), api("/status")]);
    const k = o.kpis;
    const totalReqs = o.timeline.reduce((a, p) => a + p.n, 0);
    const peak = Math.max(0, ...o.timeline.map((p) => p.n));
    const pct = (v) => (v == null ? "—" : `${v}<small>%</small>`);
    view.innerHTML = `
      <section class="kpis" aria-label="Key figures">
        <div class="kpi lead"><div class="label">Total spend</div><div class="value">${k.total_spend != null ? "<small>$</small>" + Number(k.total_spend).toFixed(4) : "—"}</div><div class="note">all teams, all time</div></div>
        <div class="kpi"><div class="label">Requests</div><div class="value">${num(k.requests)}</div><div class="note">${num(totalReqs)} in the last hour</div></div>
        <div class="kpi"><div class="label">Cache hit rate</div><div class="value">${pct(k.cache_hit_rate)}</div><div class="note">paid once, served again</div></div>
        <div class="kpi"><div class="label">Upstream failures</div><div class="value ${k.upstream_failure_rate > 10 ? "warn" : ""}">${pct(k.upstream_failure_rate)}</div><div class="note">of provider attempts</div></div>
        <div class="kpi"><div class="label">Blocked by gateway</div><div class="value">${num(k.blocked_requests)}</div><div class="note">budget or model access</div></div>
        <div class="kpi"><div class="label">Teams over budget</div><div class="value ${k.teams_over_budget ? "warn" : ""}">${k.teams_over_budget ?? "—"}<small> / ${k.teams ?? 0}</small></div><div class="note">${o.by_model.length} models routed</div></div>
      </section>

      <div class="grid g-12">
        <section class="panel span-8">
          <div class="panel-head"><h3>Requests per minute</h3><span class="hint">last 60 min · peak <span class="fig">${peak}</span></span></div>
          <div class="chart-box" id="rpm">${lineChart(o.timeline, chartWidth())}</div>
        </section>
        <section class="panel span-4">
          <div class="panel-head"><h3>Chaos switches</h3><span class="hint">take a mock provider down</span></div>
          ${status.mock ? `<div class="switches">${Object.entries(status.mock).map(([p, on]) => `
            <div class="switch-row"><div><div class="t">${esc(p)} is ${on ? "down" : "up"}</div>
              <div class="s">${on ? "every call returns 503, gateway falls back" : "flip to simulate an outage"}</div></div>
              <label class="toggle"><input type="checkbox" data-mock="${esc(p)}" ${on ? "checked" : ""} aria-label="Force ${esc(p)} failure"><span></span></label>
            </div>`).join("")}</div>` : `<div class="empty"><b>Mock provider offline</b>Start it to use the outage switches.</div>`}
          <div class="panel-head section-gap"><h3>Provider health</h3></div>
          ${o.providers.length ? `<div class="providers">${o.providers.map((p) => {
            const cls = p.failure_rate_pct >= 50 ? "down" : p.failure_rate_pct > 0 ? "warn" : "";
            return `<div class="prov ${cls}"><div class="top"><span class="dot"></span>${esc(p.provider)}</div>
              <div class="rate">${p.failure_rate_pct}<small class="muted">%</small></div><span class="sub">${num(p.failures)} failed of ${num(p.attempts)}</span></div>`;
          }).join("")}</div>` : `<div class="empty"><b>No upstream calls logged yet</b>Send a prompt from the Playground.</div>`}
        </section>

        <section class="panel span-12">
          <div class="panel-head"><h3>Budget used per team</h3><span class="hint">hard limit · requests are rejected at 100%</span></div>
          ${o.budgets.length ? o.budgets.map((b) => `
            <div class="meter-row">
              <span class="name">${esc(b.team_alias)}</span>
              ${meter(b.budget_used_pct)}
              <span class="figs"><b>${usd(b.spend, 5)}</b> of ${b.max_budget ? usd(b.max_budget, 3) : "no limit"}${b.budget_used_pct != null ? ` · ${b.budget_used_pct}%` : ""}${b.blocked ? `<span class="pill bad">blocked</span>` : ""}</span>
            </div>`).join("") : `<div class="empty"><b>No teams yet</b>Create one under Teams.</div>`}
        </section>
        <section class="panel span-6">
          <div class="panel-head"><h3>Cost per model</h3><span class="hint">USD · requests</span></div>
          ${bars(o.by_model, "model_group", "spend", (r) => `<b>${usd(r.spend, 5)}</b> · ${num(r.requests)} req`)}
        </section>
        <section class="panel span-6">
          <div class="panel-head"><h3>Spend per team</h3><span class="hint">includes deleted teams</span></div>
          ${bars(o.by_team, "team_alias", "spend", (r) => `<b>${usd(r.spend, 5)}</b> · ${num(r.requests)} req`)}
        </section>

        <section class="panel span-8">
          <div class="panel-head"><h3>Recent requests</h3><a class="hint" href="#/logs">View full log</a></div>
          <div class="table-wrap"><table>
            <thead><tr><th>When</th><th>Team / key</th><th>Model</th><th>Status</th><th class="num">Tokens</th><th class="num">Cost</th></tr></thead>
            <tbody>${o.recent.slice(0, 5).map((r) => `<tr>
              <td class="mono">${when(r.start_time)}</td>
              <td>${esc(r.team ?? "—")}<span class="sub">${esc(r.key_alias ?? "")}</span></td>
              <td class="mono">${esc(r.model_group)}<span class="sub">${esc(r.model_id ?? "")}</span></td>
              <td>${r.cache_hit ? `<span class="pill info">cache</span>` : r.status === "success" ? `<span class="pill ok">ok</span>` : `<span class="pill bad">${esc(r.status)}</span>`}</td>
              <td class="num">${num(r.total_tokens)}</td><td class="num">${usd(r.spend, 6)}</td></tr>`).join("")}</tbody>
          </table></div>
        </section>
        <section class="panel span-4 stack">
          <div class="panel-head"><h3>Blocked &amp; failing</h3><span class="hint">refusals and provider errors</span></div>
          ${o.rejections.map((r) => `<div class="switch-row"><div><div class="t">${esc(r.team ?? "unknown")}</div><div class="s">${esc(r.reason)}</div></div><span class="pill warn">${num(r.count)}×</span></div>`).join("")}
          ${o.errors.slice(0, 6).map((e) => `<div class="switch-row"><div><div class="t">${esc(e.provider)} · ${esc(e.model_group)}</div><div class="s">${esc((e.error || "").slice(0, 90))}</div></div><span class="pill ${e.status === "rejected" ? "warn" : "bad"}">${esc(e.status)}</span></div>`).join("") || `<div class="empty"><b>All quiet</b>No errors logged.</div>`}
          <div class="panel-foot"><a class="btn btn-sm" href="#/logs">Open request log</a></div>
        </section>
      </div>`;

    view.querySelectorAll("[data-mock]").forEach((el) => {
      el.onchange = async () => {
        try {
          await api("/mock/failures", { method: "POST", body: { [el.dataset.mock]: el.checked } });
          toast(`${el.dataset.mock} mock is now ${el.checked ? "DOWN" : "up"}`);
          render();
        } catch (e) { toast(e.message, "err"); el.checked = !el.checked; }
      };
    });
  },
  refresh: 5000,
};

pages.teams = {
  sub: "Who can call which models, and how much they may spend",
  title: "Teams",
  actions: `<button class="btn btn-primary" data-act="new-team">New team</button>`,
  async render(view) {
    const [teams, models] = await Promise.all([api("/teams"), api("/models")]);
    const modelNames = models.map((m) => m.model_name);
    view.innerHTML = `<section class="panel">${teams.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Team</th><th>Approved models</th><th>Budget used</th><th class="num">Keys</th><th>Status</th><th></th></tr></thead>
      <tbody>${teams.map((t) => {
        const pct = t.max_budget ? Math.round((t.spend / t.max_budget) * 100) : null;
        return `<tr>
          <td><b>${esc(t.team_alias)}</b><span class="sub">${esc(t.team_id)}</span></td>
          <td>${chips(t.models)}</td>
          <td style="min-width:200px">${meter(pct)}<span class="sub">${usd(t.spend, 5)} of ${t.max_budget != null ? usd(t.max_budget, 3) : "no limit"}${pct != null ? ` · ${pct}%` : ""}</span></td>
          <td class="num">${t.keys}</td>
          <td>${t.blocked ? `<span class="pill bad">blocked</span>` : pct != null && pct >= 100 ? `<span class="pill warn">over budget</span>` : `<span class="pill ok">active</span>`}</td>
          <td><div class="row-actions">
            <button class="btn btn-sm" data-edit="${esc(t.team_id)}">Edit</button>
            <button class="btn btn-sm" data-block="${esc(t.team_id)}" data-blocked="${t.blocked ? 1 : 0}">${t.blocked ? "Unblock" : "Block"}</button>
            <button class="btn btn-sm btn-danger" data-del="${esc(t.team_id)}" data-name="${esc(t.team_alias)}">Delete</button>
          </div></td></tr>`;
      }).join("")}</tbody></table></div>` : `<div class="empty"><b>No teams yet</b>Create a team, approve its models and set a budget.</div>`}</section>`;

    const teamForm = (t = {}) => `<form id="team-form">
      <label class="field"><span>Team name</span><input type="text" name="team_alias" required maxlength="64" value="${esc(t.team_alias ?? "")}" placeholder="e.g. search-squad"></label>
      <div class="field"><span>Approved models</span>${modelChecks(modelNames, t.models ?? [])}<small>None selected = every model.</small></div>
      <label class="field"><span>Budget (USD)</span><input type="number" name="max_budget" min="0" step="0.0001" value="${t.max_budget ?? ""}" placeholder="no limit"><small>Requests are rejected once spend reaches this.</small></label>
      <div class="form-actions"><button class="btn btn-primary" type="submit">${t.team_id ? "Save changes" : "Create team"}</button></div>
      <p class="form-error"></p></form>`;

    const bindForm = (t) => (body) => {
      const form = $("#team-form", body);
      form.onsubmit = (e) => { e.preventDefault(); submitting(form, async () => {
        const data = formData(form);
        if (t) await api(`/teams/${encodeURIComponent(t.team_id)}`, { method: "PATCH", body: data });
        else await api("/teams", { method: "POST", body: data });
        closeDrawer(); toast(t ? "Team updated" : `Team ${data.team_alias} created`); render();
      }); };
    };

    $("[data-act=new-team]").onclick = () => openDrawer("New team", teamForm(), bindForm(null));
    view.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
      const t = teams.find((x) => x.team_id === b.dataset.edit);
      openDrawer(`Edit ${t.team_alias}`, teamForm(t), bindForm(t));
    });
    view.querySelectorAll("[data-block]").forEach((b) => b.onclick = async () => {
      const act = b.dataset.blocked === "1" ? "unblock" : "block";
      try { await api(`/teams/${encodeURIComponent(b.dataset.block)}/${act}`, { method: "POST" }); toast(`Team ${act}ed`); render(); }
      catch (e) { toast(e.message, "err"); }
    });
    view.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Delete team "${b.dataset.name}" and all its keys? Apps using those keys stop working.`)) return;
      try { await api(`/teams/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast("Team deleted"); render(); }
      catch (e) { toast(e.message, "err"); }
    });
  },
};

pages.keys = {
  sub: "What dev teams get instead of raw provider keys",
  title: "Virtual keys",
  actions: `<button class="btn btn-primary" data-act="new-key">Generate key</button>`,
  async render(view) {
    const [keys, teams, models] = await Promise.all([api("/keys"), api("/teams"), api("/models")]);
    view.innerHTML = `<section class="panel">${keys.length ? `<div class="table-wrap"><table>
      <thead><tr><th>Key</th><th>Team</th><th>Models</th><th class="num">Spend</th><th>Last used</th><th>Status</th><th></th></tr></thead>
      <tbody>${keys.map((k) => `<tr>
        <td><b>${esc(k.key_alias ?? "unnamed")}</b><span class="sub">${esc(k.key_name ?? "")}</span></td>
        <td>${esc(k.team_alias ?? "—")}</td>
        <td>${chips(k.models, "team's models")}</td>
        <td class="num">${usd(k.spend, 5)}${k.max_budget != null ? `<span class="sub">of ${usd(k.max_budget, 3)}</span>` : ""}</td>
        <td class="mono">${when(k.last_active)}${k.expires ? `<span class="sub">expires ${new Date(k.expires).toLocaleDateString()}</span>` : ""}</td>
        <td>${k.blocked ? `<span class="pill bad">blocked</span>` : `<span class="pill ok">active</span>`}</td>
        <td><div class="row-actions">
          <button class="btn btn-sm" data-edit="${esc(k.token)}">Edit</button>
          <button class="btn btn-sm" data-block="${esc(k.token)}" data-blocked="${k.blocked ? 1 : 0}">${k.blocked ? "Unblock" : "Block"}</button>
          <button class="btn btn-sm btn-danger" data-del="${esc(k.token)}" data-name="${esc(k.key_alias ?? "")}">Revoke</button>
        </div></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="empty"><b>No keys yet</b>Generate one per team or app. Devs use it instead of a provider key.</div>`}</section>`;

    const teamOpts = teams.map((t) => `<option value="${esc(t.team_id)}">${esc(t.team_alias)}</option>`).join("");
    $("[data-act=new-key]").onclick = () => openDrawer("Generate key", `<form id="key-form">
        <label class="field"><span>Key name</span><input type="text" name="key_alias" required maxlength="64" placeholder="e.g. search-squad-prod"></label>
        <label class="field"><span>Team</span><select name="team_id"><option value="">No team</option>${teamOpts}</select><small>The key inherits the team's models and budget.</small></label>
        <div class="field"><span>Restrict to models (optional)</span>${modelChecks(models.map((m) => m.model_name))}</div>
        <div class="row2">
          <label class="field"><span>Key budget (USD)</span><input type="number" name="max_budget" min="0" step="0.0001" placeholder="no limit"></label>
          <label class="field"><span>Expires after</span><select name="duration"><option value="">never</option><option>1d</option><option>7d</option><option>30d</option><option>90d</option></select></label>
        </div>
        <div class="form-actions"><button class="btn btn-primary" type="submit">Generate</button></div>
        <p class="form-error"></p></form>`, (body) => {
      const form = $("#key-form", body);
      form.onsubmit = (e) => { e.preventDefault(); submitting(form, async () => {
        const created = await api("/keys", { method: "POST", body: formData(form) });
        body.innerHTML = `<div class="secret"><p><b style="color:var(--text)">Copy this key now.</b> It is shown once and is not stored by the console.</p>
          <code id="new-key">${esc(created.key)}</code>
          <div class="form-actions"><button class="btn btn-primary" id="copy-key">Copy key</button><button class="btn" id="done-key">Done</button></div></div>`;
        $("#copy-key", body).onclick = async () => { await navigator.clipboard.writeText(created.key); toast("Copied"); };
        $("#done-key", body).onclick = () => { closeDrawer(); render(); };
      }); };
    });

    view.querySelectorAll("[data-edit]").forEach((b) => b.onclick = () => {
      const k = keys.find((x) => x.token === b.dataset.edit);
      openDrawer(`Edit ${k.key_alias ?? "key"}`, `<form id="key-edit">
        <label class="field"><span>Key name</span><input type="text" name="key_alias" maxlength="64" value="${esc(k.key_alias ?? "")}"></label>
        <div class="field"><span>Restrict to models</span>${modelChecks(models.map((m) => m.model_name), k.models ?? [])}</div>
        <label class="field"><span>Key budget (USD)</span><input type="number" name="max_budget" min="0" step="0.0001" value="${k.max_budget ?? ""}"></label>
        <div class="form-actions"><button class="btn btn-primary" type="submit">Save</button></div><p class="form-error"></p></form>`, (body) => {
        const form = $("#key-edit", body);
        form.onsubmit = (e) => { e.preventDefault(); submitting(form, async () => {
          await api(`/keys/${encodeURIComponent(k.token)}`, { method: "PATCH", body: formData(form) });
          closeDrawer(); toast("Key updated"); render();
        }); };
      });
    });
    view.querySelectorAll("[data-block]").forEach((b) => b.onclick = async () => {
      const act = b.dataset.blocked === "1" ? "unblock" : "block";
      try { await api(`/keys/${encodeURIComponent(b.dataset.block)}/${act}`, { method: "POST" }); toast(`Key ${act}ed`); render(); }
      catch (e) { toast(e.message, "err"); }
    });
    view.querySelectorAll("[data-del]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Revoke key "${b.dataset.name}"? Apps using it stop working immediately.`)) return;
      try { await api(`/keys/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" }); toast("Key revoked"); render(); }
      catch (e) { toast(e.message, "err"); }
    });
  },
};

pages.providers = {
  sub: "Provider keys stay here, encrypted. Devs never see them.",
  title: "Providers & models",
  actions: `<button class="btn" data-act="new-cred">Store provider key</button><button class="btn btn-primary" data-act="new-model">Add model</button>`,
  async render(view) {
    const [creds, models, providers] = await Promise.all([api("/credentials"), api("/models"), api("/providers")]);
    view.innerHTML = `<div class="grid g-12">
      <section class="panel span-4">
        <div class="panel-head"><h3>Stored provider keys</h3><span class="hint">encrypted at rest</span></div>
        ${creds.length ? creds.map((c) => `<div class="switch-row" style="margin-bottom:8px">
            <div><div class="t">${esc(c.credential_name)}</div><div class="s">${esc(providers[c.provider] ?? c.provider)} · value hidden</div></div>
            <button class="btn btn-sm btn-danger" data-delcred="${esc(c.credential_name)}">Delete</button></div>`).join("")
          : `<div class="empty"><b>None stored</b>Keys from <span class="mono">.env</span> are used by config models. Store more here.</div>`}
      </section>
      <section class="panel span-8">
        <div class="panel-head"><h3>Model routes</h3><span class="hint">name devs call → provider model</span></div>
        <div class="table-wrap"><table>
          <thead><tr><th>Name</th><th>Provider model</th><th>Provider</th><th class="num">$ / 1M in · out</th><th></th></tr></thead>
          <tbody>${models.map((m) => `<tr>
            <td><b class="mono">${esc(m.model_name)}</b><span class="sub">${esc(m.id ?? "")}</span></td>
            <td class="mono">${esc(m.provider_model)}</td>
            <td>${esc(m.provider)}${m.credential ? `<span class="sub">key: ${esc(m.credential)}</span>` : ""}</td>
            <td class="num">${m.input_cost_per_m != null ? `${m.input_cost_per_m} · ${m.output_cost_per_m}` : "—"}</td>
            <td><div class="row-actions">${m.editable ? `<button class="btn btn-sm btn-danger" data-delmodel="${esc(m.id)}" data-name="${esc(m.model_name)}">Remove</button>` : `<span class="lock" title="Defined in gateway/config.yaml">config</span>`}</div></td>
          </tr>`).join("")}</tbody></table></div>
      </section></div>`;

    const provOpts = Object.entries(providers).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
    $("[data-act=new-cred]").onclick = () => openDrawer("Store provider key", `<form id="cred-form">
        <label class="field"><span>Provider</span><select name="provider">${provOpts}</select></label>
        <label class="field"><span>Name</span><input type="text" name="credential_name" required pattern="[A-Za-z0-9_.\\-]+" placeholder="e.g. openai-prod"></label>
        <label class="field"><span>API key</span><input type="password" name="api_key" required minlength="8" autocomplete="off" placeholder="paste the provider key"><small>Sent once to the gateway and encrypted with LITELLM_SALT_KEY. Never shown again.</small></label>
        <label class="field"><span>API base (optional)</span><input type="text" name="api_base" placeholder="only for Azure / self-hosted"></label>
        <div class="form-actions"><button class="btn btn-primary" type="submit">Store key</button></div><p class="form-error"></p></form>`, (body) => {
      const form = $("#cred-form", body);
      form.onsubmit = (e) => { e.preventDefault(); submitting(form, async () => {
        await api("/credentials", { method: "POST", body: formData(form) });
        closeDrawer(); toast("Provider key stored"); render();
      }); };
    });
    $("[data-act=new-model]").onclick = () => {
      if (!creds.length) { toast("Store a provider key first", "err"); return; }
      openDrawer("Add model", `<form id="model-form">
        <label class="field"><span>Name dev teams will call</span><input type="text" name="model_name" required placeholder="e.g. fast-chat"></label>
        <label class="field"><span>Provider key</span><select name="credential_name">${creds.map((c) => `<option value="${esc(c.credential_name)}">${esc(c.credential_name)} (${esc(providers[c.provider] ?? c.provider)})</option>`).join("")}</select></label>
        <label class="field"><span>Provider model ID</span><input type="text" name="provider_model" required placeholder="e.g. gpt-4o-mini, gemini-2.5-flash"></label>
        <div class="form-actions"><button class="btn btn-primary" type="submit">Add route</button></div><p class="form-error"></p></form>`, (body) => {
        const form = $("#model-form", body);
        form.onsubmit = (e) => { e.preventDefault(); submitting(form, async () => {
          await api("/models", { method: "POST", body: formData(form) });
          closeDrawer(); toast("Model added. Approve it for a team to use it."); render();
        }); };
      });
    };
    view.querySelectorAll("[data-delcred]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Delete stored key "${b.dataset.delcred}"? Models using it will fail.`)) return;
      try { await api(`/credentials/${encodeURIComponent(b.dataset.delcred)}`, { method: "DELETE" }); toast("Provider key deleted"); render(); }
      catch (e) { toast(e.message, "err"); }
    });
    view.querySelectorAll("[data-delmodel]").forEach((b) => b.onclick = async () => {
      if (!confirm(`Remove model route "${b.dataset.name}"?`)) return;
      try { await api(`/models/${encodeURIComponent(b.dataset.delmodel)}`, { method: "DELETE" }); toast("Model removed"); render(); }
      catch (e) { toast(e.message, "err"); }
    });
  },
};

pages.playground = {
  sub: "Call the gateway the way a dev team does",
  title: "Playground",
  async render(view) {
    const [models, teams] = await Promise.all([api("/models"), api("/teams")]);
    const names = [...new Set(models.map((m) => m.model_name))];
    const teamOpts = teams.map((t, i) => `<option value="${esc(t.team_id)}" ${i === 0 ? "selected" : ""}>${esc(t.team_alias)}</option>`).join("");
    view.innerHTML = `<div class="play">
      <form class="panel" id="play-form">
        <label class="field"><span>Virtual key</span><input type="password" name="key" required autocomplete="off" value="123" placeholder="123 or a generated sk- key" aria-describedby="play-key-hint"><small id="play-key-hint">Demo: <code>123</code> mints a 1-hour key for the team below. Paste a real virtual key to use that instead.</small></label>
        <label class="field"><span>Team for the demo key</span><select name="team_id"><option value="">No team</option>${teamOpts}</select><small>That team's model access, budget and cache apply.</small></label>
        <label class="field"><span>Model</span><select name="model">${names.map((n) => `<option>${esc(n)}</option>`).join("")}</select></label>
        <label class="field"><span>Prompt</span><textarea name="prompt" required>Explain in two sentences why a company would put a gateway in front of LLM APIs.</textarea></label>
        <div class="form-actions"><button class="btn btn-primary" type="submit">Send through gateway</button></div>
        <p class="form-error"></p>
      </form>
      <section class="panel">
        <div class="panel-head"><h3>Response</h3><span class="hint" id="play-status"></span></div>
        <div class="meta-strip" id="play-meta"></div>
        <div class="answer placeholder" id="play-answer">Send a prompt to see which deployment answered, what it cost and whether it came from cache or a fallback.</div>
      </section></div>`;
    const form = $("#play-form");
    const saved = sessionStorage.getItem("play-key");
    if (saved) form.key.value = saved;
    const savedTeam = sessionStorage.getItem("play-team");
    if (savedTeam && [...form.team_id.options].some((o) => o.value === savedTeam)) form.team_id.value = savedTeam;
    form.onsubmit = (e) => { e.preventDefault(); submitting(form, async () => {
      const body = formData(form);
      sessionStorage.setItem("play-key", body.key);
      sessionStorage.setItem("play-team", body.team_id ?? "");
      const ans = $("#play-answer");
      ans.className = "answer typing"; ans.textContent = ""; $("#play-meta").innerHTML = ""; $("#play-status").textContent = "waiting…";
      const r = await api("/playground", { method: "POST", body });
      $("#play-status").textContent = `HTTP ${r.status} · ${r.latency_s}s`;
      if (!r.ok) { ans.className = "answer"; ans.innerHTML = `<span class="pill bad">rejected</span><p>${esc(r.error)}</p>`; return; }
      ans.className = "answer"; ans.textContent = r.content;
      $("#play-meta").innerHTML = [
        `<span class="chip">served by <b>${esc(r.served_by ?? "?")}</b></span>`,
        `<span class="chip">cost <b>${usd(r.cost_usd, 6)}</b></span>`,
        `<span class="chip">tokens <b>${num(r.usage?.total_tokens)}</b></span>`,
        r.cache_hit ? `<span class="pill info">cache hit</span>` : "",
        r.attempted_fallbacks ? `<span class="pill warn">fallback ×${r.attempted_fallbacks}</span>` : "",
      ].join("");
    }); };
  },
};

pages.logs = {
  sub: "Every upstream attempt and gateway refusal. Prompts are never logged.",
  title: "Request log",
  filter: "all",
  async render(view) {
    const rows = await api("/logs?limit=300");
    const f = pages.logs.filter;
    const shown = f === "all" ? rows : rows.filter((r) => r.status === f);
    const count = (s) => rows.filter((r) => r.status === s).length;
    view.innerHTML = `<section class="panel">
      <div class="panel-head"><div class="tabs">${["all", "success", "failure", "rejected"].map((t) => `<button data-f="${t}" class="${t === f ? "active" : ""}">${t}<span class="fig muted">${t === "all" ? rows.length : count(t)}</span></button>`).join("")}</div>
      <span class="hint">showing <span class="fig">${shown.length}</span> of <span class="fig">${rows.length}</span></span></div>
      ${shown.length ? `<div class="table-wrap"><table>
        <thead><tr><th>When</th><th>Status</th><th>Team / key</th><th>Model → provider</th><th class="num">Latency</th><th class="num">Cost</th><th>Detail</th></tr></thead>
        <tbody>${shown.map((r) => `<tr>
          <td class="mono">${when(r.ts)}</td>
          <td><span class="pill ${r.status === "success" ? (r.cache_hit ? "info" : "ok") : r.status === "rejected" ? "warn" : "bad"}">${r.cache_hit ? "cache" : esc(r.status)}</span></td>
          <td>${esc(r.team ?? "—")}<span class="sub">${esc(r.key_alias ?? "")}</span></td>
          <td class="mono">${esc(r.model_group)}<span class="sub">${esc(r.provider)}</span></td>
          <td class="num">${r.latency_s != null ? r.latency_s + "s" : "—"}</td>
          <td class="num">${usd(r.cost_usd, 6)}</td>
          <td class="mono" style="max-width:360px;font-size:12px;color:var(--muted)">${esc((r.error ?? "").slice(0, 140))}</td></tr>`).join("")}</tbody></table></div>`
        : `<div class="empty"><b>Nothing here</b>No ${f === "all" ? "" : f + " "}requests logged yet.</div>`}</section>`;
    view.querySelectorAll("[data-f]").forEach((b) => b.onclick = () => { pages.logs.filter = b.dataset.f; render(); });
  },
  refresh: 8000,
};

// ---------- router + chrome ----------
async function renderLights() {
  try {
    const s = await api("/status");
    const light = (ok, label) => `<span class="light ${ok ? "ok" : "bad"}" data-label="${esc(label)} ${ok ? "up" : "down"}" role="img" aria-label="${esc(label)} ${ok ? "up" : "down"}"></span>`;
    $("#lights").innerHTML = light(s.gateway, "gateway") + light(s.database, "postgres") +
      (s.mock ? Object.entries(s.mock).map(([p, down]) => light(!down, `${p} mock`)).join("") : light(false, "mock"));
  } catch { /* signed out or offline */ }
}

let rendering = false;
const routeName = () => (location.hash.replace(/^#\//, "") || "overview").split("?")[0];
async function render() {
  const name = routeName();
  const page = pages[name] ?? pages.overview;
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === name));
  $("#page-sub").textContent = page.sub ?? "";
  $("#page-title").textContent = page.title;
  $("#page-actions").innerHTML = page.actions ?? "";
  document.title = `${page.title} · Gateway Ops`;
  clearTimeout(refreshTimer);
  if (rendering) return;
  rendering = true;
  try { await page.render($("#view")); }
  catch (e) {
    if (!(e instanceof ApiError && e.message === "signed out")) {
      // Actions are wired inside render(); if it failed they would be dead, so say why.
      document.querySelectorAll("#page-actions button").forEach((b) => { b.disabled = true; b.title = "Gateway unreachable"; });
      $("#view").innerHTML = `<section class="panel"><div class="empty"><b>Gateway unreachable</b>${esc(e.message)}. The proxy may still be starting (first run migrates the database).</div>
        <button class="btn btn-sm" id="retry">Retry now</button></section>`;
      $("#retry").onclick = () => render();
      if (!page.refresh) refreshTimer = setTimeout(render, 5000);
    }
  }
  finally { rendering = false; }
  if (routeName() !== name) return render();  // user navigated while this page was loading
  renderLights();
  if (page.refresh) refreshTimer = setTimeout(() => {
    if (!$("#drawer").classList.contains("open") && document.visibilityState === "visible") render();
    else refreshTimer = setTimeout(render, page.refresh);
  }, page.refresh);
}

function showLogin() {
  clearTimeout(refreshTimer);
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#login-key").focus();
}
function showApp() {
  $("#login").hidden = true;
  $("#app").hidden = false;
  render();
}

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  await submitting(form, async () => {
    try { await api("/login", { method: "POST", body: { master_key: $("#login-key").value } }); }
    catch (err) { $("#login-error").textContent = err.message; return; }
    $("#login-key").value = "";
    showApp();
  });
};
$("#login-fill").onclick = () => { $("#login-key").value = "123"; $("#login-key").focus(); };
$("#logout").onclick = async () => { await api("/logout", { method: "POST" }).catch(() => {}); sessionStorage.clear(); showLogin(); };
window.addEventListener("hashchange", render);

api("/session").then((s) => (s.signed_in ? showApp() : showLogin())).catch(showLogin);
