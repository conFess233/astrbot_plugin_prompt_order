// 检查器页：今日汇总、调用列表、实时推送、详情（实际请求 / 注入来源 / 响应 / 用量 / HTTP）、开关。
import {
  $, $$, apiGet, apiPost, bridge, btn, busy, chip, closeSheet, confirmBox, copyText, downloadText,
  errText, h, iconEl, ms, num, openSheet, ownerColor, pct, segBar, time, toast,
} from "./core.js";

const S = {
  items: [],
  selected: "",
  detail: null,
  round: 0,
  tab: "request",
  live: true,
  sub: null,
  poll: null,
  filters: {},
  prev: null,
};
const TABS = [["request", "实际请求"], ["build", "注入来源"], ["response", "响应"], ["usage", "用量"], ["http", "HTTP"]];

// ---------- 汇总与列表 ----------
const SCOPE_KEY = "prompt_order.ins_scope";
const SCOPES = { total: "合计", main: "主模型", side: "旁路调用" };
let scope = "total";
try { scope = SCOPES[localStorage.getItem(SCOPE_KEY)] ? localStorage.getItem(SCOPE_KEY) : "total"; } catch { /* 浏览器存储不可用时用默认值 */ }
let statsData = null;

async function loadStats() {
  try {
    statsData = await apiGet("inspector/stats", { days: 1 });
    renderStats();
  } catch (err) {
    $("#ins-stats").replaceChildren(h("p", { class: "empty", text: `汇总读取失败：${errText(err)}` }));
  }
}

function renderStats() {
  $$("#ins-scope span").forEach((s) => {
    const on = s.dataset.scope === scope;
    s.classList.toggle("on", on);
    s.setAttribute("aria-checked", on ? "true" : "false");
  });
  if (!statsData) return;
  const total = statsData.total;
  // 旧版后端没有 by_kind 时只能显示合计。
  const t = scope === "total" ? total : statsData.by_kind?.[scope] || total;
  const stat = (label, value, unit, bar) => h("div", { class: "stat" }, [
    h("div", { class: "label" }, [h("span", { text: label })]),
    h("div", { class: "value" }, [String(value), h("small", { text: unit })]), bar || null]);
  const rate = t.input ? t.cached / t.input : 0;
  const share = total.count ? ` · 占 ${pct(t.count / total.count)}` : "";
  $("#ins-stats").replaceChildren(
    stat("今日调用", num(t.count), scope === "total" ? `次 · 主模型 ${t.main} · 旁路 ${t.side}` : `次${share}`),
    stat("输入 tokens", num(t.input), `命中 ${pct(t.input ? rate : null)}`, segBar(rate, 20, rate >= 0.7 ? "ok" : rate >= 0.4 ? "warn" : "danger")),
    stat("输出 tokens", num(t.output), "tokens"),
    stat("平均轮数", t.count ? (t.rounds / t.count).toFixed(2) : "—", `出错 ${t.errors} 次`),
  );
}

let statsTimer = null;
function scheduleStats() {
  if (statsTimer) return;
  statsTimer = setTimeout(() => { statsTimer = null; loadStats(); }, 1500);
}

function pickScope(value) {
  scope = value;
  try { localStorage.setItem(SCOPE_KEY, value); } catch { /* 忽略 */ }
  renderStats();
}

function readFilters() {
  const form = $("#ins-filter");
  const data = new FormData(form);
  return {
    umo: (data.get("umo") || "").trim(),
    model: (data.get("model") || "").trim(),
    kind: data.get("kind") || "",
    q: (data.get("q") || "").trim(),
    errors: form.elements.errors.checked ? "1" : "",
    low_hit: form.elements.low_hit.checked ? "1" : "",
  };
}

export async function loadList(more = false) {
  const params = { ...S.filters, limit: 50 };
  if (more && S.items.length) params.before = S.items[S.items.length - 1].started_at;
  try {
    const res = await apiGet("inspector/traces", params);
    S.items = more ? [...S.items, ...res.items] : res.items;
    renderRows(!more);
    $("#ins-more").disabled = res.items.length < 50;
  } catch (err) {
    $("#ins-rows").replaceChildren(h("tr", {}, [h("td", { colspan: "7", class: "empty", text: `读取失败：${errText(err)}` })]));
  }
}

function rowFor(item, fresh) {
  const status = item.status === "open" ? chip("进行中", "info") : item.error ? chip("出错", "bad") : item.status === "interrupted" ? chip("中断", "warn") : chip("完成", "ok");
  const rate = item.hit_rate;
  const tr = h("tr", { class: `${S.selected === item.id ? "sel" : ""}${fresh ? " fresh" : ""}`, tabindex: "0", dataset: { id: item.id } }, [
    h("td", { class: "mono", text: time(item.started_at) }),
    h("td", {}, [h("span", { text: item.kind === "side" ? "旁路调用" : item.umo || "—" }), h("span", { class: "sub", text: item.title || "" })]),
    h("td", { class: "col-opt mono", text: (item.models || []).join(", ") || "—" }),
    h("td", { text: String(item.rounds) }),
    h("td", {}, [h("span", { text: num(item.input) }), h("span", { class: "sub", text: rate === null || rate === undefined ? "—" : `命中 ${pct(rate)}` })]),
    h("td", { class: "col-opt mono", text: item.status === "open" ? "…" : ms(item.duration_ms) }),
    h("td", { class: "col-opt" }, [status]),
  ]);
  tr.addEventListener("click", () => select(item.id));
  tr.addEventListener("keydown", (e) => { if (e.key === "Enter") select(item.id); });
  return tr;
}

function renderRows(animate) {
  const body = $("#ins-rows");
  if (!S.items.length) {
    const filtered = Object.values(S.filters).some(Boolean);
    body.replaceChildren(h("tr", {}, [h("td", { colspan: "7", class: "empty", text: filtered ? "没有符合筛选条件的记录。" : "还没有调用记录。检查器开启后，bot 每回复一次都会出现在这里。" })]));
  } else {
    body.replaceChildren(...S.items.map((item) => rowFor(item, false)));
    if (animate) $$("tr", body).forEach((tr, k) => tr.animate([{ opacity: 0, transform: "translateY(6px)" }, { opacity: 1, transform: "none" }], { duration: 220, delay: Math.min(k, 15) * 24, easing: "cubic-bezier(.2,.8,.2,1)", fill: "backwards" }));
  }
  $("#ins-total").textContent = `${S.items.length} 条`;
  $("#ins-foot").textContent = S.live ? "实时更新中" : "实时已暂停";
}

// ---------- 实时推送 ----------
function matchesFilters(item) {
  const f = S.filters;
  if (f.kind && item.kind !== f.kind) return false;
  if (f.errors || f.low_hit || f.model || f.q || f.umo) return false; // 复杂筛选下不插入，避免误显示
  return true;
}

function onLive(msg) {
  if (!S.live || !msg) return;
  if (msg.type === "gap") { loadList(); return; }
  if (msg.type === "trace_start" && matchesFilters(msg)) {
    if (S.items.some((i) => i.id === msg.id)) return;
    S.items.unshift(msg);
    $("#ins-rows").prepend(rowFor(msg, true));
    if ($("#ins-rows td[colspan]")) $("#ins-rows td[colspan]").parentNode.remove();
    $("#ins-total").textContent = `${S.items.length} 条`;
  }
  if (msg.type === "trace_end") {
    const idx = S.items.findIndex((i) => i.id === msg.id);
    if (idx >= 0) {
      S.items[idx] = msg;
      const old = $(`#ins-rows tr[data-id="${CSS.escape(msg.id)}"]`);
      if (old) old.replaceWith(rowFor(msg, false));
    }
    if (S.selected === msg.id) select(msg.id, true);
    scheduleStats();
  }
  if (msg.type === "round_end" && S.selected === msg.trace_id) select(msg.trace_id, true);
}

async function subscribe() {
  if (S.sub || !bridge.subscribeSSE) return;
  try {
    S.sub = await bridge.subscribeSSE("inspector/live", {
      onMessage: ({ parsed }) => onLive(parsed),
      onError: () => startPolling(),
    });
    stopPolling();
  } catch {
    startPolling();
  }
}

async function unsubscribe() {
  if (!S.sub) return;
  const id = S.sub;
  S.sub = null;
  try { await bridge.unsubscribeSSE(id); } catch { /* 页面卸载时宿主可能已断开 */ }
}

function startPolling() {
  if (S.poll) return;
  $("#ins-foot").textContent = "实时连接中断，已改为每 5 秒刷新";
  S.poll = setInterval(() => { if (S.live) loadList(); }, 5000);
}
function stopPolling() {
  clearInterval(S.poll);
  S.poll = null;
}

// ---------- 详情 ----------
async function select(id, quiet = false) {
  S.selected = id;
  $$("#ins-rows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === id));
  try {
    const data = await apiGet("inspector/trace", { id });
    if (S.selected !== id) return;
    if (!quiet || !S.detail || S.detail.id !== id) S.round = 0;
    S.detail = data;
    S.prev = null;
    renderDetail();
  } catch (err) {
    if (!quiet) toast(`读取详情失败：${errText(err)}`, "bad");
  }
}

function overview() {
  return [h("h3", { text: "调用检查器" }),
    h("p", { class: "empty", text: "点击左侧一行查看这次调用实际发出的请求、每个插件注入了什么、用量与命中率。内容很多时可以加宽或全屏查看。" })];
}

function renderDetail() {
  const side = $("#ins-side");
  const d = S.detail;
  if (!d) { side.replaceChildren(h("div", { class: "side-body" }, overview())); return; }
  const wide = $("#ins-layout").classList.contains("wide");
  const head = h("div", { class: "side-head" }, [
    chip(d.kind === "side" ? "旁路调用" : "主对话", "plain"), h("span", { class: "grow" }),
    h("button", { type: "button", class: "icon-btn", "data-expand": "", "aria-pressed": wide ? "true" : "false", "aria-label": wide ? "收窄面板" : "加宽面板", title: wide ? "收窄面板" : "加宽面板", onclick: toggleWide }, [iconEl(wide ? "right" : "left")]),
    h("button", { type: "button", class: "icon-btn", "aria-label": "全屏查看", title: "全屏查看", onclick: fullscreen }, [iconEl("maximize")]),
  ]);
  const item = S.items.find((i) => i.id === d.id) || {};
  const cost = d.cost ?? item.cost;
  const meta = h("dl", { class: "kv" }, [
    h("dt", { text: "会话" }), h("dd", { class: "mono", text: d.umo || "—" }),
    h("dt", { text: "发送者" }), h("dd", { text: d.sender || "—" }),
    h("dt", { text: "布局" }), h("dd", { text: d.layout_id || "—" }),
    h("dt", { text: "开始" }), h("dd", { class: "mono", text: time(d.started_at, true) }),
    h("dt", { text: "用量" }), h("dd", { text: `输入 ${num(d.input)}（命中 ${num(d.input_cached)} · ${pct(d.hit_rate)}）· 输出 ${num(d.output)}` }),
    h("dt", { text: "耗时" }), h("dd", { text: `${ms(d.duration_ms)} · ${d.rounds} 轮` }),
    cost !== undefined && cost !== null ? h("dt", { text: "估算费用" }) : null,
    cost !== undefined && cost !== null ? h("dd", { text: `$${cost}` }) : null,
  ]);
  const rounds = d.round_list || [];
  const roundTabs = h("div", { class: "subtabs", role: "tablist", "aria-label": "轮次" }, rounds.map((r, i) =>
    h("button", { type: "button", role: "tab", "aria-selected": i === S.round ? "true" : "false", text: `第${r.index}轮 ${r.kind}${r.error ? " ✕" : ""}`, onclick: () => { S.round = i; renderDetail(); } })));
  const tabs = h("div", { class: "subtabs", role: "tablist", "aria-label": "详情" }, TABS.map(([key, label]) =>
    h("button", { type: "button", role: "tab", "aria-selected": key === S.tab ? "true" : "false", text: label, onclick: () => { S.tab = key; renderDetail(); } })));
  side.replaceChildren(h("div", { class: "side-body" }, [head, h("h3", { text: d.title || "（无消息文本）" }), meta,
    rounds.length > 1 ? roundTabs : null, tabs, h("div", { id: "ins-tab" }, tabContent(S.tab))]));
}

function toggleWide() {
  if (innerWidth <= 1100) { fullscreen(); return; }
  $("#ins-layout").classList.toggle("wide");
  renderDetail();
}

function fullscreen() {
  const d = S.detail;
  if (!d) return;
  const left = h("div", { style: { display: "grid", gap: "12px", alignContent: "start" } }, [
    h("h4", { text: d.title || "（无消息文本）" }), ...tabContent("usage")]);
  const right = h("div", {}, [...tabContent("request"), h("div", { class: "section" }, [h("h4", { text: "注入来源" }), ...tabContent("build")])]);
  openSheet(`调用详情 · ${time(d.started_at, true)}`, [left, right]);
}

const currentRound = () => (S.detail && S.detail.round_list ? S.detail.round_list[S.round] : null);

function requestBody(rnd) {
  const wire = (rnd.attempts || []).find((a) => a.request_body && typeof a.request_body === "object");
  if (wire) return { body: wire.request_body, label: rnd.wire === "_query" ? "provider._query 入参（SDK 发出前）" : "线路层：实际发出的请求体", truncated: wire.body_truncated };
  return { body: rnd.params && rnd.params.input, label: "非线路层：调用层入参（未取得实际发出的请求体）", truncated: false };
}

function tabContent(tab) {
  const rnd = currentRound();
  if (tab === "build") return buildTab();
  if (!rnd) return [h("p", { class: "empty", text: "这次触发没有发生 LLM 调用。" })];
  if (tab === "request") return requestTab(rnd);
  if (tab === "response") return responseTab(rnd);
  if (tab === "usage") return usageTab(rnd);
  return httpTab(rnd);
}

function jsonActions(name, data) {
  const text = JSON.stringify(data, null, 2);
  return h("div", { style: { display: "flex", gap: "8px", margin: "8px 0" } }, [
    btn("复制 JSON", { iconName: "copy", onClick: () => copyText(text) }),
    btn("下载", { iconName: "download", onClick: () => downloadText(name, text) }),
  ]);
}

function requestTab(rnd) {
  const { body, label, truncated } = requestBody(rnd);
  const out = [h("p", { class: "empty", text: `${label}${truncated ? "（超过上限，已截断）" : ""}` })];
  if (!body) return [...out, h("p", { class: "empty", text: "没有记录到请求内容。" })];
  const msgs = Array.isArray(body.messages) ? body.messages : Array.isArray(body.contexts) ? body.contexts : null;
  out.push(h("dl", { class: "kv" }, [
    h("dt", { text: "模型" }), h("dd", { class: "mono", text: body.model || rnd.model }),
    h("dt", { text: "工具" }), h("dd", { text: Array.isArray(body.tools) ? `${body.tools.length} 个` : `${(rnd.params.tools || []).length} 个` }),
    h("dt", { text: "消息" }), h("dd", { text: msgs ? `${msgs.length} 条` : "—" }),
  ]));
  if (msgs) {
    const diverge = S.prev && S.prev.round === S.round ? S.prev.index : -1;
    out.push(h("div", { class: "msg-list" }, msgs.map((m, i) => {
      const content = typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 2);
      return h("details", { class: i === diverge ? "diverge" : "" }, [
        h("summary", {}, [chip(`${i} · ${m.role}`, m.role === "system" ? "info" : "plain"), h("span", { class: "grow", text: (content || "").slice(0, 120) }), h("span", { class: "mono", text: `${(content || "").length} 字` })]),
        h("pre", { class: "code", text: content || (m.tool_calls ? JSON.stringify(m.tool_calls, null, 2) : "") }),
      ]);
    })));
  }
  if (body.system_prompt) out.push(h("div", { class: "field", text: "system_prompt" }), h("pre", { class: "code", text: body.system_prompt }));
  out.push(h("details", {}, [h("summary", { text: "完整 JSON" }), h("pre", { class: "code", text: JSON.stringify(body, null, 2) })]));
  out.push(jsonActions(`request-${S.detail.id}-r${rnd.index}.json`, body));
  return out;
}

function buildTab() {
  const b = S.detail.build;
  if (!b || !b.runs) return [h("p", { class: "empty", text: S.detail.kind === "side" ? "旁路调用不经过构建阶段，没有注入记录。" : "没有构建阶段记录。" })];
  const owners = [...new Set(b.runs.map((r) => r.owner))];
  const out = [h("p", { class: "empty", text: `重排结果：${b.note || "—"}` })];
  for (const text of b.degraded || []) out.push(h("ul", { class: "warn-list" }, [h("li", { class: "warn" }, [iconEl("info"), h("span", { text })])]));
  out.push(h("div", { class: "legend" }, owners.map((o) => h("span", {}, [h("i", { style: { "--c": ownerColor(o) } }), o]))));
  out.push(h("div", { class: "field", text: "SYSTEM 来源表（按插件上色）" }));
  out.push(h("div", { class: "runs" }, b.runs.map((r) => h("span", { style: { "--c": ownerColor(r.owner) }, title: r.owner, text: r.text }))));
  out.push(h("div", { class: "section" }, [h("h4", { text: "钩子改动（按执行顺序）" }),
    b.changes && b.changes.length ? h("ul", { class: "timeline" }, b.changes.map((c) => h("li", { class: c.after_arrange ? "bad" : "ok" }, [
      h("strong", { text: c.owner }), ` ${c.summary}`, h("small", { text: `${c.handler} · ${c.kind}${c.after_arrange ? " · 重排之后" : ""}` })])))
      : h("p", { class: "empty", text: "没有插件修改请求。" })]));
  if (b.after_tail) {
    out.push(h("div", { class: "section" }, [h("h4", { text: "重排后的附加片段" }),
      h("div", { class: "msg-list" }, b.after_tail.map((p, i) => h("details", {}, [h("summary", {}, [chip(String(i + 1)), h("span", { class: "grow", text: (p.text || "").slice(0, 100) })]), h("pre", { class: "code", text: p.text || "" })])))]));
  }
  out.push(h("details", {}, [h("summary", { text: "重排前的 SYSTEM 全文" }), h("pre", { class: "code", text: b.before_system || "" })]));
  out.push(h("details", {}, [h("summary", { text: "重排后的 SYSTEM 全文" }), h("pre", { class: "code", text: b.after_system || "" })]));
  return out;
}

function responseTab(rnd) {
  const r = rnd.response || {};
  const out = [h("dl", { class: "kv" }, [
    h("dt", { text: "结束原因" }), h("dd", { class: "mono", text: r.finish_reason || "—" }),
    h("dt", { text: "响应 ID" }), h("dd", { class: "mono", text: r.id || "—" }),
    h("dt", { text: "出错" }), h("dd", { text: rnd.error || "无" }),
  ])];
  if (r.tool_calls && r.tool_calls.length) out.push(h("div", { class: "field", text: "工具调用" }), h("pre", { class: "code", text: r.tool_calls.map((t) => `${t.name}(${t.args})`).join("\n") }));
  if (r.reasoning) out.push(h("div", { class: "field", text: "思考内容" }), h("pre", { class: "code", text: r.reasoning }));
  out.push(h("div", { class: "field", text: "回复正文" }), h("pre", { class: "code", text: r.text === undefined ? "（未保存回复正文）" : r.text || "（空）" }));
  return out;
}

function usageTab(rnd) {
  const u = rnd.usage || {};
  const rate = u.input ? (u.input_cached || 0) / u.input : null;
  const out = [h("dl", { class: "kv" }, [
    h("dt", { text: "模型" }), h("dd", { class: "mono", text: `${rnd.model} · ${rnd.provider_id}` }),
    h("dt", { text: "输入" }), h("dd", { text: num(u.input) }),
    h("dt", { text: "命中缓存" }), h("dd", { text: `${num(u.input_cached)}（${pct(rate)}）` }),
    h("dt", { text: "输出" }), h("dd", { text: `${num(u.output)}${u.reasoning ? `（其中思考 ${num(u.reasoning)}）` : ""}` }),
    h("dt", { text: "耗时" }), h("dd", { text: `${ms(rnd.duration_ms)}${rnd.ttft_ms ? ` · 首字 ${ms(rnd.ttft_ms)}` : ""}` }),
  ]), segBar(rate || 0, 24, rate >= 0.7 ? "ok" : rate >= 0.4 ? "warn" : "danger")];
  const cmp = h("div", { class: "section" }, [h("h4", { text: S.round > 0 ? "与本次上一轮对比" : "与同会话上一次调用对比" })]);
  if (S.prev && S.prev.round === S.round) cmp.append(h("p", { text: S.prev.text }));
  else if (S.detail.kind === "main") cmp.append(btn("找出前缀从哪里开始不同", { cls: "btn btn-tonal btn-sm", onClick: (e) => comparePrev(e.currentTarget) }));
  else cmp.append(h("p", { class: "empty", text: "旁路调用不做对比。" }));
  out.push(cmp);
  if (u.raw) out.push(h("details", {}, [h("summary", { text: "供应商原始 usage" }), h("pre", { class: "code", text: JSON.stringify(u.raw, null, 2) })]));
  return out;
}

async function comparePrev(button) {
  const d = S.detail;
  const rnd = currentRound();
  const round = S.round;
  let prev = d;
  let prevRound = round > 0 ? d.round_list[round - 1] : null;
  if (!prevRound) {
    const idx = S.items.findIndex((i) => i.id === d.id);
    const older = S.items.slice(idx + 1).find((i) => i.umo === d.umo && i.kind === "main" && i.rounds > 0);
    if (!older) { toast("列表里没有同会话更早的调用，可以先加载更多", "bad"); return; }
    prev = await busy(button, "对比中", () => apiGet("inspector/trace", { id: older.id })).catch((err) => { toast(errText(err), "bad"); return null; });
    if (!prev || S.detail !== d) return;
    const list = prev.round_list || [];
    prevRound = list[list.length - 1];
    if (!prevRound) { toast("上一次调用没有记录到轮次", "bad"); return; }
  }
  const where = prev === d ? `本次第 ${prevRound.index} 轮` : `上一次（${time(prev.started_at)}）`;
  const a = requestBody(prevRound).body;
  const b = requestBody(rnd).body;
  let text;
  let index = -1;
  if (!a || !b || !Array.isArray(a.messages) || !Array.isArray(b.messages)) {
    text = "两次调用都需要线路层记录才能对比消息。";
  } else if (JSON.stringify(a.tools || null) !== JSON.stringify(b.tools || null)) {
    text = "工具定义不同：工具位于整个提示词最前面，这一轮基本无法命中缓存。";
  } else {
    const n = Math.min(a.messages.length, b.messages.length);
    index = 0;
    while (index < n && JSON.stringify(a.messages[index]) === JSON.stringify(b.messages[index])) index += 1;
    const role = b.messages[index] ? b.messages[index].role : "—";
    text = index === 0
      ? "第 0 条（system）就不同：system 内容变了，整个请求的缓存失效。"
      : index >= b.messages.length
        ? "消息完全一致。"
        : `前 ${index} 条消息与${where}一致；从第 ${index} 条（${role}）开始不同，已在“实际请求”中标出。${index === 1 ? "第 1 条就不同，通常是历史被截断。" : ""}`;
  }
  S.prev = { round, index, text };
  renderDetail();
}

function httpTab(rnd) {
  const attempts = rnd.attempts || [];
  if (!attempts.length) return [h("p", { class: "empty", text: rnd.wire === "call" ? "捕获深度为“调用层”，不记录 HTTP。" : "没有记录到 HTTP 请求。" })];
  return attempts.map((a, i) => h("div", { class: "section" }, [
    h("h4", { text: `第 ${i + 1} 次尝试` }),
    h("dl", { class: "kv" }, [
      h("dt", { text: "请求" }), h("dd", { class: "mono", text: `${a.method} ${a.url}` }),
      h("dt", { text: "状态" }), h("dd", {}, [a.status ? chip(String(a.status), a.status < 400 ? "ok" : "bad") : chip(a.error || "无响应", "bad")]),
      h("dt", { text: "耗时" }), h("dd", { text: ms(a.duration_ms) }),
    ]),
    h("details", {}, [h("summary", { text: "请求头（已脱敏）" }), h("pre", { class: "code", text: JSON.stringify(a.request_headers, null, 2) })]),
    h("details", {}, [h("summary", { text: "响应头" }), h("pre", { class: "code", text: JSON.stringify(a.response_headers, null, 2) })]),
  ]));
}

// ---------- 开关面板 ----------
const FIELDS = [
  ["enabled", "启用检查器", "bool", "关闭后包装直接透传，不记录任何调用。"],
  ["depth", "捕获深度", "depth", "线路层记录实际发出的请求；调用层只记模型、用量和轮数。"],
  ["capture_side", "记录旁路调用", "bool", "插件自己调用模型（如记忆总结、图片描述）的请求，数量可能很大。"],
  ["save_response", "保存回复正文", "bool", "关闭后只保留结束原因和工具调用名。"],
  ["fold_media", "折叠图片与音频", "bool", "把 base64 替换为带大小和摘要的占位文字。"],
  ["persist", "落盘保存", "bool", "关闭后只在内存中保留最近的记录。"],
  ["body_limit_kb", "单次请求体上限（KB）", "int", "64～16384，超出部分截断。"],
  ["retention_days", "保留天数", "int", "1～90。"],
  ["retention_mb", "总大小上限（MB）", "int", "50～10240。"],
  ["umo_include", "只记录这些会话", "text", "每行一个 umo 通配符，留空表示全部。"],
  ["umo_exclude", "排除这些会话", "text", "每行一个 umo 通配符。"],
];

async function openSettings() {
  let status;
  try {
    status = await apiGet("inspector/status");
  } catch (err) {
    toast(`读取检查器状态失败：${errText(err)}`, "bad");
    return;
  }
  const saved = status.settings || {};
  const draft = {};
  const rows = FIELDS.map(([key, label, kind, help]) => {
    const value = saved[key] ?? (kind === "bool" ? key !== "capture_side" : kind === "depth" ? "wire" : kind === "int" ? "" : "");
    let control;
    if (kind === "bool") {
      control = h("span", { class: `switch${value ? " on" : ""}`, role: "switch", tabindex: "0", "aria-checked": value ? "true" : "false", "aria-label": label });
      const flip = () => { const on = !control.classList.contains("on"); control.classList.toggle("on", on); control.setAttribute("aria-checked", on ? "true" : "false"); draft[key] = on; };
      control.addEventListener("click", flip);
      control.addEventListener("keydown", (e) => { if (e.key === " " || e.key === "Enter") { e.preventDefault(); flip(); } });
    } else if (kind === "depth") {
      control = h("select", { class: "input", "aria-label": label }, [h("option", { value: "wire", text: "线路层" }), h("option", { value: "call", text: "调用层" })]);
      control.value = value || "wire";
      control.addEventListener("change", () => { draft[key] = control.value; });
    } else if (kind === "int") {
      control = h("input", { class: "input", type: "number", value: value, "aria-label": label, style: { maxWidth: "160px" } });
      control.addEventListener("change", () => { draft[key] = Number.parseInt(control.value, 10); });
    } else {
      control = h("textarea", { class: "input", "aria-label": label, rows: "3" });
      control.value = value || "";
      control.addEventListener("change", () => { draft[key] = control.value; });
    }
    return h("div", { class: "form-row" }, [h("div", { class: "lbl" }, [h("strong", { text: label })]), h("div", {}, [control, h("div", { class: "help", text: help })])]);
  });
  const err = h("p", { class: "zone-msg" });
  const saveBtn = btn("保存开关", { cls: "btn btn-primary", iconName: "check", onClick: async (e) => {
    if (!Object.keys(draft).length) { closeSheet(); return; }
    try {
      await busy(e.currentTarget, "保存中", () => apiPost("inspector/config", draft));
      closeSheet();
      toast("检查器开关已保存，立即生效");
    } catch (ex) {
      err.textContent = `保存失败：${errText(ex)}`;
    }
  } });
  const clearBtn = btn("清空全部记录", { cls: "btn btn-danger", iconName: "trash", onClick: clearAll });
  const providers = Object.entries(status.providers || {});
  const info = h("div", { style: { display: "grid", gap: "12px", alignContent: "start" } }, [
    h("h4", { text: "运行状态" }),
    h("dl", { class: "kv" }, [
      h("dt", { text: "磁盘占用" }), h("dd", { text: `${(status.disk_bytes / 1048576).toFixed(1)} MB` }),
      h("dt", { text: "进行中" }), h("dd", { text: `${status.open_traces} 条` }),
      h("dt", { text: "实时连接" }), h("dd", { text: `${status.subscribers} 个` }),
    ]),
    h("h4", { text: "各 provider 的线路层方式" }),
    providers.length ? h("ul", { class: "dialog-list" }, providers.map(([id, mode]) => h("li", {}, [h("span", { class: "grow mono", text: id }), chip(mode === "httpx" ? "httpx（实际请求）" : mode === "_query" ? "_query（SDK 入参）" : "仅调用层", mode === "httpx" ? "ok" : "warn")])))
      : h("p", { class: "empty", text: "还没有包装到 provider。" }),
    clearBtn,
  ]);
  openSheet("检查器开关", [info, h("div", { class: "form-grid" }, [...rows, err, h("div", { class: "confirm" }, [h("div", { class: "row" }, [btn("取消", { cls: "btn btn-ghost", onClick: closeSheet }), saveBtn])])])]);
}

async function clearAll() {
  closeSheet();
  const yes = await confirmBox("清空调用记录", "删除内存和磁盘上的全部调用记录（进行中的记录保留）。这个操作无法撤销。", "确认清空全部记录");
  if (!yes) return;
  try {
    const res = await apiPost("inspector/clear", { confirm: true });
    S.items = [];
    S.detail = null;
    renderRows(false);
    renderDetail();
    loadStats();
    toast(`已清空 ${res.cleared} 条记录`);
  } catch (err) {
    toast(`清空失败：${errText(err)}`, "bad");
  }
}

// ---------- 初始化 ----------
let started = false;
export async function show() {
  if (!started) {
    started = true;
    $$("#ins-scope span").forEach((s) => {
      s.addEventListener("click", () => pickScope(s.dataset.scope));
      s.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickScope(s.dataset.scope); } });
    });
    renderStats();
    await Promise.all([loadStats(), loadList()]);
    renderDetail();
  }
  subscribe();
}

export function refresh() {
  return Promise.all([loadStats(), loadList()]);
}

export function init() {
  $("#ins-filter").addEventListener("submit", async (e) => {
    e.preventDefault();
    S.filters = readFilters();
    const submit = e.currentTarget.querySelector("[type=submit]");
    await busy(submit, "查询中", () => loadList());
  });
  $("#ins-more").addEventListener("click", (e) => busy(e.currentTarget, "加载中", () => loadList(true)));
  $("#ins-live").addEventListener("click", (e) => {
    S.live = !S.live;
    const b = e.currentTarget;
    b.setAttribute("aria-pressed", S.live ? "false" : "true");
    b.replaceChildren(iconEl(S.live ? "pause" : "play"), S.live ? "暂停实时" : "恢复实时");
    $("#ins-foot").textContent = S.live ? "实时更新中" : "实时已暂停";
    if (S.live) loadList();
  });
  $("#ins-settings").addEventListener("click", openSettings);
  $("#ins-export").addEventListener("click", async (e) => {
    try {
      await busy(e.currentTarget, "导出中", () => bridge.download("inspector/export", { ...S.filters, limit: 500 }, "prompt_order_traces.jsonl"));
    } catch (err) {
      toast(`导出失败：${errText(err)}`, "bad");
    }
  });
  window.addEventListener("pagehide", unsubscribe);
}

export function focusSearch() {
  $("#ins-filter input[name=q]").focus();
}
