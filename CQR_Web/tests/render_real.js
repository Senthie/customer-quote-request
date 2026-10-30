/**
 * 用**真实产物**跑一遍页面逻辑（不需要浏览器）。
 *
 * `page_harness.js` 用的是假数据（钉住回归）；这个脚本复用它的最小 DOM shim，
 * 但数据全部来自正在跑的局域网服务（/api/exports、/api/export、/api/schema），
 * 所以能回答「我这份真产物在页面上到底长什么样」——比如三条记录的 Tab 有没有出来。
 *
 *   用法：node render_real.js <index.html> [base] [relpath]
 *   默认 base = http://127.0.0.1:8765，relpath = 取任务列表里最新那个任务
 *
 * 只打印，不做断言（退出码 0 = 渲染过程没抛异常）。
 */
const fs = require("fs");
const http = require("http");

const pagePath = process.argv[2];
const BASE = process.argv[3] || "http://127.0.0.1:8765";
let RELPATH = process.argv[4] || "";
if (!pagePath) {
  console.error("用法: node render_real.js <index.html> [base] [relpath]");
  process.exit(2);
}

function getJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      let buf = "";
      res.on("data", (c) => (buf += c));
      res.on("end", () => {
        try { resolve(JSON.parse(buf)); } catch (e) { reject(new Error(url + " → " + e.message)); }
      });
    }).on("error", reject);
  });
}

/* ── 只取 harness 的 shim 部分（断言段之前），并在末尾把内部变量挂出来 ── */
const HARNESS = __dirname + "/page_harness.js";
const harnessSrc = fs.readFileSync(HARNESS, "utf8");
const cut = harnessSrc.indexOf("/* ── 断言");
if (cut < 0) { console.error("page_harness.js 结构变了：找不到断言段"); process.exit(2); }
eval(harnessSrc.slice(0, cut) + "\n;globalThis.__H = {ctx, EXPORTS, RECORDS, TASK, SCHEMA, pageEl};");

const H = globalThis.__H;
const ctx = H.ctx;

/* harness 的 shim 是给「5 个字段」的假 schema 用的；真实 schema 有 63 个字段 + 分组提示，
   页面会 querySelector 找徽章位置。这里按「.class / tag」做最小实现。 */
const N = ctx.Node;
function matches(el, sel) {
  if (!(el instanceof N)) return false;
  if (sel.startsWith(".")) return String(el.className).split(/\s+/).includes(sel.slice(1));
  return el.tagName === sel.toUpperCase();
}
function walk(root, sel, all) {
  const out = [];
  const stack = [...root.children];
  while (stack.length) {
    const c = stack.shift();
    if (!(c instanceof N)) continue;
    if (matches(c, sel)) { if (!all) return c; out.push(c); }
    stack.push(...c.children);
  }
  return all ? out : null;
}
N.prototype.querySelector = function (sel) { return walk(this, sel, false); };
N.prototype.querySelectorAll = function (sel) { return walk(this, sel, true); };
N.prototype.closest = () => null;
N.prototype.getAttribute = function (k) { return this.attrs[k]; };
N.prototype.contains = () => false;

(async () => {
  const schema = await getJSON(BASE + "/api/schema");
  const exportsPayload = await getJSON(BASE + "/api/exports");
  const tasks = exportsPayload.tasks || [];
  if (!RELPATH) {
    if (!tasks.length) { console.error("任务列表是空的"); process.exit(1); }
    RELPATH = tasks[0].relpath;
  }
  const detail = await getJSON(BASE + "/api/export?path=" + encodeURIComponent(RELPATH));

  ctx.fetch = async (url) => {
    if (url.startsWith("api/exports")) return { ok: true, json: async () => exportsPayload };
    if (url.startsWith("api/schema")) return { ok: true, json: async () => schema };
    if (url.startsWith("api/pending")) return { ok: true, json: async () => ({ root: "", items: [] }) };
    if (url.startsWith("api/export")) return { ok: true, json: async () => detail };
    throw new Error("unexpected fetch " + url);
  };

  const api = ctx.__api;
  api.state.schema = schema;
  api.applyList(exportsPayload, { items: [] });

  const task = tasks.find((t) => t.relpath === RELPATH);
  await api.selectExport(task);

  console.log("=== 任务列表（真实数据） ===");
  console.log(api.renderList().text().replace(/\s+/g, " ").slice(0, 600));

  const tabs = api.renderTabs();
  const tabEls = tabs.find("tab");
  console.log("\n=== 产品记录 Tab ===");
  console.log("Tab 数量:", tabEls.length);
  tabEls.forEach((t, i) => console.log(`  [${i + 1}] class="${t.className}" ${t.text().replace(/\s+/g, " ").slice(0, 80)}`));

  console.log("\n=== 逐条渲染 ===");
  api.render();
  const n = (detail.records || []).length;
  for (let i = 0; i < n; i++) {
    if (i) api.setActiveRecord(i);
    const text = H.pageEl.text().replace(/\s+/g, " ");
    const head = (text.match(/第 \d+ \/ \d+ 条产品记录/) || ["（没找到『第 N / M 条』标题）"])[0];
    const banner = text.includes("未完成 · 不得交付") ? "有未完成横幅" : "无未完成横幅";
    const cpmc = (text.match(/产品名称\s*(\S+)/) || ["", "?"])[1];
    console.log(`  [${i + 1}] ${head} | ${cpmc} | ${banner} | Tab.class="${tabEls[i].className}"`);
  }
})().catch((e) => { console.error("渲染失败:", e); process.exit(1); });
