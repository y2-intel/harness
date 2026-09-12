"""Collect new, reviewed product notes without assigning versions in source."""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess


SECTIONS = ("Breaking Changes", "New Features", "Improvements", "Bug Fixes", "Security")


def render_notes(tag: str, fragments: list[str]) -> str:
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag):
        raise ValueError("release tag must be strict SemVer")
    sections: dict[str, list[str]] = {name: [] for name in SECTIONS}
    for fragment in fragments:
        section = None
        if len(fragment.encode()) > 65536:
            raise ValueError("release note fragment exceeds 64 KiB")
        for line in fragment.splitlines():
            if not line.strip():
                continue
            if line.startswith("### ") and line[4:] in sections:
                section = line[4:]
                continue
            if section is None or not re.fullmatch(r"- \*\*[^*]+:\*\* \S.*", line):
                raise ValueError("notes require supported sections and named single-line bullets")
            if re.search(r"(?<![A-Za-z0-9_])Y2(?![A-Za-z0-9_])|#[0-9]+\b|github\.com/[^\s)]+/(pull|issues|commit)/", line):
                raise ValueError("notes must use lowercase y2 and omit tracker references")
            if line not in sections[section]:
                sections[section].append(line)
    parts = [f"### {name}\n\n" + "\n".join(sections[name]) for name in SECTIONS if sections[name]]
    return ("\n\n".join(parts) if parts else f"y2 {tag}") + "\n"


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True)


def collect_fragments(source: str, previous: str) -> list[str]:
    if previous:
        paths = git("diff", "--name-only", "--diff-filter=A", previous, source, "--", "changes/")
    else:
        paths = git("ls-tree", "-r", "--name-only", source, "--", "changes/")
    fragments = []
    for path in sorted(paths.splitlines()):
        if re.fullmatch(r"changes/[a-z0-9][a-z0-9_-]*\.md", path):
            fragments.append(git("show", f"{source}:{path}"))
    return fragments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=pathlib.Path)
    parser.add_argument("--output", required=True, type=pathlib.Path)
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    args.output.write_text(render_notes(plan["tag"], collect_fragments(plan["source_sha"], plan["previous_tag"])))


if __name__ == "__main__":
    main()
