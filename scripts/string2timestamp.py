#!/usr/bin/env python3
"""
string2timestamp.py - 将日期字符串转换为毫秒时间戳

支持格式:
- 2027-05-01
- 2027/05/01  
- 2027.05.01
- May-27 (解释为 2027-05-01)
- 2027-05 (解释为 2027-05-01)
"""
import sys
import re
from datetime import datetime, timezone


def parse_date(date_str: str) -> datetime:
    """解析多种日期格式"""
    date_str = date_str.strip()
    
    # 尝试带时间的格式 YYYY-MM-DD HH:MM:SS
    for fmt in ["%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y.%m.%d %H:%M:%S"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    # 尝试标准格式 YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD
    for fmt in ["%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"]:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    
    # 尝试 YYYY-MM (默认为 1 日)
    for fmt in ["%Y-%m", "%Y/%m", "%Y.%m"]:
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.replace(day=1)
        except ValueError:
            continue
    
    # 尝试 MMM-YY 格式 (如 May-27)
    month_map = {
        'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
    }
    
    # 匹配 May-27 或 May-2027
    match = re.match(r'([a-zA-Z]{3})[-/](\d{2,4})', date_str)
    if match:
        month_abbr = match.group(1).lower()
        year_str = match.group(2)
        
        if month_abbr in month_map:
            month = month_map[month_abbr]
            # 处理 2 位数年份
            year = int(year_str)
            if year < 100:
                year = 2000 + year
            
            return datetime(year, month, 1)
    
    raise ValueError(f"无法解析日期格式: {date_str}")


def main():
    if len(sys.argv) < 2:
        print("用法: python string2timestamp.py <日期字符串>", file=sys.stderr)
        print("示例: python string2timestamp.py 2027-05-01", file=sys.stderr)
        sys.exit(1)
    
    date_str = " ".join(sys.argv[1:])
    
    try:
        dt = parse_date(date_str)
        # 使用 UTC 时区
        dt = dt.replace(tzinfo=timezone.utc)
        timestamp_ms = int(dt.timestamp() * 1000)
        print(timestamp_ms)
    except ValueError as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
