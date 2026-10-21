#!/usr/bin/env python3
"""
main.py - 客户报价请求处理主入口

把「任意客户的报价文件」转换成 ERP 导入用的 59 字段 JSON。

多客户架构（2026-09-24 重构）
--------------------------------------------------------------------
本文件**不含任何客户的列名**。客户的习惯全部声明在 `customers/<id>.yaml`：

    邮件/附件  →  ingest_email.py（取附件 + 客户单号）
                        ↓
    客户文件   →  extract_fields.py（按档案抽成「规范键 → 值」）
                        ↓
    map_fields →  规范键 + 档案规则 + ERP 模板  →  59 字段 ERP JSON
                        ↓
    CPMS       →  ★ 脚本不生成，必须由 agent 亲自通读原文、翻译、分段后写回

新增客户不需要改这个文件，只需加一份档案；只有当客户的**表格布局是全新形态**
时才需要在 extract_fields.py 加布局策略。见 customers/README.md。
--------------------------------------------------------------------
"""
import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cplb_tree  # noqa: E402  （ERP 产品类别树）
from customer_profile import CustomerProfile, resolve_profile  # noqa: E402
from erp_schema import (declared_exceptions, normalize_result,  # noqa: E402
                        normalize_value, validate_result)
from extract_fields import (CustomerData, load_customer_data,  # noqa: E402
                            load_customer_unit)
from ingest_email import newest_message, read_attachment_meta  # noqa: E402
from input_unit import (InputUnit, finalize_batch, find_unit_meta,  # noqa: E402
                        resolve_input_unit, resolve_input_units)
from internal_fields import internal_field_keys, load_internal_fields  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "assets" / "add_customer_quote_request.json"

# 客户校准语料库（真实客户原文，**不进仓库** —— .gitignore 里有它）
SAMPLES_DIR = REPO_ROOT / "customer-files" / "samples"

# 布局/取值兜底常量
DEFAULT_UNIT = "mm"
# 单文件通道支持的格式（文件夹/压缩包里的成员由 input_unit 逐个筛，规则一致）
SUPPORTED_INPUT_SUFFIXES = (".csv", ".xlsx", ".xls", ".docx", ".txt", ".pdf")

# ── 运行基准日期 ─────────────────────────────────────────────────────────
# 「今天是几号」会影响两类结果：报价截止日期是否过期、WCRQ 的 today_plus_days。
# 用 `main.py --today YYYY-MM-DD` 覆盖，让回归测试与补跑可复现（否则测试结果
# 会随时间漂移：今天没过期、明天就过期了）。
_TODAY: Optional[date] = None


def today() -> date:
    """运行基准日期（默认系统当天，--today 可覆盖）。"""
    return _TODAY or datetime.now(timezone.utc).astimezone().date()


class Logger:
    """日志记录器"""

    def __init__(self, log_file: Optional[Path] = None):
        self.log_file = log_file
        self.start_time = datetime.now()

    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(self, message: str, level: str = "INFO"):
        log_line = f"[{self._timestamp()}] [{level}] {message}"
        print(log_line)
        if self.log_file:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_line + "\n")

    def info(self, message: str):
        self.log(message, "INFO")

    def warn(self, message: str):
        self.log(message, "WARN")

    def error(self, message: str):
        self.log(message, "ERROR")

    def debug(self, message: str):
        self.log(message, "DEBUG")

    def section(self, title: str):
        self.info("")
        self.info(f"========== {title} ==========")

    def field_mapping(self, field_name: str, index: int, total: int):
        self.info("")
        self.info(f"[字段 {index}/{total}] {field_name}")

    def elapsed_time(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()


# ── 查表类工具（1.0.42 内联）──────────────────────────────────────────────
# 以前这三张表各有一个独立脚本，main.py 用 `subprocess + uv run` 去查：
# 一次运行要起 4~8 个进程（每个 ~0.4s），还硬依赖 PATH 里有 uv、且工作目录对。
# 表本身只有几十行，直接读更省事、也更好查错。逻辑与那四个脚本逐字段等价，
# 由 tests/test_lookup_tables.py 对着原表逐行钉住。
PAGE_THICKNESS_PATH = REPO_ROOT / "assets" / "page_thickness.xlsx"
PRODUCT_CATEGORIES_PATH = REPO_ROOT / "assets" / "product_categories.xlsx"

_MONTH_ABBR = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
               "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

_table_cache: dict = {}


def _load_table(path: Path, required: list, logger: "Logger",
                csv_fallback: bool = False) -> Any:
    """读一张查询表（缺列 / 读不出来 → 返回 None 并 WARN）。同进程内缓存。"""
    key = str(path)
    if key in _table_cache:
        return _table_cache[key]

    import pandas as pd

    df = None
    source = path
    if not source.exists() and csv_fallback:
        csv_path = source.with_suffix(".csv")
        if csv_path.exists():
            source = csv_path
    try:
        if not source.exists():
            logger.warn(f"  查表失败：{source.name} 不存在")
        else:
            df = pd.read_csv(source) if source.suffix.lower() == ".csv" else pd.read_excel(source)
            missing = [c for c in required if c not in df.columns]
            if missing:
                logger.warn(f"  查表失败：{source.name} 缺少列 {missing}")
                df = None
    except Exception as exc:                                  # 表坏了不该炸整单
        logger.warn(f"  查表失败：{source.name} 读不出来（{exc}）")
        df = None

    _table_cache[key] = df
    return df


def _cell(value: Any) -> str:
    """表里的单元格 → 干净字符串（NaN/None → 空串，整数值不带 .0）。"""
    try:
        if value is None or (isinstance(value, float) and value != value):
            return ""
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def parse_product_categories(value: str, logger: Logger) -> tuple:
    """查产品类别表，返回 (CPLB, ZLBM)。

    ⚠️ 这张表的 `customer_product_categories` 列**只有 1 行有值**（`paperback`），
    所以本兜底实际只覆盖 `format=paperback` 一种写法；其余一律落到档案兜底值。
    命中多行时取**最后一行** —— 与旧脚本「逐行打印、main 取最后一条」一致。
    """
    query = str(value).strip()
    logger.info(f"  查表: product_categories.xlsx ← {query!r}")
    df = _load_table(PRODUCT_CATEGORIES_PATH,
                     ["customer_product_categories", "CPLB", "ZLBM"], logger)
    if df is None:
        return "", ""

    categories = df["customer_product_categories"].astype(str).str.strip().str.lower()
    matched = df.loc[categories == query.lower()]
    if matched.empty:
        logger.info("  查询结果: 未命中")
        return "", ""

    row = matched.iloc[-1]
    cplb, zlbm = _cell(row["CPLB"]), _cell(row["ZLBM"])
    logger.info(f"  查询结果: CPLB={cplb}, ZLBM={zlbm}")
    return cplb, zlbm


def get_page_thickness(page_style: str, faw: str, logger: Logger) -> float:
    """查纸张厚度（克重传裸数字，不带 g）。查不到 → WARN 并返回 0.0。

    三级匹配，与旧脚本一致：精确 → 包含 → 同纸型里按克重就近（并列取较大者，
    宁厚勿薄 —— 书脊算窄会装不下）。
    """
    import pandas as pd

    style, weight = str(page_style).strip(), str(faw).strip()
    logger.info(f"  查表: page_thickness.xlsx ← {style} / {weight}")
    df = _load_table(PAGE_THICKNESS_PATH, ["page_style", "faw/g", "thickness"],
                     logger, csv_fallback=True)
    if df is None:
        logger.warn(f"  {style} 克重 {weight} 查询失败，改用 克重×0.001 粗估")
        return 0.0

    styles = df["page_style"].astype(str).str.strip()
    weights = df["faw/g"].astype(str).str.strip()

    # 1) 精确
    matched = df.loc[(styles == style) & (weights == weight)]
    # 2) 包含（客户常写 "80g"、"100 gsm"）
    if matched.empty:
        matched = df.loc[styles.str.contains(style, case=False, na=False, regex=False)
                         & weights.str.contains(weight, case=False, na=False, regex=False)]
    # 3) 同纸型下按克重就近（表里只有离散克重，90g 这种中间值才走到这里）
    if matched.empty:
        sub = df.loc[styles == style].copy()
        target = pd.to_numeric(re.sub(r"[^0-9.]", "", weight), errors="coerce")
        if not sub.empty and pd.notna(target):
            sub["_w"] = pd.to_numeric(sub["faw/g"], errors="coerce")
            sub = sub.dropna(subset=["_w"])
            if not sub.empty:
                sub["_d"] = (sub["_w"] - target).abs()
                best = sub.sort_values(["_d", "_w"], ascending=[True, False]).iloc[0]
                logger.warn(f"  {style} {weight} 无精确数据，按最近克重 "
                            f"{int(best['_w'])} 取值（thickness={best['thickness']}）")
                return float(best["thickness"])

    if matched.empty:
        logger.warn(f"  {style} 克重 {weight} 查询失败，改用 克重×0.001 粗估")
        return 0.0

    if len(matched) > 1:
        # 旧脚本在这里会把多行拼成一段文本，`float()` 吃掉 → 0.0。这里保持同样的
        # 结果：宁可返回 0（WARN 可见），也不要随便挑一行的厚度当真值。
        logger.warn(f"  {style} {weight} 命中 {len(matched)} 行，无法确定 → 按查询失败处理")
        return 0.0

    thickness = float(matched.iloc[0]["thickness"])
    logger.info(f"  {style}厚度: {thickness}")
    return thickness


def _parse_date_value(date_str: str) -> Optional[datetime]:
    """把客户文件里的日期写法解成 datetime（解不出返回 None）。

    顺序照抄原 string2timestamp.py：带时间 → 纯日期 → 年月 → `May-27`。
    """
    text = str(date_str).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y.%m.%d %H:%M:%S",
                "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    for fmt in ("%Y-%m", "%Y/%m", "%Y.%m"):
        try:
            return datetime.strptime(text, fmt).replace(day=1)
        except ValueError:
            continue
    matched = re.match(r"([a-zA-Z]{3})[-/](\d{2,4})", text)
    if matched:
        month = _MONTH_ABBR.get(matched.group(1).lower())
        if month:
            year = int(matched.group(2))
            return datetime(year + 2000 if year < 100 else year, month, 1)
    return None


def resolve_cplb(customer_data: CustomerData, profile: CustomerProfile,
                 logger: Logger) -> tuple[str, str]:
    """产品类别 CPLB：档案关键词映射 → 旧查表兜底 → 档案兜底值。

    返回值一定是「编号;名称」，且编号必须存在于 assets/cplb_tree.yaml，
    否则打 WARN 并退到档案兜底值（宁可粗，不可错 —— 写错编号导入会丢）。
    返回 (CPLB 值, 来源说明)。
    """
    fallback = str(profile.rule("product_category.fallback", "32.00;其他"))

    # 1) 档案 cplb.map：客户文件里的写法 → 树节点编号（可精确指定，多客户解耦的关键）
    keys = profile.rule("cplb.match_keys",
                        ["format", "binding", "finishes", "project_description", "series_name"])
    blob = " ".join(str(customer_data.get(k) or "") for k in (keys or []))
    for entry in profile.rule("cplb.map", []) or []:
        pattern = str(entry.get("match", ""))
        code = str(entry.get("node", "")).strip()
        requires = str(entry.get("requires", "") or "")
        if not pattern or not code:
            continue
        # requires：同一份原文里还必须**另有**证据才认（例如「冲形状」得配合「胶装」，
        # 否则会把冲形状的精装/线圈也吞进来）。缺 requires = 无条件命中。
        if requires and not re.search(requires, blob, re.IGNORECASE):
            continue
        if re.search(pattern, blob, re.IGNORECASE):
            if cplb_tree.exists(code):
                label = cplb_tree.label_of(code)
                logger.info(f"  类别映射: 命中 /{pattern}/"
                            + (f"（且 /{requires}/）" if requires else "")
                            + f" → {label}")
                return label, f"档案 cplb.map（命中 /{pattern}/）"
            logger.warn(f"  档案 cplb.map 命中 /{pattern}/，但编号 {code} 不在产品类别树里 → 忽略")

    # 2) 旧路径：产品类别映射表（assets/product_categories.xlsx 的 customer_product_categories 列）
    format_value = customer_data.get("format")
    if format_value:
        cplb, zlbm = parse_product_categories(format_value, logger)
        if cplb and zlbm:
            if cplb_tree.exists(zlbm):
                return cplb_tree.label_of(zlbm), f"产品类别表查表（format={format_value}）"
            logger.warn(f"  产品类别表给出的编号 {zlbm} 不在产品类别树里，改用兜底值")

    # 3) 兜底
    if not cplb_tree.exists(fallback.split(";")[0].strip()):
        logger.warn(f"  档案兜底值 {fallback} 也不在树里 → 用 32.00;其他")
        fallback = "32.00;其他"
    return fallback, f"档案兜底值（product_category.fallback）"


def derive_cpsx(customer_data: CustomerData, profile: CustomerProfile,
                logger: Logger) -> tuple[str, str]:
    """产品属性 CPSX：由「目标年龄段 + 安全检查」推导。

    业务确认（2026-09-23）：这两项**要落库**，落点就是 CPSX；同时仍必须写进 CPMS。
    阈值与取值可在客户档案 `rules.cpsx` 覆盖；推不出来就留空，绝不猜。
    返回 (ERP 合法取值 或 "", 来源说明)。
    """
    threshold = float(profile.rule("cpsx.threshold", 3))
    above = str(profile.rule("cpsx.above", "三岁以上"))
    below = str(profile.rule("cpsx.below", "三岁以下"))
    above_test = str(profile.rule("cpsx.above_with_test", "3岁以上（要寄第三方测试）"))
    below_test = str(profile.rule("cpsx.below_with_test", "3岁以下（要寄第三方测试）"))
    true_values = [str(v).lower() for v in
                   (profile.rule("cpsx.safety_true", ["y", "yes", "true", "是", "1", "✓"]) or [])]

    safety_raw = str(customer_data.get("safety_checks") or "").strip()
    age_raw = str(customer_data.get("target_age_group") or "").strip()
    safety = (safety_raw.lower() in true_values) if safety_raw else None
    m = re.search(r"(\d+(?:\.\d+)?)", age_raw)
    age = float(m.group(1)) if m else None

    if age is None:
        if safety_raw or age_raw:
            logger.warn(f"  产品属性 CPSX: 取不到年龄段（target_age_group={age_raw!r}，"
                        f"安全检查={safety_raw!r}）→ 留空，需人工判断")
        return "", ""

    is_above = age >= threshold
    if safety:
        value = above_test if is_above else below_test
    else:
        value = above if is_above else below
    source = (f"年龄段={age_raw}（{'≥' if is_above else '<'}{threshold:g} 岁）"
              + (f" + 安全检查={safety_raw}" if safety_raw else ""))
    return value, source


def parse_date_to_datestr(date_str: str, logger: Logger) -> str:
    """标准化为 YYYY-MM-DD 字符串（v2: JHRQ / WCRQ 为 string）。"""
    parsed = _parse_date_value(date_str)
    if parsed is not None:
        out = parsed.strftime("%Y-%m-%d")
        logger.info(f"  转换结果: {out}")
        return out
    logger.warn(f"  未能解析日期: {date_str!r}，尝试正则回退")

    m = re.search(r'(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?', str(date_str))
    if m:
        y, mo, d = m.group(1), int(m.group(2)), int(m.group(3) or 1)
        out = f"{y}-{mo:02d}-{d:02d}"
        logger.info(f"  回退解析: {out}")
        return out

    m2 = re.match(r'([A-Za-z]{3})-(\d{2})', str(date_str).strip())
    if m2:
        months = ["jan", "feb", "mar", "apr", "may", "jun",
                  "jul", "aug", "sep", "oct", "nov", "dec"]
        try:
            mo = months.index(m2.group(1).lower()) + 1
            out = f"20{m2.group(2)}-{mo:02d}-01"
            logger.info(f"  回退解析: {out}")
            return out
        except ValueError:
            pass

    logger.warn("  日期解析失败，返回空字符串")
    return ""


def check_offer_deadline(customer_data: CustomerData, profile: CustomerProfile,
                         logger: Logger) -> Optional[dict]:
    """报价截止日期检查（业务 2026-09-24：「超过了报价截止日期，你记得提醒」）。

    取规范键 `offer_deadline`（由客户档案负责抽出来；NGV 在 enquiry 分区，
    实测 5/5 份样本都有值）。返回 None 表示该单没取到截止日期；否则返回：
        {"deadline": "YYYY-MM-DD", "expired": bool, "days": int, "message": str}
    其中 days > 0 表示「已逾期 N 天」，= 0 表示「就是今天」，< 0 表示「还剩 N 天」。
    这是给业务的提醒，**不是** ERP 字段（ERP 模板里没有报价截止日期）。
    """
    raw = str(customer_data.get("offer_deadline") or "").strip()
    if not raw:
        return None

    # 复用既有的日期标准化（吃 Excel 的 `2026-06-26 00:00:00` 这种写法）
    deadline_str = parse_date_to_datestr(raw, logger)
    if not deadline_str:
        logger.warn(f"  报价截止日期: 取到 {raw!r} 但解析不出日期 —— 需人工确认")
        return None

    try:
        deadline = datetime.strptime(deadline_str, "%Y-%m-%d").date()
    except ValueError:
        logger.warn(f"  报价截止日期: {deadline_str!r} 不是合法日期 —— 需人工确认")
        return None

    base = today()
    days = (base - deadline).days
    expired = days > 0
    if expired:
        message = f"报价截止日期 {deadline_str} 已过期 {days} 天（基准日 {base:%Y-%m-%d}）"
    elif days == 0:
        message = f"报价截止日期 {deadline_str} 就是今天（基准日 {base:%Y-%m-%d}）"
    else:
        message = f"报价截止日期 {deadline_str}（还剩 {-days} 天，基准日 {base:%Y-%m-%d}）"

    if expired and bool(profile.rule("deadline.warn_when_expired", True)):
        logger.warn(f"  {message} —— **必须提醒业务：确认这单还报不报**")
    else:
        logger.info(f"  {message}")

    return {"deadline": deadline_str, "expired": expired, "days": days,
            "message": message}


def generate_task_id(logger: Logger) -> str:
    """本次任务号（产物目录名 / 日志「任务ID」/ validation.json 的 task_id 同一个）。"""
    task_id = uuid4().hex
    logger.info(f"生成任务ID: {task_id}")
    return task_id


# ── 数值/单位解析 ────────────────────────────────────────────────────────
def _to_number(text: str) -> Optional[float]:
    m = re.search(r"(\d+(?:\.\d+)?)", str(text))
    return float(m.group(1)) if m else None


def _num(value: float) -> Any:
    """整数就返回 int，否则 float（保持输出 JSON 里 198 而不是 198.0）。"""
    if value is None:
        return 0
    return int(value) if float(value).is_integer() else round(float(value), 2)


def _de_number(text: str) -> Optional[float]:
    """欧式数字 → float。`17.500` 是 17500（点作千分位），`7,5` 是 7.5。

    NGV 用德式写法，`4 x 17.500` 必须算成 70000 而不是 70。
    判定靠位数：点后正好 3 位且形如 `1.234.567` → 千分位；否则点/逗号当小数点。
    英文千分位写法（`3,000` / `17,500`）同样按千分位处理 —— 逗号后正好 3 位就是它。

    实测踩过：`3 x 7000 cps. = 21.000 cps` 这种带「= 总数」的写法，
    旧的 `^N x M$` 全串匹配失败后退回「取第一个数字」，静默算成 `3`（真值 21000）。
    单值和乘法分别处理见 `parse_single_quantity()`。
    """
    m = re.search(r"(\d[\d.,]*)", str(text or ""))
    if not m:
        return None
    raw = m.group(1).rstrip(".,")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", raw) or re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw):
        raw = raw.replace(".", "").replace(",", "")
    else:
        raw = raw.replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_single_quantity(text: str, logger: Optional[Logger] = None) -> Optional[int]:
    """解析「单一总数」的数量写法 → int；解析不出或为 0 返回 None。

    实测 NGV 的四种写法（见 customers/ngv.yaml rules.quantity.mode: single）：

        `512580cps`                     → 512580
        `4 x 17.500`                    → 70000      （4 款 × 17500 套，德式千分位）
        `3 x 7000 cps. = 21.000 cps`    → 21000      （带「= 总数」，以总数为准）
        `0`                             → None       （报价阶段常还没数量，留 0 + WARN）

    解析顺序：① 显式总数（`= …` / `total …`）→ ② `N x M` 乘法 → ③ 全串只有一个数字时取它。
    ①② 同时存在且不一致 → **打 WARN 并取总数**（总数是客户自己算好的，且差异必须可见）。
    ③ 只在「整串只有一个数字」时才敢取 —— 多个数字而没有可识别写法时**宁可留 0 报 WARN**，
    不许悄悄取第一个（那正是上面 `3 x 7000 …` 算成 `3` 的坑）。
    """
    s = str(text or "").strip()
    if not s:
        return None

    # ① 显式总数：`= 21.000 cps` / `total: 21.000`
    total = None
    m_total = re.search(
        r"(?:=|gesamt|total\s*[:=]?)\s*(\d[\d.,]*)\s*(?:cps|pcs|copies|ex\.?)?",
        s, re.IGNORECASE)
    if m_total:
        total = _de_number(m_total.group(1))

    # ①b 显式总数（数字在前）：`QTY: 60.000 St. in total (20.000 cps/title)`
    # 客户常把「总数」写在数字**后面**，前面那个才是真正的总数 —— 后面括号里的
    # 是「每款多少」。实测 CU26070901：这一行不认就会退回 ③ 兜底并把 60.000 丢掉。
    if total is None:
        m_total_post = re.search(
            r"(\d[\d.,]*)\s*(?:st\.?|stk\.?|cps\.?|pcs\.?|copies|ex\.?)?\s*"
            r"(?:in\s+total|insgesamt)\b",
            s, re.IGNORECASE)
        if m_total_post:
            total = _de_number(m_total_post.group(1))

    # ② `N x M` 乘法（允许后面还有其他文字）
    product = None
    mul_desc = ""
    m_mul = re.search(r"(\d+)\s*[x×*]\s*(\d[\d.,]*)", s)
    if m_mul:
        count = int(m_mul.group(1))
        unit = _de_number(m_mul.group(2))
        if count and unit:
            product = int(count * unit)
            mul_desc = f"{count}×{unit:g}={product}"

    if product and total:
        if int(product) != int(total):
            if logger is not None:
                logger.warn(f"  数量写法 {text!r}：乘法 {mul_desc} 与客户写的"
                            f"总数 {int(total)} 不一致 → 取总数 {int(total)}，请人工核对")
        return int(total)
    if product:
        return int(product)
    if total:
        return int(total)

    # ③ 兜底：整串只有一个数字才取
    numbers = re.findall(r"\d[\d.,]*", s)
    if len(numbers) == 1:
        value = _de_number(numbers[0])
        if value:
            return int(value)
        return None

    if logger is not None:
        logger.warn(f"  数量 {text!r} 解析不出（{len(numbers)} 个数字、无可识别写法）"
                    f"—— CPSL1 保持默认值 0，需人工确认")
    return None


def parse_size(size_str: str, profile: CustomerProfile, logger: Logger) -> dict:
    """按档案 `rules.size`（单位 + 顺序）解析尺寸串 → {CD/KD/GD: 数值}。

    例（igl，order=[CD,KD,GD]，unit=mm）：`198mm x 129mm x 10mm` → CD=198 KD=129 GD=10

    ★ ERP 口径（业务 2026-10-20 拍板）：**CD = 长（较长边）、KD = 宽（较短边）**。
      客户写法五花八门（NGV 的 TPS 是「先宽后高」、igl 是「H x W x D」），所以解析完
      统一兜底：CD < KD 就交换。这样「产品规格：【长*宽*厚】」（CPMS 概要行）恒成立。
      同时把客户原始两段保留在 `_raw` 里 —— 书脊方向要靠「装订边是哪条边」算，
      交换后光看 CD/KD 已经分不出横竖版。
    """
    unit = str(profile.rule("size.unit", DEFAULT_UNIT))
    order = profile.rule("size.order", ["CD", "KD", "GD"]) or ["CD", "KD", "GD"]
    normalized = re.sub(re.escape(unit), " ", str(size_str), flags=re.IGNORECASE)
    chunks = [c for c in re.split(r"[x×*]", normalized) if c.strip()]
    out: dict = {}
    raw: list = []
    for idx, chunk in enumerate(chunks[:len(order)]):
        value = _to_number(chunk)
        if value is not None:
            out[order[idx]] = _num(value)
            raw.append(_num(value))
    out["_raw"] = raw[:2]

    # CD=长 / KD=宽（业务 2026-10-20 对 NGV 拍板）：客户给的是「先宽后高」还是「先高后宽」
    # 都不管，落库前统一成长边在前。交换必须留痕 —— 否则有人会以为是客户写错了。
    # ⚠️ 只对声明了 `size.long_first: true` 的客户生效：igl 的附件列本身是「H x W」，
    #    已核对的口径是 CD=H（不是长度方向），没拿到 igl 的 ERP 反证前不动它。
    if (profile.rule("size.long_first", False)
            and out.get("CD") and out.get("KD") and out["CD"] < out["KD"]):
        out["CD"], out["KD"] = out["KD"], out["CD"]
        if logger is not None:
            logger.info(f"  尺寸交换: 客户写 {raw[0]} x {raw[1]}，"
                        f"按 ERP 口径（CD=长 / KD=宽）落 CD={out['CD']} KD={out['KD']}")
    return out


def spine_from_binding_direction(direction: str, raw: list,
                                 size_str: str) -> Optional[str]:
    """装订边位置 + 版式 → 书脊在长边还是短边（"长书脊" / "短书脊" / None）。

    「at the top / at the head / at the bottom」 = 装订在**上/下边** → 书脊沿水平边（宽）；
    「at the side / left / right / spine」 = 书脊沿垂直边（高）。
    再跟较长边比较：书脊长度 == 较长边 → 长书脊，否则短书脊。

    业务 2026-10-20 拍板 **CD=长 / KD=宽** 之后，这一步不再是「猜方向」——
    方向（哪条边装订）+ 版式（竖/横）就能算出书脊在长边还是短边：
      * 真单 BJXQ260708007：148x210 portrait + `at the top` → 书脊在 148 边 → **短书脊**
        （业务 ERP 的 CPMS 概要行写的正是【短书脊】，ERP `Drop_SJFX` 他们留空了）。
    判不出来（没有方向词 / 没有尺寸）→ 返回 None，由调用方留空。
    """
    text = str(direction or "").strip().lower()
    if not text or len(raw or []) < 2:
        return None
    vertical_edge = any(k in text for k in
                        ("side", "left", "right", "spine", "侧", "左", "右", "书脊"))
    horizontal_edge = any(k in text for k in
                          ("top", "head", "bottom", "foot", "顶", "头", "天头", "底", "地脚"))
    if not (vertical_edge or horizontal_edge):
        return None

    first, second = raw[0], raw[1]
    landscape = bool(re.search(r"landscape|querformat|横版|横排|横",
                               str(size_str or ""), re.IGNORECASE))
    width, height = (max(first, second), min(first, second)) if landscape \
        else (min(first, second), max(first, second))
    spine_len = width if horizontal_edge else height
    return "长书脊" if spine_len >= max(first, second) else "短书脊"


def parse_extent(extent_str: str) -> tuple:
    """解析页数：`120pp + cover` → (120, 1)。"""
    pp = 0
    cover = 0
    m = re.search(r"(\d+)\s*pp", str(extent_str), re.IGNORECASE)
    if m:
        pp = int(m.group(1))
    if "cover" in str(extent_str).lower():
        cover = 1
    return pp, cover


def parse_material(material_str: str) -> tuple:
    """解析材质串 → (克重, 原始类型串)。"""
    faw = 0
    m = re.search(r"(\d+(?:\.\d+)?)\s*gsm", str(material_str), re.IGNORECASE)
    if m:
        faw = float(m.group(1))
    rest = re.sub(r"^\s*\d+(?:\.\d+)?\s*gsm\s*", "", str(material_str), flags=re.IGNORECASE)
    return faw, rest.strip()


def resolve_page_style(material_str: str, role: str, profile: CustomerProfile,
                       logger: Logger) -> str:
    """客户怎么写的材质 → 厚度表里的纸张类型（档案 rules.materials.<role>）。

    这就是 TODO 里长期挂着的「纸张类型映射表」：`woodfree` → 书纸、
    `C1S artboard` → 光粉纸，规则可被任一客户档案覆盖。
    """
    text = str(material_str).lower()
    for entry in profile.rule(f"materials.{role}.map", []) or []:
        if re.search(str(entry.get("match", "")), text):
            style = str(entry.get("page_style", "")).strip()
            if style:
                logger.info(f"  材质映射（{role}）: {material_str} → {style}"
                            f"（命中 /{entry.get('match')}/）")
                return style
    default = str(profile.rule(f"materials.{role}.default", "书纸"))
    logger.warn(f"  材质映射（{role}）: 未命中任何规则，材质 {material_str!r} 兜底用 {default}")
    return default


def calculate_thickness(extent_pp: int, pp_faw: float, extent_cover: int, cover_faw: float,
                        pp_style: str, cover_style: str, logger: Logger) -> float:
    """GD = 内页数/2 × 内页厚度 + 封面数 × 封面厚度。"""
    pp_thickness = get_page_thickness(pp_style, f"{_num(pp_faw)}", logger)
    cover_thickness = get_page_thickness(cover_style, f"{_num(cover_faw)}", logger)
    if pp_thickness == 0:
        pp_thickness = pp_faw * 0.001
    if cover_thickness == 0:
        cover_thickness = cover_faw * 0.001
    total = (extent_pp / 2) * pp_thickness + extent_cover * cover_thickness
    return round(total, 2)


# 【CPMS 由 agent 亲自撰写 —— 脚本不再生成任何 CPMS 内容】
# 业务确认（2026-09-23）：产品描述（CPMS）无法用 Python 统一处理。每个客户的报价文件
# 结构、栏目命名、信息分布都不同，必须通读全文、翻译成中文、按类别分段并优化格式。
# 因此 build_cpms_draft() 及其类别常量已全部删除（1.0.11）。脚本把 CPMS 留空，
# 由 agent 跑完后亲自补写，详见 SKILL.md 5.1.6。


def versions_from_email(profile: CustomerProfile, email_meta: Optional[dict],
                        logger: Optional[Logger] = None) -> tuple:
    """款数 / 每款语言数只写在**邮件正文**时的兜底。

    实测（LG26090401，2026-10-21）：客户附件是德文的 `Titel+Mengen` 表，里面没有
    `N titles, N languages` 这种规范表述，而正文第一句就写着
    `There are 10 titles in 10 languages based on standard language change:`
    → 只按附件取会把 TZS 落成 1、YYS 落成 0（业务真实录入是 10/10）。

    只看**最新一封**（`newest_message`）：引用的旧邮件里写着上一个项目
    `6 titles 4 languages`，拿它当本单事实同样是错的。

    返回 (款数|None, 语言数|None)；两个独立判定，各自取不到就是 None。
    """
    meta = email_meta or {}
    title_pattern = profile.rule("versions.title_pattern")
    language_pattern = profile.rule("versions.language_pattern")
    if not title_pattern and not language_pattern:
        return None, None

    text = "\n".join(str(meta.get(k) or "") for k in ("subject", "body"))
    text = newest_message(text)
    if not text.strip():
        return None, None

    def pick(pattern) -> Optional[int]:
        if not pattern:
            return None
        m = re.search(str(pattern), text, re.IGNORECASE)
        return int(m.group(1)) if m else None

    titles, langs = pick(title_pattern), pick(language_pattern)
    if (titles is not None or langs is not None) and logger:
        logger.info(f"  邮件正文兜底: 款数={titles} / 语言数={langs}")
    return titles, langs


# ── 字段映射 ─────────────────────────────────────────────────────────────
def map_fields(customer_data: CustomerData, template: dict, profile: CustomerProfile,
               logger: Logger, email_meta: Optional[dict] = None) -> dict:
    """把「规范键 + 客户档案规则 + ERP 模板」映射成 59 字段结果。

    赋值来源三类：
      1. 档案 erp_fixed        —— 该客户/业务层的固定值
      2. 由规范键推导          —— CPLB / CPMC / CD / KD / GD / Drop_SJFX / JHRQ …
      3. 模板默认值            —— 其余字段（见 SKILL.md 5.2）
    本函数**读不到客户列名**，只读规范键 —— 这是多客户解耦的关键。
    """
    result = template.copy()
    total_fields = len(template)
    email_meta = email_meta or {}

    success_count = 0
    default_count = 0
    failed_count = 0
    assigned: list = []          # 真正被赋值的字段名（用于诚实统计，不写死数字）

    logger.info(f"开始映射 {total_fields} 个字段...")
    logger.info(f"客户档案: {profile.id}（{profile.name}）")

    task_id = generate_task_id(logger)
    result["_task_id"] = task_id

    def assign(field: str, value: Any, source: str, index: int) -> bool:
        """赋值并记录日志。返回是否算「成功赋值」。"""
        nonlocal success_count
        logger.field_mapping(field, index, total_fields)
        logger.info(f"  来源: {source}")
        result[field] = value
        logger.info(f"  映射值: {value}")
        logger.info("  状态: 成功")
        success_count += 1
        assigned.append(field)
        return True

    # ── 1. 档案固定值 ──────────────────────────────────────
    for field, value in profile.erp_fixed_values().items():
        index = list(template.keys()).index(field) + 1 if field in template else 0
        if value == "" or value is None:
            # ★「显式留空」（客户偏好表口径，业务确认 2026-09-24）：
            #   该客户这个字段就该空着（推不出来就不猜），但模板自带默认值
            #   （如 PXH 模板默认 1）→ 必须真的清掉，否则默认值会悄悄留在产物里。
            result[field] = ""
            logger.field_mapping(field, index, total_fields)
            logger.info(f"  来源: 客户档案偏好表（{profile.id}）—— 显式留空")
            logger.info("  映射值: （空字符串）")
            logger.info("  状态: 成功（空值即正确）")
            assigned.append(field)
            continue
        assign(field, value, f"客户档案固定值（{profile.id}）", index)

    # ── 2. 产品类别 CPLB（档案映射 → 产品类别树校验）────────
    cplb_value, cplb_source = resolve_cplb(customer_data, profile, logger)
    cplb_node = cplb_value.split(";")[0].strip()
    result["CPLB"] = cplb_value
    if cplb_tree.exists(cplb_node) and "." in cplb_node:
        logger.field_mapping("CPLB", 8, total_fields)
        logger.info(f"  来源: {cplb_source}")
        logger.info(f"  映射值: {cplb_value}")
        node = cplb_tree.nodes()[cplb_node]
        logger.info(f"  业务定义: {node.get('criteria', '(无)')}")
        logger.info("  状态: 成功")
        success_count += 1
        assigned.append("CPLB")
    else:
        logger.warn(f"  来源: {cplb_source} → 只到顶层/兜底值 {cplb_value}，"
                    f"建议先用 scripts/cplb_tree.py 定到具体子类")
        default_count += 1

    # 产品类别顶层名（树里的 top_name）—— 后面多处判据都要用：
    # 套装没有尺寸（录系统写 0/0/0）、也没有书脊方向，靠这一层判断。
    cplb_top = ""
    if cplb_tree.exists(cplb_node):
        cplb_top = str((cplb_tree.nodes().get(cplb_node) or {}).get("top_name") or "")

    # ── 3. 产品名称 CPMC（英文表达）+ CPMC_ZH（中文表达）──────
    # 业务 2026-09-24：「将 CPMC 修改为存英文表达，使用 CPMC_ZH 来标注中文表达」。
    # 档案声明两个模板，占位符：
    #   {salesperson} {cn_name} {cplb} {cplb_name} {product_name} {series_name}
    #   {fsc} {our_ref}
    #     cpmc.template      → CPMC（**英文**，不许出现中文）
    #     cpmc.zh_template   → CPMC_ZH（中文表达；没声明就是空 = 该客户还没拆）
    # 默认模板 {cplb}-{series_name} = igl 的既有口径（历史 ERP 记录就是这种写法）。
    cpmc_tpl = str(profile.rule("cpmc.template", "{cplb}-{series_name}"))
    zh_tpl = str(profile.rule("cpmc.zh_template", "") or "")
    product_name = str(customer_data.get("product_name") or "")
    series_name = str(customer_data.get("series_name") or "")
    cplb_full = str(result.get("CPLB", ""))
    # 业务员（档案 rules.cpmc.salesperson）：该客户固定对接人，NGV = KM
    #   （业务 2026-09-24 拍板）。留空则该段省略，不塞占位符。
    salesperson = str(profile.rule("cpmc.salesperson", "") or "")
    # 中文品名（档案 rules.cpmc.cn_name）：只能人工拟 —— 档案里为空时用**显式占位符**，
    # 让「还没填」在产物里一眼可见、并且能被 needs_agent 检查抓出来（而不是静默少一段）。
    cn_name = str(profile.rule("cpmc.cn_name", "") or "")
    if not cn_name:
        cn_name = "【中文品名待定】"
    # fsc 先过 ERP 枚举归一化：表格里写 "FSC mix" 也要进 CPMC 成 "FSC Mix"
    fsc_raw = str(customer_data.get("fsc") or "").strip()
    fsc_legal = str(normalize_value("FSCMC", fsc_raw)[0] or "") if fsc_raw else ""
    tokens = (
        ("{salesperson}", salesperson),
        ("{cn_name}", cn_name),
        ("{cplb}", cplb_full),
        ("{cplb_name}", cplb_full.split(";")[-1]),
        ("{product_name}", product_name),
        ("{series_name}", series_name),
        ("{fsc}", fsc_legal),
        ("{our_ref}", str(customer_data.get("our_ref") or "")),
    )

    def render(tpl: str) -> str:
        out = tpl
        for token, value in tokens:
            out = out.replace(token, value)
        out = re.sub(r"[-/\s]*\((?:)\)", "", out)     # 占位符空了就抹掉空括号
        out = re.sub(r"\s{2,}", " ", out).strip(" -/")
        # 业务员留空时会留下前导 "/"，收一下（`{salesperson}/Aldi …` → `Aldi …`）
        return re.sub(r"^[/\s]+", "", out)

    cpmc = render(cpmc_tpl)
    assign("CPMC", cpmc, f"档案 rules.cpmc.template 拼装（规范键 product_name={product_name!r}）", 11)
    cpmc_zh = render(zh_tpl) if zh_tpl else ""
    if cpmc_zh:
        result["_cpmc_zh"] = cpmc_zh      # 下划线开头 = 内部字段，step5b 跳过、step6 落到 CPMC_ZH

    # ── 3b. 产品英文名 ECPMC ──────────────────────────────────────
    # 业务 2026-10-20 指出：CPMS 写了，`ECPMC`（产品英文名）却空着。
    # 业务 2026-10-21 澄清：ECPMC 是**报价的原始产品名**，不限于英文 ——
    #   德文 `Malbücher`、日文、法文一律照抄，不许因为「不是英文」就留空。
    #   （ERP 字段 label 叫「产品英文名」是历史命名，口径以业务这句为准。）
    # ERP 表单里 CPMC / ECPMC 是两个独立字段：
    #   CPMC  = 产品名称（NGV 落中文译名；igl 仍是「类别;系列名」的英文串，不动）
    #   ECPMC = **原始产品名**（客户文件里的品名原文）
    # 取值口径：照抄客户原文，**不翻译、不拼类别、不带 FSC**（那是 CPMC 的事）。
    # 档案用 `rules.ecpmc.source_keys` 声明从哪个规范键取（按顺序取第一个非空）；
    # 没声明 = 该客户不产出 ECPMC（保持原行为，不动 igl 基线）。
    # ⚠️ 取不到（纯文本邮件没有 `Title:` 标签）时脚本留空并 WARN —— 这时**必须由
    #    agent 从客户原文照抄补上**，`cpms_todo.py` 会把这种「档案要求但字段空着」列为待办。
    ecpmc_keys = profile.rule("ecpmc.source_keys", []) or []
    ecpmc_value, ecpmc_src = "", ""
    for key in ecpmc_keys:
        val = str(customer_data.get(str(key)) or "").strip()
        if val:
            ecpmc_value, ecpmc_src = val, str(key)
            break
    if ecpmc_value:
        ecpmc_idx = list(template.keys()).index("ECPMC") + 1 if "ECPMC" in template else 0
        assign("ECPMC", ecpmc_value,
               f"档案 rules.ecpmc（规范键 {ecpmc_src}，客户文件英文品名照抄）", ecpmc_idx)
    elif ecpmc_keys:
        logger.warn(f"  ECPMC（产品英文名）: 档案 rules.ecpmc 声明的规范键 {ecpmc_keys} 都没取到 → 留空")

    if profile.rule("cpmc.needs_agent", False) or zh_tpl:
        # 兜底：模板里凡是没被任何占位符填上的「待定」标记，都要在产物里点名，
        # 免得 agent 忘了补就直接交付（实测踩过：留了【业务员待填】却以为脚本会填）。
        for label, value in (("CPMC", cpmc), ("CPMC_ZH", cpmc_zh)):
            pending = re.findall(r"[【\[][^】\]]*待[^】\]]*[】\]]", value)
            if pending:
                logger.warn(f"  客户约定：{label} 里的 {', '.join(pending)} 需人工拟定 —— "
                            f"**交付前必须由 agent 补全**（当前值: {value}）")
            elif value:
                logger.info(f"  {label}: {value}")
    if zh_tpl:
        # 硬不变量（默认）：CPMC 只放英文，中文表达归 CPMC_ZH。
        # 业务 2026-10-19 起，NGV 的 CPMC 就是**产品名称的中文译名**
        # （档案 `cpmc.allow_chinese: true`）→ 这种客户不再报中文告警。
        allow_cn = bool(profile.rule("cpmc.allow_chinese", False))
        if not allow_cn and re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", cpmc):
            logger.warn(f"  CPMC 里出现了中文/全角字符：{cpmc!r} —— 业务口径是「CPMC 存英文表达、"
                        f"中文表达落 CPMC_ZH」，请检查 customers/{profile.id}.yaml 的 cpmc.template")
    else:
        logger.info("  提示: 该客户未声明 rules.cpmc.zh_template → CPMC_ZH 为空"
                    "（CPMC 仍是单份，没拆英文/中文）")

    # ── 4. 款数 TZS / 每款语言数 YYS ───────────────────────
    # 款数与语言数常挤在同一格（NGV：`12 titles, 8 countries, 5 languages`），
    # 用档案 rules.versions 的两个正则分别取。
    versions_raw = str(customer_data.get("versions") or "")
    title_pattern = profile.rule("versions.title_pattern")
    language_pattern = profile.rule("versions.language_pattern")

    email_titles, email_langs = versions_from_email(profile, email_meta, logger)

    num_titles = None
    if versions_raw and title_pattern:
        m = re.search(str(title_pattern), versions_raw, re.IGNORECASE)
        if m:
            num_titles = int(m.group(1))
    tzs_source = (f"versions（规范键）={versions_raw!r} 用 rules.versions.title_pattern 提取"
                  if versions_raw else
                  f"number_of_titles={customer_data.get('number_of_titles')!r}")
    if num_titles is None and email_titles is not None:
        num_titles = email_titles
        tzs_source = "邮件正文（最新一封）用 rules.versions.title_pattern 提取"
    if num_titles is None:
        raw_titles = customer_data.get("number_of_titles", "1")
        num_titles = int(raw_titles) if str(raw_titles).isdigit() else 1
    assign("TZS", num_titles, tzs_source, 12)

    # YYS 取不到时怎么办由档案 rules.versions.when_missing 决定
    #   （NGV：0 —— 业务确认 2026-09-24「提取不到就为 0」，因为客户的邮件可能后面
    #    补充了内容而附件没更新，0 是「待业务核对」的信号；不许猜、也不留 1）
    when_missing = profile.rule("versions.when_missing")
    lang_count = None
    if versions_raw and language_pattern:
        m = re.search(str(language_pattern), versions_raw, re.IGNORECASE)
        if m:
            lang_count = int(m.group(1))
    if lang_count is None and email_langs is not None:
        lang_count = email_langs
        versions_raw = versions_raw or f"邮件正文: {email_langs} languages"

    if lang_count is None:
        if when_missing is not None:
            assign("YYS", when_missing,
                   f"versions（规范键）={versions_raw!r} 里提取不到语言数 "
                   f"→ 档案 rules.versions.when_missing={when_missing}（提取不到就为 0，业务确认 2026-09-24）"
                   if versions_raw else
                   f"客户文件里没有语言数 → 档案 rules.versions.when_missing={when_missing}",
                   14)
        else:
            logger.warn(f"  YYS: versions={versions_raw!r} 里没有语言数 "
                        f"→ 保持档案固定值 {result.get('YYS')}，需人工确认")
    elif lang_count <= 1 and profile.rule("versions.empty_when_single", False):
        # 档案 rules.versions.empty_when_single：单语言不在 ERP 里落库。
        # 业务依据（2026-09-24）：4 份 NGV 导出里，附件写 `4 titles, 1 language`
        # 的两单（CU26072301 / CU26072302）YYS 都是空的；写 `5 languages` 的
        # CU26061701 才是 5。所以「1」要留空，不能落成 1。
        result["YYS"] = ""
        logger.info(f"  来源: versions（规范键）={versions_raw!r} → 语言数 {lang_count}")
        logger.info("  映射值: YYS 留空（档案 rules.versions.empty_when_single）")
        logger.info("  依据: 单语言不落库 —— 附件写「1 language」的两单，ERP 里 YYS 均为空")
    else:
        assign("YYS", lang_count,
               f"versions（规范键）={versions_raw!r} 用 rules.versions.language_pattern 提取"
               f"（覆盖档案固定值）", 14)

    # ── 5. 数量 CPSL1 ~ CPSL10 ────────────────────────────
    # 两种口径由档案 rules.quantity.mode 决定：
    #   tiers  —— igl 式：客户给一排档位（3k/5k/7.5k）→ 依次落 CPSL1~CPSL10
    #   single —— NGV 式：客户只给一个总数 → 落 CPSL1，其余留 0
    qty_mode = str(profile.rule("quantity.mode", "tiers")).strip().lower()
    logger.field_mapping("CPSL1~CPSL10", 15, total_fields)
    if qty_mode == "single":
        qty_text = str(customer_data.get("quantity") or "")
        qty = parse_single_quantity(qty_text, logger)
        if qty:
            result["CPSL1"] = qty
            assigned.append("CPSL1")
            logger.info(f"  来源: quantity（规范键）={qty_text!r}（档案 quantity.mode=single）")
            logger.info(f"  映射: CPSL1 = {qty}，CPSL2~CPSL10 保持默认值 0")
            logger.info("  状态: 成功")
            success_count += 1
        else:
            logger.warn(f"  来源: quantity（规范键）={qty_text!r} 取不到有效数量 "
                        f"→ CPSL1 保持默认值 0，需人工确认")
            default_count += 1
    else:
        tiers = list(customer_data.quantity_tiers or [])
        if tiers:
            for idx, qty in enumerate(tiers, start=1):
                result[f"CPSL{idx}"] = qty
                assigned.append(f"CPSL{idx}")
            logger.info(f"  来源: 数量档位（档案 rules.quantity_tiers 识别）{tiers}")
            logger.info(f"  映射: CPSL1~CPSL{len(tiers)} = {tiers}")
            if len(tiers) < 10:
                logger.info(f"  未使用的档位 CPSL{len(tiers) + 1}~CPSL10 保持默认值 0")
            logger.info("  状态: 成功")
            success_count += len(tiers)
        else:
            logger.warn("  来源: 未识别到数量档位行，CPSL1~CPSL10 全部保持默认值 0")
            logger.info("  提示: 检查客户文件写法与档案 rules.quantity_tiers 的开关")

    # ── 6. 规格 CD / KD / GD ───────────────────────────────
    logger.field_mapping("CD/KD", 27, total_fields)
    size_str = customer_data.get("size")
    # 档案 rules.size.zero_tops：该类别录系统时尺寸一律写 0/0/0。
    # 业务依据（2026-09-24）：NGV 的套装（盒子 —— 卡盒、卡片+底座组合）不按单件尺寸录入。
    # 附件的 TPS 是盒内卡片的尺寸（160x104 / 100x155），但 ERP 记录里 CD/KD/GD 全是 0。
    zero_tops = [str(t) for t in (profile.rule("size.zero_tops", []) or [])]
    size_info: dict = {}
    if cplb_top and cplb_top in zero_tops:
        for field in ("CD", "KD", "GD"):
            result[field] = 0
        logger.info(f"  来源: 档案 rules.size.zero_tops —— 产品类别顶层「{cplb_top}」"
                    f"录系统尺寸一律写 0/0/0（附件 TPS={size_str or '未提供'} 不采用）")
        logger.info("  映射值: CD=0 KD=0 GD=0")
        logger.info("  状态: 成功（业务决定，非「取不到默认值」）")
        success_count += 3
        assigned.extend(["CD", "KD", "GD"])
    elif size_str:
        logger.info(f"  来源: size（规范键）={size_str}")
        parsed = parse_size(size_str, profile, logger)
        size_info = parsed
        for field in ("CD", "KD"):
            if field in parsed:
                result[field] = parsed[field]
                assigned.append(field)
                logger.info(f"  解析: {field}={parsed[field]}")
        if "GD" in parsed:
            result["GD"] = parsed["GD"]
            assigned.append("GD")
            logger.info(f"  映射值: GD={parsed['GD']}（尺寸里直接给了厚度）")
        elif "CD" in parsed and "KD" in parsed:
            logger.info("  D尺寸未提供，将计算得出")
        if "CD" in parsed and "KD" in parsed:
            success_count += 2
            logger.info("  状态: 成功")
        else:
            logger.warn("  尺寸解析出的段数不足（需要至少 长 x 宽）")
            default_count += 2
    else:
        logger.warn("  来源: 未提供 size，使用默认值")
        default_count += 2

    # ── 7. 厚度 GD（没直接给时按页数 + 纸张算）──────────────
    # 档案可用 rules.thickness.enabled 关掉：NGV 的 ERP 导出里 GD 恒为 0
    # （书本的厚度写在 CPMS 里，不进 ERP 规格），所以对 NGV 关闭本步。
    logger.field_mapping("GD", 29, total_fields)
    if not profile.rule("thickness.enabled", True):
        logger.info("  来源: 档案 rules.thickness.enabled=false —— 该客户不按页数推算厚度，"
                    "GD 保持模板默认值 0")
        default_count += 1
    elif not result.get("GD"):
        extent_str = customer_data.get("extent")
        text_material = customer_data.get("text_material")
        cover_material = customer_data.get("cover_material")
        if extent_str and text_material and cover_material:
            logger.info("  来源: 计算")
            logger.info(f"    - extent（规范键）: {extent_str}")
            logger.info(f"    - text_material（规范键）: {text_material}")
            logger.info(f"    - cover_material（规范键）: {cover_material}")
            extent_pp, extent_cover = parse_extent(extent_str)
            pp_faw, _ = parse_material(text_material)
            cover_faw, _ = parse_material(cover_material)
            logger.info(f"    - 解析: pp={extent_pp}, cover={extent_cover}")
            logger.info(f"    - 解析: pp_faw={_num(pp_faw)}g, cover_faw={_num(cover_faw)}g")
            if extent_pp > 0 and pp_faw > 0 and cover_faw > 0:
                pp_style = resolve_page_style(text_material, "text", profile, logger)
                cover_style = resolve_page_style(cover_material, "cover", profile, logger)
                result["GD"] = calculate_thickness(
                    extent_pp, pp_faw, extent_cover, cover_faw, pp_style, cover_style, logger)
                assigned.append("GD")
                logger.info(f"  映射值: {result['GD']}")
                logger.info("  状态: 成功")
                success_count += 1
            else:
                logger.warn("  计算参数不足，使用默认值")
                default_count += 1
        else:
            logger.warn("  来源: 未提供（需要 extent + text_material + cover_material），使用默认值")
            default_count += 1
    else:
        logger.info("  已由尺寸解析提供，跳过计算")

    # ── 8. 书脊方向 Drop_SJFX ──────────────────────────────
    logger.field_mapping("Drop_SJFX", 9, total_fields)
    cd = result.get("CD", 0) or 0
    kd = result.get("KD", 0) or 0
    spine_mode = str(profile.rule("spine.mode", "cd_vs_kd"))
    # （cplb_top 已在步骤 2 算好，见上方 —— 这里直接用）

    if spine_mode == "binding_side":
        # NGV 口径：装订行右边给了 `<N> mm side`，N 等于较长边即长书脊。
        # 依据：CU26072302 的 145x210 + `210 mm side` → ERP Drop_SJFX=长书脊。
        skip_tops = [str(t) for t in (profile.rule("spine.skip_tops", []) or [])]
        side = _to_number(customer_data.get("binding_side") or "")
        # 装订方向原文（`at the top` / `at the head` …）：**只进 CPMS，不参与 ERP 判定**。
        # 业务口径（2026-10-20 二次拍板）：**识别不到装订方向 → 书脊方向 Drop_SJFX 留空（JSON null）**。
        # 不落「长书脊」：那是猜的。「顶/底/左/右」是装订边位置，和 ERP 的长/短书脊
        # 不是同一维度，反推不出长/短 → 认不出来就空着，交给业务在 ERP 里补。
        direction = str(customer_data.get("binding_direction") or "").strip()
        if cplb_top and cplb_top in skip_tops:
            logger.info(f"  来源: 档案 rules.spine.skip_tops —— 产品类别顶层「{cplb_top}」"
                        f"无书脊方向，保持空值")
        elif cd > 0 and kd > 0 and side:
            longer = max(cd, kd)
            if side >= longer:
                result["Drop_SJFX"] = str(profile.rule("spine.long", "长书脊"))
            else:
                result["Drop_SJFX"] = str(profile.rule("spine.short", "短书脊"))
            logger.info(f"  来源: 装订边（规范键 binding_side={_num(side)}mm）"
                        f"与较长的边 {_num(longer)}mm 比较")
            if direction:
                logger.info(f"  装订方向原文: {direction!r}（只写进 CPMS）")
            logger.info(f"  映射值: {result['Drop_SJFX']}")
            logger.info("  状态: 成功")
            success_count += 1
            assigned.append("Drop_SJFX")
        else:
            # 没有 `N mm side`：用「装订边位置 + 版式」换算书脊在长边还是短边。
            # 业务 2026-10-20 拍板 CD=长/KD=宽 之后这一步是几何换算，不是猜
            # （真单 BJXQ260708007：148x210 portrait + at the top → 短书脊）。
            derived = spine_from_binding_direction(direction, size_info.get("_raw") or [],
                                                   str(customer_data.get("size") or ""))
            if derived:
                result["Drop_SJFX"] = str(profile.rule(
                    "spine.long" if derived == "长书脊" else "spine.short", derived))
                logger.info(f"  来源: 装订方向原文 {direction!r} + 版式"
                            f"（无 `N mm side`）→ {result['Drop_SJFX']}")
                logger.info("  映射值: " + str(result["Drop_SJFX"]))
                logger.info("  状态: 成功")
                success_count += 1
                assigned.append("Drop_SJFX")
            else:
                # 业务口径 2026-10-20：识别不到 → **留空（JSON null）**，不猜「长书脊」。
                fallback = str(profile.rule("spine.default", "") or "").strip()
                result["Drop_SJFX"] = fallback or None
                logger.warn("  识别不到装订边/装订方向 → 书脊方向**留空**（null），"
                            "按业务口径不猜长/短书脊，交业务在 ERP 里补")
                if direction:
                    logger.info(f"  装订方向原文: {direction!r}（推不出长/短 → 不落库）")
                default_count += 1
    elif cd > 0 and kd > 0:
        logger.info(f"  来源: 尺寸比较（档案 rules.spine.mode={spine_mode}）")
        if cd > kd:
            result["Drop_SJFX"] = str(profile.rule("spine.long", "长书脊"))
            logger.info(f"  判断: CD > KD ({cd} > {kd}) -> {result['Drop_SJFX']}")
        else:
            result["Drop_SJFX"] = str(profile.rule("spine.short", "短书脊"))
            logger.info(f"  判断: CD <= KD ({cd} <= {kd}) -> {result['Drop_SJFX']}")
        logger.info("  状态: 成功")
        logger.info("  提示: 合法枚举（短书脊 / 长书脊 / 四边裁）见 assets/erp_form_schema.yaml；"
                    "「四边裁」需业务给出判据后再加规则")
        success_count += 1
        assigned.append("Drop_SJFX")
    else:
        logger.warn("  来源: 尺寸数据不足，使用默认值")
        default_count += 1

    # ── 8b. 单位 DWMC（档案 rules.unit：按产品类别顶层定「本 / 套」）──
    # 业务依据（2026-09-23）：NGV 的书本用「本」、套装（含盒子/卡片组合）用「套」，
    # 4 份 ERP 导出核对通过。档案没写 rules.unit 的客户不受影响。
    unit_cfg = profile.rule("unit")
    if isinstance(unit_cfg, dict) and unit_cfg:
        unit_default = str(unit_cfg.get("default", "") or "")
        unit_value = unit_default
        for entry in unit_cfg.get("map") or []:
            if str(entry.get("cplb_top", "")) and str(entry.get("cplb_top")) == cplb_top:
                unit_value = str(entry.get("unit", unit_default))
                break
        if unit_value:
            assign("DWMC", unit_value,
                   f"档案 rules.unit（产品类别顶层「{cplb_top or '未知'}」→ {unit_value}）", 13)

    # ── 9. 装订方式 ZDFS（档案按需开启）─────────────────────
    binding_keys = profile.rule("binding.match_keys", ["binding"]) or ["binding"]
    binding_text = " ".join(
        str(customer_data.get(k) or "") for k in binding_keys
    ).strip()
    if profile.rule("binding.enabled", False):
        mapped = ""
        text = str(binding_text).lower()
        for key, value in (profile.rule("binding.map", {}) or {}).items():
            if str(key).lower() in text:
                mapped = str(value)
                logger.info(f"  装订映射: 命中 '{key}' → {mapped}")
                break
        if mapped:
            assign("ZDFS", mapped, f"binding（规范键）={binding_text}", 10)
        else:
            fallback_binding = str(profile.rule("binding.default", ""))
            logger.warn(f"  装订方式未命中映射表（binding={binding_text!r}）"
                        f"，使用档案默认值 {fallback_binding!r}")
            result["ZDFS"] = fallback_binding
            default_count += 1
    elif binding_text:
        logger.info(f"  装订方式 ZDFS: 档案未启用映射（binding={binding_text!r}，"
                    f"口径待业务确认 → 保持模板默认值）")

    # ── 10. 交货日期 JHRQ ──────────────────────────────────
    # 档案 rules.dates.JHRQ.mode: empty → 该客户交货日期不落库（保持空）。
    # 业务依据（2026-09-24）：NGV 的 4 份 ERP 导出 JHRQ 全是空，即使附件给了
    # ETA（CU26061701 写 2027-03-15）也不填 —— 交货日期到下单阶段才定。
    logger.field_mapping("JHRQ", 34, total_fields)
    if str(profile.rule("dates.JHRQ.mode", "from_key")).strip().lower() in ("empty", "none"):
        logger.info("  来源: 档案 rules.dates.JHRQ.mode=empty —— 该客户交货日期不落库")
        logger.info("  映射值: 保持空值（业务决定，不是「取不到默认值」）")
    else:
        jhrq_source = str(profile.rule("dates.JHRQ.from", "pub_month"))
        jhrq_value = customer_data.get(jhrq_source)
        if jhrq_value:
            assign("JHRQ", parse_date_to_datestr(jhrq_value, logger),
                   f"{jhrq_source}（规范键，档案 dates.JHRQ.from）={jhrq_value}", 34)
        else:
            logger.warn(f"  来源: 规范键 {jhrq_source} 未提供，使用默认值")
            default_count += 1

    # ── 11. 预计下单日期 WCRQ ──────────────────────────────
    # 同上：mode=empty → 不落库（NGV 4 份导出 WCRQ 全空）。
    if str(profile.rule("dates.WCRQ.mode", "today_plus_days")).strip().lower() in ("empty", "none"):
        logger.info("  来源: 档案 rules.dates.WCRQ.mode=empty —— 该客户预计下单日期不落库")
        logger.info("  映射值: 保持空值（业务决定，不是「取不到默认值」）")
    else:
        days = int(profile.rule("dates.WCRQ.days", 10))
        future_date = today() + timedelta(days=days)
        assign("WCRQ", future_date.strftime("%Y-%m-%d"),
               f"当前日期 + {days} 天（档案 dates.WCRQ.days）", 42)

    # ── 11b. 报价截止日期检查（不是 ERP 字段，是给业务的提醒）─────
    # 业务 2026-09-24：「超过了报价截止日期，你记得提醒」。
    # 结果挂到 `_deadline`，由 step5b 跳过（`_` 前缀）、step6 弹出写进
    # validation.json 的 reminders，并在跑完的 stdout 末尾单独提醒。
    deadline_info = check_offer_deadline(customer_data, profile, logger)
    if deadline_info:
        result["_deadline"] = deadline_info

    # ── 12. 单号类字段：KHDH 客户单号 / KHDDH 香港单号 ──────
    # 档案 `email:` 段可以声明**多组**单号规则，每组用 `target` 指定落到哪个 ERP 字段：
    #     email.khdh:  { target: KHDH,  ... }   # 客户单号
    #     email.khddh: { target: KHDDH, ... }   # 香港单号
    # ⚠️ 2026-09-24 修正：这两个键只差一个字母，此前混用 —— 附件里的
    #   「香港单号 CU26051601-01」被写进了 KHDH（客户单号），而模板第 43 键
    #   KHDDH（第 43 键，中文标签就是「香港单号」）永远是空的。4 份权威 ERP 导出显示：
    #   香港单号进 KHDDH、KHDH 是空的。现在按 target 各归各位。
    # 取值优先级（每组独立）：邮件标题/正文（sidecar）→ 附件列 → 全表扫描到的规范键。
    number_rules = profile.number_rules()
    if not number_rules:
        logger.warn("  档案 email 段没有声明单号规则 → 单号相关字段保持空值")
    for rule_key, rule_cfg, target in number_rules:
        target_index = list(template.keys()).index(target) + 1 if target in template else 0
        logger.field_mapping(target, target_index, total_fields)
        value = ""
        source = ""
        email_numbers = email_meta.get("numbers") or {}
        from_email = str(email_numbers.get(target) or "").strip()
        if not from_email and target == "KHDH":
            # 老 sidecar 兼容：ingest_email 早期只写 khdh 一个键
            from_email = str(email_meta.get("khdh") or "").strip()
        if from_email:
            value, source = from_email, f"邮件（{email_meta.get('khdh_source') or '标题/正文'}）"
        if not value:
            for key in rule_cfg.get("attachment_keys") or []:
                found = customer_data.get(key)
                if found:
                    value, source = str(found), f"附件列（规范键 {key}）"
                    break
        if not value:
            scanned = str(customer_data.get(rule_key) or "").strip()
            if scanned:
                value, source = scanned, f"附件扫描（规范键 {rule_key}）"
        if value:
            assign(target, value, source, target_index)
        else:
            logger.warn(f"  {target}（{rule_key}）: 邮件标题/正文与附件都没取到，保持空值")
            if not email_meta:
                logger.warn("  提示: 用 ingest_email.py 投递邮件可自动带上标题/正文里的单号")
            default_count += 1

    # ── 12b. 产品属性 CPSX（年龄段 + 安全检查 → ERP 中文枚举）──
    # 业务确认（2026-09-23）：这两项**要落库**，落点就是 CPSX。
    # 注意：落库**不替代** CPMS —— 产品描述里仍须写清年龄段与安全要求。
    cpsx_index = list(template.keys()).index("CPSX") + 1 if "CPSX" in template else 0
    logger.field_mapping("CPSX", cpsx_index, total_fields)
    safety = customer_data.get("safety_checks")
    age = customer_data.get("target_age_group")
    if safety or age:
        logger.info(f"  客户文件: 安全检查={safety or '未提供'} / 目标年龄段={age or '未提供'}")
    cpsx_value, cpsx_source = derive_cpsx(customer_data, profile, logger)
    # 推不出来时的口径（档案 rules.cpsx.when_missing）：
    #   空   → 留空 + WARN（公共默认：不猜）
    #   非空 → 落该值（NGV 业务 2026-09-24：「CPSX 如果没有获取到就显示**无**」）
    cpsx_missing = str(profile.rule("cpsx.when_missing", "") or "").strip()
    if cpsx_value:
        legal, status = normalize_value("CPSX", cpsx_value)
        assign("CPSX", legal, f"{cpsx_source}（档案 rules.cpsx 推导）", cpsx_index)
        if status == "unknown":
            logger.warn(f"  产品属性 {legal!r} 不在 ERP 枚举里 —— 检查客户档案 rules.cpsx 的取值")
    elif cpsx_missing:
        legal, status = normalize_value("CPSX", cpsx_missing)
        assign("CPSX", legal, "档案 rules.cpsx.when_missing（该客户约定：取不到就落此值）", cpsx_index)
        logger.info(f"  产品属性 CPSX: 推不出年龄段 → 按客户口径落 {legal!r}")
        if status == "unknown":
            logger.warn(f"  产品属性 {legal!r} **不在 ERP 下拉枚举里**（枚举: 三岁以上 / 三岁以下 / "
                        f"其他 / 3岁以下（要寄第三方测试）/ 3岁以上（要寄第三方测试））—— "
                        f"导入前请业务确认：是要「{legal}」还是 ERP 的兜底项「其他」"
                        f"（改 customers/{profile.id}.yaml 的 rules.cpsx.when_missing 一处即可）")
    else:
        logger.warn("  产品属性 CPSX: 推导不出（缺年龄段）→ 保持模板默认值，需人工确认")
        default_count += 1
    if safety or age:
        logger.info("  提示: 年龄段/安全检查已落库到 CPSX，但**仍必须**写进 CPMS（见 5.1.6）")

    # ── 12c. FSC 类别 FSCMC ────────────────────────────────
    # 客户文件里 FSC 常写在纸张行（"230 gsm art card C1S (FSC Mix)"），
    # 由档案 rules.scan 扫出规范键 fsc 后落到 FSCMC（取值按 ERP 枚举归一化）。
    fsc_value = str(customer_data.get("fsc") or "").strip()
    fsc_index = list(template.keys()).index("FSCMC") + 1 if "FSCMC" in template else 0
    if fsc_value:
        legal, status = normalize_value("FSCMC", fsc_value)
        assign("FSCMC", legal, f"fsc（规范键，档案 rules.scan 扫描）={fsc_value!r}", fsc_index)
        if status == "unknown":
            logger.warn(f"  FSC 类别 {legal!r} 不在 ERP 枚举里 —— 检查客户文件写法")
    else:
        # 扫描不到有两种情况：文件里根本没写 FSC，或**写了多个不同取值**被
        # 档案 `unique: true` 判为「不唯一就留空」（业务确认 2026-09-24）。
        # 后者是真信息，不能混成一句「没写」。
        notes = [str(n) for n in (getattr(customer_data, "scan_notes", None) or [])
                 if "fsc" in str(n)]
        if notes:
            logger.warn(f"  FSC 类别 FSCMC: {notes[0]}")
            logger.info("  → 保持模板默认值（空）；多材质混用时该填哪个推不出来，由业务核对")
        else:
            logger.info("  FSC 类别 FSCMC: 客户文件里没写 → 保持模板默认值（空）")

    # ── 13. 产品描述 CPMS —— 脚本不生成 ────────────────────
    logger.field_mapping("CPMS", 56, total_fields)
    logger.warn("  CPMS 留空 —— 脚本不生成产品描述，必须由 agent 亲自补写（全文翻译 + 分类分段）")
    logger.info("  来源: 无（等待 agent 亲力亲为）")
    default_count += 1

    # ── 14. 业务口径：值为「无」的字段一律落 JSON null ──────────
    # 业务 2026-09-24：「CPSX 或者其他值的『无』使用 json 里的 null 填充」。
    # 为什么这样最干净：ERP 自己的导出里**空值就是 null**（权威导出 `CPLD`/`KHDH`/`CPSX`
    # 都是 None），所以「无」落 null 既符合业务语言、又天然不触发下拉枚举校验
    # （null 属空值），比在字符串里塞一个枚举外的「无」正确得多。
    # 取值来自档案 `rules.null_values`（公共默认 ["无"]）—— 只有**整格恰好等于**这些词
    # 才转换，长文本里出现的「无」不受影响。
    null_values = [str(v).strip() for v in (profile.rule("null_values", ["无"]) or [])
                   if str(v).strip()]
    if null_values:
        converted = []
        for field, value in list(result.items()):
            if isinstance(value, str) and value.strip() in null_values:
                result[field] = None
                converted.append(f"{field}({value!r}→null)")
        if converted:
            logger.info(f"  业务口径 null_values={null_values} → 落 JSON null 的字段: "
                        f"{', '.join(converted)}")
            logger.info("  说明: ERP 自身导出里空值也是 null，导入端按空值处理")

    # ── 汇总 ───────────────────────────────────────────────
    logger.info("")
    logger.info(f"剩余 {total_fields - len(assigned)} 个字段使用模板默认值")
    default_count += total_fields - len(assigned)
    logger.info("")
    logger.info("字段映射完成:")
    logger.info(f"  成功: {success_count} 个")
    logger.info(f"  使用默认值: {default_count} 个")
    logger.info(f"  失败: {failed_count} 个")
    logger.info(f"  被赋值的字段（{len(assigned)} 个）: {' '.join(assigned)}")
    return result


# ── 输出 ─────────────────────────────────────────────────────────────────
def generate_output_filename(task_id: str) -> str:
    now = datetime.now()
    return f"ERP_{task_id}_{now.strftime('%Y%m%d_%H%M%S')}.json"


def create_task_directory(base_dir: Path, task_id: str) -> Path:
    task_dir = base_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def convert_to_csv(input_path: Path, output_dir: Path, logger: Logger) -> Path:
    """把输入转成 CSV 存进任务目录（文本类文件走 read_text_document）。"""
    import shutil
    import pandas as pd

    ext = input_path.suffix.lower()
    output_path = output_dir / f"{input_path.stem}.csv"

    if ext == ".csv":
        shutil.copy2(input_path, output_path)
        logger.info("  源文件已是CSV格式，直接复制")
        return output_path

    if ext in (".docx", ".txt", ".pdf"):
        from extract_fields import read_text_document
        content = read_text_document(str(input_path))
        with open(output_path, "w", encoding="utf-8-sig") as f:
            f.write("Content\n")
            f.write(content.replace("\n", " "))
        logger.info("  文本文件已转换为CSV格式")
        return output_path

    pd.read_excel(input_path).to_csv(output_path, index=False, encoding="utf-8-sig")
    return output_path


def move_file(source: Path, dest_dir: Path, new_name: str = None) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / (new_name or source.name)
    counter = 1
    original_dest = dest_path
    while dest_path.exists():
        dest_path = dest_dir / f"{original_dest.stem}_{counter}{original_dest.suffix}"
        counter += 1
    source.rename(dest_path)
    return dest_path


# ── 步骤 ─────────────────────────────────────────────────────────────────
def sample_run_guard(input_path: Path, args) -> Optional[str]:
    """语料库样本不许跑进真交付区。返回错误文本 = 拒绝执行；None = 放行。

    为什么是**硬闸**而不是步骤 1 里那句 WARN：`main.py` 跑完会把**输入文件移走**
    （`[文件 5/5] 原始文件归档`），而 `customer-files/samples/<客户>/` 是 .gitignore
    掉的**真实客户原文** —— 移走就是永久丢失，相关测试随即大面积 FileNotFoundError。
    这个坑 2026-09-24 踩了两次；第二次还顺手往 `processed/` 里留下一份 CPMS 为空的
    「未完成」产物（业务问出来才发现）。当时脚本只打了 WARN 就继续跑了。

    放行条件：把**两个**归档目录都指到语料库之外（`--processed-dir` + `--failed-dir`），
    也就是 SKILL 第 10 节给的写法。只想抽取/校准就直接用 `extract_fields.py`。
    """
    try:
        input_path.resolve().relative_to(SAMPLES_DIR.resolve())
    except (ValueError, OSError):
        return None                     # 不关它的事
    missing = [flag for flag, value in (("--processed-dir", args.processed_dir),
                                        ("--failed-dir", args.failed_dir)) if not value]
    if not missing:
        return None                     # 两个归档目录都在语料库之外，安全
    return (
        f"拒绝执行：输入是语料库样本 —— {input_path}\n"
        f"  main.py 跑完会把输入文件**移走**归档，而 samples/ 是 .gitignore 掉的客户原文，丢了找不回。\n"
        f"  要么把归档目录指到语料库之外（缺 {' '.join(missing)}）：\n"
        f"      uv run ./scripts/main.py \"{input_path}\" -c <客户> \\\n"
        f"          --processed-dir /tmp/cqr_scratch/processed --failed-dir /tmp/cqr_scratch/failed\n"
        f"  要么只做抽取/校准（不动原文件）：uv run ./scripts/extract_fields.py \"{input_path}\" -c <客户>"
    )


def _as_unit(value) -> InputUnit:
    """兼容老调用：允许直接传 Path（= 单文件单元）。"""
    if isinstance(value, InputUnit):
        return value
    return resolve_input_unit(value)


def read_attachment_meta_for_unit(unit: InputUnit, logger=None) -> dict:
    """读「单元」的邮件副档。

    单文件：跟以前一样读 `<文件>.meta.json`；
    文件夹 / 压缩包：`_email.meta.json`（ingest_email 投递多附件邮件时写的那种）。
    """
    if unit.is_single_file:
        return read_attachment_meta(unit.path)
    meta_path = find_unit_meta(unit)
    if not meta_path:
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        if logger is not None:
            logger.warn(f"邮件副档读不了，忽略: {meta_path}")
        return {}


def convert_unit_to_csv(unit: InputUnit, customer_data: CustomerData,
                        output_dir: Path, logger: Logger) -> Path:
    """把「单元合并后的抽取结果」写成 CSV（长表：文件 / 规范键 / 值）。

    单文件走 `convert_to_csv`（源文件快照）；单元没有单一「源表」，
    所以 CSV 记的是**合并后的规范键 + 每条的出处文件**，方便人工核对。
    """
    import csv

    output_path = output_dir / f"{unit.stem}.csv"
    rows = [("来源文件", "规范键", "值")]
    for key, value in (customer_data.values or {}).items():
        rows.append(((customer_data.sources or {}).get(key, ""), key, str(value)))
    for key, value in (customer_data.scanned or {}).items():
        rows.append(("（全表扫描）", key, str(value)))
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    logger.info(f"  单元合并表（{len(rows) - 1} 行，含出处列）")
    return output_path


def step1_input_validation(unit: InputUnit, logger: Logger) -> bool:
    unit = _as_unit(unit)
    logger.section("步骤 1: 输入验证")
    # 业务口径（2026-10-20）：单文件 = 独一份报价；文件夹 / 压缩包 = 这一份报价的全部参考文件。
    input_path = unit.path
    logger.info(f"输入单元: {input_path}（{unit.label}）")
    if unit.role == "root_files":
        logger.warn("父目录根下的散文件按口径 1 单独成一份 —— "
                    "若它其实是给各子目录共用的说明，请人工复制进各子目录后重跑")
    if not unit.is_single_file:
        logger.info(f"单元成员 {len(unit.members)} 个（合并成 **一份** 报价）:")
        for member in unit.members:
            logger.info(f"  - {member.name}")
        if unit.skipped:
            logger.warn(f"已跳过 {len(unit.skipped)} 个不支持/临时文件: "
                        f"{[Path(s).name for s in unit.skipped][:8]}")

    if unit.is_single_file:
        if input_path.suffix.lower() not in SUPPORTED_INPUT_SUFFIXES:
            logger.error(f"文件格式检查: 失败 (不支持的格式 {input_path.suffix})")
            return False
        logger.info(f"文件格式检查: 通过 ({input_path.suffix})")
        logger.info(f"文件大小: {input_path.stat().st_size} 字节")
    else:
        logger.info(f"单元类型: {unit.kind_cn} —— 成员文件格式均已校验")

    if "pending" not in input_path.parts:
        logger.warn(f"文件不在 pending 目录，可能已被处理: {input_path}")

    logger.info("步骤 1 完成，状态: 成功")
    return True


def step2_load_customer_data(unit: InputUnit, profile: CustomerProfile,
                             logger: Logger) -> CustomerData:
    unit = _as_unit(unit)
    logger.section("步骤 2: 读取客户数据（客户档案驱动）")
    logger.info(f"客户档案: {profile.id}（{profile.name}，状态 {profile.status}）")
    logger.info(f"布局策略: {profile.rule('layout.strategies', [])}")
    if not unit.is_single_file:
        logger.info(f"输入单元: {unit.label} —— "
                    f"{len(unit.members)} 个参考文件合并成一份报价")

    customer_data = load_customer_unit(unit, profile, logger)
    logger.info(f"布局命中: {customer_data.layout or '(无)'}")
    logger.info(f"提取规范键: {len(customer_data.values)} 个")
    for key, value in customer_data.values.items():
        src = (customer_data.sources or {}).get(key)
        logger.info(f"  - {key}: {value}" + (f"   [{src}]" if src else ""))
    if customer_data.quantity_tiers:
        logger.info(f"  - 数量档位: {customer_data.quantity_tiers}")
    if customer_data.conflicts:
        logger.warn(f"单元内有 {len(customer_data.conflicts)} 处同键不同值 —— "
                    f"交付前必须人工核对（明细见 .extracted.json 的 conflicts）")
    if customer_data.unmatched:
        logger.warn(f"有 {len(customer_data.unmatched)} 个标签未识别: {customer_data.unmatched[:8]}")
        logger.warn(f"  → 用 calibrate_customer.py --customer {profile.id} 校准档案")
    if customer_data.extras_found:
        logger.info(f"已知附加信息（非 ERP 字段，必须写进 CPMS）共 {len(customer_data.extras_found)} 项:"
                    f" {customer_data.extras_found[:10]}")

    logger.info("步骤 2 完成，状态: 成功")
    return customer_data


def step3_load_erp_template(logger: Logger) -> dict:
    logger.section("步骤 3: 读取 ERP 模板")
    logger.info(f"模板文件: {TEMPLATE_PATH}")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = json.load(f)
    logger.info(f"模板字段数: {len(template)}")
    for key, value in list(template.items())[:5]:
        logger.info(f"  - {key}: {value}")
    logger.info(f"  ... (共 {len(template)} 个字段)")
    logger.info("步骤 3 完成，状态: 成功")
    return template


def step5_map_fields(customer_data: CustomerData, template: dict, profile: CustomerProfile,
                     logger: Logger, email_meta: dict) -> dict:
    logger.section("步骤 5: 字段映射")
    result = map_fields(customer_data, template, profile, logger, email_meta)
    logger.info(f"步骤 5 完成，状态: 成功，耗时: {logger.elapsed_time():.1f}s")
    return result


def step5b_validate_erp_values(result: dict, logger: Logger,
                              profile: Optional[CustomerProfile] = None) -> list:
    """步骤 5b: 按 ERP 表单权威字典归一化 + 校验取值。

    字典 = assets/erp_form_schema.yaml（从真实 ERP 表单转录的合法枚举）。
    目的：客户/脚本给出的值必须对齐 ERP 下拉框，否则导入时会被丢弃或报错。
    历史教训：0e39e2e2（2026-05-25）ZDFS 填了「无线胶装」，ERP 只认「胶装/PAD书」。

    客户档案可用 `rules.required_empty` 声明「本客户这几项本来就该是空的」，
    避免字典的通用必填规则产生误报（NGV：KHDH、套装单的 Drop_SJFX）。
    """
    logger.section("步骤 5b: ERP 取值归一化与校验")

    result, changes = normalize_result(result, logger)
    normalized = [c for c in changes if c["status"] == "normalized"]
    if normalized:
        logger.info(f"归一化 {len(normalized)} 个字段（对齐 ERP 枚举）")
    else:
        logger.info("无需归一化：取值已全部是 ERP 合法值")

    allow_empty = []
    allow_values: dict = {}
    if profile is not None:
        allow_empty = [str(f) for f in (profile.rule("required_empty", []) or [])]
        if allow_empty:
            logger.info(f"按档案豁免必填校验的字段（本客户就该为空）: {allow_empty}")
        # 口径例外：业务拍板用、但不在 ERP 下拉枚举里的取值（如 NGV 的 CPSX=无）。
        # 不加这层的话**每一单**都会红一次，把 0-error 闸门变成噪声。
        allow_values = declared_exceptions(profile.rule("enum_exceptions", []))
        for f, vals in allow_values.items():
            logger.info(f"按档案声明的口径例外（不在 ERP 枚举里，报 WARN 不报 error）: "
                        f"{f} = {' / '.join(vals)}")
    issues = validate_result(result, allow_empty=allow_empty, allow_values=allow_values,
                             internal_fields=internal_field_keys())
    errors = [i for i in issues if i["level"] == "error"]
    warns = [i for i in issues if i["level"] == "warn"]

    for issue in errors:
        logger.error(f"[ERP字典] {issue['field']}: {issue['message']}")
    for issue in warns:
        logger.warn(f"[ERP字典] {issue['field']}: {issue['message']}")

    if not issues:
        logger.info("校验通过：枚举 / 类型 / 必填三项全过")
    else:
        logger.info(f"校验结果: error {len(errors)} 个，warn {len(warns)} 个"
                    f"（明细见任务目录的 .validation.json）")

    logger.info("步骤 5b 完成，状态: 成功")
    return issues


def step6_output_results(unit: InputUnit, result: dict, processed_dir: Path,
                         logger: Logger, customer_data: CustomerData,
                         profile: CustomerProfile, issues: Optional[list] = None,
                         strip_internal: bool = False) -> dict:
    unit = _as_unit(unit)
    logger.section("步骤 6: 输出结果")

    input_path = unit.path
    stem = unit.stem
    task_id = result.pop("_task_id", "unknown")
    # 报价截止日期提醒（get 前先弹掉，否则下面的键断言会因为多一个键而失败）
    deadline_info = result.pop("_deadline", None)
    # 内部字段的取值（`CPMC_ZH` 等）：由 map_fields 放进下划线键，这里取出、稍后落到内部字段
    cpmc_zh = str(result.pop("_cpmc_zh", "") or "")
    logger.info(f"任务ID: {task_id}")

    # 给业务的提醒（业务 2026-09-24：「这提醒现在每次跑都会自动出来」→ 固化成一个字段）。
    # 结构化版本进 validation.json 的 reminders；文本版本进内部字段 `Reminder` + stdout 醒目块。
    # 这里就准备好，因为内部字段在下面几行就要用上。
    reminders: list[dict] = []
    if deadline_info and deadline_info.get("expired"):
        reminders.append({
            "kind": "offer_deadline_expired",
            "deadline": deadline_info["deadline"],
            "days_over": deadline_info["days"],
            "message": f"{deadline_info['message']} —— 请与业务确认这单是否还要报价",
        })
    reminder_text = "\n".join(r["message"] for r in reminders)

    # 字段一致性断言：
    #   ① 前 59 键必须与 ERP 模板**键集合 + 顺序**完全一致（下游按位置读，一个都不许动）
    #   ② 其后只能跟 assets/internal_export_fields.yaml 声明的内部字段，顺序也要一致
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl_keys = list(json.load(f).keys())
    internal_fields = [] if strip_internal else load_internal_fields()
    internal_keys = [str(f["key"]) for f in internal_fields]
    out_keys = list(result.keys())
    erp_part = out_keys[:len(tpl_keys)]
    if erp_part != tpl_keys:
        missing = [k for k in tpl_keys if k not in erp_part]
        extra = [k for k in erp_part if k not in tpl_keys]
        logger.error(f"字段一致性断言失败: 缺失={missing} 多余={extra}")
        raise ValueError(f"输出字段与模板不一致: 缺失={missing} 多余={extra}")
    if out_keys != erp_part:
        raise ValueError(f"map_fields 多产出了未声明的键: "
                         f"{out_keys[len(tpl_keys):]}（只有 `_` 前缀的内部键才允许）")

    # 追加内部导出字段（业务 2026-09-24：ID 标注本次执行的任务，方便日志维护）
    providers = {
        "task_id": task_id,
        "cpmc_zh": cpmc_zh,
        "reminder": reminder_text,
        # 配置版本：来自客户档案顶层 version（公共默认值在 customers/_defaults.yaml）。
        # 业务 2026-09-24：产物要能一眼看出是哪一代口径跑出来的，方便技能迭代。
        "config_version": profile.version,
    }
    for spec in internal_fields:
        key, source = str(spec["key"]), str(spec.get("source", ""))
        if source not in providers:
            raise ValueError(f"内部字段 {key} 的 source={source!r} 没有实现"
                             f"（可用: {sorted(providers)}）—— 见 assets/internal_export_fields.yaml")
        result[key] = providers[source]
        logger.info(f"  内部字段 {key}（{spec.get('label', '')}）= "
                    f"{str(providers[source])[:60]!r}")

    # 追加后再断言一次：键 = ERP 59 键（原序）+ 声明的内部字段（原序）
    appended = list(result.keys())[len(tpl_keys):]
    if appended != internal_keys:
        raise ValueError(f"内部字段与声明不一致: 实际={appended} 声明={internal_keys}"
                         f"（见 assets/internal_export_fields.yaml）")
    logger.info(f"字段一致性断言: 通过（ERP {len(erp_part)} 字段顺序与模板一致"
                + (f"，其后追加内部字段 {internal_keys}" if internal_keys else
                   "；本次 --strip-internal → 只输出 ERP 字段")
                + "）")

    task_dir = create_task_directory(processed_dir, task_id)
    logger.info(f"创建任务目录: {task_dir}")

    output_files = []

    # 1. CSV 转换
    logger.info("")
    logger.info("[文件 1/5] CSV转换")
    logger.info(f"  源文件: {input_path.name}" + ("" if unit.is_single_file else f"（{unit.kind_cn}单元）"))
    if unit.is_single_file:
        csv_path = convert_to_csv(input_path, task_dir, logger)
    else:
        csv_path = convert_unit_to_csv(unit, customer_data, task_dir, logger)
    logger.info(f"  输出: {csv_path.name}")
    logger.info("  状态: 成功")
    output_files.append(csv_path)

    # 2. 抽取结果落盘（审计 + 给 agent 写 CPMS 用）
    logger.info("")
    logger.info("[文件 2/5] 抽取明细（规范键 / 未识别标签）")
    extracted_path = task_dir / f"{stem}.extracted.json"
    with open(extracted_path, "w", encoding="utf-8") as f:
        f.write(customer_data.to_json())
    logger.info(f"  输出: {extracted_path.name}")
    logger.info(f"  规范键: {len(customer_data.values)} 个"
                f"，未识别标签: {len(customer_data.unmatched)} 个")
    output_files.append(extracted_path)

    # 3. ERP JSON
    logger.info("")
    logger.info("[文件 3/5] ERP JSON")
    output_filename = generate_output_filename(task_id)
    logger.info(f"  文件名: {output_filename}")
    logger.info(f"  字段数: {len(result)}")
    output_path = task_dir / output_filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"  输出路径: {output_path}")
    logger.info("  状态: 成功")
    output_files.append(output_path)

    # 4. ERP 取值校验报告（按 assets/erp_form_schema.yaml 的合法枚举核对）
    logger.info("")
    logger.info("[文件 4/5] ERP 取值校验报告")
    issues = issues or []
    validation_path = task_dir / f"{stem}.validation.json"
    report = {
        "schema": "assets/erp_form_schema.yaml",
        "schema_version": 1,
        "customer": profile.id,
        "checked_fields": len([k for k in result if not k.startswith("_")]),
        "errors": [i for i in issues if i["level"] == "error"],
        "warnings": [i for i in issues if i["level"] == "warn"],
        "reminders": reminders,
        # 输入单元（一份报价由几个文件组成）+ 单元内的取值冲突，交付前必须核对
        "unit": {
            "kind": unit.kind,
            "kind_cn": unit.kind_cn,
            "label": unit.label,
            "role": unit.role,
            "path": str(unit.path),
            "origin": str(unit.origin) if unit.origin else None,
            # 压缩包拆成多份时：包只在第 1 份的任务目录里，其余份记包名 + 归属
            "source_archive": (unit.origin.name
                               if unit.from_archive and unit.origin else None),
            "archive_owner": unit.archive_owner,
            "members": [m.name for m in unit.members],
            "skipped": [Path(s).name for s in unit.skipped],
            # 口径 1（森西 2026-10-20 拍板）：根下散文件**单独成单**，不并进任何子目录。
            # 它也可能本来是「给各子目录共用的总则」—— 所以这里留一句话提醒人工看。
            "note": ("父目录根下的散文件按口径单独成一份；若它是给各子目录共用的说明，"
                     "请人工复制进各子目录后重跑" if unit.role == "root_files" else None),
        },
        "conflicts": customer_data.conflicts,
        "task_id": task_id,
        "summary": {
            "error": len([i for i in issues if i["level"] == "error"]),
            "warn": len([i for i in issues if i["level"] == "warn"]),
        },
    }
    with open(validation_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"  输出: {validation_path.name}")
    logger.info(f"  error {report['summary']['error']} 个 / warn {report['summary']['warn']} 个")
    for r in reminders:
        logger.warn(f"  ⚠ 提醒: {r['message']}")
    logger.info("  状态: 成功")
    output_files.append(validation_path)

    # 5. 原始文件归档
    logger.info("")
    logger.info("[文件 5/5] 原始文件归档")
    logger.info(f"  源路径: {input_path}")
    if unit.is_single_file:
        moved_path = move_file(input_path, task_dir)
    else:
        # 文件夹 / 压缩包：整包搬进任务目录（一份报价的所有参考文件必须待在一起）
        destination = task_dir / input_path.name
        shutil.move(str(input_path), str(destination))
        moved_path = destination
        logger.info(f"  {unit.kind_cn}整包归档（{len(unit.members)} 个参考文件）")
    logger.info(f"  目标路径: {moved_path}")
    logger.info("  状态: 成功")
    output_files.append(moved_path)

    # 同批拆分：原压缩包只随**第 1 份**归档（不复制 N 份）
    if unit.from_archive and unit.origin and unit.origin.exists():
        if unit.archive_owner:
            zip_dest = task_dir / unit.origin.name
            shutil.move(str(unit.origin), str(zip_dest))
            logger.info(f"  原压缩包一并归档（本批多份报价共用这一个来源包）: {zip_dest.name}")
            output_files.append(zip_dest)
        else:
            logger.info(f"  原压缩包不重复归档（同批第 1 份已归档）: {unit.origin}")

    # 邮件副档一起归档（保持任务自包含）
    meta_path = find_unit_meta(unit)
    if meta_path and meta_path.exists():
        if unit.is_batch_member and meta_path.parent != unit.path:
            # 同一批多份报价共用的副档：复制一份，原件留到批次收尾（finalize_batch）
            copied_meta = task_dir / meta_path.name
            shutil.copy2(meta_path, copied_meta)
            logger.info(f"  邮件副档一并归档（本批共用，复制一份）: {copied_meta.name}")
            output_files.append(copied_meta)
        else:
            moved_meta = move_file(meta_path, task_dir)
            logger.info(f"  邮件副档一并归档: {moved_meta.name}")
            output_files.append(moved_meta)

    logger.info("")
    logger.info("输出文件列表:")
    for f in output_files:
        logger.info(f"  - {f}")

    logger.info("步骤 6 完成，状态: 成功")

    return {
        "task_id": task_id,
        "task_dir": task_dir,
        "output_files": output_files,
        "erp_json": output_path,
        "extracted_json": extracted_path,
        "validation_json": validation_path,
        "deadline": deadline_info,
        "reminders": reminders,
    }


# ── 入口 ─────────────────────────────────────────────────────────────────
def move_unit_to_failed(unit: InputUnit, failed_dir: Optional[Path],
                        logger: Logger) -> Optional[Path]:
    """把这一份报价的原始输入挪进失败目录。

    压缩包拆出来的子单元活在临时解压目录里（随后 cleanup 会删掉），所以复制留证。
    """
    if not failed_dir:
        return None
    src = unit.path
    if not src.exists():
        logger.warn(f"失败原件已不在原处（可能已被归档）: {src}")
        return None
    try:
        failed_task_id = datetime.now().strftime("failed_%Y%m%d_%H%M%S")
        failed_task_dir = create_task_directory(failed_dir, failed_task_id)
        if unit.is_single_file:
            moved = move_file(src, failed_task_dir)
        elif unit.from_archive:
            destination = failed_task_dir / src.name
            shutil.copytree(src, destination)
            moved = destination
        else:
            destination = failed_task_dir / src.name
            shutil.move(str(src), str(destination))
            moved = destination
        logger.info(f"原始文件已移动到失败目录: {moved}")
        # 邮件上下文跟着走（同批复用的副档，重跑时不用再去找邮件）
        meta_path = find_unit_meta(unit)
        if meta_path and meta_path.exists() and unit.path not in meta_path.parents:
            shutil.copy2(meta_path, failed_task_dir / meta_path.name)
            logger.info(f"邮件副档一并留证: {meta_path.name}")
        return moved
    except Exception as move_error:
        logger.error(f"移动文件失败: {move_error}")
        return None


def process_unit(unit: InputUnit, args, processed_dir: Path,
                 logger: Logger) -> dict:
    """处理**一份**报价（一个输入单元）：步骤 1 → 6 + 给业务的提示。

    异常直接抛给调用方：这一份进失败目录，同批其它份继续跑。
    """
    if not step1_input_validation(unit, logger):
        raise ValueError("输入验证失败")

    email_meta = read_attachment_meta_for_unit(unit, logger)
    if email_meta:
        logger.info(f"读到邮件副档: 标题={email_meta.get('subject', '')[:60]!r} "
                    f"客户单号={email_meta.get('khdh') or '(无)'}")

    # 客户识别：**先看正文，再看文件名/邮件上下文**。
    # ⚠️ 2026-09-24 修的真 bug：只凭文件名打分曾被误判 —— NGV 的
    #   「160x104_Colouring Cards Aldi Süd…」因为文件名含 "colour" 命中了
    #   igl 的文件名关键词（10 分）就胜出，而正文里的 NGV 特征
    #   （enquiry / Angebotsfrist / VK-Verpackung … 500 分）压根没被看过，
    #   结果整单字段全错（CPLB 落到「32.00;其他」、erp_fixed 一个没上）。
    #   现在：表格类文件一律把**正文文本**一起送进识别，文件名只当加分项。
    probe_texts = []
    if email_meta:
        probe_texts = [email_meta.get("subject", ""), email_meta.get("body", "")]
    # 单元：所有成员的正文都参与识别（一份报价里哪份文件写了客户特征都算）
    for probe_member in unit.members:
        if probe_member.suffix.lower() in (".xlsx", ".xls", ".csv"):
            try:
                from customer_profile import collect_probe_texts
                probe_texts = probe_texts + collect_probe_texts(probe_member)
            except Exception as probe_error:
                logger.warn(f"读取 {probe_member.name} 正文用于客户识别失败（跳过该文件）: {probe_error}")
        elif probe_member.suffix.lower() in (".txt", ".docx", ".pdf"):
            try:
                from extract_fields import read_text_document
                probe_texts.append(read_text_document(str(probe_member))[:4000])
            except Exception as probe_error:
                logger.warn(f"读取 {probe_member.name} 正文用于客户识别失败（跳过该文件）: {probe_error}")

    profile, source = resolve_profile(
        args.customer, texts=probe_texts, filename=unit.stem,
        sender=email_meta.get("sender", ""),
        subject=email_meta.get("subject", ""), logger=logger,
    )
    if source != "显式指定":
        # 自动识别先用「文件名 + 邮件上下文」做探针；识别不到时再用文件内容正式判一次，
        # 避免因为文件名没线索而误走兜底档案。
        from customer_profile import auto_detect

        if source == "兜底" and probe_texts:
            try:
                candidates = auto_detect(probe_texts, filename=unit.stem)
                if candidates:
                    profile = candidates[0][0]
                    source = "自动识别（文件内容）"
                    logger.info(f"客户档案（文件内容识别）: {profile.id} 得分 {candidates[0][1]}")
            except Exception as detect_error:
                logger.warn(f"文件内容识别失败，沿用兜底档案: {detect_error}")

    logger.info(f"选定客户档案: {profile.id}（{source}）")
    logger.info(f"日志文件: {logger.log_file}")

    customer_data = step2_load_customer_data(unit, profile, logger)
    template = step3_load_erp_template(logger)
    result = step5_map_fields(customer_data, template, profile, logger, email_meta)
    issues = step5b_validate_erp_values(result, logger, profile)
    output_info = step6_output_results(
        unit, result, processed_dir, logger, customer_data, profile, issues,
        strip_internal=args.strip_internal)

    logger.info("")
    logger.info("========== 任务完成 ==========")
    logger.info(f"总耗时: {logger.elapsed_time():.1f} 秒")
    logger.info("处理状态: 成功")
    logger.info(f"任务ID: {output_info['task_id']}")
    logger.info(f"客户档案: {profile.id}（{source}）")
    logger.info(f"日志文件: {logger.log_file}")
    logger.info("========== 任务结束 ==========")

    # ★ 脚本能力边界：CPMS 必须由 agent 亲自补写
    erp_json = output_info["erp_json"]
    logger.info("")
    logger.info("========== 待 agent 亲自完成 ==========")
    logger.info(f"CPMS（产品描述）当前为空，产物: {erp_json}")
    logger.info("请通读客户原始文件全文 → 翻译成中文 → 按类别分段优化 → 写回 CPMS 字段。")
    logger.info(f"抽取明细（规范键+未识别标签）: {output_info['extracted_json']}")
    logger.info("此环节无法用 Python 统一处理，脚本不生成任何 CPMS 内容。")
    logger.info("======================================")
    print("")
    print("=" * 64)
    print("!! CPMS 为空 —— 必须由 agent 亲力亲为补写 !!")
    print(f"   产物: {erp_json}")
    print(f"   抽取明细: {output_info['extracted_json']}")
    print("   步骤: 通读客户原文 -> 翻译成中文 -> 按类别分段优化 -> 写回 CPMS")
    if customer_data.extras_found:
        print(f"   别漏这些非 ERP 字段的附加信息（{len(customer_data.extras_found)} 项）:")
        print(f"     {', '.join(customer_data.extras_found[:10])}")
        print(f"     （完整清单见 {output_info['extracted_json']} 的 extras_found）")
    # 长文本素材：分区表单（NGV）没有「附加信息栏位」，要写进 CPMS 的规格细节
    # 全在长文本规范键里（specials / notes / packaging_specials…）。没有这条提示时，
    # extras 为空的客户会看不到「原文还有一大段要翻译」的信号。
    long_texts = [(k, str(v)) for k, v in (customer_data.values or {}).items()
                  if len(str(v or "")) >= 80]
    if long_texts:
        print(f"   别漏这些长文本规范键（{len(long_texts)} 项，原文细节都在里面）:")
        for key, val in long_texts[:5]:
            head = val.replace("\n", " ")[:70]
            print(f"     - {key}: {head}…")
        print(f"     （完整内容见 {output_info['extracted_json']} 的 values）")
    print("=" * 64)

    # ★ ERP 取值校验结果：有 error 时必须人工确认，别直接导入
    v_errors = [i for i in (issues or []) if i["level"] == "error"]
    v_warns = [i for i in (issues or []) if i["level"] == "warn"]
    print("")
    if v_errors:
        print("=" * 64)
        print(f"!! ERP 取值校验：{len(v_errors)} 个 error —— 导入前必须处理 !!")
        for i in v_errors:
            print(f"   ✗ {i['field']}: {i['message']}")
        print(f"   明细: {output_info['validation_json']}")
        print("=" * 64)
    else:
        print(f"ERP 取值校验: 通过（0 error / {len(v_warns)} warn）")

    # ★ 报价截止日期提醒（业务 2026-09-24 硬要求：过期必须提醒，不许埋在日志里）
    for r in (output_info.get("reminders") or []):
        print("")
        print("=" * 64)
        print(f"!! 提醒业务：{r['message']} !!")
        print(f"   报价截止: {r['deadline']}（已逾期 {r['days_over']} 天）")
        print("=" * 64)

    output_info["profile_id"] = profile.id
    output_info["profile_source"] = source
    output_info["validation_errors"] = len(v_errors)
    output_info["validation_warnings"] = len(v_warns)
    output_info["unit"] = unit.summary()
    return output_info


def run_lookup(kind: str, pg: Optional[str], faw: Optional[str],
               value: Optional[str]) -> int:
    """单点查表（`--lookup`）—— 替代 1.0.42 删掉的三个查询脚本。

    - `thickness`：`--pg 书纸 --faw 90` → 打印厚度（表里没有就按最近克重）
    - `category` ：`--value paperback` → 打印 `CPLB: … , ZLBM: …`
    - `date`     ：`--value May-27` → 打印 `2027-05-01`
    """
    logger = Logger()
    if kind == "thickness":
        if not pg or faw is None:
            print("!! --lookup thickness 需要 --pg <纸张类型> 与 --faw <克重>", file=sys.stderr)
            return 2
        thickness = get_page_thickness(pg, faw, logger)
        print(f"{thickness:g}" if thickness else "0")
        return 0 if thickness else 1

    if not value:
        print(f"!! --lookup {kind} 需要 --value", file=sys.stderr)
        return 2

    if kind == "category":
        cplb, zlbm = parse_product_categories(value, logger)
        if not cplb and not zlbm:
            print(f"未命中：{value!r}（这张表只登记了 paperback 一种写法）")
            return 1
        print(f"CPLB: {cplb}, ZLBM: {zlbm}")
        return 0

    date_str = parse_date_to_datestr(value, logger)
    print(date_str or "（解析不出）")
    return 0 if date_str else 1


def main():
    parser = argparse.ArgumentParser(description="客户报价请求处理（多客户档案驱动）")
    parser.add_argument("input_file", nargs="?", default=None,
                        help="输入路径：单文件 = 独一份报价；文件夹 / .zip = 一份报价的"
                             "全部参考文件（里面若有子目录，则子目录各算一份报价）")
    parser.add_argument("--lookup", choices=["thickness", "category", "date"], default=None,
                        help="只查表、不处理报价：thickness（配 --pg/--faw）/ category / date"
                             "（都打印查询结果，退出码 0 = 查到）")
    parser.add_argument("--customer", "-c", default="auto",
                        help="客户档案 id（默认 auto = 按文件内容自动识别；"
                             "可用 ./scripts/customer_profile.py --list 查看）")
    parser.add_argument("--output-dir", "-o", default=None, help="输出目录路径")
    parser.add_argument("--processed-dir", default=None, help="处理后原始文件移动到的目录")
    parser.add_argument("--failed-dir", default=None, help="处理失败时原始文件移动到的目录")
    parser.add_argument("--today", default=None, metavar="YYYY-MM-DD",
                        help="运行基准日期（默认系统当天）。覆盖它会影响：报价截止日期是否"
                             "判为过期、WCRQ 的 today+N 天。回归测试/历史补跑用，保证可复现。")
    parser.add_argument("--strip-internal", action="store_true",
                        help="只输出 59 个 ERP 字段，不追加内部字段（ID / CPMC_ZH）。"
                             "给「ERP 导入端不收未知字段」的情况兜底；默认是追加"
                             "（见 assets/internal_export_fields.yaml）")
    parser.add_argument("--pg", default=None, help="--lookup thickness：纸张类型，如 书纸")
    parser.add_argument("--faw", default=None, help="--lookup thickness：克重，裸数字如 90")
    parser.add_argument("--value", default=None, help="--lookup category / date：要查的值")
    args = parser.parse_args()

    # 单点查表（不读报价文件，直接返回）
    if args.lookup:
        return run_lookup(args.lookup, args.pg, args.faw, args.value)
    if not args.input_file:
        parser.error("缺少 input_file（若只想查表，用 --lookup thickness/category/date）")

    # 基准日期（在做任何日期比较之前设好）
    global _TODAY
    if args.today:
        try:
            _TODAY = datetime.strptime(args.today.strip(), "%Y-%m-%d").date()
        except ValueError:
            print(f"!! --today 需要 YYYY-MM-DD 格式，收到 {args.today!r}", file=sys.stderr)
            return 1

    input_path = Path(args.input_file)
    customer_files_dir = REPO_ROOT / "customer-files"
    customer_files_dir.mkdir(parents=True, exist_ok=True)
    (customer_files_dir / "processed").mkdir(exist_ok=True)
    (customer_files_dir / "failed").mkdir(exist_ok=True)

    processed_dir = Path(args.processed_dir) if args.processed_dir else customer_files_dir / "processed"
    failed_dir = Path(args.failed_dir) if args.failed_dir else customer_files_dir / "failed"

    # ── 输入单元：一份报价 = 单文件 | 文件夹 | 压缩包 ──────────────────────
    # 业务口径（2026-10-20）：单个文件是**独一份报价**；文件夹 / 压缩包里的文件
    # 则是**这一份报价的全部参考文件** —— 合并成一份，只出一个 ERP JSON。
    # 补充口径（1.0.38）：文件夹 / 压缩包里**有子目录**时，子目录各算一份报价
    # （一个文件夹里躺着两个独立单，别给合并成一份）。
    try:
        units = resolve_input_units(input_path)
    except ValueError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1

    # 语料库样本不许跑进真交付区（会把客户原文移走 + 留下未完成产物）
    guard_error = sample_run_guard(input_path, args)
    if guard_error:
        print(f"!! {guard_error}", file=sys.stderr)
        return 1

    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"quote_request_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = Logger(log_file)

    logger.info("========== 任务开始 ==========")
    logger.info(f"源文件: {input_path}")
    if len(units) > 1:
        logger.info(f"输入单元: 拆成 {len(units)} 份报价（有子目录 → 子目录各算一份）")
        for number, each in enumerate(units, 1):
            logger.info(f"  第 {number} 份: {each.stem}（{each.label}）")
    else:
        logger.info(f"输入单元: {units[0].label}")
    logger.info(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    succeeded: list[dict] = []
    failures: list[tuple[InputUnit, Exception]] = []
    try:
        for number, unit in enumerate(units, 1):
            if len(units) > 1:
                logger.info("")
                logger.info("=" * 60)
                logger.info(f"第 {number}/{len(units)} 份报价: {unit.stem}（{unit.label}）")
                logger.info("=" * 60)
            try:
                succeeded.append(process_unit(unit, args, processed_dir, logger))
            except Exception as unit_error:
                logger.error(f"第 {number}/{len(units)} 份处理失败: {unit_error}")
                failures.append((unit, unit_error))
                move_unit_to_failed(unit, failed_dir, logger)
                logger.info(f"处理状态: 失败 —— 继续处理同批剩余的 {len(units) - number} 份")
    finally:
        # 批次收尾：共享邮件副档清掉 + 被搬空的父目录删掉
        # （失败那份的副档已经复制进 failed/ 留证，所以这里一律清）
        if len(units) > 1:
            try:
                note = finalize_batch(units, logger)
                if note:
                    logger.info(note)
            except Exception as batch_error:
                logger.warn(f"批次收尾失败（不影响产物）: {batch_error}")
        # zip 解压出来的临时副本用完就删（原压缩包已归档 / 已退回失败目录）
        for each in units:
            try:
                each.cleanup()
            except Exception:
                pass

    # ── 汇总 ──────────────────────────────────────────────────────────────
    if len(units) > 1:
        print("")
        print("=" * 64)
        print(f"本批共 {len(units)} 份报价：成功 {len(succeeded)}，失败 {len(failures)}")
        for info in succeeded:
            print(f"  ✓ {Path(info['erp_json']).name}  task={info['task_id']}")
            print(f"     CPMS 待写 → {info['task_dir']}")
        for unit, error in failures:
            print(f"  ✗ {unit.stem}: {error}")
        print("  每份都还没写 CPMS —— 逐个补写，别只写第一份。")
        print("=" * 64)
        logger.info("")
        logger.info("========== 任务结束 ==========")
        logger.info(f"本批共 {len(units)} 份报价：成功 {len(succeeded)}，失败 {len(failures)}")
        logger.info(f"总耗时: {logger.elapsed_time():.1f} 秒")
        print(f"日志文件: {log_file}")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
