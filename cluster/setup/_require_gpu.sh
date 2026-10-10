# Refuse to run cluster tooling anywhere but the GPU box, so Helm and HAMi can
# never be invoked on the laptop path. Sourced by k3s.sh, fleet.sh, engine.sh
# and observability.sh.
if [[ "$(uname -s)" == "Darwin" ]]; then
  echo "Lambda operates like a cluster. This script is for the GPU box."
  echo "FakeWorker on the Mac does not run Helm or HAMi."
  exit 1
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "nvidia-smi missing — this is the FakeWorker/laptop path. Do not helm here."
  exit 1
fi
