#!/usr/bin/env python3
"""
xlsx2csv.py - 将 Excel 文件转换为 CSV 格式

用法:
    python xlsx2csv.py <输入文件> [选项]
    
示例:
    python xlsx2csv.py input.xlsx
    python xlsx2csv.py input.xlsx -o output.csv
    python xlsx2csv.py input.xlsx -s "Sheet2"
"""
import argparse
import sys
from pathlib import Path

try:
    import pandas as pd
except ImportError:
    print("错误：需要安装 pandas。请运行: uv add pandas", file=sys.stderr)
    sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="将 Excel 文件转换为 CSV 格式"
    )
    parser.add_argument(
        "input_file",
        help="输入的 Excel 文件路径"
    )
    parser.add_argument(
        "-o", "--output",
        help="输出的 CSV 文件路径（默认：与输入文件同名，扩展名改为 .csv）"
    )
    parser.add_argument(
        "-s", "--sheet",
        default=0,
        help="要转换的工作表名称或索引（默认：第一个工作表）"
    )
    parser.add_argument(
        "--encoding",
        default="utf-8-sig",
        help="输出文件编码（默认：utf-8-sig，带 BOM 的 UTF-8）"
    )
    parser.add_argument(
        "--index",
        action="store_true",
        help="保留行索引（默认：不保留）"
    )
    
    args = parser.parse_args()
    
    # 检查输入文件
    input_path = Path(args.input_file)
    if not input_path.exists():
        print(f"错误：文件 '{input_path}' 不存在。", file=sys.stderr)
        sys.exit(1)
    
    # 确定输出文件路径
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.with_suffix(".csv")
    
    # 读取 Excel 文件
    try:
        # 尝试将 sheet 参数转换为整数（索引）
        try:
            sheet = int(args.sheet)
        except ValueError:
            sheet = args.sheet  # 保持为字符串（工作表名称）
        
        df = pd.read_excel(input_path, sheet_name=sheet)
    except Exception as e:
        print(f"读取 Excel 文件时出错: {e}", file=sys.stderr)
        sys.exit(1)
    
    # 保存为 CSV
    try:
        df.to_csv(
            output_path,
            index=args.index,
            encoding=args.encoding
        )
        print(f"转换完成: {output_path}")
    except Exception as e:
        print(f"保存 CSV 文件时出错: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
