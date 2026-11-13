// 历史压缩页：开关、节省量汇总、最近的提炼记录与原文对照。
import {
  $, $$, apiGet, apiPost, chip, errText, h, iconEl, ms, num, openSheet, time, toast,
} from "./core.js";

const S = {
  data: null,
  selected: "",
  detail: null,
  saving: new Set(),
};
let wired = false;

const SWITCHES = [
  ["#cp-enabled", "enabled", "已启用历史压缩", "已关闭历史压缩"],
  ["#cp-dry", "dry_run", "已开启试运行：只计算与提炼，不改写历史", "已关闭试运行：下一次请求起压缩写回历史"],
  ["#cp-reminders", "reminders", "将压缩历史提醒块", "不再压缩历史提醒块"],
  ["#cp-tools", "tool_results", "将压缩历史工具结果", "不再压缩历史工具结果"],
];

function setSwitch(el, on) {
  el.setAttribute("aria-checked", on ? "true" : "false");
}

// 提交期间禁用开关，避免连续点击发出相互覆盖的请求。
async function flip(el, key, onText, offText) {
  if (!S.data || S.saving.has(key)) return;
  const next = !S.data.settings[key];
  S.saving.add(key);
  el.setAttribute("aria-disabled", "true");
  el.classList.add("busy");
  try {
    await apiPost("compact/settings", { [key]: next });
    S.data.settings[key] = next;
    setSwitch(el, next);
    renderStats();
    toast(next ? onText : offText);
  } catch (err) {
    toast(`保存失败：${errText(err)}`, "bad");
  } finally {
    S.saving.delete(key);
    el.removeAttribute("aria-disabled");
    el.classList.remove("busy");
  }
}

function wire() {
  if (wired) return;
  wired = true;
  for (const [id, key, onText, offText] of SWITCHES) {
    const el = $(id);
    el.addEventListener("click", () => flip(el, key, onText, offText));
    el.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        flip(el, key, onText, offText);
      }
    });
  }
  $("#cp-days").addEventListener("change", () => show());
}

export async function show() {
  wire();
  try {
    S.data = await apiGet("compact/overview", {
      days: Number($("#cp-days").value) || 7,
      limit: 100,
    });
  } catch (err) {
    $("#cp-stats").replaceChildren(h("p", { class: "empty", text: `读取失败：${errText(err)}` }));
    return;
  }
  for (const [id, key] of SWITCHES) setSwitch($(id), S.data.settings[key]);
  renderStats();
  renderRows();
  if (S.selected && !S.data.recent.some((r) => r.key === S.selected)) {
    S.selected = "";
    S.detail = null;
  }
  renderSide();
}

// ---------- 汇总 ----------
function renderStats() {
  const s = S.data.settings;
  const mode = S.data.events[s.dry_run ? "dry" : "live"] || { requests: 0, saved: 0, new_saved: 0 };
  const ok = S.data.digests.ok || { count: 0, original: 0, digest: 0, avg_ms: 0 };
  const failed = S.data.digests.error || { count: 0 };
  const kinds = S.data.kinds || {};
  const stat = (label, value, unit) => h("div", { class: "stat" }, [
    h("div", { class: "label" }, [h("span", { text: label })]),
    h("div", { class: "value" }, [String(value), h("small", { text: unit })]),
  ]);
  const perReq = mode.requests ? Math.round(mode.saved / mode.requests) : null;
  const ratio = ok.original ? `${Math.round((ok.digest / ok.original) * 100)}%` : "—";
  $("#cp-stats").replaceChildren(
    stat(
      s.dry_run ? "每次请求可少发" : "每次请求少发",
      perReq === null ? "—" : num(perReq),
      `字 · ${num(mode.requests)} 次请求平均`,
    ),
    stat(
      "新压缩",
      num(mode.new_saved),
      `字 · 提醒块 ${num(kinds.reminders || 0)} 条 · 节选 ${num(kinds.trim_items || 0)} 条`,
    ),
    stat("提炼", num(ok.count), `次成功 · 失败 ${num(failed.count)} · 平均 ${ms(ok.avg_ms)}`),
    stat("提炼压缩比", ratio, `${num(ok.original)} → ${num(ok.digest)} 字`),
  );
  const notes = [];
  if (!s.enabled) notes.push(chip("历史压缩已关闭", "bad"));
  else if (s.dry_run) notes.push(chip("试运行：只计算，不改写历史", "warn"));
  else notes.push(chip("生效中", "ok"));
  notes.push(s.distill_provider_id
    ? chip(`提炼模型 ${s.distill_provider_id}`, "info")
    : chip("未选提炼模型：只按规则截取", "warn"));
  notes.push(chip(`等待提炼 ${s.wait_seconds} 秒`), chip(`进行中 ${S.data.runtime.inflight}`));
  $("#cp-notes").replaceChildren(...notes);
}

// ---------- 列表 ----------
function appliedChip(item) {
  if (item.state !== "ok") return chip("失败", "bad");
  if (item.applied === "digest") return chip("已替换", "ok");
  if (item.applied === "trim") return chip("已按规则截取", "warn");
  return chip(S.data.settings.dry_run ? "试运行未写回" : "未使用", "plain");
}

function renderRows() {
  const body = $("#cp-rows");
  const rows = S.data.recent || [];
  if (!rows.length) {
    const text = S.data.settings.distill_provider_id
      ? "还没有提炼记录。回合里出现长工具结果（联网搜索、知识库等）后，结束时会在后台提炼。"
      : "在插件配置的“历史压缩 → 提炼模型”里选择一个模型后，长工具结果会在回合结束后提炼成笔记。";
    body.replaceChildren(h("tr", {}, [h("td", { colspan: "5", class: "empty", text })]));
    return;
  }
  body.replaceChildren(...rows.map((r) => {
    const size = r.state === "ok"
      ? `${num(r.original_chars)} → ${num(r.digest_chars)}`
      : num(r.original_chars);
    const tr = h("tr", { class: r.key === S.selected ? "sel" : "", tabindex: "0", dataset: { key: r.key } }, [
      h("td", { class: "mono", text: time(r.created_at, true) }),
      h("td", {}, [
        h("span", { text: r.question || "（无）" }),
        h("span", { class: "sub", text: (r.tools || []).join("、") }),
      ]),
      h("td", { class: "mono col-opt", text: size }),
      h("td", { class: "mono col-opt", text: ms(r.duration_ms) }),
      h("td", {}, [appliedChip(r)]),
    ]);
    tr.addEventListener("click", () => select(r.key));
    tr.addEventListener("keydown", (e) => {
      if (e.key === "Enter") select(r.key);
    });
    return tr;
  }));
}

async function select(key) {
  S.selected = key;
  $$("#cp-rows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.key === key));
  try {
    S.detail = await apiGet("compact/digest", { key });
  } catch (err) {
    toast(`读取摘要失败：${errText(err)}`, "bad");
    return;
  }
  renderSide();
}

// ---------- 右侧卡片 ----------
function toggleWide() {
  if (innerWidth <= 1100) {
    fullscreen();
    return;
  }
  $("#cp-layout").classList.toggle("wide");
  renderSide();
}

function fullscreen() {
  const item = S.detail;
  if (!item) return;
  const left = h("div", { class: "sheet-col" }, [h("h4", { text: item.question || "（无问题文本）" }), ...metaNodes(item)]);
  const right = h("div", {}, textSections(item, true));
  openSheet(`提炼详情 · ${time(item.created_at, true)}`, [left, right]);
}

function metaNodes(item) {
  const saved = item.original_chars && item.digest_chars ? 1 - item.digest_chars / item.original_chars : null;
  const size = item.state === "ok"
    ? `${num(item.original_chars)} → ${num(item.digest_chars)} 字${saved === null ? "" : `（少 ${Math.round(saved * 100)}%）`}`
    : `原文 ${num(item.original_chars)} 字`;
  return [h("dl", { class: "kv" }, [
    h("dt", { text: "会话" }), h("dd", { class: "mono", text: item.umo }),
    h("dt", { text: "工具" }), h("dd", { text: (item.tools || []).join("、") }),
    h("dt", { text: "模型" }), h("dd", { text: item.model || "—" }),
    h("dt", { text: "长度" }), h("dd", { text: size }),
    h("dt", { text: "状态" }), h("dd", {}, [
      appliedChip(item),
      item.error ? h("span", { class: "sub", text: ` ${item.error}` }) : null,
    ]),
  ])];
}

function textSections(item, open) {
  return [
    h("div", { class: "section" }, [
      h("h4", { text: "提炼结果" }),
      h("pre", { class: "code", text: item.digest || "（无）" }),
    ]),
    h("div", { class: "section" }, [
      h("h4", { text: "机器人当时的回答" }),
      h("pre", { class: "code", text: item.answer || "（无）" }),
    ]),
    h("div", { class: "section" }, [
      h("details", { open: open || null }, [
        h("summary", { text: `工具原文（${num(item.original_chars)} 字）` }),
        h("pre", { class: "code", text: item.original || "" }),
      ]),
    ]),
  ];
}

function renderSide() {
  const side = $("#cp-side");
  const item = S.detail;
  if (!item) {
    side.replaceChildren(h("div", { class: "side-body" }, [
      h("h3", { text: "提炼详情" }),
      h("p", {
        class: "empty",
        text: "选择左侧一条记录，对照工具原文和提炼结果。确认提炼质量后，在上方关闭试运行，压缩才会写回对话历史。",
      }),
    ]));
    return;
  }
  const wide = $("#cp-layout").classList.contains("wide");
  const head = h("div", { class: "side-head" }, [
    h("h3", { class: "grow", text: item.question || "（无问题文本）" }),
    h("button", {
      type: "button",
      class: "icon-btn",
      "data-expand": "",
      "aria-pressed": wide ? "true" : "false",
      "aria-label": wide ? "收窄面板" : "加宽面板",
      title: wide ? "收窄面板" : "加宽面板",
      onclick: toggleWide,
    }, [iconEl(wide ? "right" : "left")]),
    h("button", {
      type: "button",
      class: "icon-btn",
      "aria-label": "全屏查看",
      title: "全屏查看",
      onclick: fullscreen,
    }, [iconEl("maximize")]),
  ]);
  const body = wide
    ? [head, h("div", { class: "wide-cols" }, [h("div", {}, textSections(item, false)), h("div", {}, metaNodes(item))])]
    : [head, ...metaNodes(item), ...textSections(item, false)];
  side.replaceChildren(h("div", { class: "side-body" }, body));
}
