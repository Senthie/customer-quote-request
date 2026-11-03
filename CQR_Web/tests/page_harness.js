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
    /* ⚠️ shim 必须照抄浏览器**原生** ParentNode.append() 的行为：它**不过滤** null /
       undefined / false，会把它们统统变成文本节点（页面上就出现字面的「null」）。
       过滤 null 是 el() 的职责 —— el() 在调用 append 之前就筛过了。
       这里以前做过「跳过 null」，结果页面里 `x.append(a, cond ? b : null)` 这种写法
       在测试里永远绿、到浏览器里长出「1null」「0null」（2026-10-30 业务报的真 bug）。 */
    for (const k of kids.flat(Infinity)) this.children.push(k);
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

/* ★ v3：第 1 条记录带内部确认清单（其余两条故意不带 → 顺带验「旧记录不显示清单块」） */
const QA_ITEMS = [
  { id: "Q_ZDFS", field: "ZDFS", question: "装订方式为空（建议填写）", answer: "",
    status: "open", blocking: false, source: "pipeline",
    asked_at: "2026-10-30", answered_at: null },
  { id: "Q_PACK", field: "CPMS", question: "装箱几本一箱？", answer: "",
    status: "open", blocking: true, source: "agent",
    asked_at: "2026-10-30", answered_at: null },
];
RECORDS[0].schema_version = 3;
RECORDS[0].Reminder = "装箱几本一箱？";
RECORDS[0].question_answers = QA_ITEMS;

/* POST /api/answer 收到的请求体（断言「页面到底发了什么」用） */
let postedAnswer = null;

const SCHEMA = {
  config_version: 2,
  erp_fields: [
    { key: "CPLB", label: "产品类别", type: "text" },
    { key: "CPMC", label: "产品名称", type: "text" },
    { key: "TZS", label: "款数", type: "number" },
    { key: "CPSL1", label: "数量1", type: "number" },
    { key: "CPMS", label: "产品描述", type: "text" },
    /* ★ 联动下拉（业务 2026-10-30）：SHDQ 送货地区的候选值随 YSLX 运输类型变。
       legal = 全集（10 个，香港两组都有），legal_groups = 分组关系。 */
    { key: "YSLX", label: "运输类型", type: "text" },
    { key: "SHDQ", label: "送货地区", type: "text",
      legal: ["FOB盐田(普通)", "香港", "FOB(美加线)", "FOB(NGV)", "DPU", "日本（Tokyo）",
              "FOB蛇口", "工厂自提", "东莞凤岗", "深圳"],
      linked_to: "YSLX",
      legal_groups: [
        { when: ["FOB盐田(普通)", "FOBHK", "FOB(拼柜)", "DPU", "CIF",
                 "FOB(底价)", "FOB蛇口", "FOB(美加线)"],
          options: ["FOB盐田(普通)", "香港", "FOB(美加线)", "FOB(NGV)", "DPU",
                    "日本（Tokyo）", "FOB蛇口"] },
        { when: ["国内"], options: ["工厂自提", "香港", "东莞凤岗", "深圳"] },
      ] },
  ],
  internal_fields: [
    { key: "ID", label: "任务ID", source: "task_id", kind: "value" },
    { key: "question_answers", label: "内部确认清单", source: "question_answers",
      kind: "questions" },
  ],
  config_version_note: "",
  groups: [{ name: "产品信息", hint: "", keys: ["CPLB", "CPMC", "TZS", "CPSL1"] },
           { name: "描述与备注", hint: "", keys: ["CPMS"] }],
  internal_group: { name: "内部", hint: "" },
};

const ctx = {
  console,
  document: doc,
  window: {
    print() {},
    // beforeprint（打印前展开清单）等钩子：存下来，harness 可以手动触发
    _listeners: {},
    addEventListener(name, fn) { (this._listeners[name] = this._listeners[name] || []).push(fn); },
    removeEventListener(name, fn) {
      const arr = this._listeners[name] || [];
      this._listeners[name] = arr.filter((f) => f !== fn);
    },
    fire(name) { for (const fn of this._listeners[name] || []) fn({ type: name }); },
  },
  Node: Node_,
  location: { search: "" },
  history: { replaceState() {} },
  localStorage: { getItem: () => null, setItem: () => {} },
  setInterval: () => 0,
  setTimeout: (fn) => fn(),
  alert: (m) => { throw new Error("alert: " + m); },
  URLSearchParams, encodeURIComponent, JSON, Number, String, Object, Array,
  Math, Date, Map, Set, Promise,
  fetch: async (url, opts) => {
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
    if (url.startsWith("api/answer")) {
      const body = JSON.parse((opts && opts.body) || "{}");
      postedAnswer = body;
      const rec = RECORDS[Math.max(0, (body.record || 1) - 1)];
      const it = (rec.question_answers || []).find((q) => q.id === body.id);
      if (it) {
        const answer = String(body.answer || "").trim();
        it.answer = answer;
        it.status = body.status || (answer ? "confirmed" : "open");
        it.answered_at = it.status === "confirmed" ? "2026-11-02" : null;
      }
      return { ok: true, json: async () => ({
        item: it, summary: { total: 2, open: 1, open_blocking: 0, confirmed: 1, waived: 0 },
        gates: [], reminder: "", mtime: 1_800_000_100,
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
         " renderQuestions, recordQuestions, saveAnswer, answerKey, canWriteAnswers," +
         // v3：折叠状态（默认折叠，业务 2026-10-30）
         " qaOpenKey, qaIsOpen, toggleQuestions, firstQuestionPeek," +
         // 联动下拉（SHDQ ← YSLX）：候选值按父字段筛
         " legalForField, specOf};",
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

  /* ── v3：内部确认清单 + 页面上写答案（2026-10-30）────────────────── */
  api.setActiveRecord(0);
  api.state.scanOk = true;
  api.render();

  // 先验「默认折叠」（业务 2026-10-30：「内部确认清单 默认是折叠状态」）
  const countTag = (tag) => {
    let n = 0;
    (function walk(node) {
      for (const c of node.children || []) {
        if (c && c.tagName === tag) n += 1;
        if (c && c.children) walk(c);
      }
    })(pageEl);
    return n;
  };
  const qaHead = pageEl.find("qa-head")[0];
  checks.push(
    ["默认折叠：清单标题还在（能看见有这块）", pageEl.text().includes("内部确认清单")],
    ["默认折叠：徽章照样报未确认项数", /2 项未确认/.test(pageEl.text())],
    ["默认折叠：正文没构建（0 个文本框）", countTag("TEXTAREA") === 0],
    ["默认折叠：标题行 aria-expanded=false", qaHead && qaHead.attrs["aria-expanded"] === "false"],
    ["默认折叠：标题行可点（role=button + 有 click 监听）",
      qaHead && qaHead.attrs.role === "button" && typeof qaHead.listeners.click === "function"],
    ["默认折叠：给出首条未确认项的预览", /Q_ZDFS · 装订方式为空/.test(pageEl.text())],
    ["默认折叠：折叠状态按「产物#第几条」记",
      api.qaIsOpen(0) === false && api.qaOpenKey(0) === REL + "#1"],
  );

  // 点标题行 → 展开
  qaHead.listeners.click();
  checks.push(
    ["点标题行能展开", api.qaIsOpen(0) === true && countTag("TEXTAREA") === 2],
    ["展开后 aria-expanded=true", pageEl.find("qa-head")[0].attrs["aria-expanded"] === "true"],
    ["展开后提示语换回「待办只记这里」", pageEl.text().includes("待办只记这里")],
  );

  // 展开状态要活过 10 秒自动刷新（整体重建 DOM 不能把块合上）
  api.render();
  checks.push(["自动刷新重建 DOM 后保持展开", countTag("TEXTAREA") === 2]);

  const pageQ = pageEl.text();
  const qaInputs = (() => {
    const hits = [];
    (function walk(n) {
      for (const c of n.children || []) {
        if (c && c.tagName === "TEXTAREA") hits.push(c);
        if (c && c.children) walk(c);
      }
    })(pageEl);
    return hits;
  })();
  const saveBtns = (() => {
    const hits = [];
    (function walk(n) {
      for (const c of n.children || []) {
        if (c && String(c.className).includes("qa-save")) hits.push(c);
        if (c && c.children) walk(c);
      }
    })(pageEl);
    return hits;
  })();

  checks.push(
    ["详情页出现内部确认清单块", pageQ.includes("内部确认清单")],
    ["清单显示未确认项数", /2 项未确认/.test(pageQ)],
    ["每个确认项都有答案文本框", qaInputs.length === 2],
    ["每个确认项都有保存按钮", saveBtns.length === 2],
    ["清单不会当普通字段渲染", !pageQ.includes("[object Object]")],
  );

  // 折叠回去 → 文本框消失，但已经打的草稿还在
  const keyDraft = api.answerKey(0, "Q_PACK");
  qaInputs[1].listeners.input({ target: { value: "12 本/箱" } });
  pageEl.find("qa-head")[0].listeners.click();
  checks.push(
    ["再点一次折叠回去", api.qaIsOpen(0) === false && countTag("TEXTAREA") === 0],
    ["折叠不影响已打的草稿", api.state.drafts[keyDraft] === "12 本/箱"],
  );
  // 后面还要验打字/保存 → 重新展开
  pageEl.find("qa-head")[0].listeners.click();
  checks.push(["重新展开", countTag("TEXTAREA") === 2]);

  // 打印前自动展开：折叠时 body 根本没构建，否则打印出来只有一个标题
  pageEl.find("qa-head")[0].listeners.click();      // 先折叠
  ctx.window.fire("beforeprint");
  checks.push(["打印前自动展开清单", api.qaIsOpen(0) === true && countTag("TEXTAREA") === 2]);

  // 没有 question_answers 的旧记录 → 不出现清单块（v2 零噪音）
  api.setActiveRecord(1);
  checks.push(["旧记录（无清单）不显示清单块", !pageEl.text().includes("内部确认清单")]);
  api.setActiveRecord(0);
  api.render();

  // 打字 → 进草稿（自动刷新重建 DOM 时靠它不丢字）
  const key2 = api.answerKey(0, "Q_PACK");
  qaInputs[1].listeners.input({ target: { value: "12 本/箱" } });
  checks.push(["输入答案会存进草稿", api.state.drafts[key2] === "12 本/箱"]);

  // 点保存 → 页面发了什么（path/record/id/answer/mtime 四样都得对）
  await saveBtns[1].listeners.click();
  checks.push(
    ["保存时发的 path 是当前产物", postedAnswer && postedAnswer.path === REL],
    ["保存时发的 record 从 1 起", postedAnswer && postedAnswer.record === 1],
    ["保存时发的 id 是正确的确认项", postedAnswer && postedAnswer.id === "Q_PACK"],
    ["保存时发的 answer 是输入框内容", postedAnswer && postedAnswer.answer === "12 本/箱"],
    ["保存时带上乐观锁 mtime", postedAnswer && postedAnswer.mtime === 1_800_000_000],
    ["保存成功后清掉草稿", api.state.drafts[key2] === undefined],
    ["保存后乐观锁换成服务端的新 mtime", api.state.currentMeta.mtime === 1_800_000_100],
    ["服务端回写的答案进了页面数据",
      RECORDS[0].question_answers[1].status === "confirmed"
      && RECORDS[0].question_answers[1].answer === "12 本/箱"],
    ["保存后徽章变成 1 项未确认", /1 项未确认/.test(pageEl.text())],
  );

  /* ── 版本徽章的 null 安全（业务 2026-10-30 报的「很多的 null」）──────────
     老产物 JSON 里是 `"version": null`（不是缺键）。页面原来只判 undefined，
     于是徽章拼成「vnull」—— 列表里几十个任务能看到一串 null。 */
  const savedVer = TASK.version;
  TASK.version = null;
  api.applyList(EXPORTS, { items: [] });
  const nullVerList = api.renderList().text();
  TASK.version = savedVer;
  api.applyList(EXPORTS, { items: [] });
  checks.push(
    ["version 为 null 时不再渲染 vnull", !nullVerList.includes("vnull")],
    ["version 为 null 时标成「v1（无 version）」", nullVerList.includes("v1（无 version）")],
  );

  /* ── 数字字段后面不许跟字面 null（业务 2026-10-30：「值明明存在值，还是会出现 null」）
     根因：`line.append(span, 条件 ? 徽章 : null)` —— 浏览器原生 append() 不过滤 null，
     会把它当文本节点插进去 → 「1null」「0null」。所以这里直接抠出该字段的值格文本看。 */
  const numField = (key) => {
    const f = pageEl.find("field").find((x) => {
      const lbl = x.find("field-label")[0];
      return lbl && lbl.text().includes(key);
    });
    const v = f && f.find("field-value")[0];
    return v ? v.text() : "";
  };
  const tzsCell = numField("TZS");        // 假数据 TZS=3 → 短数字（≤4 字符）走 null 分支
  const cpslCell = numField("CPSL1");     // 假数据 CPSL1=182670 → 长数字走千分位徽章分支
  checks.push(
    ["短数字字段值后面没有 null（TZS=3）", tzsCell.includes("3") && !/null/i.test(tzsCell)],
    ["长数字字段值后面没有 null（CPSL1=182670）", /182,670/.test(cpslCell) && !/null/i.test(cpslCell)],
    ["有值的字段整页不会出现「值+null」", !/\d\s*null/i.test(pageEl.text())],
  );

  /* ── 联动下拉：SHDQ 送货地区的候选值随 YSLX 运输类型变（业务 2026-10-30）
     页面只做**展示筛选**：父字段有值 → 只列该组；父字段空 → 全集并注明「随…变化」。
     合法性判定仍由服务端 validate_result 负责（前端不重写枚举判定）。 */
  const shdqSpec = api.specOf("SHDQ");
  const savedCur = api.state.current;
  api.state.current = { YSLX: "国内" };
  const domSites = api.legalForField(shdqSpec);
  api.state.current = { YSLX: "FOB(拼柜)" };
  const tradeSites = api.legalForField(shdqSpec);
  api.state.current = {};
  const allSites = api.legalForField(shdqSpec);
  api.state.current = savedCur;
  checks.push(
    ["SHDQ 联动：国内 → 只剩 4 个国内送货点",
      domSites.values.join("|") === "工厂自提|香港|东莞凤岗|深圳"],
    ["SHDQ 联动：标题点明是按运输类型筛的", domSites.title.includes("运输类型")],
    ["SHDQ 联动：外贸 → 7 个（含 FOB(NGV) 与全角 日本（Tokyo））",
      tradeSites.values.length === 7 && tradeSites.values.includes("FOB(NGV)")
      && tradeSites.values.includes("日本（Tokyo）")],
    ["SHDQ 联动：外贸那组不含国内送货点", !tradeSites.values.includes("工厂自提")],
    ["SHDQ 联动：父字段空着 → 全集 10 个并注明「随…变化」",
      allSites.values.length === 10 && allSites.title.includes("随")],
    ["SHDQ 联动：非联动字段照旧给全集标题",
      api.legalForField(api.specOf("CPLB")).values.length === 0],
  );

  let failed = 0;
  for (const [name, ok] of checks) {
    console.log((ok ? "  ✓ " : "  ✗ ") + name);
    if (!ok) failed++;
  }
  console.log(failed ? `\n${failed}/${checks.length} 项失败` : `\n${checks.length} 项全过`);
  process.exit(failed ? 1 : 0);
})();
