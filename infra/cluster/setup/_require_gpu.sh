# Refuse to run cluster tooling anywhere it cannot work, so Helm and HAMi can
# never be invoked on the laptop path — or inside somebody else's container.
# Sourced by k3s.sh, fleet.sh, engine.sh and observability.sh.
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "This script is for the GPU box, not the Mac."
  echo "FakeWorker on the Mac does not run Helm or HAMi."
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi missing — this is the FakeWorker/laptop path. Do not helm here."
  exit 1
fi

# A container with a GPU passes every check above and still cannot host a
# cluster: k3s installs itself as a systemd service, and HAMi's device plugin
# needs the kernel. Left to run, `curl get.k3s.io | sh` writes its unit files,
# enables them, and only then fails with
#
#     System has not been booted with systemd as init system (PID 1).
#     Failed to connect to bus: Host is down
#
# which names systemd and says nothing about the machine being a container —
# after two minutes and a half-installed k3s.
#
# `systemctl --version` is NOT the test. The binary ships in plenty of images;
# what matters is whether it is PID 1. Checking the version is how this was
# missed in the first place.
if [[ "$(cat /proc/1/comm 2>/dev/null)" != "systemd" ]]; then
  echo
  echo "  This box is a container, not a host — PID 1 is $(cat /proc/1/comm 2>/dev/null || echo unknown)."
  echo
  echo "  k3s installs as a systemd service and HAMi slices the GPU through the"
  echo "  kernel. Neither can happen inside someone else's container, so nothing"
  echo "  below this point can work here."
  echo
  echo "  On RunPod that means a standard Pod. Book an Instant Cluster or a"
  echo "  bare-metal instance instead — see PREREQUISITES.md step 6."
  echo
  exit 1
fi
