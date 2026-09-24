"""`uv run python -m erp_view [--port …] [--no-open] [--dir …]`"""
from __future__ import annotations

from .app import main

if __name__ == "__main__":
    raise SystemExit(main())
