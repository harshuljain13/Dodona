# Commands — every step, in order

Copy-pasteable. **[`RUNBOOK.md`](RUNBOOK.md) explains *why* each step exists and
what its gate means; this file is just the commands.** When something fails, go
back to the runbook — it has a failure-mode table per step.

Every block is marked **`[Mac]`** or **`[GPU]`**. Getting that wrong is the most
common way to waste ten minutes.

> **Status: untested.** Nothing in this file has been run end to end yet. Steps
> marked ⚠ are the ones most likely to need adjusting on first contact.

---

## The short version

```bash
cp .env.example .env     # set BOX and SSH_KEY
make cluster             # then: make observability && make ssh
```

There are four targets and no more: `cluster`, `observability`, `ssh`, `test`.
`make cluster` runs the steps below over ssh — it is not a second
implementation, it calls the same scripts in `infra/cluster/setup/`. **If a step is
wrong, fix the script, not `infra/cluster/setup/up.sh`.**

It needs **no venv and no vLLM on the box**: the pods bring their own vLLM in
the container image, and `gate.py` is stdlib-only. The whole-card engine below
is a separate, optional measurement — `bash infra/cluster/setup/up.sh engine` — kept
because comparing it against a slice is what shows that CUDA-graph overhead
does not extrapolate. `make cluster` does not need it.

Everything after this point is that sequence broken out, which is what you want
when something fails and you need to run one piece at a time.

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
bash infra/cluster/setup/sync.sh
```

Lands the whole project at `~/dodona` on the box. Excludes `.venv`,
`__pycache__`, `.git`, `metrics/*` and **`.env`** — so the box has the code but
not your secrets.

## Step 2 — open the tunnel, and leave it open `[Mac]`

```bash
bash infra/cluster/setup/tunnel.sh
```

Gives you a shell on the box in `~/dodona`, with these ports forwarded back:

| Port | What | Live after |
|---|---|---|
| 8000 | vLLM, whole card | `make cluster`, before the fleet |
| 3000 | Open WebUI (docker) | `make cluster` |
| 8080 | the gateway | `make cluster` |
| 30800, 30801 | vLLM prefill / decode NodePorts | `make cluster` |
| 30030, 30080 | Open WebUI / gateway NodePorts | `make cluster` |
| 31495 | Grafana | `make observability` |

**Lambda's firewall allows only SSH.** Everything is reached at `127.0.0.1:<port>`
on the Mac — never the public IP. Open a second terminal and run `tunnel.sh` again if
you want a shell plus a free prompt.

---

# One model on the bare box

No Kubernetes, no slices. One process with the whole card. **This exists to
verify the arithmetic before any orchestration can confuse it.**

## Step 3 — what is the card? `[GPU]`

```bash
nvidia-smi
nvidia-smi --query-gpu=name,memory.total,compute_cap --format=csv
```

Expect `A100`, `40960 MiB`, compute capability `8.0`.

**`8.0` is sm80, which has no fp8 tensor cores.** That is why the model is int4
and not fp8 — see [`docs/project/01-capacity.md`](project/01-capacity.md).
If this says `8.9` or `9.0`, stop: the whole capacity chain should be redone,
because fp8 becomes available and int4 stops being forced.

```bash
docker --version && docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

That last one proves the NVIDIA container runtime works. If it fails, nothing
below will run.

## Step 4 — serve the model `[GPU]`

Two ways. **Use the scripts** — they exist, they read `.env`, and they encode the
flags that are in the capacity arithmetic so you cannot forget one.

### 4a — the scripted path

```bash
cd ~/dodona
bash infra/cluster/setup/box_packages.sh      # venv, requirements, vllm, verify
bash infra/cluster/setup/engine.sh       # serve, on :8000
```

`box_packages.sh` builds `.venv`, installs `requirements.txt` and
`requirements-load.txt` from the project root, installs `vllm>=0.28`, then
verifies that torch and vLLM import and can see the device. **If that last check
fails, stop** — a CUDA/torch mismatch surfaces here as a missing `libcudart.so.*`
at import time, and it is far cheaper to find now than once the fleet is up.

`engine.sh` serves with these defaults, each overridable from the
environment:

| | Default | Why |
|---|---|---|
| `LOCAL_MODEL` | `Qwen/Qwen2.5-7B-Instruct-AWQ` | int4 is forced on sm80 — no fp8 tensor cores |
| `MAX_MODEL_LEN` | `16384` | The context the capacity figures assume. At 8,192 the decode pool is 22, not 11 |
| `MAX_NUM_SEQS` | `64` | Deliberately **above** the KV limit of 5.5, so the cache runs out before the scheduler does |
| `UTIL` | `0.90` | The 0.90 in `12 × 0.90 = 10.80 GiB`. 0.90 of whatever the process can see: the card here, the slice once the fleet is up |
| `SCHEDULING_POLICY` | `fcfs` | The lab used `priority`, which earns its place once the gateway sets one per request. Nothing does yet |

It also reads `compute_cap` and warns if the card is not sm80 — because on sm89
or sm90 fp8 becomes available, int4 stops being forced, and
[`docs/project/01-capacity.md`](project/01-capacity.md) step 4 needs redoing.

### 4b — the container path, if the venv fights you

Pinned CUDA and torch, so it sidesteps the mismatch entirely. The cost is that
the control plane later runs outside the container and has to reach in.

```bash
docker run -d --name vllm-engine --gpus all --ipc=host -p 8000:8000 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai:latest \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --max-model-len 16384 --max-num-seqs 64 --gpu-memory-utilization 0.90 \
  --enable-prefix-caching
```

`--ipc=host` is not optional: vLLM uses shared memory and Docker's default 64 MB
limit produces crashes that look like anything but a shm limit. The HF cache
mount means a restart does not re-download the ~5.6 GB of weights.

### Either way — wait for it

```bash
# scripted: it is in the foreground, watch the log
# container:
docker logs -f vllm-engine
```

Wait for `Application startup complete`. ⚠ **`READY` is not the same as "can
serve"** — CUDA-graph capture continues past startup, so the first request is
slow. That is the warmth problem, and it is why the hop step re-quotes TTFT.

### Flags deliberately *not* set yet

| Flag | When | Why not now |
|---|---|---|
| `--quantization awq_marlin` | never, probably | vLLM reads `quantization_config` from `config.json` and picks a kernel itself. Forcing it only narrows the choice, and turns an unavailable kernel into a hard error instead of a working fallback. The budget check reads back which it took |
| `--enable-auto-tool-choice`<br/>`--tool-call-parser hermes` | with the agent | See below |
| `--kv-cache-dtype fp8` | never | sm80 cannot, and it would halve bytes/token — how fast the cache fills is the thing being studied |

### Why `hermes`, and why it can wait

By default vLLM hands back whatever the model generated as **text** in
`choices[0].message.content`. When Qwen2.5 calls a tool it emits
`<tool_call>{"name": "web_search", ...}</tool_call>` — and without a parser that
arrives as a *string*, not the structured `tool_calls` array the OpenAI contract
defines.

`--enable-auto-tool-choice` turns parsing on; `--tool-call-parser hermes` names
the format. `hermes` is the `<tool_call>…</tool_call>` convention the
Qwen2.5-Instruct family emits, so for this model it is the right parser, not an
arbitrary pick.

**It matters because of what reads the response.** LangGraph binds tools through
the OpenAI `tools` parameter and looks for `tool_calls` coming back. No parser →
no `tool_calls` → AgentShip concludes the model answered in prose → the loop ends
after one turn. That looks like a weak model and is nothing of the kind. Same bug
in its other half as the dropped-`tools` problem in `infra/control/router/pools.py`.

But **this step has no tools and no agent**, and Open WebUI does not use them. The
flags buy nothing here and widen the surface that can fail while we establish
something much simpler: does the model serve, and is the arithmetic right.

## The capacity checks `[GPU]`

### It answers

```bash
curl -s localhost:8000/v1/models | python3 -m json.tool

curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen2.5-7B-Instruct-AWQ",
  "messages": [{"role":"user","content":"Name one interaction between St John'\''s wort and conventional medication. One sentence."}],
  "max_tokens": 80
}' | python3 -m json.tool
```

### The metrics exist

```bash
curl -s localhost:8000/metrics | grep -E 'kv_cache_usage_perc|num_requests_(running|waiting)|prefix_cache'
```

`vllm:kv_cache_usage_perc` must be present. **Everything from observability onward
reads this**, and admission is a function of it.

### `kv_bytes_per_token` ⚠ **the one that matters most**

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

### What the engine actually reserved

```bash
nvidia-smi --query-compute-apps=pid,used_memory --format=csv
```

Predicted **5.20 GiB** of weights. The process will show more than that, because
`--gpu-memory-utilization 0.90` makes vLLM pre-allocate its KV pool — so expect
roughly `0.90 × 40 GiB ≈ 36 GiB` reserved. To see the weights/KV split:

```bash
docker logs vllm-engine 2>&1 | grep -iE 'KV cache|model weights|graph capturing|awq|marlin|quantiz'
```

vLLM prints `# GPU blocks:` and the memory it attributed to weights. **Record
both numbers** — they go in the as-deployed table, and the budget check tells you
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

# Open WebUI, for hand testing

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

Open **http://127.0.0.1:30030** — through the tunnel from step 2, not the public IP.

**Gate: you have a conversation with it in a browser.** That is this step done, and
it means the engine is good *by hand* before any control plane exists. Every later
failure then has a much smaller search space.

---

## Teardown `[GPU]`

```bash
docker stop vllm-engine open-webui && docker rm vllm-engine open-webui
```

The HF cache survives in `~/.cache/huggingface`, so a restart does not
re-download.

**Stop the Lambda instance when you are done.** It bills by the hour whether or
not anything is running.

---

# Stages 2–10

Not written as commands yet, because each depends on what the step before it
measured. [`RUNBOOK.md`](RUNBOOK.md) has the shape of each:

| | Needs from the step before |
|---|---|
| k3s + HAMi, three slices | the measured overhead, to size the slices |
| Prometheus + Grafana | three pods to scrape |
| gateway, pass-through | a scrape, so admission has state to read |
| guardrails + tenant window | traffic flowing through the gateway |
| admission as a socket | the live `kv_cache_usage_perc` |
| placement | two decode pods to choose between |
| KV hop + warm gate | a KV connector, and `KV_BACKEND` confirmed |
| the voice agent | the tool allowlist verified at the gateway |
| 10 | measure | all of the above, unchanged across runs |

This file gets a section per step as each one is actually brought up and the
commands are known to work — rather than guessed in advance and quietly wrong.

---

## See also

| | |
|---|---|
| [`RUNBOOK.md`](RUNBOOK.md) | The bring-up in order, the gate at each step, and a failure-mode table |
| [`docs/cluster/COMMANDS.md`](cluster/COMMANDS.md) | Debugging reference inherited from the course lab — port chains, pod inspection, Grafana queries. Written for that layout, so paths differ |
| [`docs/project/01-capacity.md`](project/01-capacity.md) | Where every predicted number here comes from |
