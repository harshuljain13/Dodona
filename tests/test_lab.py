from __future__ import annotations

from pathlib import Path

from gateway.metrics import METRICS
from gateway.repl import Lab

ROOT = Path(__file__).resolve().parents[1]

def _join(lab: Lab, *lines: str) -> str:
    return "\n".join(row for line in lines for row in lab.exec(line))

def test_text_and_vision_land_on_different_pods() -> None:
    lab = Lab()
    text = _join(lab, "send text")
    vision = _join(lab, "send vision")
    assert "text-0 both" in text
    assert "vision-0 both" in vision
    assert "via=local" in text and "via=local" in vision
    assert "kv_hop=" in text and "None" in text

def test_replicas_and_slice_change_place_and_oom() -> None:
    lab = Lab()
    out = _join(lab, "replicas text 3 vision 1")
    assert "text=3" in out
    assert any(w.id.startswith("text-") for w in lab.left) and len(lab.left) == 3
    _join(lab, "send text")
    assert lab.history[-1]["pods"].startswith("text-")
    _join(lab, "slice text 80")
    oom = _join(lab, "send text tokens=200")
    assert "slice_oom" in oom
    assert "via=local" in oom
    assert lab.history[-1]["via"] == "local"

def test_zero_vision_replicas_overflows() -> None:
    lab = Lab()
    _join(lab, "replicas vision 0")
    out = _join(lab, "send vision")
    assert "via=overflow" in out
    assert "503" in out or "no eligible" in out.lower() or "no pool" in out.lower() or "PLACE" in out

def test_audio_leaves_text_429_stays() -> None:
    lab = Lab()
    audio = _join(lab, "send audio")
    assert "via=overflow" in audio
    _join(lab, "cap 10")
    refuse = _join(lab, "send text tokens=64")
    assert "429" in refuse
    assert "via=local" in refuse
    assert "overflow" not in refuse.split("ROUTE", 1)[-1]

def test_force_codes_leave_or_stay() -> None:
    lab = Lab()
    leave_503 = _join(lab, "force 503")
    leave_529 = _join(lab, "force 529")
    stay_429 = _join(lab, "force 429")
    stay_500 = _join(lab, "force 500")
    stay_oom = _join(lab, "force slice_oom")
    assert "via=overflow" in leave_503 and "LEAVE" in leave_503
    assert "via=overflow" in leave_529
    assert "via=local" in stay_429 and "stay" in stay_429
    assert "via=local" in stay_500
    assert "via=local" in stay_oom
    codes = _join(lab, "codes")
    assert "503" in codes and "LEAVE" in codes
    assert "429" in codes and "stay" in codes.lower()

def test_phase_split_names_prefill_and_decode_pods() -> None:
    lab = Lab()
    out = _join(lab, "split phase", "replicas prefill 2 decode 2", "send text")
    assert "split=phase" in out
    last = lab.history[-1]["pods"]
    assert "prefill=" in last and "decode=" in last

def test_hop_then_evict_records_transfer() -> None:
    lab = Lab()
    hop = _join(lab, "hop")
    assert "kv_hop=" in hop
    handoff = hop.split("HANDOFF", 1)[-1].splitlines()[0]
    assert "None" not in handoff
    assert lab.bus.hops
    evict = _join(lab, "evict")
    assert "evict" in evict.lower()
    assert METRICS.kv_evict_total >= 1

def test_kernel_and_pressure_commands() -> None:
    lab = Lab()
    kernel = _join(lab, "kernel 2048 2048")
    assert "backend" in kernel
    assert "naive/flash=" in kernel
    demo = _join(lab, "pressure 1024 prefix_protect")
    assert "policy=prefix_protect" in demo
    assert "old-unique" in demo
    warm = _join(lab, "warmup 1500")
    assert "speedup" in warm
    seeded = _join(lab, "warmup seed")
    assert "sys=256" in seeded
    assert lab.bus.cached("text-0", "sys") == 256

def test_metrics_command_dumps_prometheus_text() -> None:
    lab = Lab()
    _join(lab, "send text")
    out = _join(lab, "metrics")
    assert "orch_requests_total" in out
    assert "orch_request_duration_seconds" in out
    assert "orch_replica_healthy" in out
    profile = _join(lab, "profile")
    assert "gateway" in profile and "pick" in profile
    assert "requests=" in profile

def test_lab_source_stays_on_the_laptop() -> None:
    src = (ROOT / "gateway" / "repl.py").read_text()
    assert "helm" not in src
    assert "os.system" not in src and "subprocess" not in src
    assert "FakeOverflow" in src
    assert "crewai" not in src.lower()
    assert "openrouter" not in src.lower()
