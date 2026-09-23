#!/usr/bin/env python3
"""
docx2json.py - DOCX → 规范键 JSON（薄封装）

⚠️ 本脚本**不再自带解析逻辑**。历史版本里它有一套「只要行里含 ':' 就当键值对」的
弱解析，与 `main.py` 的口径不一致。1.0.13（2026-09-24）起统一：抽取逻辑只有一份，
在 `extract_fields.py`，由客户档案驱动。本脚本只是它的命令行前端。
（docx 里的**表格**也会被读进来 —— 客户的报价单常以 docx 表格形式发来。）

用法：
    uv run ./scripts/docx2json.py <文件.docx> [--customer igl] [--text]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from customer_profile import resolve_profile          # noqa: E402
from extract_fields import extract_from_text, read_text_document  # noqa: E402


class _Logger:
    def info(self, m, *a):
        pass

    def warn(self, m, *a):
        print(f"[WARN] {m}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="DOCX → 规范键 JSON（客户档案驱动）")
    parser.add_argument("input_file")
    parser.add_argument("--customer", "-c", default="auto")
    parser.add_argument("--text", action="store_true", help="只打印提取到的纯文本")
    args = parser.parse_args()

    path = Path(args.input_file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 1

    if args.text:
        print(read_text_document(str(path)))
        return 0

    logger = _Logger()
    profile, _ = resolve_profile(args.customer, filename=path.name, logger=logger)
    data = extract_from_text(str(path), profile, logger)
    print(json.dumps(data.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
