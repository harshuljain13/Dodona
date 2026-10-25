#!/usr/bin/env python3
"""Every value `.env` has to carry is present, named, and not a placeholder.

The failure this prevents is finding a missing key twelve minutes into
`make cluster`, reported by whatever happened to need it — a HuggingFace 401
deep inside a weight download, or LiteLLM announcing "No API credentials" as
though a cloud account were missing. Each of those names its own symptom and
not its cause.

    python3 infra/cluster/setup/check_env.py     (or: make doctor)

Run on: Mac, before the first bring-up. Reads `.env` and touches nothing else.

**No value is ever printed.** The output is variable names and verdicts, so it
can be pasted into an issue, read over a shoulder, or put on a projector during
a workshop without leaking anything.

`.env` is parsed rather than sourced: sourcing it would run whatever is in the
file, and under `set -u` a single unset reference silently abandons the rest —
which is exactly the half-loaded state this check exists to catch.
"""
from __future__ import annotations

import os
import re
import sys

#: What must be set for a bring-up to complete. The comment is the consequence
#: of it being absent, because that is the question someone has when they read
#: a failure here.
REQUIRED = {
    "BOX": "no box to drive — `make cluster` has nothing to ssh to",
    "SSH_KEY": "no key to ssh with",
    "SSH_PORT": "defaults to 22; RunPod uses a high port, so a wrong one looks like a dead box",
    "HF_TOKEN": "the model download fails partway through bring-up",
    "DEEPGRAM_API_KEY": "`make app` refuses to start (typing still works)",
    "FIRECRAWL_API_KEY": "both agent tools return 401 and it answers from memory, sounding certain",
}

#: Absent is a choice here, not a mistake, so these are reported and never fail.
OPTIONAL = {
    "OVERFLOW_API_KEY": "overflow never leaves — same as a cluster that is keeping up",
    "AGENTSHIP_OTEL_EXPORTERS": "the agent's spans are built and ship nowhere",
}

#: The older names, kept working so an existing `.env` needs no editing.
ALIASES = {"BOX": "LAMBDA", "SSH_KEY": "LAMBDA_SSH_KEY"}

#: A value that is still the template's. `OPENAI_API_KEY` is deliberately a
#: fixed fake string, so "not-used" is correct there and only there.
PLACEHOLDER = re.compile(r"xxxx|YOUR_|your_|<.*>", re.I)

ASSIGN = re.compile(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$")


def read_env(path: str) -> dict[str, str]:
    """Parse `NAME=value` and `export NAME=value`, stripping one layer of quotes."""
    values: dict[str, str] = {}
    with open(path, errors="replace") as handle:
        for line in handle:
            if line.lstrip().startswith("#"):
                continue
            if match := ASSIGN.match(line):
                name, raw = match.group(1), match.group(2).strip()
                if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                    raw = raw[1:-1]
                values[name] = raw
    return values


def resolve(values: dict[str, str], name: str) -> str:
    """Return the value under `name` or its legacy alias, whichever is filled."""
    if values.get(name):
        return values[name]
    alias = ALIASES.get(name)
    return values.get(alias, "") if alias else ""


def main() -> int:
    root = os.path.join(os.path.dirname(__file__), "..", "..", "..")
    path = os.path.join(root, ".env")
    if not os.path.exists(path):
        print("  no .env — copy it first:  cp .env.example .env")
        print("  then fill it in: PREREQUISITES.md")
        return 1

    values = read_env(path)
    bad = 0

    for name, consequence in REQUIRED.items():
        value = resolve(values, name)
        if not value:
            print(f"  MISSING   {name:<20} {consequence}")
            bad += 1
        elif PLACEHOLDER.search(value):
            # Worth separating from missing: it reads as filled in, and fails
            # later at the point of use rather than here.
            print(f"  TEMPLATE  {name:<20} still the placeholder from .env.example")
            bad += 1
        else:
            print(f"  ok        {name}")

    # The one value that is a path rather than a secret, so it can be checked
    # for real. A key that is not there fails with "Permission denied", which
    # reads like the box rejecting you rather than a wrong path on this laptop.
    key = os.path.expanduser(os.path.expandvars(resolve(values, "SSH_KEY")))
    if key and not os.path.isfile(key):
        print(f"  MISSING   {'SSH_KEY file':<20} no key at {key}")
        bad += 1
    elif key and (os.stat(key).st_mode & 0o077):
        # ssh silently refuses a world-readable key.
        print(f"  WARN      {'SSH_KEY perms':<20} run: chmod 600 {key}")

    for name, effect in OPTIONAL.items():
        if values.get(name):
            print(f"  ok        {name}")
        else:
            print(f"  unset     {name:<24} {effect}")

    print()
    if bad:
        print(f"  {bad} to fix before `make cluster` — see PREREQUISITES.md")
        return 1
    print("  .env is complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
