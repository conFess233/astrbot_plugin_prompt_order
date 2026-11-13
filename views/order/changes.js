// 改动记录页：钩子改动记录与覆盖范围两种显示，用分段切换。
import { $, $$, apiGet, chip, errText, h, iconEl, ownerColor, time, toast } from "./core.js";

let samples = [];
let selected = "";
let mode = "changes";
let wired = false;

function wire() {
  if (wired) return;
  wired = true;
  $$("#chg-mode span").forEach((span) => {
    const pick = () => {
      if (span.dataset.mode === mode) return;
      mode = span.dataset.mode;
      show().catch((err) => toast(`加载失败：${errText(err)}`, "bad"));
    };
    span.addEventListener("click", pick);
    span.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); }
    });
  });
}

export async function show() {
  wire();
  $$("#chg-mode span").forEach((span) => {
    const on = span.dataset.mode === mode;
    span.classList.toggle("on", on);
    span.setAttribute("aria-checked", on ? "true" : "false");
  });
  $("#chg-pane-changes").hidden = mode !== "changes";
  $("#chg-pane-coverage").hidden = mode !== "coverage";
  $("#h-changes").textContent = mode === "changes" ? "钩子改动记录" : "覆盖范围";
  if (mode === "changes") await loadChanges();
  else await loadCoverage();
}

async function loadChanges() {
  try {
    samples = (await apiGet("samples", { limit: 100 })).items || [];
  } catch (err) {
    $("#chg-rows").replaceChildren(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: `读取失败：${errText(err)}` })]));
    return;
  }
  const body = $("#chg-rows");
  if (!samples.length) {
    body.replaceChildren(h("tr", {}, [h("td", { colspan: "4", class: "empty", text: "还没有样本。插件启用后，每次主对话请求都会留下一份构建记录。" })]));
  } else {
    body.replaceChildren(...samples.map((s) => {
      const tr = h("tr", { class: s.id === selected ? "sel" : "", tabindex: "0", dataset: { id: s.id } }, [
        h("td", { class: "mono", text: time(s.ts, true) }),
        h("td", {}, [h("span", { text: s.umo }), h("span", { class: "sub", text: s.title || "" })]),
        h("td", { text: s.layout_id || "—" }),
        h("td", {}, [s.late_changes ? chip(`重排后改动 ${s.late_changes}`, "bad") : s.arranged ? chip("已重排", "ok") : chip("未改写", "plain")]),
      ]);
      tr.addEventListener("click", () => select(s.id));
      tr.addEventListener("keydown", (e) => { if (e.key === "Enter") select(s.id); });
      return tr;
    }));
  }
  if (!selected) renderSide(null);
}

async function select(id) {
  selected = id;
  $$("#chg-rows tr").forEach((tr) => tr.classList.toggle("sel", tr.dataset.id === id));
  try {
    renderSide(await apiGet("sample", { id }));
  } catch (err) {
    toast(`读取样本失败：${errText(err)}`, "bad");
  }
}

function renderSide(sample) {
  const side = $("#chg-side");
  if (!sample) {
    side.replaceChildren(h("div", { class: "side-body" }, [h("h3", { text: "改动记录" }),
      h("p", { class: "empty", text: "选择左侧一个样本，查看构建阶段每个钩子按执行顺序做了什么：追加、在中间插入、删改了谁的内容、改写历史或原话。标红的是在重排之后发生、没有参与排序的改动。" })]));
    return;
  }
  const runs = (sample.items && sample.items.runs) || [];
  const owners = [...new Set(runs.map((r) => r.owner))];
  const changes = sample.changes || [];
  side.replaceChildren(h("div", { class: "side-body" }, [
    h("h3", { text: sample.title || sample.umo }),
    h("dl", { class: "kv" }, [
      h("dt", { text: "会话" }), h("dd", { class: "mono", text: sample.umo }),
      h("dt", { text: "布局" }), h("dd", { text: sample.layout_id || "—" }),
      h("dt", { text: "结果" }), h("dd", { text: sample.note || "—" }),
    ]),
    ...(sample.degraded || []).map((t) => h("ul", { class: "warn-list" }, [h("li", { class: "warn" }, [iconEl("info"), h("span", { text: t })])])),
    h("div", { class: "section" }, [h("h4", { text: `钩子改动（${changes.length}）` }),
      changes.length ? h("ul", { class: "timeline" }, changes.map((c) => h("li", { class: c.after_arrange ? "bad" : "ok" }, [
        h("strong", { text: c.owner }), ` ${c.summary}`, h("small", { text: `${c.handler} · ${kindLabel(c.kind)}${c.after_arrange ? " · 重排之后" : ""}` })])))
        : h("p", { class: "empty", text: "这次请求没有插件修改提示词。" })]),
    h("div", { class: "section" }, [h("h4", { text: "SYSTEM 来源表" }),
      h("div", { class: "legend" }, owners.map((o) => h("span", {}, [h("i", { style: { "--c": ownerColor(o) } }), o]))),
      h("div", { class: "runs" }, runs.map((r) => h("span", { style: { "--c": ownerColor(r.owner) }, title: r.owner, text: r.text })))]),
    h("div", { class: "section" }, [h("h4", { text: "附加片段" }),
      h("div", { class: "msg-list" }, ((sample.items && sample.items.tail) || []).map((t) => h("details", {}, [
        h("summary", {}, [chip(t.owner), chip(t.media ? "媒体" : t.temp ? "不存档" : "写入历史", t.temp ? "plain" : "info"), h("span", { class: "grow", text: t.text.slice(0, 80) })]),
        h("pre", { class: "code", text: t.text })])))]),
  ]));
}

function kindLabel(kind) {
  return { system: "system", tail: "附加片段", tools: "工具", prompt: "用户原话", contexts: "历史" }[kind] || kind;
}

// ---------- 覆盖范围 ----------
const COVERAGE = [
  ["AstrBot 内置流程（人格、工具提示、安全模式、技能、知识库、引用……）", "按已知标记细分；认不出的归“内置（未细分）”", "能"],
  ["任意插件的 on_llm_request 钩子", "准确到插件，新装的插件自动识别", "能"],
  ["本插件之后执行的钩子", "能检测到，在改动记录里标红", "不能，已经排完"],
  ["改写历史（contexts）或用户原话（prompt）", "只记录", "按设计不排"],
  ["工具调用循环里的工具结果", "—", "不在构建阶段"],
  ["在钩子之外改请求（后台任务、直接修改 provider）", "看不到", "不能"],
  ["旁路调用（插件自己调用模型）", "检查器可记录（需打开开关）", "不参与排序"],
];

async function loadCoverage() {
  $("#cov-rows").replaceChildren(...COVERAGE.map(([a, b, c]) => h("tr", {}, [h("td", { text: a }), h("td", { text: b }), h("td", { text: c })])));
  const side = $("#cov-side");
  try {
    const [state, status, list] = await Promise.all([apiGet("state"), apiGet("inspector/status"), apiGet("samples", { limit: 100 })]);
    const items = list.items || [];
    const late = items.filter((s) => s.late_changes).length;
    const providers = Object.entries(status.providers || {});
    side.replaceChildren(h("div", { class: "side-body" }, [
      h("h3", { text: "本次运行检测到" }),
      h("dl", { class: "kv" }, [
        h("dt", { text: "追踪钩子" }), h("dd", { text: `${state.wrapped_hooks} 个` }),
        h("dt", { text: "重排后改动" }), h("dd", {}, [late ? chip(`最近 ${items.length} 个样本中有 ${late} 个`, "bad") : chip("未发现", "ok")]),
        h("dt", { text: "线路层" }), h("dd", { text: providers.length ? `${providers.filter(([, m]) => m === "httpx").length} / ${providers.length} 个 provider 取得实际请求` : "还没有包装 provider" }),
      ]),
      h("p", { class: "empty", style: { marginTop: "12px" }, text: "表中“不能”的几类改动不会出现在排序里；如果某个插件在本插件之后修改请求，可以在改动记录里看到它的名字。" }),
    ]));
  } catch (err) {
    side.replaceChildren(h("p", { class: "empty", text: `读取状态失败：${errText(err)}` }));
  }
}
