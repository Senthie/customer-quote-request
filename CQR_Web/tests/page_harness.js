/**
 * 前端逻辑的无头冒烟测试（不需要浏览器）。
 *
 * 为什么要有它：`templates/index.html` 里「任务列表」「产品记录 Tab」是纯原生 JS，
 * 只断言「文件里出现了某个函数名」证明不了**切换真的能工作**。这里 shim 一个最小
 * DOM，把页面脚本真跑一遍，然后按接口真实返回的形状渲染、逐项核对。
 *
 *   用法：node page_harness.js <index.html>
 *   退出码 0 = 全过；1 = 有失败（stdout 里列出哪几项）
 *
 * ⚠️ 这个文件不测视觉，只测逻辑：列表渲染了没有、Tab 是不是 3 个、切 Tab 之后
 *    标题/闸门/校验错误是不是跟着换。
 */
const fs = require("fs");
const vm = require("vm");

const pagePath = process.argv[2];
if (!pagePath) {
  console.error("用法: node page_harness.js <index.html>");
  process.exit(2);
}
const html = fs.readFileSync(pagePath, "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
if (!scripts.length) {
  console.error("页面里没有内联 <script>");
  process.exit(2);
}
const code = scripts.join("\n");

/* ── 最小 DOM shim ───────────────────────────────────────────────────── */
class Node_ {
  constructor(tag) {
    this.tagName = String(tag || "").toUpperCase();
    this.children = [];
    this.attrs = {};
    this.className = "";
    this.id = "";
    this._text = "";
    this.style = {};
    this.listeners = {};
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); }
  append(...kids) {
    for (const k of kids.flat(Infinity)) {
      if (k !== null && k !== undefined && k !== false) this.children.push(k);
    }
  }
  appendChild(k) { this.children.push(k); return k; }
  replaceChildren(...kids) { this.children = kids.flat(Infinity); }
  replaceWith() {}
  setAttribute(k, v) { this.attrs[k] = v; if (k === "class") this.className = v; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  setSelectionRange() {}
  focus() {}
  get selectionStart() { return null; }
  text() {
    let out = this._text ? this._text + " " : "";
    for (const c of this.children) {
      out += c instanceof Node_ ? c.text() : String(c) + " ";
    }
    return out;
  }
  find(cls) {
    const hits = [];
    if (String(this.className).split(/\s+/).includes(cls)) hits.push(this);
    for (const c of this.children) if (c instanceof Node_) hits.push(...c.find(cls));
    return hits;
  }
}

const registry = new Map();
const pageEl = new Node_("div");
pageEl.id = "page";
registry.set("page", pageEl);
const doc = {
  activeElement: null,
  title: "",
  createElement: (t) => new Node_(t),
  createTextNode: (s) => s,
  getElementById: (id) => {
    if (!registry.has(id)) { const n = new Node_("div"); n.id = id; registry.set(id, n); }
    return registry.get(id);
  },
};

/* ── 假接口数据（形状与 /api/exports、/api/export 完全一致）────────────── */
const REL = "tid-multi/ERP_tid-multi_20261030_010203.json";
const PRODUCTS = [
  { CPMC: "滑动发声书", CPMC_ZH: "滑动发声书", CPLB: "27.19;精装音乐BB书(线路)",
    TZS: 3, CPSL1: 182670, KHDH: "IAN 574239", cpms_len: 180, incomplete: [] },
  { CPMC: "亮灯书", CPMC_ZH: "亮灯书", CPLB: "27.18;普通音乐BB书(线路)",
    TZS: 1, CPSL1: 60890, KHDH: "IAN 574239", cpms_len: 0, incomplete: ["CPMS 未写"] },
  { CPMC: "揭页发声书", CPMC_ZH: "揭页发声书", CPLB: "27.05;普通揭页BB书",
    TZS: 2, CPSL1: 121780, KHDH: "IAN 574239", cpms_len: 0, incomplete: ["CPMS 未写"] },
];
const TASK = {
  task_key: "tid-multi", dir: "tid-multi", task_id: "tid-multi", relpath: REL,
  filename: "ERP_tid-multi_20261030_010203.json",
  source_name: "574239_Anlage.xlsm", source_name_from: "unit",
  mtime: 1_800_000_000, mtime_iso: "2026-10-30 01:02:03", size: 4000,
  superseded: false, broken: null, customer: "ngv", version: 2, ID: "tid-multi",
  key_count: 63, record_count: 3, cpms_len: 180, cpms_len_min: 0,
  incomplete: ["第2条：CPMS 未写", "第3条：CPMS 未写"],
  products: PRODUCTS, validation: { source: "run_report", error: 1, warn: 0 },
  extra_exports: [],
};
const EXPORTS = { root: "/processed", count: 1, task_count: 1,
                  exports: [TASK], tasks: [TASK] };

const RECORDS = PRODUCTS.map((p, i) => ({
  CPLB: p.CPLB, CPMC: p.CPMC, CPMC_ZH: p.CPMC_ZH, TZS: p.TZS, CPSL1: p.CPSL1,
  CD: 200, KD: 200, GD: 20, KHDH: p.KHDH, version: 2, ID: "tid-multi",
  CPMS: i === 0 ? "产品规格：封面 300g……".repeat(8) : "",
}));

const SCHEMA = {
  config_version: 2,
  erp_fields: [
    { key: "CPLB", label: "产品类别", type: "text" },
    { key: "CPMC", label: "产品名称", type: "text" },
    { key: "TZS", label: "款数", type: "number" },
    { key: "CPSL1", label: "数量1", type: "number" },
    { key: "CPMS", label: "产品描述", type: "text" },
  ],
  internal_fields: [], config_version_note: "",
  groups: [{ name: "产品信息", hint: "", keys: ["CPLB", "CPMC", "TZS", "CPSL1"] },
           { name: "描述与备注", hint: "", keys: ["CPMS"] }],
  internal_group: { name: "内部", hint: "" },
};

const ctx = {
  console,
  document: doc,
  window: { print() {} },
  Node: Node_,
  location: { search: "" },
  history: { replaceState() {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  setInterval: () => 0,
  setTimeout: (fn) => fn(),
  alert: (m) => { throw new Error("alert: " + m); },
  URLSearchParams, encodeURIComponent, JSON, Number, String, Object, Array,
  Math, Date, Map, Set, Promise,
  fetch: async (url) => {
    if (url.startsWith("api/exports")) return { ok: true, json: async () => EXPORTS };
    if (url.startsWith("api/pending")) return { ok: true, json: async () => ({ root: "", items: [] }) };
    if (url.startsWith("api/schema")) return { ok: true, json: async () => SCHEMA };
    if (url.startsWith("api/export")) {
      return { ok: true, json: async () => ({
        relpath: REL, data: RECORDS[0], records: RECORDS, record_count: RECORDS.length,
        validation: { source: "run_report", customer: "ngv", errors: [], warnings: [] },
        validation_by_record: RECORDS.map((r, i) => ({
          source: "run_report", customer: "ngv",
          errors: i === 1 ? [{ level: "error", field: "CPLB", message: "第2条的报错", record: 2 }] : [],
          warnings: [],
        })),
        incomplete: TASK.incomplete,
        incomplete_by_record: PRODUCTS.map((p) => p.incomplete),
      }) };
    }
    throw new Error("unexpected fetch " + url);
  },
};
ctx.globalThis = ctx;
vm.createContext(ctx);
vm.runInContext(
  code + "\n;globalThis.__api = {state, render, renderList, renderTabs," +
         " applyList, selectExport, setActiveRecord," +
         // v3：内部确认清单的渲染函数（render_real.js 要用真实数据看清单）
         " renderQuestions, recordQuestions};",
  ctx,
);

/* ── 断言 ─────────────────────────────────────────────────────────────── */
(async () => {
  const api = ctx.__api;
  api.state.schema = await (await ctx.fetch("api/schema")).json();
  api.applyList(EXPORTS, { items: [] });
  await api.selectExport(TASK);

  const listText = api.renderList().text();
  const tabs = api.renderTabs();
  const tabText = tabs.text();

  api.render();
  const page1 = pageEl.text();

  const checks = [
    ["列表主标题是「任务列表」", listText.includes("任务列表")],
    ["列表统计「N 个任务 · 共 M 条产品记录」", /1 个任务 · 共 3 条产品记录/.test(listText)],
    ["任务行标出产品条数", listText.includes("3 条产品")],
    ["任务行逐条列出产品", listText.includes("滑动发声书") && listText.includes("揭页发声书")],
    ["产品行带类别与数量", listText.includes("27.19;精装音乐BB书(线路)") && listText.includes("TZS 3")],
    ["详情页有 3 个 Tab", tabs.find("tab").length === 3],
    ["第 1 个 Tab 默认激活", tabs.find("tab")[0].className.includes("active")],
    ["Tab 上带类别与数量", tabText.includes("27.19;精装音乐BB书(线路)")],
    ["详情标题写明第几条", page1.includes("第 1 / 3 条产品记录")],
    ["任务级摘要（本次任务）出现", page1.includes("本次任务")],
    ["第 1 条已写 CPMS → 不出未完成横幅", !page1.includes("未完成 · 不得交付")],
  ];

  api.setActiveRecord(1);
  const page2 = pageEl.text();
  checks.push(
    ["切到第 2 条后标题跟着变", page2.includes("第 2 / 3 条产品记录")],
    ["第 2 条 CPMS 未写 → 出现未完成横幅", page2.includes("未完成 · 不得交付")],
    ["第 2 条的校验错误只在自己的 Tab 显示", page2.includes("第2条的报错")],
    ["第 2 个 Tab 打上 bad 标记", api.renderTabs().find("tab")[1].className.includes("bad")],
    ["第 1 个 Tab 不再是 active", !api.renderTabs().find("tab")[0].className.includes("active")],
  );

  let failed = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "  ✓ " : "  ✗ ") + name);
    if (!ok) failed++;
  }
  console.log(failed ? `\n${failed}/${checks.length} 项失败` : `\n${checks.length} 项全过`);
  process.exit(failed ? 1 : 0);
})();
