// 配置页：按插件配置说明渲染全部配置项；草稿只在右下角胶囊保存或放弃。
import { $, $$, apiGet, apiPost, btn, errText, h, toast } from "./core.js";

const S = {
  groups: [],
  fields: {},
  saved: {},
  draft: {},
  providers: [],
  errors: {},
  rows: {},
  loaded: false,
};
let onDirty = () => {};

export function setDirtyListener(fn) {
  onDirty = fn;
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const groupKey = (key) => (key.includes(".") ? key.split(".")[0] : "");

export function changedKeys() {
  return Object.keys(S.draft).filter((key) => !same(S.draft[key], S.saved[key]));
}

export function dirtyCount() {
  return changedKeys().length;
}

// ---------- 载入、保存、放弃 ----------
export async function show() {
  // 有草稿时不重新拉取，避免覆盖；保存时后端会检查别处的修改。
  if (S.loaded && dirtyCount()) return;
  apply(await apiGet("config"));
}

function apply(data) {
  S.groups = data.groups || [];
  S.fields = {};
  for (const group of S.groups) for (const field of group.fields) S.fields[field.key] = field;
  S.saved = data.values || {};
  S.draft = { ...S.saved };
  S.providers = data.providers || [];
  S.errors = {};
  S.loaded = true;
  render();
  onDirty(0);
}

export async function save() {
  const bad = Object.keys(S.errors);
  if (bad.length) {
    const first = S.rows[bad[0]];
    if (first) first.row.scrollIntoView({ behavior: "smooth", block: "center" });
    throw new Error(`有 ${bad.length} 项填写不正确：${bad.map((k) => S.fields[k].title).join("、")}`);
  }
  const keys = changedKeys();
  const values = {};
  const base = {};
  for (const key of keys) {
    values[key] = S.draft[key];
    base[key] = S.saved[key];
  }
  const data = await apiPost("config/save", { values, base });
  apply(data);
  return `已保存 ${keys.length} 项配置`;
}

// 放弃后重新读取：保存冲突时，页面上的“已保存值”可能已经过期。
export function discard() {
  S.draft = { ...S.saved };
  S.errors = {};
  render();
  onDirty(0);
  show().catch((err) => toast(`重新读取配置失败：${errText(err)}`, "bad"));
}

// ---------- 显示值 ----------
function shown(field, value) {
  if (value === undefined || value === null) return "—";
  if (field.type === "bool") return value ? "开启" : "关闭";
  if (field.options) {
    const i = field.options.indexOf(value);
    return i >= 0 ? field.labels[i] : String(value);
  }
  if (field.special === "select_provider") return value || "不使用";
  if (field.type === "int") return Number.isInteger(value) ? value.toLocaleString("zh-CN") : String(value);
  const text = String(value);
  if (!text.trim()) return "（空）";
  const lines = text.split("\n").filter((l) => l.trim());
  const head = lines[0].length > 40 ? `${lines[0].slice(0, 40)}…` : lines[0];
  return lines.length > 1 ? `${head}（共 ${lines.length} 行）` : head;
}

// ---------- 渲染 ----------
function render() {
  const anchors = $("#cfg-anchors");
  anchors.replaceChildren(...S.groups.map((g, i) => h("a", {
    href: `#cfg-g-${g.key || "top"}`,
    class: i === 0 ? "on" : "",
    dataset: { group: g.key },
    onclick: (e) => {
      e.preventDefault();
      $$("a", anchors).forEach((a) => a.classList.toggle("on", a === e.currentTarget));
      $(`#cfg-g-${g.key || "top"}`).scrollIntoView({ behavior: "smooth", block: "start" });
    },
  }, [g.title])));
  S.rows = {};
  $("#cfg-groups").replaceChildren(...S.groups.map((g) => h("section", {
    class: "card card-pad cfg-group",
    id: `cfg-g-${g.key || "top"}`,
    "aria-labelledby": `cfg-h-${g.key || "top"}`,
  }, [
    h("div", { class: "card-title" }, [h("div", {}, [
      h("h3", { id: `cfg-h-${g.key || "top"}`, text: g.title }),
      g.hint ? h("p", { text: g.hint }) : null,
    ])]),
    ...g.fields.map(fieldRow),
  ])));
  refreshMarks();
  renderSide();
}

function fieldRow(field) {
  const msg = h("p", { class: "zone-msg" });
  const control = makeControl(field);
  const row = h("div", { class: "form-row" }, [
    h("div", { class: "lbl" }, [h("strong", { text: field.title })]),
    h("div", { class: "ctl" }, [control.el, field.hint ? h("div", { class: "help", text: field.hint }) : null, msg]),
  ]);
  S.rows[field.key] = { row, msg, control };
  return row;
}

function makeControl(field) {
  const value = S.draft[field.key];
  if (field.type === "bool") return switchControl(field, value);
  if (field.special === "select_provider") return providerControl(field, value);
  if (field.options && field.options.length <= 4) return segmentedControl(field, value);
  if (field.options) return selectControl(field, value);
  if (field.type === "int") return intControl(field, value);
  if (field.type === "text") {
    const area = h("textarea", { class: "input", rows: "3", "aria-label": field.title });
    area.value = value || "";
    area.addEventListener("input", () => update(field.key, area.value));
    return { el: area, set: (v) => { area.value = v || ""; }, input: area };
  }
  const input = h("input", { class: "input", type: "text", "aria-label": field.title });
  input.value = value || "";
  input.addEventListener("input", () => update(field.key, input.value));
  return { el: input, set: (v) => { input.value = v || ""; }, input };
}

function switchControl(field, value) {
  const el = h("span", { class: "switch", role: "switch", tabindex: "0", "aria-label": field.title });
  const set = (v) => {
    el.classList.toggle("on", Boolean(v));
    el.setAttribute("aria-checked", v ? "true" : "false");
  };
  const flip = () => { const next = !S.draft[field.key]; set(next); update(field.key, next); };
  el.addEventListener("click", flip);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); flip(); }
  });
  set(value);
  return { el, set };
}

function segmentedControl(field, value) {
  const el = h("div", { class: "segmented", role: "radiogroup", "aria-label": field.title });
  const items = field.options.map((opt, i) => {
    const span = h("span", { role: "radio", tabindex: "0", text: field.labels[i] });
    const pick = () => { set(opt); update(field.key, opt); };
    span.addEventListener("click", pick);
    span.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
    el.append(span);
    return span;
  });
  const set = (v) => items.forEach((span, i) => {
    const on = field.options[i] === v;
    span.classList.toggle("on", on);
    span.setAttribute("aria-checked", on ? "true" : "false");
  });
  set(value);
  return { el, set };
}

function selectControl(field, value) {
  const el = h("select", { class: "input", "aria-label": field.title },
    field.options.map((opt, i) => h("option", { value: opt, text: field.labels[i] })));
  el.value = value;
  el.addEventListener("change", () => update(field.key, el.value));
  return { el, set: (v) => { el.value = v; } };
}

function providerControl(field, value) {
  const el = h("select", { class: "input", "aria-label": field.title });
  const fill = (current) => {
    const options = [h("option", { value: "", text: "不使用" })];
    for (const p of S.providers) {
      options.push(h("option", { value: p.id, text: p.model ? `${p.id}（${p.model}）` : p.id }));
    }
    if (current && !S.providers.some((p) => p.id === current)) {
      options.push(h("option", { value: current, text: `${current}（未找到，可能已删除）` }));
    }
    el.replaceChildren(...options);
    el.value = current || "";
  };
  fill(value);
  el.addEventListener("change", () => update(field.key, el.value));
  return { el, set: fill };
}

// 数值：刻度条 + 输入框；范围外或非整数时就地提示，草稿保留原样但不能保存。
function intControl(field, value) {
  const [low, high] = field.range || [null, null];
  const input = h("input", {
    class: "input num",
    type: "number",
    step: "1",
    min: low ?? null,
    max: high ?? null,
    "aria-label": field.title,
  });
  input.value = value ?? "";
  let bar = null;
  const paint = (v) => {
    if (!bar) return;
    const ratio = Number.isFinite(v) ? (v - low) / (high - low) : 0;
    const on = Math.round(Math.max(0, Math.min(1, ratio)) * 24);
    [...bar.children].forEach((i, k) => i.classList.toggle("on", k < on));
  };
  input.addEventListener("input", () => {
    const text = input.value.trim();
    const n = Number(text);
    update(field.key, text !== "" && Number.isInteger(n) ? n : text);
    paint(n);
  });
  if (field.range) {
    bar = h("div", { class: "seg", "aria-hidden": "true" });
    for (let k = 0; k < 24; k += 1) bar.append(h("i", { style: { "--k": k } }));
    requestAnimationFrame(() => paint(Number(value)));
  }
  const el = h("div", { class: "seg-input" }, [h("div", { class: "row" }, [bar, input])]);
  return { el, input, set: (v) => { input.value = v ?? ""; paint(Number(v)); } };
}

// ---------- 草稿更新 ----------
function validate(field, value) {
  if (field.type !== "int") return "";
  if (value === "" || !Number.isInteger(value)) return "请填写整数。";
  if (field.range && (value < field.range[0] || value > field.range[1])) {
    return `范围 ${field.range[0]}～${field.range[1]}。`;
  }
  return "";
}

function update(key, value) {
  const field = S.fields[key];
  const was = !same(S.draft[key], S.saved[key]);
  S.draft[key] = value;
  const { row, msg, control } = S.rows[key];
  const error = validate(field, value);
  if (error) {
    S.errors[key] = error;
    if (control.input) {
      control.input.classList.remove("err");
      void control.input.offsetWidth;
      control.input.classList.add("err");
    }
  } else {
    delete S.errors[key];
    if (control.input) control.input.classList.remove("err");
  }
  msg.textContent = error;
  const now = !same(value, S.saved[key]);
  if (now !== was) {
    row.classList.remove("flash");
    void row.offsetWidth;
    row.classList.add("flash");
    setTimeout(() => row.classList.remove("flash"), 320);
  }
  refreshMarks();
  renderSide();
  onDirty(dirtyCount());
}

function revert(key) {
  const { control } = S.rows[key];
  control.set(S.saved[key]);
  update(key, S.saved[key]);
}

function refreshMarks() {
  const dirtyGroups = new Set();
  for (const [key, { row }] of Object.entries(S.rows)) {
    const changed = !same(S.draft[key], S.saved[key]);
    row.classList.toggle("changed", changed);
    if (changed) dirtyGroups.add(groupKey(key));
  }
  $$("#cfg-anchors a").forEach((a) => {
    const has = dirtyGroups.has(a.dataset.group);
    const dot = $(".mod", a);
    if (has && !dot) a.append(h("span", { class: "mod", "aria-label": "有未保存的修改" }));
    if (!has && dot) dot.remove();
  });
}

// ---------- 右侧卡片：草稿差异 ----------
function renderSide() {
  const side = $("#cfg-side");
  const keys = changedKeys();
  if (!keys.length) {
    side.replaceChildren(h("div", { class: "side-body" }, [
      h("h3", { text: "配置" }),
      h("p", { class: "empty", text: "修改后，这里会列出所有改动，用右下角的按钮保存或放弃。" }),
      h("p", { class: "empty cfg-note", text: "这里和 AstrBot 插件管理里的配置是同一份。保存前会检查有没有在别处改过；排序页和历史压缩页上的开关也会直接改这里的值。" }),
    ]));
    return;
  }
  const titleOf = (key) => {
    const group = S.groups.find((g) => g.key === groupKey(key));
    const name = S.fields[key].title;
    return group && group.key ? `${group.title} · ${name}` : name;
  };
  const errors = Object.keys(S.errors).length;
  side.replaceChildren(h("div", { class: "side-body" }, [
    h("h3", { text: `${keys.length} 项未保存` }),
    errors ? h("p", { class: "zone-msg", text: `${errors} 项填写不正确，改好后才能保存。` }) : null,
    h("ul", { class: "diff" }, keys.map((key) => {
      const field = S.fields[key];
      return h("li", {}, [
        h("span", { class: "name", text: titleOf(key) }),
        h("span", { class: "from", text: shown(field, S.saved[key]) }),
        h("span", { class: "to", text: shown(field, S.draft[key]) }),
        btn("还原", { cls: "btn btn-ghost btn-sm cfg-revert", onClick: () => revert(key), title: "恢复为已保存的值" }),
      ]);
    })),
  ]));
}
