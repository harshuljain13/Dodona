from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_locust_and_crew_hit_orch_serve() -> None:
    locust = (ROOT / "benchmarking" / "locustfile.py").read_text()
    crew = (ROOT / "benchmarking" / "crew_flood.py").read_text()
    assert "HttpUser" in locust
    assert "/v1/chat/completions" in locust
    assert "127.0.0.1:8080" in locust
    assert "tenant" in locust
    assert "ROLES" in crew
    assert "/chat/completions" in crew
    assert "8080" in crew
    assert "litellm" not in locust.lower() and "litellm" not in crew.lower()
    assert "sk-lab" not in locust and "sk-lab" not in crew
    # Text only. The vision task went when the fleet became one model per card:
    # the gateway advertises `vision` from its allowlist while no engine serves
    # it, so that task was a permanent ~11% error rate in the UI — and a
    # permanent error rate teaches you to stop reading the error column.
    assert '"model": "text"' in locust
    assert '"model": "vision"' not in locust, (
        "a vision task cannot be served by a single-model fleet; it only "
        "produces failures that are indistinguishable from real ones")
    assert "TEXT_MODEL" not in locust and "TEXT_MODEL" not in crew
    assert "crewai" not in crew.lower() and "OPENAI" not in crew
    assert "LOCAL_BASE_URL" not in crew
    assert "LOCAL_BASE_URL" not in (ROOT / "infra" / "control" / "edge" / "harness.py").read_text()
    serve = (ROOT / "infra" / "control" / "gateway" / "serve.py").read_text()
    assert "from edge.guardrails import inspect" in serve
    assert "prepare_chat" in serve

def test_load_generators_are_separate_from_the_control_plane() -> None:
    """benchmarking/ must not import the control plane.

    A load generator that shares code with the thing it measures can agree with
    it about something they are both wrong about. These are stdlib plus locust.
    """
    for f in sorted((ROOT / "benchmarking").glob("*.py")):
        src = f.read_text()
        for forbidden in ("from gateway", "import gateway", "from router",
                          "import router", "from edge", "import edge"):
            assert forbidden not in src, f"{f.name} imports the control plane"


def test_two_folders_two_entrypoints() -> None:
    assert (ROOT / "infra" / "control" / "gateway" / "harness.py").is_file()
    assert (ROOT / "infra" / "control" / "gateway" / "repl.py").is_file()
    assert (ROOT / "benchmarking" / "locustfile.py").is_file()
    assert (ROOT / "benchmarking" / "crew_flood.py").is_file()
    assert (ROOT / "infra" / "control" / "edge" / "guardrails.py").is_file()
    assert (ROOT / "infra" / "control" / "edge" / "harness.py").is_file()
    assert not (ROOT / "app.py").exists()
    assert not (ROOT / "fakeworker").exists()
    assert not (ROOT / "load").exists()

def test_gateway_tools_do_not_import_the_app_front_door() -> None:
    repl = (ROOT / "infra" / "control" / "gateway" / "repl.py").read_text()
    harness = (ROOT / "infra" / "control" / "gateway" / "harness.py").read_text()
    for src in (repl, harness):
        assert "locust" not in src.lower()
        assert "crewai" not in src.lower()
        assert "open-webui" not in src.lower()


def test_the_load_can_actually_reach_the_admission_threshold() -> None:
    """A load generator that cannot fill the KV pool proves nothing.

    The prompts were seven tokens with `max_tokens` 32 — about 42 tokens
    resident per request. Against a ~217,000-token pool that needed 4,650
    concurrent requests to reach 0.90, while the engine runs 64. So the policy
    could never fire, every request succeeded, and the run read as "admission
    control does nothing" rather than "this test cannot ask the question".

    Off by a factor of 36, and nothing in the output said so.

    Read from the source rather than imported: `import locust` runs
    `gevent.monkey.patch_all()`, and reloading a monkey-patched module inside
    pytest recurses until the stack ends. `make test` also has to stay runnable
    with no GPU and no load dependencies.
    """
    import re

    src = (ROOT / "benchmarking" / "locustfile.py").read_text()

    def default(name: str) -> int:
        # The constant and its env var differ — PROMPT_TOKENS is set from
        # LOAD_PROMPT_TOKENS — so match the assignment and take the default
        # out of it rather than assuming the two names are the same.
        m = re.search(rf'^{name} = int\(os\.environ\.get\([^,]+, "(\d+)"\)\)',
                      src, re.M)
        assert m, f"{name} is no longer an int with a literal default"
        return int(m.group(1))

    resident = default("PROMPT_TOKENS") + default("MAX_TOKENS")
    needed = 0.90 * 217_000 / resident
    assert needed <= 64, (
        f"the default load needs {needed:.0f} concurrent requests to reach the "
        f"0.90 threshold, but an engine runs at most 64 — this load cannot "
        f"demonstrate admission control")

    # And the filler must be sized in characters, not words. The first version
    # joined PROMPT_TOKENS *words*, which overshot by 1.9x — in the one file
    # whose entire purpose is this arithmetic.
    assert "tokens * 4" in src, (
        "the filler is not sized against a character budget, so its token "
        "count will not match the number it reports")


def test_the_load_tells_a_shed_apart_from_a_quota_refusal() -> None:
    """503 is capacity and 429 is a client quota. Conflating them hides both.

    503 used to be reported as a plain failure, so admission control working
    painted the UI red — the opposite of what it means — and 429 was reported
    as a SUCCESS, which hid a per-tenant quota behind a green bar. A sweep that
    was entirely rate-limited looked like a saturated fleet.
    """
    locust = (ROOT / "benchmarking" / "locustfile.py").read_text()
    assert "503" in locust and "429" in locust, "neither outcome is handled"
    for code, word in ((503, "admission"), (429, "quota")):
        line = next((l for l in locust.splitlines()
                     if f'"{code}' in l or f"{code} " in l and "resp" in l), "")
        assert word in line.lower() or word in locust.lower(), (
            f"{code} is not explained as a {word} event")
    assert locust.index("503") != locust.index("429"), "handled identically"
