import sys
import os
import uuid
import datetime
import pandas as pd
from docx import Document

def extract_text_from_docx(filepath):
    """Extracts all text from a docx file."""
    try:
        doc = Document(filepath)
        full_text = []
        for para in doc.paragraphs:
            full_text.append(para.text)
        return "\n".join(full_text)
    except Exception as e:
        print(f"Error reading docx: {e}")
        return ""

def extract_text_from_txt(filepath):
    """Extracts all text from a txt file."""
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            return f.read()
    except Exception as e:
        print(f"Error reading txt: {e}")
        return ""

def simple_keyword_parser(text, template_fields):
    """
    A simple heuristic parser that looks for keywords in text
    and tries to find values associated with them.
    """
    extracted_data = {}
    lines = text.split('\n')
    
    # Basic mapping logic: look for lines containing key-like strings
    # This is a placeholder for more complex logic in the future
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        for field in template_fields:
            # If the field name (or part of it) is in the line
            if field.lower() in line.lower() or ":" in line:
                # Try to split by colon or common delimiters
                parts = line.split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    # Check if this key matches our template
                    # In a real scenario, we'd use a fuzzy matcher or a mapping table
                    extracted_data[key] = val
                elif len(parts) == 1:
                    # If no colon, maybe the whole line is a value for a previously found key
                    pass 

    return extracted_data

def main(input_file):
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        sys.exit(1)

    ext = os.path.splitext(input_file)[1].lower()
    print(f"Processing file: {input_file} (Ext: {ext})")
    
    # In a real implementation, this would call the existing ERP template reader
    # For this prototype, we'll just simulate finding some fields
    template_fields = ["CPDL", "CPLB", "CPMC", "TZS", "YYS", "DWMC", "BZ", "KHMBJ", "GG_length", "GG_width", "GG_height", "JHRQ", "WCRQ", "has_safety_checks", "target_age_group", "KHCPMC", "CPMS"]

    content = ""
    if ext == '.docx':
        content = extract_text_from_docx(input_file)
    elif ext == '.txt':
        content = extract_text_from_txt(input_file)
    else:
        print(f"Unsupported format: {ext}")
        sys.exit(1)

    print("Extracted Text Sample:")
    print(content[:200] + "...")

    # Perform extraction
    extracted = simple_keyword_parser(content, template_fields)
    print(f"Extracted Fields: {list(extracted.keys())}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python docx2json.py <input_file>")
        sys.exit(1)
    main(sys.argv[1])
