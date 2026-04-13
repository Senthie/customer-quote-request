#!/usr/bin/env python3
"""
customer2erp.py - 从客户 ERP 映射表查询字段映射关系

用法:
    uv run ./scripts/customer2erp.py --field <字段名> --value <查询值>
    
示例:
    uv run ./scripts/customer2erp.py --field "Format" --value "Paperback"
"""
import argparse
import sys
import pandas as pd
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="查询客户 ERP 字段映射表")
    parser.add_argument(
        "--field",
        required=True,
        help="要查询的字段名，例如 Format, Binding 等",
    )
    parser.add_argument(
        "--value",
        required=True,
        help="要匹配的值，例如 Paperback",
    )
    args = parser.parse_args()
    
    # 获取脚本所在目录
    script_dir = Path(__file__).parent
    excel_path = script_dir.parent / "assets" / "customer_erp_mapper.xlsx"
    
    # 检查文件是否存在
    if not excel_path.exists():
        print(f"错误：文件 '{excel_path}' 不存在。", file=sys.stderr)
        sys.exit(1)

    try:
        df = pd.read_excel(excel_path)
    except Exception as e:
        print(f"读取 Excel 文件时出错: {e}", file=sys.stderr)
        sys.exit(1)

    # 检查字段列是否存在
    if args.field not in df.columns:
        print(f"错误：Excel 文件中缺少字段 '{args.field}'。", file=sys.stderr)
        print(f"可用字段: {', '.join(df.columns)}", file=sys.stderr)
        sys.exit(1)

    # 匹配查询值（忽略前后空格和大小写）
    query_val = args.value.strip()
    mask = (
        df[args.field].astype(str).str.strip().str.lower()
        == query_val.lower()
    )
    matched = df.loc[mask]

    if matched.empty:
        print(f"未找到 {args.field} = '{query_val}' 的映射。")
        sys.exit(0)

    # 输出所有匹配行的数据
    print("匹配结果：")
    for idx, row in matched.iterrows():
        for col in df.columns:
            print(f"{col}: {row[col]}")
        print("---")


if __name__ == "__main__":
    main()
