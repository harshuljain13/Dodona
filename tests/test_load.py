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
    assert '"model": "text"' in locust and '"model": "vision"' in locust
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
