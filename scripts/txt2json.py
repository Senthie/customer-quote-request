import sys
import os

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
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        for field in template_fields:
            if field.lower() in line.lower() or ":" in line:
                parts = line.split(':', 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    val = parts[1].strip()
                    extracted_data[key] = val

    return extracted_data

def main(input_file):
    if not os.path.exists(input_file):
        print(f"File not found: {input_file}")
        sys.exit(1)

    ext = os.path.splitext(input_file)[1].lower()
    print(f"Processing file: {input_file} (Ext: {ext})")
    
    template_fields = ["CPDL", "CPLB", "CPMC", "TZS", "YYS", "DWMC", "BZ", "KHMBJ", "GG_length", "GG_width", "GG_height", "JHRQ", "WCRQ", "has_safety_checks", "target_age_group", "KHCPMC", "CPMS"]

    content = extract_text_from_txt(input_file)
    print("Extracted Text Sample:")
    print(content[:200] + "...")

    extracted = simple_keyword_parser(content, template_fields)
    print(f"Extracted Fields: {list(extracted.keys())}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python txt2json.py <input_file>")
        sys.exit(1)
    main(sys.argv[1])
