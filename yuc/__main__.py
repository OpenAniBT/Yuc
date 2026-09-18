from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .archive import sync, validate_archive


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive public Yuc articles as raw HTML and structured JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sync_parser = sub.add_parser("sync", help="discover, fetch and validate all articles")
    sync_parser.add_argument("--full", action="store_true", help="fetch full responses without conditional requests")
    sync_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    validate_parser = sub.add_parser("validate", help="verify manifest, schemas, raw hashes and reproducible parsing offline")
    validate_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    try:
        if args.command == "sync":
            result = sync(args.data_dir, full=args.full)
            Path("reports").mkdir(exist_ok=True)
            Path("reports/sync.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            result = validate_archive(args.data_dir)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
