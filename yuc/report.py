"""Local run reports and escaped GitHub Actions diagnostics."""
from __future__ import annotations

import datetime as dt
import html
import json
import os
from pathlib import Path
import re
import tempfile


def fatal_report(exc: Exception, *, full: bool) -> dict:
    return {
        "state": "fatal", "published": False, "full": full,
        "completedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "failures": [{"url": None, "pageId": None,
                      "kind": type(exc).__name__, "message": str(exc)}],
    }


def _markdown(value: object) -> str:
    # Values are text, never Markdown links or source-provided HTML.
    value = re.sub(r"([\\`*_{}\[\]()#+.!~\-])", r"\\\1", str(value))
    value = html.escape(value, quote=True).replace("|", "&#124;")
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def summary_markdown(report: dict) -> str:
    lines = ["# Yuc 同步报告", "",
             f"- 状态：{_markdown(report['state'])}",
             f"- 已发布到本地归档：{'是' if report.get('published') else '否'}",
             f"- 完成时间（UTC）：{_markdown(report['completedAt'])}"]
    if "consecutiveFailures" in report:
        lines.append(f"- 连续降级次数：{report['consecutiveFailures']}")
    if "pages" in report:
        lines.append(f"- 已归档页面：{report['pages']}")
    if "changedFiles" in report:
        lines.append(f"- 变更文件：{len(report['changedFiles'])}")
    if report.get("discovery"):
        lines.append(f"- 页面发现：{_markdown(report['discovery']['state'])}")
    if report.get("pageStates"):
        states = report["pageStates"]
        lines.append("- 页面检查：" + "，".join(
            f"{state} {sum(page['state'] == state for page in states)}"
            for state in ("current", "stale", "unavailable")))
    if report.get("failures"):
        lines.extend(["", "## 失败详情", "", "| 页面或步骤 | 错误类型 | 说明 |",
                      "| --- | --- | --- |"])
        for failure in report["failures"]:
            target = failure.get("url") or failure.get("pageId") or "同步"
            lines.append(f"| {_markdown(target)} | {_markdown(failure['kind'])} | {_markdown(failure['message'])} |")
    if report.get("sourceChanges"):
        lines.extend(["", "## 来源数量变化", "", "| 页面 | 集合 | 原数量 | 当前数量 |",
                      "| --- | --- | --- | --- |"])
        for change in report["sourceChanges"]:
            lines.append("| " + " | ".join(_markdown(change[key]) for key in (
                "pageId", "collection", "previousCount", "currentCount")) + " |")
    lines.extend(["", "本报告描述同步阶段；是否已提交和推送，请查看工作流后续步骤。", ""])
    return "\n".join(lines)


def prepare_report_dir(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # A failed run must never leave the previous run's success report in place.
    for name in ("sync.json", "summary.md"):
        (root / name).unlink(missing_ok=True)


def _write_atomic(path: Path, payload: str) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            output.write(payload)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_report(root: Path, report: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    # Render both before replacing either destination.
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    summary = summary_markdown(report)
    _write_atomic(root / "sync.json", payload)
    _write_atomic(root / "summary.md", summary)


def annotations(report: dict) -> list[str]:
    level = "error" if (report.get("state") == "fatal"
                        or report.get("consecutiveFailures", 0) >= 3) else "warning"
    result = []
    for failure in report.get("failures", []):
        target = failure.get("url") or failure.get("pageId") or "sync"
        message = f"{target}: {failure['kind']}: {failure['message']}"
        # Workflow-command data escaping; no source text enters command properties.
        message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        result.append(f"::{level}::{message}")
    return result
