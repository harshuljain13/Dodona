# Debugging commands — module 10

10 shares its cluster with module 9, so
**[module 9's COMMANDS.md](COMMANDS.md)** still applies for
Kubernetes, HAMi, NVIDIA and KEDA. This file covers what 10 adds: the tunnel,
Grafana, the new metrics, the load tools, and Open WebUI.

```
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
```

## Names in this cluster

| Thing | Name | Namespace |
|---|---|---|
| Browser chat | `open-webui`, NodePort `30030` | `default` |
| Metrics | `prometheus-server` | `monitoring` |
| Dashboards | `grafana`, NodePort `31495` | `monitoring` |
| Dashboard source | ConfigMap `class10-dashboards` | `monitoring` |
| GPU telemetry | `dcgm-exporter` (DaemonSet) | `monitoring` |

---

## The tunnel — check this first

Almost every 10 failure is a dead tunnel, not a broken cluster.

```
ps aux | grep 'ssh -i' | grep -v grep
```

Run on: Mac. Is the Step 2 session still alive.

```
lsof -iTCP:8080 -sTCP:LISTEN
lsof -nP -iTCP -sTCP:LISTEN | grep -E '8000|8001|8080|30030|30080|31495|50051'
```

Which forwarded ports are actually bound locally. A missing port means that
`-L` failed — usually something else already holds it.

```
curl -sf http://127.0.0.1:8080/metrics >/dev/null && echo "gateway reachable" || echo "TUNNEL DOWN"
```

The one-line health check. If this fails, re-run `bash setup/ssh.sh`; nothing
else will work until it passes.

---

## Grafana

```
kubectl -n monitoring get pods,svc
```

Is the observability stack up. It is installed by `day2_observability.sh`
(Step 22), *not* by the cluster bring-up — missing pods usually means Step 22
was skipped.

```
kubectl -n monitoring get secret grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
```

The admin password. Regenerated on every install; user is always `admin`.

```
kubectl -n monitoring get configmap class10-dashboards -o jsonpath='{.metadata.labels}{"\n"}'
```

**Must include `grafana_dashboard=1`.** The Grafana sidecar only imports
ConfigMaps carrying that label — a missing label is the usual reason dashboards
do not appear.

```
kubectl -n monitoring get configmap class10-dashboards -o jsonpath='{.data}' | jq -r 'keys[]'
```

Which of the ten dashboards actually loaded.

```
kubectl -n monitoring logs deploy/grafana -c grafana-sc-dashboard --tail=30
```

The sidecar's own log — it says what it imported and what it rejected.

```
python -m observability.dashboards
```

Regenerate the ten JSON files from `dashboards.py`. Run on: Mac or box. Do this
after changing a metric name, then re-run Step 22.

---

## Prometheus

```
kubectl -n monitoring port-forward svc/prometheus-server 9090:80
```

Then `http://localhost:9090` for the expression browser.

```
curl -s 'http://localhost:9090/api/v1/targets' | jq -r '.data.activeTargets[] | "\(.health) \(.labels.job)"' | sort | uniq -c
```

**Which scrape targets are healthy.** A dashboard that is flat usually means a
`down` target here, not a missing metric.

```
curl -s 'http://localhost:9090/api/v1/query?query=orch_request_duration_seconds_count' | jq .
```

Are the 10 histograms arriving at all.

---

## The 10 metrics

```
curl -s http://127.0.0.1:8080/metrics | grep orch_
```

Everything module 9 exposed, plus:

| Metric | Reads as |
|---|---|
| `orch_request_duration_seconds{stage}` | **Histogram** per stage: `gateway`, `pick`, `local`, `overflow`, `e2e` |
| `orch_replica_kv_free_ratio{pod}` | Per-pod KV headroom — module 9 had one fleet-wide gauge |
| `orch_replica_queue_depth{pod}` | Per-pod queue |
| `orch_replica_tokens_in_flight{pod}` | Per-pod load |
| `orch_replica_healthy{pod}`, `_saturating{pod}` | Per-pod filter state |
| `orch_replica_waiting{pod}`, `_running{pod}` | Straight from vLLM |
| `orch_sticky_total` | Placements won by prefix affinity |
| `orch_place_total` | Placement attempts |

```
curl -s http://127.0.0.1:8080/metrics | grep orch_request_duration_seconds_sum
```

**Where the time goes.** Divide `_sum` by `_count` per stage for the mean — this
is exactly what the REPL's `profile` prints.

```
curl -s http://127.0.0.1:8080/metrics | grep orch_replica_ | grep -v ' 0$'
```

Only the replicas doing something. Useful when a pool has scaled and you want to
know which pod is hot.

```
curl -sf http://127.0.0.1:50051/metrics
```

Mooncake's metrics — new in 10. Hop and block counts, the numbers behind the
Mooncake KV dashboard.

---

## The REPL

```
python -m gateway.repl
```

Run on: Mac, after `source .env`. Replaces module 9's `app.py`.

| Command | Does |
|---|---|
| `profile` | **Latency by stage** — the 10 command |
| `metrics` | Dump the Prometheus text directly |
| `hop <prompt>` | Force a prefill → decode KV hop through Mooncake |
| `evict` | Drop the last hopped prefix, bumps `orch_kv_evict_total` |
| `board` | Last hops: which pod, via, and the text returned |
| `codes` | Status codes seen so far |
| `status` · `pods` | Per-pod KV, queue, health |
| `saturate` · `healthy` · `slice` · `force` | Inject faults |
| `flood` · `cap` · `split` · `replicas` | Change load and policy |

Steps 13-15 produce **no KV hop** — they are single-pick capability routing.
Only `hop` exercises the two-phase path.

---

## Engine tuning (no GPU)

```
python -m kernels.engineering --q 128 --kv 2048
python -m kernels.engineering --q 1 --kv 2048
```

HBM bytes for one attention call. Second form is decode (`q=1`) — the
`naive/flash` ratio collapses to ~1, showing FlashAttention is a prefill win.

```
python -m router.kv_eviction --policy prefix_protect --need 1024
python -m router.kv_eviction --policy lru --need 1024
```

Who gets evicted to free 1024 tokens. Compare `lru`, `lfu`, `priority`,
`prefix_protect` — the last protects shared prefixes over unique ones.

```
python -m router.warmup --budget 1500 --prefix 256
```

Warmup cost vs first-request latency. The reported `speedup` is **startup time,
not inference speed**.

```
python -m gateway.harness --list
python -m gateway.harness --behavior fleet-soak
python -m app.harness --list
```

Ten fleet scenarios and six guardrail scenarios against simulated workers.
`fleet-soak` passing means `completed=3 shed_503=17` — heavy shedding is correct.

```
KV_BACKEND=nccl python -m gateway.repl
```

Switch the KV hop transport. `nccl` and `nixl` are **no-op stubs**, so hops stop
reaching Mooncake while requests still succeed — zero hops on the dashboard does
not prove the hop did not happen.

---

## Load generation

```
locust -f app/locustfile.py --host http://127.0.0.1:8080
```

Run on: Mac, after `pip install -r requirements-load.txt`. UI on `:8089`.
`--host` must stay `127.0.0.1:8080` — that is the tunnel.

```
locust -f app/locustfile.py --host http://127.0.0.1:8080 \
  --headless -u 4 -r 2 -t 60s
```

Headless, 4 users, spawn rate 2, one minute — the Step 28 settings without the
browser. Good for repeating a run.

```
ORCH_URL=http://127.0.0.1:8080/v1 python -m app.crew_flood --rounds 48 --workers 8
```

Threaded burst rather than steady load. This is what moves the queue-depth and
shed panels; Locust's think time mostly does not.

```
python -m app.harness --list
python -m gateway.harness --list
```

The scenario harnesses, 10's equivalent of module 9's `fakeworker.run --list`.

---

## Open WebUI

```
kubectl logs deploy/open-webui --tail=30
kubectl get svc open-webui -o jsonpath='{.spec.ports[0].nodePort}{"\n"}'
```

Should print `30030`.

```
kubectl get deploy open-webui -o jsonpath='{.spec.template.spec.containers[0].env}' | jq .
```

**Check `OPENAI_API_BASE_URL` is `http://orch-serve:8080/v1`.** If it points at
a vLLM pod instead, browser chat bypasses admission and routing entirely and
none of it lands on your dashboards.

```
curl -s http://127.0.0.1:8080/v1/models | jq .
```

The model list Open WebUI's picker reads. 10 adds `/models` and `/v1/models` to
orch-serve so it looks OpenAI-compatible; an empty list means an empty picker.

---

## Guardrails

Guardrails reject with **400 before admission**, so these never reach the router
and never appear in `orch_shed_total`.

```
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"nope","messages":[{"role":"user","content":"hi"}]}'
```

Expect `400` — `model_not_allowed`.

| Rejection | Trigger |
|---|---|
| `model_not_allowed` | Model not in the allowlist |
| `bad_max_tokens` | Non-integer or <= 0 |
| `prompt_too_long` | Over 8 000 characters |
| `empty_prompt` | Text model, no text |
| `vision_needs_image` | Vision model, no image part |

`max_tokens` is silently defaulted to 512 when absent and clamped to 512 when
higher — a request asking for more is not rejected, just quietly capped.

---

## Symptom → first command

| Symptom | Run this |
|---|---|
| Any `127.0.0.1` page dead | `curl -sf http://127.0.0.1:8080/metrics` — tunnel, not cluster |
| Grafana has no dashboards | Check `grafana_dashboard=1` on the ConfigMap, then the sidecar log |
| Dashboards empty but present | Prometheus `activeTargets` — look for `down` |
| `profile` shows nothing | No traffic yet, or the histogram never received a sample |
| Mooncake hops stay 0 | Expected until REPL `hop`. Steps 13-15 do not hop |
| Sticky / overflow stay 0 | Expected unless those paths fire |
| Browser chat missing from dashboards | `OPENAI_API_BASE_URL` points at vLLM, not orch-serve |
| Model picker empty | `curl http://127.0.0.1:8080/v1/models` |
| Locust all failures | `--host` must be `http://127.0.0.1:8080`, not the Lambda IP |
| Flood of 429 under load | Working as intended — tenant cap is 10 000 tokens/min |
| Requests 400 before routing | Guardrails. See the table above |
