// 排序页：布局草稿、拖拽排序、约束校验、片段库、预览与警告、保存与回滚。
import {
  $, $$, LEVEL, apiGet, apiPost, btn, busy, chip, closeSheet, confirmBox, errText, h, iconEl,
  openSheet, ownerColor, segBar, toast,
} from "./core.js";

const S = {
  saved: null,
  draft: [],
  current: 0,
  report: null,
  samples: [],
  sampleId: "",
  preview: null,
  previewMode: "after",
  selected: "",
  drag: null,
  enabled: true,
  dryRun: true,
  wrapped: 0,
};
const clone = (x) => JSON.parse(JSON.stringify(x));
const SPECIAL = {
  "@unplaced": ["未排片段", "没写进布局的片段落在这里，保持原区和原相对顺序"],
  "@media": ["图片 / 音频", "非文本块只能留在 TAIL 区"],
};
const NOTE = "@note:";
const isNote = (key) => String(key).startsWith(NOTE);
const noteOf = (key) => (layout().notes || []).find((n) => NOTE + n.id === key) || null;
const firstLine = (text, n = 28) => {
  const line = String(text || "").trim().split("\n")[0];
  return line.length > n ? `${line.slice(0, n)}…` : line;
};
// 旧版保存的布局没有这两个字段，补齐后草稿与已保存版本才能正确比较。
function normalize(l) {
  if (!Array.isArray(l.notes)) l.notes = [];
  if (!Array.isArray(l.rewrites)) l.rewrites = [];
  return l;
}
let onDirty = () => {};

export function setDirtyListener(fn) {
  onDirty = fn;
}

const layout = () => S.draft[S.current];
const segStats = () => (S.report && S.report.stats && S.report.stats.segments) || {};
const statOf = (key) => segStats()[key] || segStats()[String(key).split("#")[0]] || null;

// ---------- 载入与草稿 ----------
export async function load() {
  const data = await apiGet("state");
  S.saved = data.config;
  S.saved.layouts.forEach(normalize);
  S.enabled = data.enabled;
  S.dryRun = data.dry_run;
  S.strip = Boolean(data.strip_reasoning);
  S.images = Boolean(data.strip_history_images);
  S.json = data.minify_json !== false;
  S.wrapped = data.wrapped_hooks;
  // 首次载入，或没有未保存修改时，用已保存配置重建草稿；有草稿时保留。
  if (!S.draft.length || !dirtyCount()) S.draft = clone(S.saved.layouts);
  S.current = Math.min(S.current, S.draft.length - 1);
  renderAll();
  await refreshReport(true);
  return data;
}

export function statusLine() {
  const mode = !S.enabled ? "重排已关闭" : S.dryRun ? "试运行" : "重排生效中";
  return `已连接 · 布局 v${S.saved ? S.saved.version : "—"} · ${mode}`;
}

export function dirtyCount() {
  if (!S.saved) return 0;
  const saved = new Map(S.saved.layouts.map((l) => [l.id, JSON.stringify(l)]));
  const draft = new Map(S.draft.map((l) => [l.id, JSON.stringify(l)]));
  let n = 0;
  for (const [id, text] of draft) if (saved.get(id) !== text) n += 1;
  for (const id of saved.keys()) if (!draft.has(id)) n += 1;
  return n;
}

function changed({ report = true } = {}) {
  renderAll();
  onDirty(dirtyCount());
  if (report) scheduleReport();
}

let reportTimer = null;
function scheduleReport() {
  clearTimeout(reportTimer);
  reportTimer = setTimeout(() => refreshReport(false), 400);
}

async function refreshReport(withSamples) {
  const current = layout();
  if (!current) return;
  try {
    if (withSamples) {
      const res = await apiGet("samples", { layout: current.id, limit: 30 });
      S.samples = res.items || [];
      if (!S.samples.some((s) => s.id === S.sampleId)) S.sampleId = S.samples[0] ? S.samples[0].id : "";
    }
    S.report = await apiPost("report", { layout: current });
    S.preview = S.sampleId ? await apiPost("preview", { layout: current, sample_id: S.sampleId }) : null;
  } catch (err) {
    S.report = S.report || { warnings: [], stats: { requests: 0, segments: {} } };
    toast(`刷新统计失败：${errText(err)}`, "bad");
  }
  renderAll();
}

export async function save(button) {
  try {
    const res = await apiPost("save", { layouts: S.draft, base_version: S.saved.version });
    S.saved = res.config;
    S.saved.layouts.forEach(normalize);
    S.draft = clone(S.saved.layouts);
    changed({ report: false });
    return `布局已保存为 v${res.version}${S.dryRun ? "（当前为试运行，不会改写请求）" : "，立即生效"}`;
  } catch (err) {
    throw new Error(errText(err));
  } finally {
    if (button) button.blur();
  }
}

export function discard() {
  S.draft = clone(S.saved.layouts);
  S.current = Math.min(S.current, S.draft.length - 1);
  changed();
}

// ---------- 约束 ----------
function denyReason(key, zone) {
  if (key.startsWith("tool:")) return zone === "tools" ? "" : "C3：工具只能在 TOOLS 区里排序";
  if (isNote(key)) return zone === "system" ? "" : "常驻说明只能放在 SYSTEM 区";
  if (zone === "tools") return "C3：TOOLS 区只放工具";
  if (key === "@media") return zone === "tail" ? "" : "C2：图片和音频只能留在 TAIL 区";
  const st = statOf(key);
  if (zone === "system" && st) {
    if (st.media) return "C2：图片和音频只能留在 TAIL 区";
    if (st.zone === "tail" && !st.temp) return "C1：这个片段会写入历史，不能移进 SYSTEM 区";
  }
  return "";
}

function zoneList(zone) {
  return zone === "system" ? layout().system : layout().tail;
}

function placeEntry(key, zone, index) {
  const cur = layout();
  if (zone === "tools") {
    const order = cur.tools.order.filter((k) => k !== key);
    order.splice(index ?? order.length, 0, key);
    cur.tools.order = order;
    return;
  }
  cur.system = cur.system.filter((k) => k !== key);
  cur.tail = cur.tail.filter((k) => k !== key);
  const list = zoneList(zone);
  const at = index === undefined || index < 0 ? Math.max(0, list.indexOf("@unplaced")) : index;
  list.splice(Math.min(at, list.length), 0, key);
}

function removeEntry(key) {
  const cur = layout();
  cur.system = cur.system.filter((k) => k !== key);
  cur.tail = cur.tail.filter((k) => k !== key);
}

// 与后端 pattern_rank 一致：精确 key 优先，通配符只覆盖原本就在该区的片段。
function placedZone(key, st) {
  const base = key.split("#")[0];
  for (const zone of ["system", "tail"]) if (zoneList(zone).some((e) => e === key || e === base)) return zone;
  if (st) {
    const list = zoneList(st.zone === "system" ? "system" : "tail");
    if (list.includes(`${st.owner}/*`)) return st.zone;
  }
  return "";
}

// ---------- 渲染 ----------
export function renderAll() {
  if (!S.saved || !layout()) return;
  renderBar();
  renderZone("system");
  renderZone("tail");
  renderTools();
  renderLibrary();
  renderSide();
}

function renderBar() {
  const select = $("#layout-select");
  select.replaceChildren(...S.draft.map((l, i) => h("option", { value: i, text: `${l.name}（${l.id}）` })));
  select.value = String(S.current);
  const cur = layout();
  $("#layout-scope").value = cur.umo_globs.join(", ");
  $("#layout-name").value = cur.name;
  $("#layout-del").disabled = S.draft.length <= 1;
  setSwitch($("#sw-enabled"), S.enabled);
  setSwitch($("#sw-dry"), S.dryRun);
  setSwitch($("#sw-strip"), S.strip);
  setSwitch($("#sw-images"), S.images);
  setSwitch($("#sw-json"), S.json);
}

function setSwitch(el, on) {
  el.setAttribute("aria-checked", on ? "true" : "false");
}

function metaChips(key, st) {
  if (SPECIAL[key]) return [chip(SPECIAL[key][1])];
  if (isNote(key)) {
    const note = noteOf(key);
    return note ? [chip("常驻说明", "info"), chip(`${note.text.length} 字`)] : [chip("说明已删除", "warn")];
  }
  if (key.endsWith("/*")) return [chip("整组", "info"), chip(`来源 ${key.slice(0, -2)}`)];
  if (!st) return [chip("最近未出现", "warn")];
  const [label, kind] = LEVEL[st.level] || ["—", "plain"];
  const out = [chip(label, kind), chip(`出现 ${Math.round(st.presence * 100)}%`, st.presence >= 0.95 ? "plain" : "warn"), chip(`${st.avg_len} 字`)];
  if (st.zone === "tail") out.push(chip(st.temp ? "不存档" : "写入历史", st.temp ? "plain" : "info"));
  if (st.zone !== placedZone(key, st) && placedZone(key, st)) out.push(chip(`来自 ${st.zone.toUpperCase()}`, "info"));
  return out;
}

function segCard(key, zone, index) {
  const st = statOf(key);
  const special = SPECIAL[key];
  let owner = st ? st.owner : key.split("/")[0];
  if (key.endsWith("/*")) owner = key.slice(0, -2);
  if (isNote(key)) owner = "prompt_order";
  let title = special ? special[0] : key;
  if (isNote(key)) title = `常驻说明 · ${firstLine(noteOf(key)?.text) || key.slice(NOTE.length)}`;
  const card = h("li", {
    class: `seg-card${special ? " ph" : ""}${S.selected === key ? " sel" : ""}`,
    draggable: "true",
    tabindex: "0",
    dataset: { key, zone },
    "aria-label": `${title}，${zone === "library" ? "未排" : `${zone.toUpperCase()} 区第 ${index + 1} 个`}`,
  }, [
    h("span", { class: "grip" }, [iconEl("grip")]),
    h("span", { class: "bar", style: { "--c": special ? "var(--line-strong)" : ownerColor(owner) } }),
    h("div", { class: "main" }, [h("div", { class: "key", title, text: title }), h("div", { class: "meta" }, metaChips(key, st))]),
    h("div", { class: "acts" }, cardActions(key, zone, index)),
  ]);
  card.addEventListener("click", (e) => {
    if (e.target.closest(".acts")) return;
    S.selected = S.selected === key ? "" : key;
    renderAll();
  });
  card.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { S.selected = key; renderAll(); }
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowDown") && zone !== "library") {
      e.preventDefault();
      move(key, zone, index + (e.key === "ArrowUp" ? -1 : 1));
    }
  });
  card.addEventListener("dragstart", (e) => {
    S.drag = { key, from: zone };
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", key);
    requestAnimationFrame(() => card.classList.add("dragging"));
  });
  card.addEventListener("dragend", () => {
    card.classList.remove("dragging");
    S.drag = null;
    clearDrop();
  });
  return card;
}

function mini(name, label, onClick, disabledReason = "") {
  const b = h("button", { type: "button", class: "mini", "aria-label": label, title: disabledReason || label, disabled: disabledReason ? true : null }, [iconEl(name)]);
  b.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
  return b;
}

function cardActions(key, zone, index) {
  if (zone === "library") {
    return [
      mini("up", "放入 SYSTEM 区", () => { placeEntry(key, "system"); changed(); }, denyReason(key, "system")),
      mini("down", "放入 TAIL 区", () => { placeEntry(key, "tail"); changed(); }, denyReason(key, "tail")),
    ];
  }
  if (zone === "tools") {
    return layout().tools.mode === "custom"
      ? [mini("up", "上移", () => move(key, zone, index - 1)), mini("down", "下移", () => move(key, zone, index + 1))]
      : [];
  }
  const other = zone === "system" ? "tail" : "system";
  const acts = [
    mini("up", "上移", () => move(key, zone, index - 1)),
    mini("down", "下移", () => move(key, zone, index + 1)),
    mini(zone === "system" ? "down" : "up", `移到 ${other.toUpperCase()} 区`, () => { placeEntry(key, other); changed(); }, denyReason(key, other) || (SPECIAL[key] ? "占位项只能在本区移动" : "")),
  ];
  if (isNote(key)) acts.push(mini("trash", "删除常驻说明", () => deleteNote(key)));
  else if (!SPECIAL[key]) acts.push(mini("x", "移出布局", () => { removeEntry(key); if (S.selected === key) S.selected = ""; changed(); }));
  return acts;
}

function move(key, zone, to) {
  const list = zone === "tools" ? layout().tools.order : zoneList(zone);
  const from = list.indexOf(key);
  if (from < 0 || to < 0 || to >= list.length) return;
  list.splice(from, 1);
  list.splice(to, 0, key);
  changed();
  requestAnimationFrame(() => {
    const el = $$(`#zone-${zone} .seg-card`).find((c) => c.dataset.key === key);
    if (el) el.focus();
  });
}

function renderZone(zone) {
  const box = $(`#zone-${zone}`);
  const list = zoneList(zone);
  box.replaceChildren(...list.map((key, i) => segCard(key, zone, i)));
  if (!list.length) box.append(h("li", { class: "empty", text: "把片段拖到这里" }));
}

function toolKeys() {
  const cur = layout();
  const seen = Object.values(segStats()).filter((s) => s.zone === "tools").sort((a, b) => a.avg_order - b.avg_order);
  const names = seen.map((s) => s.key.slice(5));
  if (cur.tools.mode === "by_name") return [...names].sort();
  if (cur.tools.mode === "custom") {
    const rest = names.filter((n) => !cur.tools.order.includes(n)).sort();
    return [...cur.tools.order, ...rest];
  }
  return names;
}

function renderTools() {
  const cur = layout();
  $$("#tools-mode span").forEach((s) => {
    const on = s.dataset.mode === cur.tools.mode;
    s.classList.toggle("on", on);
    s.setAttribute("aria-checked", on ? "true" : "false");
  });
  const box = $("#zone-tools");
  const names = toolKeys();
  box.replaceChildren(...names.map((name, i) => {
    const card = segCard(`tool:${name}`, "tools", i);
    card.draggable = cur.tools.mode === "custom";
    card.querySelector(".key").textContent = name;
    const st = statOf(`tool:${name}`);
    card.querySelector(".meta").replaceChildren(chip(st ? `来源 ${st.owner}` : "最近未出现", st ? "plain" : "warn"), ...(st && st.presence < 0.95 ? [chip(`出现 ${Math.round(st.presence * 100)}%`, "warn")] : []));
    return card;
  }));
  if (!names.length) box.append(h("li", { class: "empty", text: "还没有统计到工具" }));
}

function renderLibrary() {
  const box = $("#library");
  const groups = new Map();
  for (const st of Object.values(segStats())) {
    if (st.zone === "tools" || st.media || isNote(st.key)) continue;
    if (placedZone(st.key, st)) continue;
    if (!groups.has(st.owner)) groups.set(st.owner, []);
    groups.get(st.owner).push(st);
  }
  if (!groups.size) {
    const requests = (S.report && S.report.stats && S.report.stats.requests) || 0;
    box.replaceChildren(h("p", { class: "empty", text: requests ? "所有出现过的片段都已排好。" : "这个布局还没有命中过请求。等 bot 回复几条消息后，这里会列出识别到的片段。" }));
    return;
  }
  box.replaceChildren(...[...groups.entries()].map(([owner, items]) => {
    const wholeZones = [...new Set(items.map((i) => (i.zone === "system" ? "system" : "tail")))];
    return h("div", { class: "lib-group" }, [
      h("h4", {}, [
        h("span", { class: "bar", style: { width: "4px", height: "14px", borderRadius: "2px", background: ownerColor(owner) } }),
        h("span", { text: owner }),
        h("span", { class: "grow" }),
        btn("整组放入原区", { onClick: () => { wholeZones.forEach((z) => placeEntry(`${owner}/*`, z)); changed(); }, title: "以“来源/*”放入，之后这个插件新出现的片段也会排在这里" }),
      ]),
      h("ul", { class: "lib-list" }, items.sort((a, b) => a.avg_order - b.avg_order).map((st) => segCard(st.key, "library", 0))),
    ]);
  }));
}

// ---------- 右侧卡片 ----------
function renderSide() {
  const side = $("#arrange-side");
  const body = h("div", { class: "side-body" });
  if (S.selected) body.append(...detailNodes(S.selected));
  else body.append(...overviewNodes());
  side.replaceChildren(body);
}

function overviewNodes() {
  const stats = (S.report && S.report.stats) || { requests: 0 };
  const warnings = (S.report && S.report.warnings) || [];
  const head = h("div", { class: "side-head" }, [h("h3", { class: "grow", text: "预览与警告" }),
    S.preview ? h("button", { type: "button", class: "icon-btn", "aria-label": "全屏查看预览", title: "全屏查看预览", onclick: openPreviewSheet }, [iconEl("maximize")]) : null]);
  const nodes = [head, h("dl", { class: "kv" }, [
    h("dt", { text: "统计窗口" }), h("dd", { text: `最近 ${stats.requests || 0} 次请求` }),
    h("dt", { text: "模式" }), h("dd", { text: !S.enabled ? "重排已关闭" : S.dryRun ? "试运行：只计算" : "生效中" }),
    h("dt", { text: "追踪钩子" }), h("dd", { text: `${S.wrapped} 个` }),
  ])];
  const est = estimate();
  if (est) nodes.push(h("div", { class: "section" }, [h("h4", { text: "估算" }), h("p", { class: "empty", text: est })]));
  nodes.push(h("div", { class: "section" }, [h("h4", { text: `警告（${warnings.length}）` }),
    warnings.length ? h("ul", { class: "warn-list" }, warnings.map((w) => h("li", { class: w.level }, [iconEl(w.level === "tip" ? "sparkle" : "info"), h("span", { text: w.text })])))
      : h("p", { class: "empty", text: "没有发现会破坏缓存的排法。" })]));
  nodes.push(previewSection(), rulesOverview());
  return nodes;
}

function estimate() {
  const segs = Object.values(segStats());
  if (!segs.length) return "";
  let gain = 0;
  let loss = 0;
  for (const st of segs) {
    if (st.zone === "tools" || st.media) continue;
    const zone = placedZone(st.key, st);
    if (st.zone === "tail" && zone === "system" && st.temp) gain += st.avg_len * st.presence;
    if (st.zone === "system" && zone === "tail") loss += st.avg_len * st.presence;
  }
  if (!gain && !loss) return "与原顺序相比，可缓存的内容没有变化。";
  const parts = [];
  if (gain) parts.push(`每轮多约 ${Math.round(gain)} 字进入可缓存的 SYSTEM 区`);
  if (loss) parts.push(`约 ${Math.round(loss)} 字移出 SYSTEM 区、改为每轮计费`);
  return `${parts.join("；")}（按最近出现率加权）。`;
}

function previewSection() {
  const sec = h("div", { class: "section" }, [h("h4", { text: "用草稿预览" })]);
  if (!S.samples.length) {
    sec.append(h("p", { class: "empty", text: "还没有样本：这个布局命中请求后才能预览。" }));
    return sec;
  }
  const select = h("select", { class: "input", "aria-label": "选择样本" }, S.samples.map((s) => h("option", { value: s.id, text: `${new Date(s.ts * 1000).toLocaleTimeString()} · ${s.title || s.umo}` })));
  select.value = S.sampleId;
  select.addEventListener("change", async () => { S.sampleId = select.value; await refreshReport(false); });
  const modes = h("div", { class: "subtabs", role: "tablist" }, [["before", "重排前"], ["after", "重排后"]].map(([mode, label]) =>
    h("button", { type: "button", role: "tab", "aria-selected": S.previewMode === mode ? "true" : "false", text: label, onclick: () => { S.previewMode = mode; renderSide(); } })));
  sec.append(select, modes, ...previewBody(false));
  return sec;
}

function previewBody(full) {
  const p = S.preview;
  if (!p) return [h("p", { class: "empty", text: "选择样本后显示。" })];
  const out = [];
  if (p.error) out.push(h("ul", { class: "warn-list" }, [h("li", { class: "danger" }, [iconEl("info"), h("span", { text: p.error })])]));
  for (const v of p.violations || []) out.push(h("ul", { class: "warn-list" }, [h("li", { class: "warn" }, [iconEl("info"), h("span", { text: v })])]));
  const side = S.previewMode === "before" || full === "before" ? p.before : p.after;
  out.push(h("div", { class: "field", text: `SYSTEM（${side.system.length} 段）` }), orderList(side.system));
  out.push(h("div", { class: "field", text: `TAIL（${side.tail.length} 段）` }), orderList(side.tail));
  if (full) {
    out.push(h("div", { class: "field", text: "SYSTEM 全文" }), h("pre", { class: "code", text: side.system_text || "" }));
    if (side.tail_text) out.push(h("div", { class: "field", text: "TAIL 全文" }), h("pre", { class: "code", text: side.tail_text.join("\n\n") }));
  }
  return out;
}

function orderList(keys) {
  return h("ol", { class: "lib-list", style: { margin: "4px 0 10px" } }, keys.map((key) => {
    const st = statOf(key);
    const owner = st ? st.owner : key.split("/")[0];
    return h("li", { class: "seg-card", style: { cursor: "default", gridTemplateColumns: "4px minmax(0,1fr)" } }, [
      h("span", { class: "bar", style: { "--c": ownerColor(owner) } }), h("div", { class: "key", text: key, title: key })]);
  }));
}

function openPreviewSheet() {
  const before = h("div", {}, [h("h4", { text: "重排前" }), ...previewBody("before")]);
  const saveMode = S.previewMode;
  S.previewMode = "after";
  const after = h("div", {}, [h("h4", { text: "重排后（使用草稿）" }), ...previewBody("after")]);
  S.previewMode = saveMode;
  openSheet("预览：重排前后对比", [before, after]);
}

function detailNodes(key) {
  const st = statOf(key);
  const seg = S.preview && S.preview.segments.find((s) => s.key === key);
  const head = h("div", { class: "side-head" }, [h("h3", { class: "grow", text: SPECIAL[key] ? SPECIAL[key][0] : key }),
    h("button", { type: "button", class: "icon-btn", "aria-label": "返回概览", title: "返回概览", onclick: () => { S.selected = ""; renderAll(); } }, [iconEl("x")])]);
  const nodes = [head];
  if (SPECIAL[key]) return [...nodes, h("p", { class: "empty", text: SPECIAL[key][1] })];
  if (isNote(key)) return [...nodes, ...noteDetail(key)];
  if (key.endsWith("/*")) return [...nodes, h("p", { class: "empty", text: `来源 ${key.slice(0, -2)} 在本区的所有片段按原相对顺序放在这里；之后它新出现的片段也会排在这里。跨区移动需要单独放置具体片段。` })];
  nodes.push(h("dl", { class: "kv" }, [
    h("dt", { text: "来源" }), h("dd", { text: st ? st.owner : "—" }),
    h("dt", { text: "原区" }), h("dd", { text: st ? st.zone.toUpperCase() : "—" }),
    h("dt", { text: "稳定度" }), h("dd", {}, st ? [chip(...(LEVEL[st.level] || ["—", "plain"])), ` 出现过 ${st.distinct} 种内容`] : ["最近未出现"]),
    h("dt", { text: "出现率" }), h("dd", {}, st ? [segBar(st.presence, 20, st.presence >= 0.95 ? "ok" : "warn")] : ["—"]),
    h("dt", { text: "平均长度" }), h("dd", { text: st ? `${st.avg_len} 字` : "—" }),
  ]));
  if (seg) nodes.push(h("div", { class: "section" }, [h("h4", { text: "样本原文" }), h("pre", { class: "code", text: seg.text })]));
  if (st && st.zone === "tail" && !st.media) nodes.push(rewriteSection(key, st));
  nodes.push(splitSection(st), aliasSection(key));
  return nodes;
}

// ---------- 常驻说明 ----------
function createNote(text = "") {
  const cur = layout();
  let n = cur.notes.length + 1;
  while (cur.notes.some((x) => x.id === `n${n}`)) n += 1;
  const id = `n${n}`;
  cur.notes.push({ id, text });
  placeEntry(NOTE + id, "system");
  return NOTE + id;
}

// 输入为空时：边框变红并轻晃，同时在旁边写明原因。
function flagEmpty(input, msg, text) {
  input.classList.remove("err");
  void input.offsetWidth;
  input.classList.add("err");
  msg.textContent = text;
}

function openNoteForm() {
  const area = h("textarea", {
    class: "input",
    rows: "8",
    maxlength: "4000",
    placeholder: "写一段每轮都一样的说明，例如：若用户消息中出现 <RAG-Faiss-Memory>，其中是过去的记录，与当前对话冲突时以当前为准。",
  });
  const err = h("p", { class: "zone-msg" });
  const ok = btn("加入 SYSTEM 区", {
    cls: "btn btn-primary",
    onClick: () => {
      if (!area.value.trim()) {
        flagEmpty(area, err, "说明不能为空。");
        return;
      }
      S.selected = createNote(area.value);
      closeSheet();
      changed();
      toast("已添加常驻说明（草稿），保存后生效");
    },
  });
  openSheet("新建常驻说明", [h("div", { class: "confirm" }, [
    h("p", {
      class: "empty",
      text: "常驻说明会作为一个片段放进 SYSTEM 区（会被缓存），可以拖动调整位置。它不会自动删除本轮片段里的同类内容；需要的话，在 TAIL 区片段的详情里添加改写规则。",
    }),
    h("label", { class: "field" }, ["说明内容", area]),
    err,
    h("div", { class: "row" }, [btn("取消", { cls: "btn btn-ghost", onClick: closeSheet }), ok]),
  ])], { single: true });
  area.focus();
}

// 删除只改草稿；通知里可以撤销，撤销后恢复原位置。
function deleteNote(key) {
  const cur = layout();
  const index = cur.notes.findIndex((n) => NOTE + n.id === key);
  if (index < 0) return;
  const [note] = cur.notes.splice(index, 1);
  const at = cur.system.indexOf(key);
  removeEntry(key);
  if (S.selected === key) S.selected = "";
  changed();
  toast("已删除常驻说明（草稿）", "ok", "撤销", () => {
    cur.notes.splice(index, 0, note);
    if (at >= 0) cur.system.splice(Math.min(at, cur.system.length), 0, key);
    changed();
  });
}

function noteDetail(key) {
  const note = noteOf(key);
  if (!note) return [h("p", { class: "empty", text: "这条常驻说明已被删除。" })];
  const area = h("textarea", { class: "input", rows: "10", maxlength: "4000", "aria-label": "说明内容" });
  const err = h("p", { class: "zone-msg" });
  area.value = note.text;
  area.addEventListener("change", () => {
    if (!area.value.trim()) {
      flagEmpty(area, err, "说明不能为空，已恢复原文。");
      area.value = note.text;
      return;
    }
    note.text = area.value;
    changed();
  });
  return [
    h("p", {
      class: "empty",
      text: "这段文字每轮都会原样放进 SYSTEM 区，按布局里的位置排列。修改后作为草稿，保存布局后生效。",
    }),
    h("dl", { class: "kv" }, [
      h("dt", { text: "ID" }), h("dd", { class: "mono", text: note.id }),
      h("dt", { text: "长度" }), h("dd", { text: `${note.text.length} 字` }),
    ]),
    h("div", { class: "section" }, [h("h4", { text: "内容" }), area, err]),
    h("div", { class: "section" }, [
      btn("删除这条说明", { cls: "btn btn-danger btn-sm", iconName: "trash", onClick: () => deleteNote(key) }),
    ]),
  ];
}

// ---------- 改写规则 ----------
function ruleTargets(key, st) {
  return [key.split("#")[0], `${st.owner}/*`];
}

function ruleHits(index) {
  const rules = (S.preview && S.preview.rewrite && S.preview.rewrite.rules) || [];
  return rules.find((r) => r.index === index) || null;
}

function afterText(key) {
  const p = S.preview;
  if (!p) return null;
  const i = p.after.tail.indexOf(key);
  return i >= 0 ? p.after.tail_text[i] : null;
}

function deleteRule(rule) {
  const cur = layout();
  const index = cur.rewrites.indexOf(rule);
  if (index < 0) return;
  cur.rewrites.splice(index, 1);
  changed();
  toast("已删除改写规则（草稿）", "ok", "撤销", () => {
    cur.rewrites.splice(Math.min(index, cur.rewrites.length), 0, rule);
    changed();
  });
}

function ruleItem(rule) {
  const hit = ruleHits(layout().rewrites.indexOf(rule));
  const enabled = h("input", { type: "checkbox", "aria-label": "启用这条规则" });
  enabled.checked = rule.enabled !== false;
  enabled.addEventListener("change", () => {
    rule.enabled = enabled.checked;
    changed();
  });
  const cut = (t) => (t.length > 60 ? `${t.slice(0, 60)}…` : t);
  let hitChip = null;
  if (S.preview) {
    hitChip = hit
      ? chip(`本样本命中 ${hit.hits} 处，少 ${hit.saved} 字`, "ok")
      : chip("本样本未命中", "warn");
  }
  return h("li", {}, [
    enabled,
    h("div", { class: "rule-body" }, [
      h("div", { class: "rule-find", text: cut(rule.find) }),
      h("div", { class: "rule-replace", text: `→ ${rule.replace ? cut(rule.replace) : "（删除）"}` }),
      h("div", { class: "rule-meta" }, [
        chip(rule.target.endsWith("/*") ? `整个来源 ${rule.target.slice(0, -2)}` : "本片段", "plain"),
        rule.enabled === false ? chip("已停用", "warn") : null,
        rule.regex ? chip("正则", "info") : null,
        hitChip,
      ]),
    ]),
    btn("删除规则", { cls: "btn btn-danger btn-sm", onClick: () => deleteRule(rule) }),
  ]);
}

function rewriteSection(key, st) {
  const sec = h("div", { class: "section" }, [
    h("h4", { text: "改写规则" }),
    h("p", {
      class: "empty",
      text: "发送前对这个片段做文字替换，按顺序执行，之后再做 JSON 压缩。替换为空即删除。规则只在重排生效（非试运行）时作用于请求；预览随时可用。",
    }),
  ]);
  const targets = ruleTargets(key, st);
  const rules = layout().rewrites.filter((r) => targets.includes(r.target));
  if (rules.length) sec.append(h("ul", { class: "dialog-list rule-list" }, rules.map(ruleItem)));

  const find = h("textarea", {
    class: "input",
    rows: "3",
    maxlength: "2000",
    placeholder: "查找的文字（可以从上方样本原文复制）",
  });
  const replace = h("input", { class: "input", maxlength: "2000", placeholder: "替换为（留空表示删除）" });
  const regex = h("input", { type: "checkbox" });
  const asNote = h("input", { type: "checkbox" });
  const scope = h("select", { class: "input" }, [
    h("option", { value: targets[0], text: "只作用于这个片段" }),
    h("option", { value: targets[1], text: `作用于来源 ${st.owner} 的全部 TAIL 片段` }),
  ]);
  const err = h("p", { class: "zone-msg" });
  const add = btn("添加改写规则", {
    cls: "btn btn-tonal btn-sm",
    iconName: "plus",
    onClick: () => {
      if (!find.value) {
        flagEmpty(find, err, "请填写要查找的文字。");
        return;
      }
      if (regex.checked) {
        try {
          new RegExp(find.value);
        } catch (e) {
          flagEmpty(find, err, `正则表达式有误：${e.message}`);
          return;
        }
      }
      layout().rewrites.push({
        target: scope.value,
        find: find.value,
        replace: replace.value,
        regex: regex.checked,
        enabled: true,
      });
      const withNote = asNote.checked && !regex.checked;
      if (withNote) createNote(find.value);
      changed();
      toast(withNote ? "已添加改写规则，并把查找内容加为常驻说明（草稿）" : "已添加改写规则（草稿），预览已更新");
    },
  });
  sec.append(
    h("label", { class: "field" }, ["查找", find]),
    err,
    h("label", { class: "field" }, ["替换为", replace]),
    h("label", { class: "field" }, ["范围", scope]),
    h("label", { class: "check" }, [regex, "按正则表达式匹配（Python 语法，替换里可用 \\1 引用分组）"]),
    h("label", { class: "check" }, [asNote, "同时把查找的文字加为 SYSTEM 区常驻说明（之后可编辑措辞）"]),
    h("div", { class: "form-actions" }, [add]),
  );
  const after = afterText(key);
  if (after !== null) {
    sec.append(h("h4", { text: "改写后（本样本）" }), h("pre", { class: "code", text: after }));
  }
  return sec;
}

function rulesOverview() {
  const all = layout().rewrites;
  const rw = S.preview && S.preview.rewrite;
  const sec = h("div", { class: "section" }, [h("h4", { text: `改写规则（${all.length}）` })]);
  if (rw) {
    const bits = [];
    if (rw.json_blocks) bits.push(`JSON 压缩少 ${rw.json_saved} 字`);
    if (rw.rule_saved) bits.push(`改写规则少 ${rw.rule_saved} 字`);
    const text = bits.length ? `本样本：${bits.join("，")}。` : "本样本没有被改写的内容。";
    sec.append(h("p", { class: "empty", text }));
  }
  if (!all.length) {
    sec.append(h("p", { class: "empty", text: "还没有改写规则。点开 TAIL 区的片段，在详情里添加。" }));
    return sec;
  }
  sec.append(h("ul", { class: "dialog-list rule-list" }, all.map(ruleItem)));
  return sec;
}

function splitSection(st) {
  const sec = h("div", { class: "section" }, [h("h4", { text: "细分" }),
    h("p", { class: "empty", text: "同一个插件一次加了好几块内容时，填一段出现在行首的文字作为锚点，从锚点处切开。" })]);
  if (!st) return sec;
  const input = h("input", { class: "input", placeholder: "锚点文字，例如 <ScoringMode>", maxlength: "200" });
  const add = btn("添加细分规则", { cls: "btn btn-tonal btn-sm", iconName: "plus", onClick: () => {
    const anchor = input.value;
    if (!anchor.trim()) { input.classList.remove("err"); void input.offsetWidth; input.classList.add("err"); return; }
    layout().split_rules.push({ owner: st.owner, anchor, line_start: true });
    changed();
    toast("已添加细分规则（草稿），预览已按新规则切分");
  } });
  const rules = layout().split_rules.filter((r) => r.owner === st.owner);
  sec.append(h("div", { style: { display: "flex", gap: "8px" } }, [input, add]));
  if (rules.length) {
    sec.append(h("ul", { class: "dialog-list", style: { marginTop: "8px" } }, rules.map((r) => h("li", {}, [h("span", { class: "grow mono", text: r.anchor }),
      btn("删除", { cls: "btn btn-danger btn-sm", onClick: () => { layout().split_rules = layout().split_rules.filter((x) => x !== r); changed(); } })]))));
  }
  return sec;
}

function aliasSection(key) {
  const sec = h("div", { class: "section" }, [h("h4", { text: "别名" }),
    h("p", { class: "empty", text: "片段第一行变了导致被认成新片段时，把它指向布局里原来的片段。" })]);
  const targets = [...layout().system, ...layout().tail].filter((k) => !k.startsWith("@") && !k.endsWith("/*") && k !== key);
  const current = layout().aliases[key] || "";
  const select = h("select", { class: "input", "aria-label": "别名目标" }, [h("option", { value: "", text: "不设别名" }), ...targets.map((t) => h("option", { value: t, text: t }))]);
  select.value = current;
  select.addEventListener("change", () => {
    if (select.value) layout().aliases[key] = select.value;
    else delete layout().aliases[key];
    changed();
  });
  sec.append(select);
  return sec;
}

// ---------- 拖放 ----------
function clearDrop() {
  $$(".zone").forEach((z) => z.classList.remove("over", "deny"));
  $$(".drop-line").forEach((l) => l.remove());
}

function dropIndex(zoneEl, y) {
  const cards = $$(".seg-card:not(.dragging)", zoneEl);
  for (let i = 0; i < cards.length; i += 1) {
    const r = cards[i].getBoundingClientRect();
    if (y < r.top + r.height / 2) return [i, cards[i]];
  }
  return [cards.length, null];
}

function bindZone(zoneEl) {
  const zone = zoneEl.dataset.zone;
  zoneEl.addEventListener("dragover", (e) => {
    if (!S.drag) return;
    if (zone === "tools" && (layout().tools.mode !== "custom" || S.drag.from !== "tools")) return;
    const reason = denyReason(S.drag.key, zone) || (SPECIAL[S.drag.key] && S.drag.from !== zone ? "占位项只能在本区移动" : "");
    clearDrop();
    if (reason) {
      zoneEl.classList.add("deny");
      zoneEl.title = reason;
      return;
    }
    e.preventDefault();
    zoneEl.title = "";
    zoneEl.classList.add("over");
    const [, before] = dropIndex(zoneEl, e.clientY);
    const line = h("li", { class: "drop-line", "aria-hidden": "true" });
    if (before) zoneEl.insertBefore(line, before);
    else zoneEl.append(line);
  });
  zoneEl.addEventListener("dragleave", (e) => {
    if (!zoneEl.contains(e.relatedTarget)) clearDrop();
  });
  zoneEl.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!S.drag) return;
    const [index] = dropIndex(zoneEl, e.clientY);
    const { key, from } = S.drag;
    clearDrop();
    if (zone === "tools") {
      const order = toolKeys();
      const name = key.slice(5);
      const from_i = order.indexOf(name);
      order.splice(from_i, 1);
      order.splice(index > from_i ? index - 1 : index, 0, name);
      layout().tools.order = order;
    } else {
      const list = zoneList(zone);
      const old = list.indexOf(key);
      const at = from === zone && old >= 0 && old < index ? index - 1 : index;
      placeEntry(key, zone, at);
    }
    S.drag = null;
    changed();
  });
}

// ---------- 布局增删与版本 ----------
function layoutForm(title, seed, onSubmit) {
  const id = h("input", { class: "input", value: seed.id, maxlength: "40", placeholder: "小写字母、数字、-、_" });
  const name = h("input", { class: "input", value: seed.name, maxlength: "40" });
  const err = h("p", { class: "zone-msg" });
  const ok = btn("确定", { cls: "btn btn-primary", onClick: () => {
    const value = id.value.trim();
    if (!/^[a-z0-9_-]{1,40}$/.test(value)) { err.textContent = "ID 只能用小写字母、数字、- 和 _，最多 40 个字符。"; return; }
    if (S.draft.some((l) => l.id === value)) { err.textContent = "这个 ID 已经被其他布局使用。"; return; }
    onSubmit(value, name.value.trim() || value);
    closeSheet();
  } });
  openSheet(title, [h("div", { class: "confirm" }, [
    h("label", { class: "field" }, ["布局 ID", id]), h("label", { class: "field" }, ["名称", name]), err,
    h("div", { class: "row" }, [btn("取消", { cls: "btn btn-ghost", onClick: closeSheet }), ok])])], { single: true });
  id.focus();
}

async function showVersions() {
  let items = [];
  try {
    items = (await apiGet("versions")).items || [];
  } catch (err) {
    toast(`读取历史版本失败：${errText(err)}`, "bad");
    return;
  }
  const list = items.length
    ? h("ul", { class: "dialog-list" }, items.map((v) => h("li", {}, [
      chip(`v${v.version}`, v.version === S.saved.version ? "ok" : "plain"),
      h("span", { class: "grow", text: `${new Date(v.updated_at * 1000).toLocaleString()} · ${v.layouts} 个布局${v.label ? ` · ${v.label}` : ""}` }),
      v.version === S.saved.version ? chip("当前") : btn("回滚到此版本", { onClick: (e) => rollback(v.version, e.currentTarget) }),
    ])))
    : h("p", { class: "empty", text: "还没有保存过布局。" });
  openSheet("历史版本", [list], { single: true });
}

async function rollback(version, button) {
  if (dirtyCount()) {
    toast("有未保存的修改，请先保存或放弃后再回滚", "bad");
    return;
  }
  closeSheet();
  const yes = await confirmBox("回滚布局", `把全部布局恢复成 v${version} 的内容，并保存为新版本 v${S.saved.version + 1}。命中这些布局的会话缓存会失效一次。`, `回滚到 v${version}`);
  if (!yes) return;
  try {
    const res = await busy(button, "回滚中", () => apiPost("rollback", { version, base_version: S.saved.version }));
    S.saved = res.config;
    S.saved.layouts.forEach(normalize);
    S.draft = clone(S.saved.layouts);
    changed();
    toast(`已回滚，当前为 v${res.version}`);
  } catch (err) {
    toast(`回滚失败：${errText(err)}`, "bad");
  }
}

export async function recommend(button) {
  try {
    const res = await busy(button || $("#layout-recommend"), "生成中", () => apiPost("recommend", { layout: layout() }));
    const cur = layout();
    // 推荐来自统计：去掉已删除的常驻说明，补回还没统计到的说明（放在未排片段之前）。
    const notes = cur.notes.map((n) => NOTE + n.id);
    const system = res.layout.system.filter((k) => !isNote(k) || notes.includes(k));
    const missing = notes.filter((k) => !system.includes(k));
    const at = system.includes("@unplaced") ? system.indexOf("@unplaced") : system.length;
    system.splice(at, 0, ...missing);
    cur.system = system;
    cur.tail = res.layout.tail.filter((k) => !isNote(k));
    changed();
    toast("已生成推荐排序（草稿），保存后生效");
  } catch (err) {
    toast(`生成推荐失败：${errText(err)}`, "bad");
  }
}

async function toggleSetting(key, el) {
  const next = el.getAttribute("aria-checked") !== "true";
  try {
    await apiPost("settings", { [key]: next });
    if (key === "enabled") S.enabled = next;
    else if (key === "dry_run") S.dryRun = next;
    else if (key === "strip_reasoning") S.strip = next;
    else if (key === "minify_json") S.json = next;
    else S.images = next;
    setSwitch(el, next);
    renderSide();
    onDirty(dirtyCount());
    const messages = {
      enabled: next ? "已启用重排" : "已关闭重排，只追踪不改写",
      dry_run: next ? "已进入试运行" : "已退出试运行，重排结果会写回请求",
      strip_reasoning: next ? "已开启：发给 DeepSeek 的请求会清空历史轮次的思考内容" : "已关闭清除历史思考",
      strip_history_images: next ? "已开启：历史轮次的图片会换成文字占位" : "已关闭省略历史图片",
      minify_json: next ? "已开启：本轮片段里的 JSON 发送前去掉多余空白" : "已关闭 JSON 压缩",
    };
    toast(messages[key]);
  } catch (err) {
    toast(`修改失败：${errText(err)}`, "bad");
  }
}

// ---------- 初始化 ----------
export function init() {
  $$(".zone").forEach(bindZone);
  $("#layout-select").addEventListener("change", (e) => {
    S.current = Number(e.target.value);
    S.selected = "";
    S.sampleId = "";
    changed({ report: false });
    refreshReport(true);
  });
  $("#layout-scope").addEventListener("change", (e) => {
    layout().umo_globs = e.target.value.split(/[,，\n]/).map((s) => s.trim()).filter(Boolean);
    changed();
  });
  $("#layout-name").addEventListener("change", (e) => {
    layout().name = e.target.value.trim() || layout().id;
    changed({ report: false });
  });
  $("#layout-new").addEventListener("click", () => layoutForm("新建布局", { id: "", name: "" }, (id, name) => {
    S.draft.push({ id, name, umo_globs: [], system: ["@unplaced"], tail: ["@unplaced", "@media"], tools: { mode: "keep", order: [] }, aliases: {}, split_rules: [], separator: "\n\n", enabled: true, notes: [], rewrites: [] });
    S.current = S.draft.length - 1;
    changed();
  }));
  $("#layout-copy").addEventListener("click", () => layoutForm("复制布局", { id: `${layout().id}-copy`, name: `${layout().name} 副本` }, (id, name) => {
    const copy = clone(layout());
    Object.assign(copy, { id, name, umo_globs: [] });
    S.draft.push(copy);
    S.current = S.draft.length - 1;
    changed();
  }));
  $("#layout-del").addEventListener("click", async () => {
    const cur = layout();
    const yes = await confirmBox("删除布局", `从草稿中删除布局“${cur.name}”（${cur.id}）。保存后才会生效，之前可以放弃修改。`, `删除“${cur.name}”`);
    if (!yes) return;
    S.draft.splice(S.current, 1);
    S.current = 0;
    changed();
  });
  $("#layout-recommend").addEventListener("click", (e) => recommend(e.currentTarget));
  $("#layout-versions").addEventListener("click", showVersions);
  $$("#tools-mode span").forEach((s) => {
    const pick = () => { layout().tools.mode = s.dataset.mode; if (s.dataset.mode === "custom" && !layout().tools.order.length) layout().tools.order = toolKeys(); changed(); };
    s.addEventListener("click", pick);
    s.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
  });
  $("#note-new").addEventListener("click", () => openNoteForm());
  for (const [id, key] of [["#sw-enabled", "enabled"], ["#sw-dry", "dry_run"], ["#sw-strip", "strip_reasoning"], ["#sw-images", "strip_history_images"], ["#sw-json", "minify_json"]]) {
    const el = $(id);
    el.addEventListener("click", () => toggleSetting(key, el));
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleSetting(key, el); } });
  }
}
