// 入口：等待 bridge 就绪，处理悬浮岛导航、悬浮按钮、草稿胶囊与快捷键。
import * as arrange from "./arrange.js";
import * as changes from "./changes.js";
import * as compact from "./compact.js";
import * as config from "./config.js";
import { $, $$, bridge, closeSheet, errText, iconEl, toast } from "./core.js";
import { hydrate } from "./icons.js";
import * as inspector from "./inspector.js";

const island = $("#island");
const pill = $(".pill", island);
let view = "arrange";

function movePill(button, instant) {
  if (instant) pill.style.transition = "none";
  pill.style.width = `${button.offsetWidth}px`;
  pill.style.transform = `translateX(${button.offsetLeft}px)`;
  if (instant) requestAnimationFrame(() => { pill.style.transition = ""; });
}

async function switchView(name) {
  view = name;
  $$("button", island).forEach((b) => {
    if (b.dataset.view === name) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  movePill($(`[data-view="${name}"]`, island));
  $$(".view").forEach((v) => v.classList.toggle("on", v.id === `view-${name}`));
  try {
    if (name === "inspector") await inspector.show();
    if (name === "compact") await compact.show();
    if (name === "changes") await changes.show();
    if (name === "config") await config.show();
  } catch (err) {
    toast(`加载失败：${errText(err)}`, "bad");
  }
  renderPill();
}

// ---------- 草稿胶囊：悬浮按钮展开为“放弃 / N 项未保存 / 保存” ----------
// 排序页（布局）与配置页各有草稿；胶囊优先对应当前模块，否则对应另一个有草稿的模块。
const fab = $("#fab");
const menu = $("#fab-menu");
const pillBox = $("#pill");
const dirty = { arrange: 0, config: 0 };
const PILL_TEXT = { arrange: (n) => `${n} 个布局未保存`, config: (n) => `${n} 项配置未保存` };
const SOURCES = { arrange, config };
let target = "";
let saving = false;

function pillTarget() {
  if (dirty[view]) return view;
  return Object.keys(dirty).find((k) => dirty[k]) || "";
}

function renderPill() {
  if (saving) return;
  target = pillTarget();
  if (target) $("#pill-n").textContent = PILL_TEXT[target](dirty[target]);
  if (target && !pillBox.classList.contains("on")) {
    fab.style.display = "none";
    setMenu(false);
    pillBox.style.display = "flex";
    requestAnimationFrame(() => pillBox.classList.add("on"));
  }
  if (!target && pillBox.classList.contains("on")) {
    pillBox.classList.remove("on");
    setTimeout(() => {
      if (pillBox.classList.contains("on")) return;
      pillBox.style.display = "none";
      fab.style.display = "";
    }, 320);
  }
}

function setDirty(source, n) {
  dirty[source] = n;
  if (source === "arrange" && $("#conn-dot").className === "dot") {
    $("#conn-text").textContent = arrange.statusLine();
  }
  const badge = $(`#badge-${source}`);
  badge.hidden = !n;
  badge.textContent = String(n);
  movePill($(`[data-view="${view}"]`, island));
  renderPill();
}

$("#save").addEventListener("click", async () => {
  if (!target || saving) return;
  const button = $("#save");
  const saved = [...button.childNodes];
  const source = target;
  saving = true;
  button.disabled = true;
  button.replaceChildren(Object.assign(document.createElement("span"), { className: "spin" }), "保存中");
  let ok = false;
  try {
    const message = source === "config" ? await config.save() : await arrange.save(button);
    ok = true;
    pillBox.classList.add("done");
    button.replaceChildren(iconEl("check"), "已保存");
    await new Promise((r) => setTimeout(r, 600));
    toast(message);
  } catch (err) {
    // 保存失败保留草稿，原因写在通知里，胶囊保持展开。
    toast(`保存失败：${errText(err)}`, "bad");
  } finally {
    setTimeout(() => {
      pillBox.classList.remove("done");
      button.replaceChildren(...saved);
      button.disabled = false;
      saving = false;
      renderPill();
    }, 340);
  }
  // 配置页可能改了排序页的开关，重新读取（排序页有草稿时保留草稿）。
  if (ok && source === "config") {
    try {
      await arrange.load();
    } catch (err) {
      toast(`刷新排序页失败：${errText(err)}`, "bad");
    }
  }
});
$("#discard").addEventListener("click", () => {
  if (!target || saving) return;
  SOURCES[target].discard();
  toast(target === "config" ? "已放弃配置修改" : "已放弃布局修改");
});

function setMenu(open) {
  fab.setAttribute("aria-expanded", open ? "true" : "false");
  menu.classList.toggle("on", open);
  $$("a", menu).forEach((a) => { a.tabIndex = open ? 0 : -1; });
  if (open) $("a", menu).focus();
}
fab.addEventListener("click", () => setMenu(fab.getAttribute("aria-expanded") !== "true"));
menu.addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (!a) return;
  setMenu(false);
  fab.focus();
  const act = a.dataset.act;
  if (act === "refresh") refreshAll();
  if (act === "recommend") { switchView("arrange"); arrange.recommend(); }
  if (act === "inspector") switchView("inspector");
  if (act === "search") { switchView("inspector").then(inspector.focusSearch); }
});
menu.addEventListener("keydown", (e) => {
  const items = $$("a", menu);
  const idx = items.indexOf(document.activeElement);
  if (e.key === "ArrowDown") { e.preventDefault(); items[(idx + 1) % items.length].focus(); }
  if (e.key === "ArrowUp") { e.preventDefault(); items[(idx - 1 + items.length) % items.length].focus(); }
  if (e.key === "Enter" && idx >= 0) { e.preventDefault(); items[idx].click(); }
});

async function refreshAll() {
  try {
    await arrange.load();
    if (view === "inspector") await inspector.refresh();
    if (view === "compact") await compact.show();
    if (view === "changes") await changes.show();
    if (view === "config") await config.show();
    const kept = view === "config" && config.dirtyCount();
    toast(kept ? "数据已刷新；配置有未保存的修改，未重新读取" : "数据已刷新");
  } catch (err) {
    toast(`刷新失败：${errText(err)}`, "bad");
  }
}

document.addEventListener("keydown", (e) => {
  const typing = /input|textarea|select/i.test(document.activeElement && document.activeElement.tagName);
  if (e.key === "Escape") {
    if (closeSheet()) return;
    if (menu.classList.contains("on")) { setMenu(false); fab.focus(); }
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "r" || e.key === "R") refreshAll();
  if (e.key === "/") { e.preventDefault(); switchView("inspector").then(inspector.focusSearch); }
});
$("#sheet-close").addEventListener("click", closeSheet);
$("#scrim").addEventListener("click", closeSheet);
$("#refresh").addEventListener("click", refreshAll);
$$("button", island).forEach((b) => b.addEventListener("click", () => switchView(b.dataset.view)));
window.addEventListener("resize", () => movePill($(`[data-view="${view}"]`, island), true));
window.addEventListener("beforeunload", (e) => {
  if (arrange.dirtyCount() || config.dirtyCount()) { e.preventDefault(); e.returnValue = ""; }
});

// ---------- 启动 ----------
async function boot() {
  hydrate(document);
  movePill($(`[data-view="${view}"]`, island), true);
  arrange.init();
  inspector.init();
  arrange.setDirtyListener((n) => setDirty("arrange", n));
  config.setDirtyListener((n) => setDirty("config", n));
  try {
    await bridge.ready();
    await arrange.load();
    $("#conn-dot").className = "dot";
    $("#conn-text").textContent = arrange.statusLine();
  } catch (err) {
    $("#conn-dot").className = "dot bad";
    $("#conn-text").textContent = `连接失败：${errText(err)}`;
  }
}

boot();
