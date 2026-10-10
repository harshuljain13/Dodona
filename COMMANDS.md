# Commands — every step, in order

Copy-pasteable. **[`RUNBOOK.md`](RUNBOOK.md) explains *why* each stage exists and
what its gate means; this file is just the commands.** When something fails, go
back to the runbook — it has a failure-mode table per stage.

Every block is marked **`[Mac]`** or **`[GPU]`**. Getting that wrong is the most
common way to waste ten minutes.

> **Status: untested.** Nothing in this file has been run end to end yet. Steps
> marked ⚠ are the ones most likely to need adjusting on first contact.

---

## Step 0 — `.env` `[Mac]`

```bash
cd ~/work/projects/experiments_and_courses/llm-inference/dodona
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
export LAMBDA=ubuntu@YOUR_LAMBDA_IP
export LAMBDA_SSH_KEY=$HOME/.ssh/YOUR_LAMBDA_KEY
```

```bash
chmod 600 "$LAMBDA_SSH_KEY"          # ssh refuses a world-readable key
grep -c . .env                       # sanity: non-empty
```

`.env` is gitignored and must never be committed.

## Step 1 — ship the code to the box `[Mac]`

```bash
bash cluster/setup/sync_to_lambda.sh
```

Lands the whole project at `~/dodona` on the box. Excludes `.venv`,
`__pycache__`, `.git`, `metrics/*` and **`.env`** — so the box has the code but
not your secrets.

## Step 2 — open the tunnel, and leave it open `[Mac]`

```bash
bash cluster/setup/ssh.sh
```

Gives you a shell on the box in `~/dodona`, with these ports forwarded back:

| Port | What | Live after |
|---|---|---|
| 8000 | vLLM prefill worker | step 4 |
| 8001, 8002 | vLLM decode workers | stage 2 |
| 3000 | Open WebUI (docker) | step 7 |
| 8080 | the gateway | stage 4 |
| 31495 | Grafana | stage 3 |
| 50051 | Mooncake | stage 8 |
| 30030, 30080 | k8s NodePorts | stage 2 |

**Lambda's firewall allows only SSH.** Everything is reached at `127.0.0.1:<port>`
on the Mac — never the public IP. Open a second terminal and run `ssh.sh` again if
you want a shell plus a free prompt.

---

# Stage 0 — one model on the bare box

No Kubernetes, no slices. One container with the whole card. **This stage exists
to verify the arithmetic before any orchestration can confuse it.**

## Step 3 — what is the card? `[GPU]`

```bash
nvidia-smi
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv
```

Expect `A100`, `40960 MiB`, compute capability `8.0`.

**`8.0` is sm80, which has no fp8 tensor cores.** That is why the model is int4
and not fp8 — see [`docs/project/01-capacity.md`](docs/project/01-capacity.md).
If this says `8.9` or `9.0`, stop: the whole capacity chain should be redone,
because fp8 becomes available and int4 stops being forced.

```bash
docker --version && docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

That last one proves the NVIDIA container runtime works. If it fails, nothing
below will run.

## Step 4 — serve the model `[GPU]`

```bash
docker pull vllm/vllm-openai:latest

docker run -d --name vllm-stage0 --gpus all --ipc=host \
  -p 8000:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --max-model-len 16384 \
  --max-num-seqs 64 \
  --gpu-memory-utilization 0.90
```

**Four flags, and every one of them is in the capacity arithmetic.** Nothing else
belongs at this stage.

| | Why it is here |
|---|---|
| `--max-model-len 16384` | The context the capacity figures assume. Change it and the decode pool moves — at 8,192 it is 22, not 11 |
| `--max-num-seqs 64` | Deliberately **above** the KV limit of 5.5, so the cache runs out before the scheduler does. Set it lower and a KV-pressure policy could never fire |
| `--gpu-memory-utilization 0.90` | The 0.90 in `12 × 0.90 = 10.80 GiB`. At stage 0 it applies to the whole card; from stage 2 it applies to the slice |
| `--ipc=host` | Not optional. vLLM uses shared memory and Docker's default 64 MB limit produces crashes that look like anything but a shm limit |

The HF cache mount means a restart does not re-download the ~5.6 GB of weights.

### Flags deliberately *not* here yet

| Flag | When it arrives | Why not now |
|---|---|---|
| `--quantization awq_marlin` | never, probably | vLLM reads `quantization_config` out of `config.json` and picks a kernel itself. Forcing `awq_marlin` only narrows what it may choose, and if Marlin is unavailable you get a hard error instead of a working fallback. **Better to let it choose and read back which kernel it took** — see 5d |
| `--enable-prefix-caching` | stage 10, if not already default | On by default in current vLLM. It matters for prediction P4 (>70% hit rate on the ~1k shared prefix), but at stage 0 there is no shared prefix to hit |
| `--enable-auto-tool-choice`<br/>`--tool-call-parser hermes` | **stage 9**, with the agent | See below |

### Why `hermes`, and why it can wait

By default vLLM hands back whatever the model generated as **text** in
`choices[0].message.content`. When Qwen2.5 decides to call a tool it emits
something like `<tool_call>{"name": "web_search", ...}</tool_call>` — and without
a parser that arrives as a *string*, not as the structured `tool_calls` array the
OpenAI API contract defines.

`--enable-auto-tool-choice` turns parsing on; `--tool-call-parser hermes` names
the format to parse. `hermes` is the Hermes-style `<tool_call>…</tool_call>`
convention, which is what the Qwen2.5-Instruct family emits — so for this model it
is the right parser, not an arbitrary pick.

**It matters because of what reads the response.** LangGraph binds tools through
the OpenAI `tools` parameter and looks for `tool_calls` coming back. No parser →
no `tool_calls` → AgentShip concludes the model answered in prose → the agent loop
ends after one turn. That failure looks like a weak model and is nothing of the
kind. It is the same bug in its other half as the dropped-`tools` problem in
`control/router/pools.py`.

But **stage 0 has no tools and no agent.** Open WebUI does not use them either.
So the flags buy nothing here and only widen the surface that can fail while we
are trying to establish something much simpler: does the model serve, and is the
arithmetic right.

Watch it come up — **this takes minutes, not seconds:**

```bash
docker logs -f vllm-stage0
```

Wait for `Application startup complete`. ⚠ **`READY` is not the same as "can
serve"** — CUDA-graph capture continues after startup, so the first request is
slow. That is the warmth problem, and it is why stage 8 re-quotes TTFT.

## Step 5 — the gate: four checks `[GPU]`

### 5a — it answers

```bash
curl -s localhost:8000/v1/models | python3 -m json.tool

curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
  "messages": [{"role":"user","content":"Name one interaction between St John'\''s wort and conventional medication. One sentence."}],
  "max_tokens": 80
}' | python3 -m json.tool
```

### 5b — metrics exist

```bash
curl -s localhost:8000/metrics | grep -E 'gpu_cache_usage_perc|num_requests_(running|waiting)|prefix_cache'
```

`vllm:gpu_cache_usage_perc` must be present. **Everything from stage 3 onward
reads this**, and admission is a function of it.

### 5c — `kv_bytes_per_token` ⚠ **the one that matters most**

```bash
python3 - <<'PY'
import json, glob, os
p = glob.glob(os.path.expanduser(
    "~/.cache/huggingface/hub/models--Qwen--Qwen2.5-7B-Instruct-AWQ/snapshots/*/config.json"))[0]
c = json.load(open(p))
L  = c["num_hidden_layers"]
KV = c["num_key_value_heads"]
HD = c.get("head_dim") or c["hidden_size"] // c["num_attention_heads"]
kv = 2 * L * KV * HD * 2
print(f"layers={L}  kv_heads={KV}  head_dim={HD}")
print(f"kv_bytes_per_token = {kv:,} B = {kv/1024:.0f} KiB")
print(f"16k sequence       = {kv*16384/2**30:.3f} GiB")
print()
print("PREDICTED: 28 / 4 / 128  ->  56 KiB  ->  0.875 GiB")
print("P6 HELD" if abs(kv/1024 - 56) < 2.8 else "P6 FAILED — redo every concurrency figure")
PY
```

This is prediction **P6**. If it fails, stop and fix the arithmetic in
`docs/project/01-capacity.md` before renting more time — every number in the
project moves with it.

### 5d — weights on the card

```bash
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

Predicted **5.20 GiB** of weights. The process will show more than that, because
`--gpu-memory-utilization 0.90` makes vLLM pre-allocate its KV pool — so expect
roughly `0.90 × 40 GiB ≈ 36 GiB` reserved. To see the weights/KV split:

```bash
docker logs vllm-stage0 2>&1 | grep -iE 'KV cache|model weights|graph capturing|awq|marlin|quantiz'
```

vLLM prints `# GPU blocks:` and the memory it attributed to weights. **Record
both numbers** — they go in the as-deployed table, and 5d is what tells you
whether 0.80 GiB of runtime headroom was a good guess.

### 5e — time a real prefill (optional, but do it while the card is idle)

```bash
python3 - <<'PY'
import json, time, urllib.request
prompt = "herbal interaction evidence " * 1700          # ~12k tokens
body = json.dumps({"model":"Qwen/Qwen2.5-7B-Instruct-AWQ",
                   "messages":[{"role":"user","content":prompt}],
                   "max_tokens":1}).encode()
t = time.time()
urllib.request.urlopen(urllib.request.Request(
    "http://localhost:8000/v1/chat/completions", body,
    {"Content-Type":"application/json"}), timeout=300).read()
print(f"~12k prefill, 1 output token: {time.time()-t:.2f} s")
PY
```

This is the left-hand side of Part 6's hop-vs-recompute comparison, measured on
an uncontended card. Predicted 2.5–3.7 s on a 28% slice, so expect **faster**
here with the whole card.

---

# Stage 1 — Open WebUI, for hand testing

## Step 6 — run it `[GPU]`

```bash
docker run -d --name open-webui -p 3000:8080 \
  -e OPENAI_API_BASE_URL=http://host.docker.internal:8000/v1 \
  -e OPENAI_API_KEY=dummy \
  -e WEBUI_AUTH=False \
  --add-host=host.docker.internal:host-gateway \
  -v open-webui:/app/backend/data \
  ghcr.io/open-webui/open-webui:main
```

⚠ `host.docker.internal` needs the `--add-host` line on Linux. If the model list
comes up empty, that is the first thing to check:

```bash
docker exec open-webui curl -s http://host.docker.internal:8000/v1/models | head -c 200
```

## Step 7 — talk to it `[Mac]`

Open **http://127.0.0.1:3000** — through the tunnel from step 2, not the public IP.

**Gate: you have a conversation with it in a browser.** That is stage 1 done, and
it means the engine is good *by hand* before any control plane exists. Every later
failure then has a much smaller search space.

---

## Teardown `[GPU]`

```bash
docker stop vllm-stage0 open-webui && docker rm vllm-stage0 open-webui
```

The HF cache survives in `~/.cache/huggingface`, so a restart does not
re-download.

**Stop the Lambda instance when you are done.** It bills by the hour whether or
not anything is running.

---

# Stages 2–10

Not written as commands yet, because each depends on what the stage before it
measured. [`RUNBOOK.md`](RUNBOOK.md) has the shape of each:

| Stage | | Needs from the stage before |
|---|---|---|
| 2 | k3s + HAMi, three 12 GiB slices | the real weights figure from 5d, to size the slices |
| 3 | Prometheus + Grafana | three pods to scrape |
| 4 | gateway, pass-through | a scrape, so admission has state to read |
| 5 | guardrails + tenant window | traffic flowing through the gateway |
| 6 | admission as a socket | the live `kv_cache_usage_perc` from stage 3 |
| 7 | placement | two decode pods to choose between |
| 8 | KV hop + warm gate | Mooncake up, and `KV_BACKEND` confirmed |
| 9 | the voice agent | the tool allowlist verified at stage 4 |
| 10 | measure | all of the above, unchanged across runs |

This file gets a section per stage as each one is actually brought up and the
commands are known to work — rather than guessed in advance and quietly wrong.

---

## See also

| | |
|---|---|
| [`RUNBOOK.md`](RUNBOOK.md) | The ten stages, their gates, and a failure-mode table per stage |
| [`docs/cluster/COMMANDS.md`](docs/cluster/COMMANDS.md) | Debugging reference inherited from the class-10 lab — port chains, pod inspection, Grafana queries. Written for that layout, so paths differ |
| [`docs/project/01-capacity.md`](docs/project/01-capacity.md) | Where every predicted number here comes from |
