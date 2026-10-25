#!/usr/bin/env bash
# Put Prometheus and Grafana on the box, as binaries.
#
#   Run on: the GPU box. Called by box_packages.sh, so `make fleet-runpod`
#   installs them the same way it installs vLLM.
#
# The Kubernetes path installs both with Helm, which needs a cluster — so on a
# container box there was no equivalent at all, and workers.sh printed
# "prometheus not installed — skipping" with a link. That is a fine message for
# an optional extra and a bad one for half the demo: KV pressure and the
# admission decision are things you watch, not things you read about.
#
# Idempotent. Already installed is a no-op, so `make fleet-runpod` can run this
# every time without paying for it twice.
set -uo pipefail

BIN="${OBS_BIN:-/usr/local/bin}"
WORK="${OBS_WORK:-/opt/observability}"

say()  { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
step() { printf '    %-34s' "$1"; }
ok()   { printf 'ok\n'; }
skip() { printf '%s\n' "$1"; }

mkdir -p "$BIN" "$WORK"

# Resolve the newest release rather than pinning a version that rots, with a
# fallback for when the API is rate-limited — the same approach k3s.sh takes,
# and for the same reason: a hardcoded version is a silent time bomb.
latest_tag() {
  curl -sfL -m 20 "https://api.github.com/repos/$1/releases/latest" 2>/dev/null \
    | sed -n 's/.*"tag_name" *: *"v\{0,1\}\([^"]*\)".*/\1/p' | head -1
}

say "observability  —  prometheus, grafana"

# ── Prometheus ─────────────────────────────────────────────────────────────
step "prometheus"
if command -v prometheus >/dev/null 2>&1; then
  skip "already installed"
else
  ver="$(latest_tag prometheus/prometheus)"
  ver="${ver:-3.5.0}"
  url="https://github.com/prometheus/prometheus/releases/download/v${ver}/prometheus-${ver}.linux-amd64.tar.gz"
  if curl -sfL -m 300 "$url" -o "$WORK/prom.tgz" 2>/dev/null \
     && tar -xzf "$WORK/prom.tgz" -C "$WORK" 2>/dev/null; then
    d="$(find "$WORK" -maxdepth 1 -type d -name 'prometheus-*' | head -1)"
    install -m755 "$d/prometheus" "$BIN/prometheus" 2>/dev/null
    install -m755 "$d/promtool" "$BIN/promtool" 2>/dev/null
    rm -f "$WORK/prom.tgz"
    command -v prometheus >/dev/null 2>&1 && ok || skip "FAILED"
  else
    skip "download failed ($ver) — dashboards will be unavailable"
  fi
fi

# ── Grafana ────────────────────────────────────────────────────────────────
# Versions come from GitHub, the tarball from dl.grafana.com: Grafana does not
# attach binaries to its GitHub releases.
step "grafana"
if command -v grafana >/dev/null 2>&1 || command -v grafana-server >/dev/null 2>&1; then
  skip "already installed"
else
  ver="$(latest_tag grafana/grafana)"
  ver="${ver:-11.6.1}"
  url="https://dl.grafana.com/oss/release/grafana-${ver}.linux-amd64.tar.gz"
  if curl -sfL -m 600 "$url" -o "$WORK/graf.tgz" 2>/dev/null \
     && tar -xzf "$WORK/graf.tgz" -C "$WORK" 2>/dev/null; then
    d="$(find "$WORK" -maxdepth 1 -type d -name 'grafana-*' | head -1)"
    rm -f "$WORK/graf.tgz"
    # Grafana needs its homepath beside the binary, so wrap rather than copy:
    # a bare binary on PATH starts and then cannot find its own assets.
    cat > "$BIN/grafana-server" <<WRAP
#!/usr/bin/env bash
exec "$d/bin/grafana" server --homepath "$d" "\$@"
WRAP
    chmod 755 "$BIN/grafana-server"
    command -v grafana-server >/dev/null 2>&1 && ok || skip "FAILED"
  else
    skip "download failed ($ver) — dashboards will be unavailable"
  fi
fi

exit 0
