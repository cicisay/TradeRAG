#!/usr/bin/env python3
"""Fail on job-hunting wording, assistant-tool traces, or local absolute paths."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEXT_EXT = {
    ".md", ".txt", ".py", ".js", ".html", ".css", ".json",
    ".yml", ".yaml", ".bat", ".sh", ".toml", ".ini",
}
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__",
    ".pytest_cache", "data", "models", "files", "backups",
}
SKIP_FILES = {"scripts/check_docs.py"}

TRACE_PATTERNS = [
    re.compile(p) for p in (
        r"WorkBuddy", r"workbuddy", r"wb-prompt",
        r"面试", r"简历", r"求职", r"招聘", r"面试官",
        r"作品集", r"八股", r"上岸",
        r"E:\\project-docs", r"D:\\deepseek", r"E:\\Workspace",
        r"C:\\Users\\[A-Za-z]",
    )
]
DOC_AI_PATTERNS = [
    re.compile(p) for p in (
        r"AI\s*(生成|辅助|协作|代写)",
        r"ChatGPT", r"Claude", r"Copilot",
    )
]
NAME_PATTERN = re.compile(r"workbuddy|wb-prompt|interview|面试|README-legacy", re.I)


def tracked_files() -> list[str]:
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [item for item in proc.stdout.decode("utf-8").split("\x00") if item]


def main() -> int:
    violations: list[tuple[str, int, str]] = []
    for rel in tracked_files():
        if rel in SKIP_FILES:
            continue
        if any(part in SKIP_DIRS for part in Path(rel).parts):
            continue
        path = ROOT / rel
        if not path.exists():
            continue
        if NAME_PATTERN.search(Path(rel).name):
            violations.append((rel, 0, "forbidden file name"))
        if path.suffix.lower() not in TEXT_EXT:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        is_doc = path.suffix.lower() in {".md", ".txt"}
        for lineno, line in enumerate(text.splitlines(), 1):
            for pattern in TRACE_PATTERNS:
                if pattern.search(line):
                    violations.append((rel, lineno, line.strip()[:120]))
                    break
            else:
                if is_doc:
                    for pattern in DOC_AI_PATTERNS:
                        if pattern.search(line):
                            violations.append((rel, lineno, line.strip()[:120]))
                            break

    for rel, lineno, line in violations:
        print(f"{rel}:{lineno}: {line}")
    print(f"{len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
