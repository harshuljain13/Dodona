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
MAKE_TARGETS = {"fleet-lambda", "fleet-runpod", "fleet-down", "observability", "ssh",
                "test", "load", "logs", "kv", "app", "doctor", "clean"}


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
    dispatched = set(re.findall(r"^\s{4}([\w-]+)\)\s+\w+ ;;", source, re.M))
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


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_grep_in_a_substitution_cannot_abort_the_script(script: Path) -> None:
    """`x=$(... | grep ...)` under `set -euo pipefail` exits on no match.

    grep returns 1 when it finds nothing, pipefail propagates that out of the
    pipeline, and set -e kills the script. The failure looks like the step after
    it never ran — exactly how it presented: the KV connector block aborted
    `make fleet-lambda` before the capacity checks, and the only symptom was `Error 1`
    printed after a heading.

    Backslash continuations are joined first; the real bug spanned two lines,
    and the first version of this test missed it for that reason.
    """
    text = script.read_text()
    if "pipefail" not in text:
        return
    joined = re.sub(r"\\\s*\n\s*", " ", text)
    offenders = [
        line.strip()
        for line in joined.splitlines()
        if re.search(r"=\$\(.*\|.*grep", line) and "|| true" not in line
    ]
    assert not offenders, (
        f"{script.name}: grep can abort the script, add `|| true`:\n  "
        + "\n  ".join(offenders))


def test_every_value_doctor_requires_exists_in_env_example() -> None:
    """`make doctor` demanding a line `.env.example` never had is a dead end.

    The prerequisites tell people to find each line in their `.env` and fill in
    the right-hand side. That only works if the line is there to find — and
    HF_TOKEN was not, so the one value with no template entry was also the one
    whose absence stopped a bring-up part way through a weight download.
    """
    import re

    checker = (SETUP / "check_env.py").read_text()
    block = re.search(r"^REQUIRED = \{(.*?)^\}", checker, re.S | re.M)
    assert block, "REQUIRED moved — this test can no longer see what doctor wants"
    required = set(re.findall(r'"([A-Z_]+)":', block.group(1)))
    assert required, "REQUIRED parsed as empty, so this test would pass vacuously"

    template = (ROOT / ".env.example").read_text()
    declared = set(re.findall(r"^(?:export )?([A-Z_][A-Z0-9_]*)=", template, re.M))
    missing = sorted(required - declared)
    assert not missing, f"required by `make doctor`, absent from .env.example: {missing}"


def test_the_guard_refuses_a_container_not_just_a_laptop() -> None:
    """A GPU container passes every other check and still cannot host a cluster.

    `systemctl --version` is not the test — the binary ships in plenty of
    images. PID 1 is. Checking the version is how a RunPod Pod was mistaken for
    a VM, and the cost was a k3s install that wrote its unit files and then
    failed with "Failed to connect to bus", naming systemd rather than the
    container it was trapped in.
    """
    guard = (SETUP / "_require_gpu.sh").read_text()
    assert "/proc/1/comm" in guard, "the guard must test PID 1, not systemctl --version"
    assert "container" in guard.lower()

    # And the bring-up asks before it syncs, so the answer arrives in seconds
    # rather than after a partial install.
    up = (SETUP / "up.sh").read_text()
    assert "require_a_host" in up
    for command in ("cmd_fleet_lambda", "cmd_observability"):
        body = up.split(f"{command}() {{", 1)[1].split("\n}", 1)[0]
        assert "require_a_host" in body, f"{command} does not check for a host first"


def test_scripts_that_cd_to_the_repo_root_actually_land_there() -> None:
    """A `cd` that is one level out fails late and blames the wrong thing.

    box_packages.sh computed PROJECT_ROOT two levels up from
    infra/cluster/setup/, which is infra/. pip then reported
    "Could not open requirements file: requirements.txt" — a message about pip,
    on a box, after a sync, with nothing pointing at the `cd`. The venv would
    have been built in infra/ too, where nothing else looks for it.

    So: evaluate each script's own root expression and insist it lands on a
    directory holding the files it then reads.
    """
    import re

    for name, marker in (("box_packages.sh", "requirements.txt"),):
        script = SETUP / name
        src = script.read_text()
        match = re.search(r'^PROJECT_ROOT="(.+)"$', src, re.M)
        assert match, f"{name}: no PROJECT_ROOT to check"
        # Run the expression with $0 bound to the script, exactly as it runs.
        done = subprocess.run(
            ["bash", "-c", f'printf "%s" "{match.group(1)}"', str(script)],
            capture_output=True, text=True,
        )
        assert done.returncode == 0, f"{name}: root expression failed: {done.stderr}"
        landed = Path(done.stdout.strip())
        assert (landed / marker).is_file(), (
            f"{name} cd's to {landed}, which has no {marker}"
        )


def test_workers_sets_up_its_own_environment_on_the_box() -> None:
    """The Makefile's exports do not cross an ssh connection.

    `make fleet-runpod` runs workers.sh on the GPU box. PYTHONPATH is exported by
    the Makefile on the Mac and arrives nowhere, so `python -m router.mooncake`
    was ModuleNotFoundError; `vllm` lives in .venv/bin and engine.sh calls it
    bare, so it was "command not found" even though pip had installed it. Both
    crash-looped, and both messages blamed the thing being run rather than the
    environment it was run in.
    """
    src = (ROOT / "infra" / "local" / "workers.sh").read_text()
    assert "export PYTHONPATH=" in src, "workers.sh must set PYTHONPATH itself"
    assert "infra/control" in src and "infra/cluster" in src, "both import roots"
    assert "export PATH=" in src and ".venv/bin" in src, "the venv must be on PATH for vllm"


def test_port_probe_survives_a_free_port() -> None:
    """Under `set -euo pipefail`, probing a FREE port must not kill the script.

    grep returns 1 when it matches nothing, pipefail makes that the pipeline's
    status, and a command substitution carries it into an assignment that set -e
    treats as fatal. workers.sh exited before printing a line — on a box where
    every port was free, the one case the check exists to wave through.

    Extract the function and run it under the same flags the script uses.
    """
    src = (ROOT / "infra" / "local" / "workers.sh").read_text()
    start = src.index("port_holder() {")
    body = src[start:src.index("\n}", start) + 2]
    # 59999 is almost certainly free; if it is not, the probe should still
    # return cleanly, which is the property under test either way.
    done = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{body}\nholder=$(port_holder 59999)\necho SURVIVED"],
        capture_output=True, text=True,
    )
    assert "SURVIVED" in done.stdout, (
        f"probing a free port aborted the script: rc={done.returncode} {done.stderr}"
    )
