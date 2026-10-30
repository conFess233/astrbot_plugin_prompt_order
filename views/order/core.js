// 公共工具：bridge 请求、DOM 构造、通知、全屏面板、格式化。
import { icon } from "./icons.js";

export const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// ---------- 请求：重叠请求用计数管理顶部进度线 ----------
let pending = 0;
function loading(delta) {
  pending = Math.max(0, pending + delta);
  $("#topline").classList.toggle("on", pending > 0);
}
export async function apiGet(endpoint, params) {
  loading(1);
  try {
    return await bridge.apiGet(endpoint, params);
  } finally {
    loading(-1);
  }
}
export async function apiPost(endpoint, body) {
  loading(1);
  try {
    return await bridge.apiPost(endpoint, body);
  } finally {
    loading(-1);
  }
}
export const errText = (err) => (err && err.message) || String(err || "未知错误");

// ---------- DOM ----------
// h("div", {class: "x", onclick: fn}, [子节点或文本])；文本一律走 textContent。
export function h(tag, attrs = {}, children = []) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "style" && typeof value === "object") {
      for (const [prop, v] of Object.entries(value)) {
        if (prop.startsWith("--")) el.style.setProperty(prop, v);
        else el.style[prop] = v;
      }
    }
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  if (attrs && attrs["data-i"]) el.prepend(iconEl(attrs["data-i"]));
  return el;
}
export function iconEl(name) {
  const span = document.createElement("span");
  span.style.display = "inline-grid";
  span.innerHTML = icon(name); // 只插入静态图标常量
  return span.firstChild;
}
export function btn(label, { cls = "btn btn-ghost btn-sm", iconName, onClick, title, disabled } = {}) {
  const b = h("button", { type: "button", class: cls, title, disabled: disabled || null }, [label]);
  if (iconName) b.prepend(iconEl(iconName));
  if (onClick) b.addEventListener("click", onClick);
  return b;
}
export function chip(text, kind = "plain") {
  return h("span", { class: `chip ${kind}`, text });
}

// 按钮内加载：执行期间禁用并显示进行时文案。
export async function busy(button, label, fn) {
  const saved = [...button.childNodes];
  button.disabled = true;
  button.replaceChildren(h("span", { class: "spin" }), label);
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.replaceChildren(...saved);
  }
}

// ---------- 通知 ----------
export function toast(message, kind = "ok", action, onAction) {
  const box = h("div", { class: "toast" }, [
    h("span", { class: kind === "ok" ? "ok" : "bad" }, [iconEl(kind === "ok" ? "check" : "info")]),
    h("span", { text: message }),
  ]);
  const close = () => {
    box.classList.add("out");
    setTimeout(() => box.remove(), 200);
  };
  if (action) {
    box.append(h("button", { type: "button", text: action, onclick: () => { onAction(); close(); } }), h("span", { class: "timer" }));
  }
  $("#toasts").append(box);
  setTimeout(close, action ? 5000 : kind === "ok" ? 2600 : 5200);
}

// ---------- 全屏面板 ----------
let sheetReturn = null;
export function openSheet(title, nodes, { single = false } = {}) {
  sheetReturn = document.activeElement;
  $("#sheet-title").textContent = title;
  const body = $("#sheet-body");
  body.classList.toggle("single", single);
  body.replaceChildren(...[].concat(nodes));
  $("#scrim").classList.add("on");
  $("#sheet").classList.add("on");
  $("#sheet-close").focus();
}
export function closeSheet() {
  if (!$("#sheet").classList.contains("on")) return false;
  $("#scrim").classList.remove("on");
  $("#sheet").classList.remove("on");
  if (sheetReturn && sheetReturn.focus) sheetReturn.focus();
  return true;
}

// 确认对话框：确认按钮写明具体结果；取消不产生写入。
export function confirmBox(title, message, confirmLabel, danger = true) {
  return new Promise((resolve) => {
    const done = (value) => { closeSheet(); resolve(value); };
    const ok = btn(confirmLabel, { cls: danger ? "btn btn-danger" : "btn btn-primary", onClick: () => done(true) });
    openSheet(title, [h("div", { class: "confirm" }, [
      h("p", { text: message }),
      h("div", { class: "row" }, [btn("取消", { cls: "btn btn-ghost", onClick: () => done(false) }), ok]),
    ])], { single: true });
    ok.focus();
  });
}

// ---------- 格式化 ----------
export const num = (n) => (n === null || n === undefined ? "—" : Number(n).toLocaleString("zh-CN"));
export const pct = (r) => (r === null || r === undefined ? "—" : `${Math.round(r * 100)}%`);
export const ms = (v) => (v === null || v === undefined ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`);
export function time(ts, withDate = false) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (x) => String(x).padStart(2, "0");
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return withDate ? `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${t}` : t;
}
export function ownerColor(owner) {
  let hash = 0;
  for (const ch of owner || "") hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  return `var(--owner-${owner === "AstrBot" ? 7 : hash % 7})`;
}
export const LEVEL = { static: ["静态", "ok"], occasional: ["偶尔变化", "warn"], dynamic: ["每轮变化", "bad"] };

// 分段刻度进度条
export function segBar(ratio, n = 20, kind = "") {
  const on = Math.round(Math.max(0, Math.min(1, ratio || 0)) * n);
  const bar = h("div", { class: `seg ${kind}` });
  for (let k = 0; k < n; k += 1) {
    const i = h("i", { style: { "--k": k } });
    bar.append(i);
  }
  requestAnimationFrame(() => requestAnimationFrame(() => [...bar.children].forEach((i, k) => i.classList.toggle("on", k < on))));
  return bar;
}

export function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(() => toast("已复制"), () => toast("浏览器不允许复制，请手动选择文本", "bad"));
  }
  toast("浏览器不允许复制，请手动选择文本", "bad");
  return Promise.resolve();
}

export function downloadText(filename, text, type = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = h("a", { href: url, download: filename });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
