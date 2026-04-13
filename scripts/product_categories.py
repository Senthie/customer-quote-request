#!/usr/bin/env python3
"""
product_categories.py - 从产品类别映射表查询 CPLB 和 ZLBM

用法:
    uv run ./scripts/product_categories.py --select <customer_product_categories>
    
示例:
    uv run ./scripts/product_categories.py --select "Paperback"
"""
import argparse
import sys
import pandas as pd
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="查询产品类别映射表")
    parser.add_argument(
        "--select",
        required=True,
        help="要匹配的 customer_product_categories 值，例如 Paperback",
    )
    args = parser.parse_args()
    
    # 获取脚本所在目录，并拼接 assets/product_categories.xlsx
    script_dir = Path(__file__).parent
    excel_path = script_dir.parent / "assets" / "product_categories.xlsx"
    
    # 检查文件是否存在
    if not excel_path.exists():
        print(f"错误：文件 '{excel_path}' 不存在。", file=sys.stderr)
        sys.exit(1)

    try:
        df = pd.read_excel(excel_path)
    except Exception as e:
        print(f"读取 Excel 文件时出错: {e}", file=sys.stderr)
        sys.exit(1)

    # 检查必要的列是否存在
    required_cols = ["customer_product_categories", "CPLB", "ZLBM"]
    for col in required_cols:
        if col not in df.columns:
            print(f"错误：Excel 文件中缺少必需的列 '{col}'。", file=sys.stderr)
            sys.exit(1)

    # 匹配查询值（忽略前后空格和大小写）
    query_val = args.select.strip()
    mask = (
        df["customer_product_categories"].astype(str).str.strip().str.lower()
        == query_val.lower()
    )
    matched = df.loc[mask]

    if matched.empty:
        print(f"未找到 customer_product_categories 为 '{query_val}' 的行。")
        sys.exit(0)

    # 输出匹配到的 CPLB 和 ZLBM
    print("匹配结果：")
    for idx, row in matched.iterrows():
        print(f"CPLB: {row['CPLB']}, ZLBM: {row['ZLBM']}")


if __name__ == "__main__":
    main()
