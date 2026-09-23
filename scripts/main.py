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
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cplb_tree  # noqa: E402  （ERP 产品类别树）
from customer_profile import CustomerProfile, resolve_profile  # noqa: E402
from erp_schema import normalize_result, normalize_value, validate_result  # noqa: E402
from extract_fields import CustomerData, load_customer_data  # noqa: E402
from ingest_email import read_attachment_meta  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / "assets" / "add_customer_quote_request.json"

# 布局/取值兜底常量
DEFAULT_UNIT = "mm"


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


def run_script(script_name: str, *args) -> str:
    """运行子脚本并返回 stdout（失败返回空串）。"""
    script_path = Path(__file__).resolve().parent / script_name
    result = subprocess.run(["uv", "run", str(script_path), *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


# ── 查表类工具 ───────────────────────────────────────────────────────────
def parse_product_categories(value: str, logger: Logger) -> tuple:
    """查产品类别表，返回 (CPLB, ZLBM)。"""
    logger.info(f"  调用: product_categories.py --select {value}")
    output = run_script("product_categories.py", "--select", value)
    cplb = zlbm = ""
    for line in output.split("\n"):
        if "CPLB:" in line and "ZLBM:" in line:
            for part in line.split(","):
                if "CPLB:" in part:
                    cplb = part.split(":", 1)[1].strip()
                elif "ZLBM:" in part:
                    zlbm = part.split(":", 1)[1].strip()
    logger.info(f"  查询结果: CPLB={cplb}, ZLBM={zlbm}")
    return cplb, zlbm


def get_page_thickness(page_style: str, faw: str, logger: Logger) -> float:
    """查纸张厚度（克重传裸数字，不带 g）。"""
    logger.info(f"  调用: select_page_thickness.py --pg {page_style} --faw {faw}")
    output = run_script("select_page_thickness.py", "--pg", page_style, "--faw", faw)
    try:
        thickness = float(output)
        logger.info(f"  {page_style}厚度: {thickness}")
        return thickness
    except ValueError:
        logger.warn(f"  {page_style} 克重 {faw} 查询失败，改用 克重×0.001 粗估")
        return 0.0


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
        if not pattern or not code:
            continue
        if re.search(pattern, blob, re.IGNORECASE):
            if cplb_tree.exists(code):
                label = cplb_tree.label_of(code)
                logger.info(f"  类别映射: 命中 /{pattern}/ → {label}")
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
    logger.info(f"  调用: string2timestamp.py {date_str}（取日期部分）")
    output = run_script("string2timestamp.py", str(date_str))
    try:
        ts = int(output)
        out = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        logger.info(f"  转换结果: {out}")
        return out
    except (ValueError, TypeError, OSError, OverflowError) as e:
        logger.warn(f"  未能换算: {e}")

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


def generate_task_id(logger: Logger) -> str:
    logger.info("调用: uuid_utils.py")
    task_id = run_script("uuid_utils.py")
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


def parse_size(size_str: str, profile: CustomerProfile, logger: Logger) -> dict:
    """按档案 `rules.size`（单位 + 顺序）解析尺寸串 → {CD/KD/GD: 数值}。

    例（igl，order=[CD,KD,GD]，unit=mm）：`198mm x 129mm x 10mm` → CD=198 KD=129 GD=10
    """
    unit = str(profile.rule("size.unit", DEFAULT_UNIT))
    order = profile.rule("size.order", ["CD", "KD", "GD"]) or ["CD", "KD", "GD"]
    normalized = re.sub(re.escape(unit), " ", str(size_str), flags=re.IGNORECASE)
    chunks = [c for c in re.split(r"[x×*]", normalized) if c.strip()]
    out: dict = {}
    for idx, chunk in enumerate(chunks[:len(order)]):
        value = _to_number(chunk)
        if value is not None:
            out[order[idx]] = _num(value)
    return out


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

    # ── 3. 产品名称 CPMC = CPLB + 系列名 ───────────────────
    series_name = customer_data.get("series_name")
    assign("CPMC", f"{result.get('CPLB', '')}-{series_name}",
           "CPLB + series_name（规范键）", 11)

    # ── 4. 款数 TZS ────────────────────────────────────────
    num_titles = customer_data.get("number_of_titles", "1")
    assign("TZS", int(num_titles) if str(num_titles).isdigit() else 1,
           f"number_of_titles（规范键）={num_titles}", 12)

    # ── 5. 数量档位 CPSL1 ~ CPSL10 ─────────────────────────
    logger.field_mapping("CPSL1~CPSL10", 15, total_fields)
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
    if size_str:
        logger.info(f"  来源: size（规范键）={size_str}")
        parsed = parse_size(size_str, profile, logger)
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
    logger.field_mapping("GD", 29, total_fields)
    if not result.get("GD"):
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
    if cd > 0 and kd > 0:
        logger.info(f"  来源: 尺寸比较（档案 rules.spine.mode={profile.rule('spine.mode', 'cd_vs_kd')}）")
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

    # ── 9. 装订方式 ZDFS（档案按需开启）─────────────────────
    binding_text = customer_data.get("binding")
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
    logger.field_mapping("JHRQ", 34, total_fields)
    jhrq_source = str(profile.rule("dates.JHRQ.from", "pub_month"))
    jhrq_value = customer_data.get(jhrq_source)
    if jhrq_value:
        assign("JHRQ", parse_date_to_datestr(jhrq_value, logger),
               f"{jhrq_source}（规范键，档案 dates.JHRQ.from）={jhrq_value}", 34)
    else:
        logger.warn(f"  来源: 规范键 {jhrq_source} 未提供，使用默认值")
        default_count += 1

    # ── 11. 预计下单日期 WCRQ ──────────────────────────────
    days = int(profile.rule("dates.WCRQ.days", 10))
    future_date = datetime.now() + timedelta(days=days)
    assign("WCRQ", future_date.strftime("%Y-%m-%d"),
           f"当前日期 + {days} 天（档案 dates.WCRQ.days）", 42)

    # ── 12. 客户单号 KHDH ──────────────────────────────────
    # 业务确认：来源 = 客户邮件内容（标题/正文）或邮件附件。
    # 优先级：邮件标题/正文（ingest_email.py 写进副档）→ 附件列（档案 email.khdh.attachment_keys）
    khdh = ""
    khdh_source = ""
    email_khdh = str(email_meta.get("khdh", "")).strip()
    if email_khdh:
        khdh, khdh_source = email_khdh, f"邮件（{email_meta.get('khdh_source') or '标题/正文'}）"
    else:
        for key in profile.email.get("khdh", {}).get("attachment_keys", []) or []:
            value = customer_data.get(key)
            if value:
                khdh, khdh_source = str(value), f"附件列（规范键 {key}）"
                break
        if not khdh:
            khdh = str(customer_data.get("khdh", "")).strip()
            if khdh:
                khdh_source = "附件列（规范键 khdh）"
    if khdh:
        assign("KHDH", khdh, khdh_source, 33)
    else:
        logger.warn("  来源: 邮件标题/正文与附件列都未取到，使用默认值")
        if not email_meta:
            logger.warn("  提示: 用 ingest_email.py 投递邮件可自动带上标题/正文里的客户单号")
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
    if cpsx_value:
        legal, status = normalize_value("CPSX", cpsx_value)
        assign("CPSX", legal, f"{cpsx_source}（档案 rules.cpsx 推导）", cpsx_index)
        if status == "unknown":
            logger.warn(f"  产品属性 {legal!r} 不在 ERP 枚举里 —— 检查客户档案 rules.cpsx 的取值")
    else:
        logger.warn("  产品属性 CPSX: 推导不出（缺年龄段）→ 保持模板默认值，需人工确认")
        default_count += 1
    if safety or age:
        logger.info("  提示: 年龄段/安全检查已落库到 CPSX，但**仍必须**写进 CPMS（见 5.1.6）")

    # ── 13. 产品描述 CPMS —— 脚本不生成 ────────────────────
    logger.field_mapping("CPMS", 56, total_fields)
    logger.warn("  CPMS 留空 —— 脚本不生成产品描述，必须由 agent 亲自补写（全文翻译 + 分类分段）")
    logger.info("  来源: 无（等待 agent 亲力亲为）")
    default_count += 1

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
def step1_input_validation(input_path: Path, logger: Logger) -> bool:
    logger.section("步骤 1: 输入验证")
    logger.info(f"输入文件: {input_path}")

    supported = (".csv", ".xlsx", ".xls", ".docx", ".txt", ".pdf")
    if input_path.suffix.lower() not in supported:
        logger.error(f"文件格式检查: 失败 (不支持的格式 {input_path.suffix})")
        return False
    logger.info(f"文件格式检查: 通过 ({input_path.suffix})")
    logger.info(f"文件大小: {input_path.stat().st_size} 字节")

    if "pending" not in input_path.parts:
        logger.warn(f"文件不在 pending 目录，可能已被处理: {input_path}")

    logger.info("步骤 1 完成，状态: 成功")
    return True


def step2_load_customer_data(input_path: Path, profile: CustomerProfile,
                             logger: Logger) -> CustomerData:
    logger.section("步骤 2: 读取客户数据（客户档案驱动）")
    logger.info(f"客户档案: {profile.id}（{profile.name}，状态 {profile.status}）")
    logger.info(f"布局策略: {profile.rule('layout.strategies', [])}")

    customer_data = load_customer_data(str(input_path), profile, logger)
    logger.info(f"布局命中: {customer_data.layout or '(无)'}")
    logger.info(f"提取规范键: {len(customer_data.values)} 个")
    for key, value in customer_data.values.items():
        logger.info(f"  - {key}: {value}")
    if customer_data.quantity_tiers:
        logger.info(f"  - 数量档位: {customer_data.quantity_tiers}")
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


def step5b_validate_erp_values(result: dict, logger: Logger) -> list:
    """步骤 5b: 按 ERP 表单权威字典归一化 + 校验取值。

    字典 = assets/erp_form_schema.yaml（从真实 ERP 表单转录的合法枚举）。
    目的：客户/脚本给出的值必须对齐 ERP 下拉框，否则导入时会被丢弃或报错。
    历史教训：0e39e2e2（2026-05-25）ZDFS 填了「无线胶装」，ERP 只认「胶装/PAD书」。
    """
    logger.section("步骤 5b: ERP 取值归一化与校验")

    result, changes = normalize_result(result, logger)
    normalized = [c for c in changes if c["status"] == "normalized"]
    if normalized:
        logger.info(f"归一化 {len(normalized)} 个字段（对齐 ERP 枚举）")
    else:
        logger.info("无需归一化：取值已全部是 ERP 合法值")

    issues = validate_result(result)
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


def step6_output_results(input_path: Path, result: dict, processed_dir: Path,
                         logger: Logger, customer_data: CustomerData,
                         profile: CustomerProfile, issues: Optional[list] = None) -> dict:
    logger.section("步骤 6: 输出结果")

    task_id = result.pop("_task_id", "unknown")
    logger.info(f"任务ID: {task_id}")

    # 字段一致性断言：键集合与键顺序必须与模板完全一致
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        tpl_keys = list(json.load(f).keys())
    out_keys = list(result.keys())
    if out_keys != tpl_keys:
        missing = [k for k in tpl_keys if k not in out_keys]
        extra = [k for k in out_keys if k not in tpl_keys]
        logger.error(f"字段一致性断言失败: 缺失={missing} 多余={extra}")
        raise ValueError(f"输出字段与模板不一致: 缺失={missing} 多余={extra}")
    logger.info(f"字段一致性断言: 通过（{len(out_keys)} 字段，顺序与模板一致）")

    task_dir = create_task_directory(processed_dir, task_id)
    logger.info(f"创建任务目录: {task_dir}")

    output_files = []

    # 1. CSV 转换
    logger.info("")
    logger.info("[文件 1/5] CSV转换")
    logger.info(f"  源文件: {input_path.name}")
    csv_path = convert_to_csv(input_path, task_dir, logger)
    logger.info(f"  输出: {csv_path.name}")
    logger.info("  状态: 成功")
    output_files.append(csv_path)

    # 2. 抽取结果落盘（审计 + 给 agent 写 CPMS 用）
    logger.info("")
    logger.info("[文件 2/5] 抽取明细（规范键 / 未识别标签）")
    extracted_path = task_dir / f"{input_path.stem}.extracted.json"
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
    validation_path = task_dir / f"{input_path.stem}.validation.json"
    report = {
        "schema": "assets/erp_form_schema.yaml",
        "schema_version": 1,
        "customer": profile.id,
        "checked_fields": len([k for k in result if not k.startswith("_")]),
        "errors": [i for i in issues if i["level"] == "error"],
        "warnings": [i for i in issues if i["level"] == "warn"],
        "summary": {
            "error": len([i for i in issues if i["level"] == "error"]),
            "warn": len([i for i in issues if i["level"] == "warn"]),
        },
    }
    with open(validation_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info(f"  输出: {validation_path.name}")
    logger.info(f"  error {report['summary']['error']} 个 / warn {report['summary']['warn']} 个")
    logger.info("  状态: 成功")
    output_files.append(validation_path)

    # 5. 原始文件归档
    logger.info("")
    logger.info("[文件 5/5] 原始文件归档")
    logger.info(f"  源路径: {input_path}")
    moved_path = move_file(input_path, task_dir)
    logger.info(f"  目标路径: {moved_path}")
    logger.info("  状态: 成功")
    output_files.append(moved_path)

    # 邮件副档一起归档（保持任务自包含）
    meta_path = input_path.with_name(input_path.name + ".meta.json")
    if meta_path.exists():
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
    }


# ── 入口 ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="客户报价请求处理（多客户档案驱动）")
    parser.add_argument("input_file", help="输入文件路径 (CSV, XLSX, DOCX, TXT, PDF)")
    parser.add_argument("--customer", "-c", default="auto",
                        help="客户档案 id（默认 auto = 按文件内容自动识别；"
                             "可用 ./scripts/customer_profile.py --list 查看）")
    parser.add_argument("--output-dir", "-o", default=None, help="输出目录路径")
    parser.add_argument("--processed-dir", default=None, help="处理后原始文件移动到的目录")
    parser.add_argument("--failed-dir", default=None, help="处理失败时原始文件移动到的目录")
    args = parser.parse_args()

    input_path = Path(args.input_file)
    customer_files_dir = REPO_ROOT / "customer-files"
    customer_files_dir.mkdir(parents=True, exist_ok=True)
    (customer_files_dir / "processed").mkdir(exist_ok=True)
    (customer_files_dir / "failed").mkdir(exist_ok=True)

    processed_dir = Path(args.processed_dir) if args.processed_dir else customer_files_dir / "processed"
    failed_dir = Path(args.failed_dir) if args.failed_dir else customer_files_dir / "failed"

    log_dir = REPO_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"quote_request_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    logger = Logger(log_file)

    logger.info("========== 任务开始 ==========")
    logger.info(f"源文件: {input_path}")
    logger.info(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    try:
        if not step1_input_validation(input_path, logger):
            raise ValueError("输入验证失败")

        email_meta = read_attachment_meta(input_path)
        if email_meta:
            logger.info(f"读到邮件副档: 标题={email_meta.get('subject', '')[:60]!r} "
                        f"客户单号={email_meta.get('khdh') or '(无)'}")

        # 客户识别：优先用文件内容，其次邮件上下文
        probe_texts = []
        if email_meta:
            probe_texts = [email_meta.get("subject", ""), email_meta.get("body", "")]
        profile, source = resolve_profile(
            args.customer, texts=probe_texts, filename=input_path.name,
            sender=email_meta.get("sender", ""),
            subject=email_meta.get("subject", ""), logger=logger,
        )
        if source != "显式指定":
            # 自动识别先用「文件名 + 邮件上下文」做探针；识别不到时再用文件内容正式判一次，
            # 避免因为文件名没线索而误走兜底档案。
            from customer_profile import auto_detect

            if source == "兜底" and input_path.suffix.lower() in (".xlsx", ".xls", ".csv"):
                try:
                    quick = load_customer_data(str(input_path), profile, logger=None)
                    candidates = auto_detect(quick.text_blocks, filename=input_path.name)
                    if candidates:
                        profile = candidates[0][0]
                        source = "自动识别（文件内容）"
                        logger.info(f"客户档案（文件内容识别）: {profile.id} 得分 {candidates[0][1]}")
                except Exception as detect_error:
                    logger.warn(f"文件内容识别失败，沿用兜底档案: {detect_error}")

        logger.info(f"选定客户档案: {profile.id}（{source}）")
        logger.info(f"日志文件: {log_file}")

        customer_data = step2_load_customer_data(input_path, profile, logger)
        template = step3_load_erp_template(logger)
        result = step5_map_fields(customer_data, template, profile, logger, email_meta)
        issues = step5b_validate_erp_values(result, logger)
        output_info = step6_output_results(
            input_path, result, processed_dir, logger, customer_data, profile, issues)

        logger.info("")
        logger.info("========== 任务完成 ==========")
        logger.info(f"总耗时: {logger.elapsed_time():.1f} 秒")
        logger.info("处理状态: 成功")
        logger.info(f"任务ID: {output_info['task_id']}")
        logger.info(f"客户档案: {profile.id}（{source}）")
        logger.info(f"日志文件: {log_file}")
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

        return 0

    except Exception as e:
        logger.error(f"错误: {e}")

        if failed_dir and input_path.exists():
            try:
                failed_task_id = datetime.now().strftime("failed_%Y%m%d_%H%M%S")
                failed_task_dir = create_task_directory(failed_dir, failed_task_id)
                moved_path = move_file(input_path, failed_task_dir)
                logger.info(f"原始文件已移动到失败目录: {moved_path}")
            except Exception as move_error:
                logger.error(f"移动文件失败: {move_error}")

        logger.info("")
        logger.info("========== 任务失败 ==========")
        logger.info(f"总耗时: {logger.elapsed_time():.1f} 秒")
        logger.info("处理状态: 失败")
        logger.info(f"错误信息: {e}")
        logger.info("========== 任务结束 ==========")
        return 1


if __name__ == "__main__":
    sys.exit(main())
