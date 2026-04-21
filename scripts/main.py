#!/usr/bin/env python3
"""
main.py - 客户报价请求处理主入口

整合所有步骤，将客户报价单转换为 ERP 格式
"""
import argparse
import json
import sys
import subprocess
import re
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional


class Logger:
    """日志记录器"""
    def __init__(self, log_file: Optional[Path] = None):
        self.log_file = log_file
        self.start_time = datetime.now()
        
    def _timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    def log(self, message: str, level: str = "INFO"):
        """记录日志"""
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
        """记录章节标题"""
        self.info("")
        self.info(f"========== {title} ==========")
    
    def field_mapping(self, field_name: str, index: int, total: int):
        """记录字段映射开始"""
        self.info("")
        self.info(f"[字段 {index}/{total}] {field_name}")
    
    def elapsed_time(self) -> float:
        """获取已流逝的时间（秒）"""
        return (datetime.now() - self.start_time).total_seconds()


def run_script(script_name: str, *args) -> str:
    """运行子脚本并返回输出"""
    script_dir = Path(__file__).parent
    script_path = script_dir / script_name
    
    cmd = ["uv", "run", str(script_path)] + list(args)
    result = subprocess.run(cmd, capture_output=True, text=True)
    
    if result.returncode != 0:
        return ""
    
    return result.stdout.strip()


def parse_product_categories(value: str, logger: Logger) -> tuple:
    """解析产品类别，返回 (CPLB, ZLBM)"""
    logger.info(f"  调用: product_categories.py --select {value}")
    output = run_script("product_categories.py", "--select", value)
    
    cplb = ""
    zlbm = ""
    
    for line in output.split("\n"):
        if "CPLB:" in line and "ZLBM:" in line:
            parts = line.split(",")
            for part in parts:
                if "CPLB:" in part:
                    cplb = part.split(":")[1].strip()
                elif "ZLBM:" in part:
                    zlbm = part.split(":")[1].strip()
    
    logger.info(f"  查询结果: CPLB={cplb}, ZLBM={zlbm}")
    return cplb, zlbm


def get_page_thickness(page_type: str, faw: str, logger: Logger) -> float:
    """获取纸张厚度"""
    logger.info(f"  调用: select_page_thickness.py --pg {page_type} --faw {faw}")
    output = run_script("select_page_thickness.py", "--pg", page_type, "--faw", faw)
    try:
        thickness = float(output)
        logger.info(f"  {page_type}厚度: {thickness}")
        return thickness
    except ValueError:
        logger.warn(f"  查询失败，使用默认值")
        return 0.0


def parse_date_to_timestamp(date_str: str, logger: Logger) -> int:
    """将日期字符串转换为时间戳"""
    logger.info(f"  调用: string2timestamp.py {date_str}")
    output = run_script("string2timestamp.py", date_str)
    try:
        timestamp = int(output)
        logger.info(f"  转换结果: {timestamp}")
        return timestamp
    except ValueError:
        logger.warn(f"  日期转换失败，返回 0")
        return 0


def generate_task_id(logger: Logger) -> str:
    """生成任务 ID"""
    logger.info("调用: uuid_utils.py")
    task_id = run_script("uuid_utils.py")
    logger.info(f"生成任务ID: {task_id}")
    return task_id


def _load_text_document(file_path: str, ext: str, logger: Logger) -> dict:
    """加载文本类文档 (DOCX/TXT/PDF)"""
    script_dir = Path(__file__).parent
    if ext == '.pdf':
        script_name = "pdf2json.py"
    elif ext == '.docx':
        script_name = "docx2json.py"
    else:
        script_name = "txt2json.py"
    script_path = script_dir / script_name
    
    logger.info(f"调用解析器: {script_name}")
    cmd = ["uv", "run", str(script_path), str(file_path)]
    process = subprocess.run(cmd, capture_output=True, text=True)
    
    if process.returncode != 0:
        logger.error(f"解析失败: {process.stderr}")
        raise ValueError(f"解析器错误: {process.stderr}")
    
    output = process.stdout
    result = {}
    
    # 从解析器输出中提取 Key: Value 对
    for line in output.split('\n'):
        if ':' in line and '提取的字段列表' not in line and '步骤' not in line:
            parts = line.split(':', 1)
            if len(parts) == 2:
                k, v = parts[0].strip(), parts[1].strip()
                if k and v and not k.startswith('['):
                    result[k] = v
    
    logger.info(f"从 {ext} 提取字段数: {len(result)}")
    return result


def load_customer_data(file_path: str, logger: Logger) -> dict:
    """加载客户数据文件 (CSV/XLSX/DOCX/TXT)"""
    import pandas as pd
    
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")
    
    ext = path.suffix.lower()
    logger.info(f"文件类型: {ext}")
    
    # 处理文本类文件 (DOCX/TXT/PDF)
    if ext in ['.docx', '.txt', '.pdf']:
        return _load_text_document(file_path, ext, logger)
    
    # 处理表格类文件 (CSV/XLSX/XLS)
    if ext == ".csv":
        df = pd.read_csv(file_path)
    elif ext in [".xlsx", ".xls"]:
        df = pd.read_excel(file_path)
    else:
        raise ValueError(f"不支持的文件格式: {ext}")
    
    if len(df) == 0:
        raise ValueError("文件为空")
    
    logger.info(f"读取行数: {len(df)}")
    
    unnamed_count = sum(1 for col in df.columns if "Unnamed" in str(col))
    is_horizontal = unnamed_count >= len(df.columns) * 0.8
    
    if not is_horizontal:
        logger.info("表格格式检测: 纵向格式")
        return df.iloc[0].to_dict()
    
    logger.info("表格格式检测: 横向格式")
    
    result = {}
    
    if len(df) >= 3:
        row1 = df.iloc[1]
        row2 = df.iloc[2]
        
        known_labels = ["Pub Month", "Series Name", "Number of titles", "Format", 
                       "Size (tps) H x W x D mm", "Extent (pp + cover)", 
                       "Text Material", "Cover Material", "Project description",
                       "Safety Checks Req? (Y/N)", "Target Age Group"]
        
        row1_labels = [str(row1.iloc[i]).strip() for i in range(len(row1)) if pd.notna(row1.iloc[i])]
        label_matches = sum(1 for label in row1_labels if any(kl in label for kl in known_labels))
        
        if label_matches >= 2:
            logger.info("子格式检测: 标签行+数据行")
            for i in range(len(row1)):
                label = str(row1.iloc[i]).strip() if pd.notna(row1.iloc[i]) else ""
                value = row2.iloc[i] if pd.notna(row2.iloc[i]) else ""
                if label:
                    result[label] = str(value).strip() if value else ""
            
            for idx in range(3, len(df)):
                row = df.iloc[idx]
                label = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else ""
                value = row.iloc[2] if len(row) > 2 and pd.notna(row.iloc[2]) else ""
                if label and label not in result:
                    result[label] = str(value).strip() if value else ""
            
            logger.info(f"提取字段数: {len(result)}")
            return result
    
    logger.info("子格式检测: 简单标签-值对")
    for _, row in df.iterrows():
        label = str(row.iloc[1]).strip() if len(row) > 1 and pd.notna(row.iloc[1]) else ""
        value = row.iloc[2] if len(row) > 2 and pd.notna(row.iloc[2]) else ""
        if label:
            result[label] = str(value).strip() if value else ""
    
    logger.info(f"提取字段数: {len(result)}")
    return result


def load_erp_template(logger: Logger) -> dict:
    """加载 ERP 模板"""
    script_dir = Path(__file__).parent
    template_path = script_dir.parent / "assets" / "add_customer_quote_request.json"
    
    logger.info(f"模板文件: {template_path}")
    
    with open(template_path, "r", encoding="utf-8") as f:
        template = json.load(f)
    
    logger.info(f"模板字段数: {len(template)}")
    return template


def parse_extent(extent_str: str) -> tuple:
    """解析页数字符串，返回 (pp, cover)"""
    pp = 0
    cover = 0
    
    pp_match = re.search(r'(\d+)\s*pp', extent_str, re.IGNORECASE)
    if pp_match:
        pp = int(pp_match.group(1))
    
    if 'cover' in extent_str.lower():
        cover = 1
    
    return pp, cover


def parse_material(material_str: str) -> tuple:
    """解析材料字符串，返回 (克重, 类型)"""
    faw = 0
    mat_type = ""
    
    faw_match = re.search(r'(\d+)\s*gsm', material_str, re.IGNORECASE)
    if faw_match:
        faw = int(faw_match.group(1))
    
    type_match = re.search(r'\d+\s*gsm\s+(\w+)', material_str, re.IGNORECASE)
    if type_match:
        mat_type = type_match.group(1).lower()
    
    return faw, mat_type


def calculate_thickness(extent_pp: int, pp_faw: int, extent_cover: int, cover_faw: int, logger: Logger) -> float:
    """计算书籍厚度"""
    pp_thickness = get_page_thickness("书纸", f"{pp_faw}g", logger)
    cover_thickness = get_page_thickness("光粉纸", f"{cover_faw}g", logger)
    
    if pp_thickness == 0:
        pp_thickness = pp_faw * 0.001
    if cover_thickness == 0:
        cover_thickness = cover_faw * 0.001
    
    total = (extent_pp / 2) * pp_thickness + extent_cover * cover_thickness
    return round(total, 2)


def map_fields(customer_data: dict, template: dict, logger: Logger) -> dict:
    """映射字段"""
    result = template.copy()
    
    success_count = 0
    default_count = 0
    failed_count = 0
    total_fields = len(template)
    
    logger.info(f"开始映射 {total_fields} 个字段...")
    
    # 生成任务 ID
    logger.field_mapping("任务ID", 1, total_fields)
    task_id = generate_task_id(logger)
    result["_task_id"] = task_id
    logger.info("  来源: uuid_utils.py")
    logger.info(f"  映射值: {task_id}")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 产品大类
    logger.field_mapping("CPDL", 2, total_fields)
    result["CPDL"] = "书板"
    logger.info("  来源: 固定值")
    logger.info("  映射值: 书板")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 产品类别
    logger.field_mapping("CPLB", 3, total_fields)
    format_value = customer_data.get("Format", "")
    if format_value:
        logger.info(f"  来源: Format={format_value}")
        cplb, zlbm = parse_product_categories(format_value, logger)
        if cplb and zlbm:
            result["CPLB"] = f"{zlbm};{cplb}"
            logger.info(f"  映射值: {result['CPLB']}")
            logger.info("  状态: 成功")
            success_count += 1
        else:
            logger.warn("  产品类别查询失败，使用默认值")
            result["CPLB"] = "99.99;其他"
            default_count += 1
    else:
        logger.warn("  来源: 未提供，使用默认值")
        result["CPLB"] = "99.99;其他"
        default_count += 1
    
    # id 字段 - 使用任务ID
    logger.field_mapping("id", 4, total_fields)
    result["id"] = task_id
    logger.info("  来源: 任务ID")
    logger.info(f"  映射值: {result['id']}")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 产品名称
    logger.field_mapping("CPMC", 5, total_fields)
    series_name = customer_data.get("Series Name", "")
    result["CPMC"] = f"{result.get('CPLB', '')}-{series_name}"
    logger.info("  来源: CPLB + Series Name")
    logger.info(f"  映射值: {result['CPMC']}")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 款数
    logger.field_mapping("TZS", 6, total_fields)
    num_titles = customer_data.get("Number of titles", "1")
    result["TZS"] = int(num_titles) if str(num_titles).isdigit() else 1
    logger.info(f"  来源: Number of titles={num_titles}")
    logger.info(f"  映射值: {result['TZS']}")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 规格 - 尺寸
    logger.field_mapping("GG_length/GG_width", 7, total_fields)
    size_str = customer_data.get("Size (tps) H x W x D mm", "")
    if "x" in size_str.lower():
        logger.info(f"  来源: Size (tps) H x W x D mm={size_str}")
        parts = size_str.lower().replace("mm", "").split("x")
        if len(parts) >= 2:
            result["GG_length"] = int(parts[0].strip())
            result["GG_width"] = int(parts[1].strip())
            logger.info(f"  解析: length={result['GG_length']}, width={result['GG_width']}")
            if len(parts) >= 3:
                result["GG_height"] = int(parts[2].strip())
                logger.info(f"  映射值: height={result['GG_height']}")
            else:
                logger.info("  D尺寸未提供，将计算得出")
        logger.info("  状态: 成功")
        success_count += 1
    else:
        logger.warn("  来源: 未提供，使用默认值")
        default_count += 1
    
    # 厚度计算
    logger.field_mapping("GG_height", 8, total_fields)
    if result.get("GG_height", 0) == 0:
        extent_str = customer_data.get("Extent (pp + cover)", "")
        text_material = customer_data.get("Text Material", "")
        cover_material = customer_data.get("Cover Material", "")
        
        if extent_str and text_material and cover_material:
            logger.info("  来源: 计算")
            logger.info(f"    - Extent: {extent_str}")
            logger.info(f"    - Text Material: {text_material}")
            logger.info(f"    - Cover Material: {cover_material}")
            
            extent_pp, extent_cover = parse_extent(extent_str)
            pp_faw, _ = parse_material(text_material)
            cover_faw, _ = parse_material(cover_material)
            
            logger.info(f"    - 解析: pp={extent_pp}, cover={extent_cover}")
            logger.info(f"    - 解析: pp_faw={pp_faw}g, cover_faw={cover_faw}g")
            
            if extent_pp > 0 and pp_faw > 0 and cover_faw > 0:
                result["GG_height"] = calculate_thickness(extent_pp, pp_faw, extent_cover, cover_faw, logger)
                logger.info(f"  映射值: {result['GG_height']}")
                logger.info("  状态: 成功")
                success_count += 1
            else:
                logger.warn("  计算参数不足，使用默认值")
                default_count += 1
        else:
            logger.warn("  来源: 未提供，使用默认值")
            default_count += 1
    else:
        logger.info("  已由尺寸解析提供，跳过计算")
    
    # 书脊方向 (SJFX)
    logger.field_mapping("SJFX", 9, total_fields)
    gg_length = result.get("GG_length", 0)
    gg_width = result.get("GG_width", 0)
    if gg_length > 0 and gg_width > 0:
        logger.info(f"  来源: 根据尺寸计算 (GG_length={gg_length}, GG_width={gg_width})")
        if gg_length > gg_width:
            result["SJFX"] = "长书脊"
            logger.info(f"  判断: GG_length > GG_width ({gg_length} > {gg_width}) -> 长书脊")
        else:
            result["SJFX"] = "短书脊"
            logger.info(f"  判断: GG_length <= GG_width ({gg_length} <= {gg_width}) -> 短书脊")
        logger.info(f"  映射值: {result['SJFX']}")
        logger.info("  状态: 成功")
        success_count += 1
    else:
        logger.warn("  来源: 尺寸数据不足，使用默认值")
        default_count += 1
    
    # 交货日期
    logger.field_mapping("JHRQ", 10, total_fields)
    pub_month = customer_data.get("Pub Month", "")
    if pub_month:
        logger.info(f"  来源: Pub Month={pub_month}")
        result["JHRQ"] = parse_date_to_timestamp(pub_month, logger)
        logger.info(f"  映射值: {result['JHRQ']}")
        logger.info("  状态: 成功")
        success_count += 1
    else:
        logger.warn("  来源: 未提供，使用默认值 0")
        default_count += 1
    
    # 预计下单日期
    logger.field_mapping("WCRQ", 11, total_fields)
    future_date = datetime.now() + timedelta(days=10)
    result["WCRQ"] = int(future_date.timestamp() * 1000)
    logger.info("  来源: 当前日期 + 10天")
    logger.info(f"  映射值: {result['WCRQ']}")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 安全检查
    logger.field_mapping("has_safety_checks", 12, total_fields)
    safety = customer_data.get("Safety Checks Req? (Y/N)", "N")
    logger.info(f"  来源: Safety Checks Req? (Y/N)={safety}")
    result["has_safety_checks"] = safety.upper() in ["Y", "YES", "是"]
    logger.info(f"  映射值: {result['has_safety_checks']}")
    logger.info("  状态: 成功")
    success_count += 1
    
    # 年龄段
    logger.field_mapping("target_age_group", 13, total_fields)
    age_group = customer_data.get("Target Age Group", "")
    if age_group:
        logger.info(f"  来源: Target Age Group={age_group}")
        numbers = re.findall(r'\d+', str(age_group))
        if numbers:
            result["target_age_group"] = int(numbers[0])
            logger.info(f"  解析: 提取数字 {numbers[0]}")
            logger.info(f"  映射值: {result['target_age_group']}")
            logger.info("  状态: 成功")
            success_count += 1
        else:
            logger.warn("  无法解析数字，使用默认值 0")
            default_count += 1
    else:
        logger.warn("  来源: 未提供，使用默认值 0")
        default_count += 1
    
    # 产品描述
    logger.field_mapping("CPMS", 14, total_fields)
    project_desc = customer_data.get("Project description", "")
    if project_desc:
        logger.info(f"  来源: Project description={project_desc}")
        result["CPMS"] = project_desc
        logger.info(f"  映射值: {result['CPMS']}")
        logger.info("  状态: 成功")
        success_count += 1
    else:
        logger.warn("  来源: 未提供，使用默认值")
        default_count += 1
    
    # 客户单号
    logger.field_mapping("KHCPMC", 15, total_fields)
    works_ref = ""
    for key in customer_data.keys():
        if "Works References" in key:
            works_ref = customer_data[key]
            break
    if works_ref:
        logger.info(f"  来源: Works References={works_ref}")
        result["KHCPMC"] = works_ref
        logger.info(f"  映射值: {result['KHCPMC']}")
        logger.info("  状态: 成功")
        success_count += 1
    else:
        logger.warn("  来源: 未提供，使用默认值")
        default_count += 1
    
    # 其他字段使用默认值
    remaining_fields = total_fields - 15
    logger.info("")
    logger.info(f"剩余 {remaining_fields} 个字段使用默认值")
    default_count += remaining_fields
    
    # 汇总
    logger.info("")
    logger.info("字段映射完成:")
    logger.info(f"  成功: {success_count} 个")
    logger.info(f"  使用默认值: {default_count} 个")
    logger.info(f"  失败: {failed_count} 个")
    
    return result


def generate_output_filename(task_id: str) -> str:
    """生成符合规范的输出文件名"""
    now = datetime.now()
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    return f"ERP_{task_id}_{timestamp}.json"


def create_task_directory(base_dir: Path, task_id: str) -> Path:
    """创建任务专属目录"""
    task_dir = base_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    return task_dir


def convert_to_csv(input_path: Path, output_dir: Path, logger: Logger) -> Path:
    """将文件转换为CSV格式"""
    import pandas as pd
    import shutil
    
    ext = input_path.suffix.lower()
    
    # CSV文件直接复制
    if ext == ".csv":
        output_path = output_dir / f"{input_path.stem}.csv"
        shutil.copy2(input_path, output_path)
        logger.info(f"  源文件已是CSV格式，直接复制")
        return output_path
    
    # 文本类文件转换为CSV
    if ext in ['.docx', '.txt', '.pdf']:
        output_path = output_dir / f"{input_path.stem}.csv"
        with open(input_path, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        # 简单地将内容写入CSV格式
        with open(output_path, 'w', encoding='utf-8-sig') as f:
            f.write("Content\n")
            f.write(content.replace('\n', ' '))
        logger.info(f"  文本文件已转换为CSV格式")
        return output_path
    
    # Excel文件转换
    df = pd.read_excel(input_path)
    csv_filename = f"{input_path.stem}.csv"
    output_path = output_dir / csv_filename
    df.to_csv(output_path, index=False, encoding='utf-8-sig')
    
    return output_path


def move_file(source: Path, dest_dir: Path, new_name: str = None) -> Path:
    """移动文件到目标目录"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    if new_name:
        dest_path = dest_dir / new_name
    else:
        dest_path = dest_dir / source.name
    
    counter = 1
    original_dest = dest_path
    while dest_path.exists():
        stem = original_dest.stem
        suffix = original_dest.suffix
        dest_path = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    
    source.rename(dest_path)
    return dest_path


def step1_input_validation(input_path: Path, logger: Logger) -> bool:
    """步骤 1: 输入验证"""
    logger.section("步骤 1: 输入验证")
    
    logger.info(f"输入文件: {input_path}")
    
    supported_formats = ['.csv', '.xlsx', '.xls', '.docx', '.txt', '.pdf']
    if input_path.suffix.lower() not in supported_formats:
        logger.error(f"文件格式检查: 失败 (不支持的格式 {input_path.suffix})")
        return False
    logger.info(f"文件格式检查: 通过 ({input_path.suffix})")
    
    file_size = input_path.stat().st_size
    logger.info(f"文件大小: {file_size} 字节")
    
    if "pending" not in input_path.parts:
        logger.warn(f"文件不在 pending 目录，可能已被处理: {input_path}")
    
    logger.info("步骤 1 完成，状态: 成功")
    return True


def step2_load_customer_data(input_path: Path, logger: Logger) -> dict:
    """步骤 2: 读取客户数据"""
    logger.section("步骤 2: 读取客户数据")
    
    customer_data = load_customer_data(str(input_path), logger)
    
    logger.info("提取的字段列表:")
    for key, value in list(customer_data.items())[:10]:
        logger.info(f"  - {key}: {value}")
    if len(customer_data) > 10:
        logger.info(f"  ... (共 {len(customer_data)} 个字段)")
    
    logger.info("步骤 2 完成，状态: 成功")
    return customer_data


def step3_load_erp_template(logger: Logger) -> dict:
    """步骤 3: 读取 ERP 模板"""
    logger.section("步骤 3: 读取 ERP 模板")
    
    template = load_erp_template(logger)
    
    logger.info("模板字段列表:")
    for key, value in list(template.items())[:5]:
        logger.info(f"  - {key}: {value}")
    if len(template) > 5:
        logger.info(f"  ... (共 {len(template)} 个字段)")
    
    logger.info("步骤 3 完成，状态: 成功")
    return template


def step5_map_fields(customer_data: dict, template: dict, logger: Logger) -> dict:
    """步骤 5: 字段映射"""
    logger.section("步骤 5: 字段映射")
    
    result = map_fields(customer_data, template, logger)
    
    elapsed = logger.elapsed_time()
    logger.info(f"步骤 5 完成，状态: 成功，耗时: {elapsed:.1f}s")
    return result


def step6_output_results(input_path: Path, result: dict, processed_dir: Path, logger: Logger) -> dict:
    """步骤 6: 输出结果"""
    logger.section("步骤 6: 输出结果")
    
    task_id = result.pop("_task_id", "unknown")
    logger.info(f"任务ID: {task_id}")
    
    task_dir = create_task_directory(processed_dir, task_id)
    logger.info(f"创建任务目录: {task_dir}")
    
    output_files = []
    
    # 1. CSV转换
    logger.info("")
    logger.info("[文件 1/3] CSV转换")
    logger.info(f"  源文件: {input_path.name}")
    csv_path = convert_to_csv(input_path, task_dir, logger)
    logger.info(f"  输出: {csv_path.name}")
    logger.info("  状态: 成功")
    output_files.append(csv_path)
    
    # 2. ERP JSON
    logger.info("")
    logger.info("[文件 2/3] ERP JSON")
    output_filename = generate_output_filename(task_id)
    logger.info(f"  文件名: {output_filename}")
    logger.info(f"  字段数: {len(result)}")
    output_path = task_dir / output_filename
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    logger.info(f"  输出路径: {output_path}")
    logger.info("  状态: 成功")
    output_files.append(output_path)
    
    # 3. 移动原始文件
    logger.info("")
    logger.info("[文件 3/3] 原始文件归档")
    logger.info(f"  源路径: {input_path}")
    moved_path = move_file(input_path, task_dir)
    logger.info(f"  目标路径: {moved_path}")
    logger.info("  状态: 成功")
    output_files.append(moved_path)
    
    # 输出文件列表
    logger.info("")
    logger.info("输出文件列表:")
    for f in output_files:
        logger.info(f"  - {f}")
    
    logger.info("步骤 6 完成，状态: 成功")
    
    return {
        "task_id": task_id,
        "task_dir": task_dir,
        "output_files": output_files
    }


def main():
    parser = argparse.ArgumentParser(description="客户报价请求处理")
    parser.add_argument("input_file", help="输入文件路径 (CSV, XLSX, DOCX, TXT, PDF)")
    parser.add_argument("--output-dir", "-o", default=None, help="输出目录路径")
    parser.add_argument("--processed-dir", default=None, help="处理完成后原始文件移动到的目录")
    parser.add_argument("--failed-dir", default=None, help="处理失败时原始文件移动到的目录")
    args = parser.parse_args()
    
    input_path = Path(args.input_file)
    
    # 设置默认输出目录为 <agent_workspace>/customer-files/processed
    script_dir = Path(__file__).parent
    skill_dir = script_dir.parent
    workspace_dir = skill_dir.parent.parent  # 回到 workspace-igl_demo
    customer_files_dir = workspace_dir / "customer-files"
    
    # 确保 customer-files 目录结构存在
    customer_files_dir.mkdir(parents=True, exist_ok=True)
    (customer_files_dir / "processed").mkdir(exist_ok=True)
    (customer_files_dir / "failed").mkdir(exist_ok=True)
    (customer_files_dir / "logs").mkdir(exist_ok=True)
    
    # 使用默认路径或用户指定路径
    output_dir = Path(args.output_dir) if args.output_dir else customer_files_dir / "processed"
    processed_dir = Path(args.processed_dir) if args.processed_dir else customer_files_dir / "processed"
    failed_dir = Path(args.failed_dir) if args.failed_dir else customer_files_dir / "failed"
    
    # 创建日志目录
    log_dir = customer_files_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # 先生成临时任务ID用于日志文件名
    temp_task_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"quote_request_{temp_task_id}.log"
    
    # 创建日志记录器
    logger = Logger(log_file)
    
    # 任务开始
    logger.info("========== 任务开始 ==========")
    logger.info(f"任务ID: 待生成")
    logger.info(f"源文件: {input_path}")
    logger.info(f"开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    try:
        # 步骤 1: 输入验证
        if not step1_input_validation(input_path, logger):
            raise ValueError("输入验证失败")
        
        # 步骤 2: 读取客户数据
        customer_data = step2_load_customer_data(input_path, logger)
        
        # 步骤 3: 读取 ERP 模板
        template = step3_load_erp_template(logger)
        
        # 步骤 5: 字段映射（包含生成任务ID）
        result = step5_map_fields(customer_data, template, logger)
        
        # 步骤 6: 输出结果
        output_info = step6_output_results(input_path, result, processed_dir, logger)
        
        # 任务完成
        total_time = logger.elapsed_time()
        logger.info("")
        logger.info("========== 任务完成 ==========")
        logger.info(f"总耗时: {total_time:.1f} 秒")
        logger.info(f"处理状态: 成功")
        logger.info(f"任务ID: {output_info['task_id']}")
        logger.info(f"日志文件: {log_file}")
        logger.info("========== 任务结束 ==========")
        
        return 0
        
    except Exception as e:
        logger.error(f"错误: {e}")
        
        # 移动原始文件到 failed 目录
        if failed_dir and input_path.exists():
            try:
                failed_task_id = datetime.now().strftime("failed_%Y%m%d_%H%M%S")
                failed_task_dir = create_task_directory(failed_dir, failed_task_id)
                moved_path = move_file(input_path, failed_task_dir)
                logger.info(f"原始文件已移动到失败目录: {moved_path}")
            except Exception as move_error:
                logger.error(f"移动文件失败: {move_error}")
        
        total_time = logger.elapsed_time()
        logger.info("")
        logger.info("========== 任务失败 ==========")
        logger.info(f"总耗时: {total_time:.1f} 秒")
        logger.info(f"处理状态: 失败")
        logger.info(f"错误信息: {e}")
        logger.info("========== 任务结束 ==========")
        
        return 1


if __name__ == "__main__":
    main()
