#!/usr/bin/env python3
"""
select_page_thickness.py - 查询纸张厚度

用法:
    uv run ./scripts/select_page_thickness.py --pg <纸张类型> --faw <克重>
    
示例:
    uv run ./scripts/select_page_thickness.py --pg 光粉纸 --faw 80
    uv run ./scripts/select_page_thickness.py --pg 书纸 --faw 100
"""
import argparse
import sys
import pandas as pd
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description="查询纸张厚度")
    parser.add_argument(
        "--pg",
        required=True,
        help="纸张类型，例如：光粉纸、书纸、哑粉、单粉、粉灰、灰板、牛油纸、贴纸",
    )
    parser.add_argument(
        "--faw",
        required=True,
        help="纸张克重，例如：80g、100g、250g",
    )
    args = parser.parse_args()
    
    # 获取脚本所在目录
    script_dir = Path(__file__).parent
    excel_path = script_dir.parent / "assets" / "page_thickness.xlsx"
    
    # 检查文件是否存在
    if not excel_path.exists():
        # 尝试 CSV 格式
        csv_path = script_dir.parent / "assets" / "page_thickness.csv"
        if csv_path.exists():
            try:
                df = pd.read_csv(csv_path)
            except Exception as e:
                print(f"读取 CSV 文件时出错: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            print(f"错误：纸张厚度表不存在。", file=sys.stderr)
            sys.exit(1)
    else:
        try:
            df = pd.read_excel(excel_path)
        except Exception as e:
            print(f"读取 Excel 文件时出错: {e}", file=sys.stderr)
            sys.exit(1)

    # 检查必要的列是否存在
    required_cols = ["page_style", "faw/g", "thickness"]
    for col in required_cols:
        if col not in df.columns:
            print(f"错误：文件中缺少必需的列 '{col}'。", file=sys.stderr)
            sys.exit(1)

    # 匹配查询值
    page_style = args.pg.strip()
    faw = args.faw.strip()
    
    # 尝试匹配
    mask = (
        (df["page_style"].astype(str).str.strip() == page_style) &
        (df["faw/g"].astype(str).str.strip() == faw)
    )
    matched = df.loc[mask]

    if matched.empty:
        # 尝试模糊匹配
        mask_fuzzy = (
            df["page_style"].astype(str).str.contains(page_style, case=False, na=False) &
            df["faw/g"].astype(str).str.contains(faw, case=False, na=False)
        )
        matched = df.loc[mask_fuzzy]
    
    if matched.empty:
        print(f"未找到 {page_style} {faw} 的厚度数据。")
        sys.exit(0)

    # 输出厚度
    for idx, row in matched.iterrows():
        print(row["thickness"])


if __name__ == "__main__":
    main()
