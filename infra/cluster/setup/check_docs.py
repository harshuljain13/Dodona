#!/usr/bin/env python3
"""Every relative link, anchor and asset in the docs resolves.

Cheap to run and catches the failure that is otherwise invisible until someone
clicks: a file moved and the twelve things pointing at it did not.

    python3 infra/cluster/setup/check_docs.py
"""
from __future__ import annotations

import glob
import os
import re
import sys

LINK = re.compile(r"\]\(([^)#]*)(#[^)]+)?\)")
HTML = re.compile(r'(?:src|href)="(?!http|#|mailto)([^"]+)"')
HEAD = re.compile(r"#{1,6}\s+(.*)")


def slug(text: str) -> str:
    return re.sub(r"[^\w\s-]", "", text.lower()).replace(" ", "-")


def anchors(path: str) -> set[str]:
    out, fenced = set(), False
    for line in open(path, errors="replace"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if m := HEAD.match(line):
            out.add(slug(m.group(1).strip()))
    return out


def main() -> int:
    bad = 0
    files = [f for f in glob.glob("**/*.md", recursive=True) if "/.venv/" not in f]
    for f in files:
        d, text = os.path.dirname(f), open(f, errors="replace").read()
        for m in LINK.finditer(text):
            target, anchor = m.group(1).strip(), (m.group(2) or "")[1:]
            if target.startswith(("http", "mailto")):
                continue
            path = os.path.normpath(os.path.join(d, target)) if target else f
            if target and not os.path.exists(path):
                print(f"  broken link    {f} -> {target}")
                bad += 1
                continue
            if anchor and os.path.isfile(path) and path.endswith(".md") \
                    and anchor not in anchors(path):
                print(f"  broken anchor  {f} -> {target}#{anchor}")
                bad += 1
        for m in HTML.finditer(text):
            if not os.path.exists(os.path.normpath(os.path.join(d, m.group(1)))):
                print(f"  broken asset   {f} -> {m.group(1)}")
                bad += 1
    print(f"  {len(files)} docs, {bad} broken")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
