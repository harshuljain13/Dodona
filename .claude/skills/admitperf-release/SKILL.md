---
name: admitperf-release
description: >-
  Cut an AdmitPerf release — the tag-driven flow, the TestPyPI round trip, the trusted
  publisher step only a human can do, and what cannot be undone. Use when asked to cut a
  release, tag a version, or prepare one.
---

# Cutting a release

## What cannot be undone

**A published version number can never be reused.** Not on PyPI, not on TestPyPI. If `0.1.0`
publishes broken, the fix is `0.1.1` and `0.1.0` stays broken forever. Everything below
exists because of that.

## Before anything

**Trusted Publishing must be configured first, by a human, in a browser.** The workflow
stores no token; it mints one per run over OIDC. Register at
`pypi.org/manage/account/publishing/` with owner `harshuljain13`, repo `AdmitPerf`,
workflow `release.yml`, environment `pypi` — and the same on TestPyPI.

A first-time package that is not registered on **both** indexes half-publishes: TestPyPI
succeeds, PyPI fails, and that version is now burned.

## The flow

```bash
# bump the version in pyproject.toml and admitperf/__init__.py — they must match
git commit -am "release: 0.1.0"
git tag v0.1.0
git push origin v0.1.0
```

A tag publishes. A branch never does.

The workflow then goes **TestPyPI → install back out of TestPyPI → smoke-test → PyPI**. That
round trip is the only thing that catches a missing dependency or a bad pin, because it is a
real index resolve. A local path install cannot fail that way, which is why local success
proves nothing here.

## Rehearse first

`workflow_dispatch` with `target: testpypi` runs the whole thing without cutting a tag. Do
this for any release that changes packaging, adds a dependency, or moves a module.

## Before tagging

- CI green on `main`, including the wheel job that installs into a clean venv and imports
  the public API. That job is what catches a module missing from the package manifest; the
  editable install cannot, because it sees the working tree.
- `docs/status.md` matches what ships.
- No number in the docs lacks a bundle. A release is a bad time to discover one.
