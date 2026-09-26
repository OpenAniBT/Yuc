from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .archive import sync, validate_archive
from .health import check_status
from .report import annotations, fatal_report, prepare_report_dir, write_report


def _print_result(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for annotation in annotations(result):
            print(annotation)


def _sync(args: argparse.Namespace) -> int:
    try:
        prepare_report_dir(args.report_dir)
        result = sync(args.data_dir, full=args.full, report_dir=args.report_dir)
        write_report(args.report_dir, result)
    except Exception as exc:
        result = fatal_report(exc, full=args.full)
        try:
            write_report(args.report_dir, result)
        except Exception as report_exc:
            # The report destination itself may be unavailable; preserve diagnostics.
            print("ERROR writing run report: " + json.dumps(str(report_exc)), file=sys.stderr)
        _print_result(result)
        return 1
    _print_result(result)
    return 0 if result["state"] == "healthy" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive public Yuc articles as raw HTML and structured JSON")
    sub = parser.add_subparsers(dest="command", required=True)
    sync_parser = sub.add_parser("sync", help="discover, fetch and validate all articles")
    sync_parser.add_argument("--full", action="store_true", help="fetch full responses without conditional requests")
    sync_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    sync_parser.add_argument("--report-dir", type=Path, default=Path("reports"))
    validate_parser = sub.add_parser("validate", help="verify manifest, schemas, raw hashes and reproducible parsing offline")
    validate_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    health_parser = sub.add_parser("health", help="check synchronization status and its 24-hour freshness offline")
    health_parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    try:
        if args.command == "sync":
            return _sync(args)
        elif args.command == "health":
            result = check_status(args.data_dir)
            _print_result(result)
            return 0 if result["state"] == "healthy" and result["fresh"] else 1
        else:
            result = validate_archive(args.data_dir)
        _print_result(result)
        return 0
    except Exception as exc:
        print("ERROR: " + json.dumps({"kind": type(exc).__name__, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
