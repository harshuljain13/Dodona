"""Tests for the setup scripts that check behaviour, not spelling.

This file used to grep the scripts for substrings — `assert "evict" in
observability.lower()` and so on. That passes for a script that is nonsense but
contains the word, fails on any honest rename, and caught none of the three
bugs this tooling actually shipped: a script that only parsed under bash, a
script invoked under a name that had been changed, and a guard nobody ran.

Each test below corresponds to one of those.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "infra" / "cluster" / "setup"

SHELL_SCRIPTS = sorted(SETUP.glob("*.sh"))

#: What `make` offers. Deliberately small — the Makefile is the workflow, not a
#: menu. `clean` is the one addition to the original four: a rented GPU has to
#: be returnable, and teardown cannot be expressed by any of the others.
MAKE_TARGETS = {"cluster", "observability", "ssh", "test", "load", "logs", "clean"}


def test_there_are_shell_scripts_to_check() -> None:
    """Guards every other test here against globbing nothing after a move."""
    assert len(SHELL_SCRIPTS) >= 8


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_script_parses_under_bash(script: Path) -> None:
    """`bash -n` on every script.

    up.sh once used a bash array and ran under sh, which failed at a line that
    was fine with a message pointing nowhere. Valid bash is the floor.
    """
    done = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert done.returncode == 0, f"{script.name}: {done.stderr.strip()}"


def _referenced_scripts(text: str) -> set[str]:
    return set(re.findall(r"infra/cluster/setup/([A-Za-z0-9_]+\.(?:sh|py))", text))


@pytest.mark.parametrize(
    "caller", [ROOT / "Makefile", SETUP / "up.sh", SETUP / "fleet.sh",
               ROOT / "docs" / "COMMANDS.md"],
    ids=lambda p: p.name,
)
def test_every_referenced_script_exists(caller: Path) -> None:
    """Renaming a script and missing a caller is a runtime-only failure.

    It happened to nine scripts at once, and the only symptom was a remote
    `No such file or directory` partway through a twenty-minute bring-up.
    """
    missing = sorted(n for n in _referenced_scripts(caller.read_text()) if not (SETUP / n).exists())
    assert not missing, f"{caller.name} calls scripts that do not exist: {missing}"


def test_guard_refuses_on_a_machine_with_no_gpu() -> None:
    """The guard is the only thing stopping Helm running on a laptop."""
    if shutil.which("nvidia-smi"):
        pytest.skip("this machine has a GPU, so the guard is expected to pass")
    done = subprocess.run(["bash", str(SETUP / "_require_gpu.sh")], capture_output=True, text=True)
    assert done.returncode != 0
    assert "GPU box" in done.stdout or "nvidia-smi" in done.stdout


def test_guard_is_sourced_by_every_script_that_touches_the_cluster() -> None:
    """A new script that forgets the guard can helm the laptop."""
    for name in ("k3s.sh", "fleet.sh", "observability.sh"):
        assert "_require_gpu.sh" in (SETUP / name).read_text(), f"{name} has no guard"


def test_up_sh_rejects_an_unknown_command_without_needing_env() -> None:
    """Validate before loading .env, so a typo fails fast and says so."""
    done = subprocess.run(["bash", str(SETUP / "up.sh"), "bogus"], capture_output=True, text=True)
    assert done.returncode != 0
    assert "unknown command" in done.stderr


def test_up_sh_help_lists_exactly_the_commands_it_dispatches() -> None:
    """Help text drifting from the `case` is how a command becomes undiscoverable."""
    source = (SETUP / "up.sh").read_text()
    dispatched = set(re.findall(r"^\s{4}(\w+)\)\s+\w+ ;;", source, re.M))
    assert len(dispatched) >= 6, f"dispatch table looks wrong: {dispatched}"
    done = subprocess.run(["bash", str(SETUP / "up.sh"), "--help"], capture_output=True, text=True)
    assert done.returncode == 0
    for command in dispatched:
        assert f"up.sh {command}" in done.stdout, f"{command} is dispatched but not in --help"


def test_makefile_defines_exactly_the_documented_targets() -> None:
    """The workflow, not a menu. A new one here means a new step to explain."""
    source = (ROOT / "Makefile").read_text()
    phony = set(re.search(r"^\.PHONY:\s*(.+)$", source, re.M).group(1).split())
    assert MAKE_TARGETS <= phony
    assert phony - MAKE_TARGETS == {"help"}, f"undocumented targets: {phony - MAKE_TARGETS - {'help'}}"


def test_makefile_puts_both_import_roots_on_the_pythonpath() -> None:
    """Without both, `python -m gateway.repl` cannot resolve and every
    control-plane entrypoint in the repo becomes unreachable."""
    source = (ROOT / "Makefile").read_text()
    assert re.search(r"^export PYTHONPATH\s*:?=.*control.*cluster", source, re.M)


def test_prometheus_scrapes_the_jobs_the_dashboards_query() -> None:
    """An empty Grafana panel and a missing scrape job look identical."""
    values = (ROOT / "infra" / "cluster" / "k8s" / "observability" / "prometheus-values.yaml").read_text()
    jobs = set(re.findall(r"job_name:\s*([\w-]+)", values))
    assert {"keda", "hami"} <= jobs, f"missing scrape jobs, found: {sorted(jobs)}"


def test_the_harness_never_touches_the_cluster() -> None:
    """`make test` must stay runnable with no GPU and no kubeconfig."""
    harness = (ROOT / "infra" / "control" / "gateway" / "harness.py").read_text()
    assert "helm" not in harness and "kubectl" not in harness


def test_fleet_restarts_the_gateway_so_mounted_code_takes_effect() -> None:
    """orch-serve runs code from a hostPath mount, not from its image.

    `kubectl apply` on an unchanged manifest is a no-op, so syncing new
    control-plane code changes the files on disk while the pod keeps running
    what it loaded at startup. Every edit would silently not take.
    """
    fleet = (SETUP / "fleet.sh").read_text()
    assert "rollout restart deploy/orch-serve" in fleet
    manifest = (ROOT / "infra" / "cluster" / "k8s" / "gateway" / "orch-serve.yaml").read_text()
    assert "hostPath" in manifest, "if the gateway stops mounting code, drop the restart"
