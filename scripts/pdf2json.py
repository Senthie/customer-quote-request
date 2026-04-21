#!/usr/bin/env python3
"""
pdf2json.py - PDF 文件解析器

使用 PyMuPDF 提取 PDF 文本内容，并尝试匹配 ERP 字段
"""
import sys
import os
import fitz  # PyMuPDF


def extract_text_from_pdf(filepath):
    """Extracts all text from a PDF file using PyMuPDF."""
    try:
        doc = fitz.open(filepath)
        full_text = []
        for page in doc:
            full_text.append(page.get_text())
        doc.close()
        return "\n".join(full_text)
    except Exception as e:
        print(f"Error reading PDF: {e}")
        return ""


def simple_keyword_parser(text, template_fields):
    """
    A simple heuristic parser that looks for keywords in text
    and tries to find values associated with them.
    """
    extracted_data = {}
    lines = text.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        # Look for common patterns like "Key: Value" or "Key = Value"
        for delimiter in [':', '=', '  ']:
            if delimiter in line:
                parts = line.split(delimiter, 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    if key and val and len(key) < 100:
                        extracted_data[key] = val
                        break
    
    return extracted_data


def main(input_file):
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        sys.exit(1)

    ext = os.path.splitext(input_file)[1].lower()
    print(f"Processing file: {input_file} (Ext: {ext})")
    
    template_fields = ["CPDL", "CPLB", "CPMC", "TZS", "YYS", "DWMC", "BZ", "KHMBJ", 
                       "GG_length", "GG_width", "GG_height", "JHRQ", "WCRQ", 
                       "has_safety_checks", "target_age_group", "KHCPMC", "CPMS"]

    content = extract_text_from_pdf(input_file)
    print("Extracted Text Sample:")
    print(content[:500] + "..." if len(content) > 500 else content)

    extracted = simple_keyword_parser(content, template_fields)
    print(f"\nExtracted Fields: {list(extracted.keys())}")
    
    # Print extracted data for main.py to parse
    for k, v in extracted.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pdf2json.py <input_file>")
        sys.exit(1)
    main(sys.argv[1])
