# Prerequisites

Everything you need before `make fleet-runpod`. About twenty minutes, most of it
waiting — for an account to confirm and for a box to boot, which is why those
two things overlap here.

**Steps 1–5 can be done any evening. Step 6 starts billing**, so do it when you
mean to work; the API keys in 7–9 come after it deliberately, so they get sorted
while the box boots rather than before it has started.

**Six of the steps below end with a line to paste into `.env`.** Paste each one
as you get it — step 10 checks you have all of them, but it is a lot easier to
notice a missing key while you are still on the page that issues it.

| | Step | When | Puts into `.env` |
|---|---|---|---|
| 1 | [Your laptop](#1--your-laptop) | any time | — |
| 2 | [The repo and your .env](#2--the-repo-and-your-env) | any time | *the file itself* |
| 3 | [Make an SSH key](#3--make-an-ssh-key) | any time | `SSH_KEY` |
| 4 | [A RunPod account, and credits](#4--a-runpod-account-and-credits) | **early — it can need approval** | — |
| 5 | [Add the key to RunPod](#5--add-the-key-to-runpod) | any time | — |
| 6 | **[Launch the GPU box](#6--launch-the-gpu-box)** | **when you mean to use it** | `BOX`, `SSH_PORT` |
| 7 | [HuggingFace](#7--huggingface) | while it boots | `HF_TOKEN` |
| 8 | [Deepgram](#8--deepgram) | while it boots | `DEEPGRAM_API_KEY` |
| 9 | [Firecrawl](#9--firecrawl) | while it boots | `FIRECRAWL_API_KEY` |
| 10 | [Check yourself](#10--check-yourself) | last | — |

Optional, and safe to skip: [Superlinked](#optional--superlinked) and
[Opik](#optional--opik).

---

## 1 · Your laptop

| Needed | Check |
|---|---|
| Python 3.11+ | `python3 --version` |
| Node 20+ | `node --version` |
| `git`, `ssh`, `rsync` | already there on macOS and Linux |

No Docker and no Kubernetes tooling locally. Everything that needs a cluster
runs on the box, driven over ssh.

## 2 · The repo and your `.env`

```bash
git clone https://github.com/harshuljain13/Dodona.git && cd Dodona
cp .env.example .env
```

**`.env` does not exist until you copy it, and nothing works without it.**
`.env.example` is the template, committed and safe; `.env` is yours, gitignored,
and never leaves the laptop. **Open it now in an editor and keep it open.**

Steps 3 to 9 each give you one line that lives in this file. **They are not
commands to run** — `.env` already contains every line with an empty or
placeholder value, so the job is finding that line and filling in the right-hand
side. Edit as you go: leaving it to the end means transcribing six secrets from
a scratchpad, which is how one of them ends up wrong.

It arrives with sensible values for everything that is not a secret.

## 3 · Make an SSH key

**A dedicated key, not your existing one.** `ssh-keygen` overwrites without
much ceremony, and `~/.ssh/id_ed25519` is the default name on most laptops — so
generating "a key for this" is how people silently destroy the one GitHub, a
work bastion or another provider was trusting. `-f` avoids the whole question:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/dodona_ed25519 -C "dodona"
cat ~/.ssh/dodona_ed25519.pub        # the PUBLIC half — the one you paste
```

Press Enter at the passphrase prompts, or set one; either works.

Two halves, and the difference matters. The `.pub` file is public and goes to
RunPod in step 5. The file *without* `.pub` is private, never leaves this
laptop, and is what `.env` points at — already the default in `.env.example`:

```bash
export SSH_KEY=$HOME/.ssh/dodona_ed25519
```

Prefer to reuse a key you already have? Point `SSH_KEY` at it and skip the
`ssh-keygen` line. Just do not run `ssh-keygen` without `-f`.

## 4 · A RunPod account, and credits

**Do this early.** It is the only step that can block on somebody else:
verification, a payment method, or a credit grant being applied can take hours,
and none of it can be hurried on the day.

1. Create an account at [runpod.io](https://runpod.io) and verify the email.
2. **Claim your credits.** Billing → redeem whatever code you were given, and
   confirm the balance actually changed before you close the tab. A code that
   silently failed looks identical to one that worked until the moment you try
   to launch.
3. Add a payment method if you have no credits. A GPU cannot be launched on a
   zero balance, and discovering that at the start of a session costs the
   session.

Budget, so the number is not a surprise: one A100-40GB is roughly **$1–2 per
hour**, and nothing here needs more than a few hours. You are charged from the
moment the box starts — see step 6 — and charging stops when you **destroy** it,
not when you stop using it.

Stay in the console once you are done here: step 5 is the next thing in it, and
you already have the key it wants.

Using Lambda, EC2 or your own hardware instead? Skip to step 5; everything that
follows is three values in `.env` and none of it is RunPod-specific.

## 5 · Add the key to RunPod

**Do this before you launch anything.** RunPod injects your public keys into a
box *at start*, so a box that booted without one cannot be given one afterwards
— there is no way in to add it. The only fix is to destroy it and launch again,
which is the single most common ten minutes lost on setup.

1. Copy the output of `cat ~/.ssh/dodona_ed25519.pub` — the whole line,
   starting `ssh-ed25519` and ending with the comment.
2. RunPod → **Account Settings → SSH Public Keys** → paste it → save.

It is an account-level setting, not a per-pod one: every box you launch from now
on gets it, including the one you relaunch after a mistake.

On Lambda, EC2 or your own metal the equivalent step is the provider's
"SSH keys" screen, or appending the same line to `~/.ssh/authorized_keys` on the
box.

## 6 · Launch the GPU box

> **This is the step that costs money.** A rented GPU bills from the moment it
> starts, not from the moment you use it. Launch when you are about to work —
> at the start of a session, not the night before — and **destroy** it when you
> are done. Steps 7–9 come after this one so the keys get sorted while the box
> boots.

**Target: 2 × A100-80GB.** Two cards, because the point of the fleet is that
the router *chooses* between replicas — with one card `place()` degenerates to
routing and half the dashboards describe a single process. Two is the floor;
more works and costs more.

The capacity arithmetic assumes sm80 — no fp8 tensor cores, which is *why* the
model is int4. An H100 will run it, but `nvidia-smi` reporting compute
capability `9.0` means
[`docs/project/01-capacity.md`](docs/project/01-capacity.md) needs redoing
before its numbers mean anything.

> **A standard RunPod Pod is fine.** Nothing on this branch needs systemd, root
> over the kernel, or a host you own: the fleet is plain processes — one vLLM
> per card with the gateway in front — supervised by a shell script.
>
> That was not always true. The Kubernetes path installs k3s as a systemd
> service and slices the device with HAMi, neither of which works inside
> someone else's container, so it had to refuse a Pod before syncing anything.
> It lives on the
> [`lambda-cluster`](https://github.com/harshuljain13/Dodona/tree/lambda-cluster)
> branch now, and this one does not care what PID 1 is.

**On RunPod specifically:**

1. Check step 5 is done. Launching without it costs a relaunch.
2. Launch with **≥ 100 GB** of disk. Weights are tens of gigabytes, and a full
   disk fails deep inside a download.
3. Open **Connect** and copy the **Direct TCP** line:

   ```
   ssh root@194.26.196.4 -p 22122 -i ~/.ssh/dodona_ed25519
   ```

   It gives you both remaining values. In `.env`:

   ```bash
   export BOX=root@194.26.196.4    # the host half
   export SSH_PORT=22122           # the port half  (22 on Lambda, EC2, bare metal)
   ```

> **Use Direct TCP, not `ssh.runpod.io`.** The proxy forwards no ports, and the
> tunnel is four of them — gateway, Grafana, Prometheus, the ledger. The proxy
> connects, and then nothing is reachable at `127.0.0.1`.

The box is those three values — `BOX`, `SSH_KEY`, `SSH_PORT` — and **nothing
else in the repo is provider-specific**. `LAMBDA` and `LAMBDA_SSH_KEY` are the
older names and still work.

## 7 · HuggingFace

*The box is booting. Do this and the next two while it does.*

For pulling `Qwen2.5-7B-Instruct-AWQ` onto the box.

1. Create an account at [huggingface.co](https://huggingface.co).
2. Settings → Access Tokens → **New token**, type *read*.

In `.env`:

```bash
export HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxx
```

## 8 · Deepgram

The agent's ears and mouth — `nova-3` for speech in, `aura-2` for speech out.
One account covers both, and the free credit is far more than a demo uses.

1. Sign up at [deepgram.com](https://deepgram.com).
2. Create an API key from the dashboard.

In `.env`:

```bash
export DEEPGRAM_API_KEY=xxxxxxxxxxxxxxxx
```

Without it `make app` refuses to start, and says so. Typing to the agent still
works; only the microphone needs this.

## 9 · Firecrawl

What the agent reads. Dodona is a *research* agent: it searches, opens the
sources and cites them. **One key covers both tools** — Firecrawl backs
`web_search` and `scrape_url` alike, so this is the only search account needed.

Sign up at [firecrawl.dev](https://firecrawl.dev) and copy the key.

In `.env`:

```bash
export FIRECRAWL_API_KEY=fc-xxxxxxxx
```

**This is the one people skip, and it fails invisibly.** With no key both tools
fail, the agent answers from memory, and it sounds exactly as confident as when
it has read a source. It looks like a model that hallucinates. It is a model
that has been blindfolded.

## 10 · Check yourself

### First — `.env`, complete

Every step above gave you one line of it. Here is the whole file's worth in one
place, so you can see at a glance whether anything is still blank:

```bash
# the box — step 6 (and SSH_KEY from step 3)
export BOX=root@194.26.196.4
export SSH_KEY=$HOME/.ssh/dodona_ed25519
export SSH_PORT=22122

# the keys — steps 7, 8, 9
export HF_TOKEN=hf_xxxx
export DEEPGRAM_API_KEY=xxxx
export FIRECRAWL_API_KEY=fc-xxxx
```

Six values. Everything else in `.env` already holds a working default.

Then let the repo check it for you:

```bash
make doctor
```

It names every value, says what its absence breaks, and **prints no secrets** —
so it is safe to run on a projector or paste into a message when you are stuck.
It also catches the failure a glance does not: a value still left as its
placeholder, which reads as filled in and fails later at the point of use, with
an error that never mentions `.env`.

### Then — ssh in, and check the laptop

Now prove the box answers. **This is the first time anything here leaves your
machine**, and it is the step that catches a key RunPod never installed:

`.env` is a file, not your shell — editing it sets nothing in the terminal you
are typing in. The `make` targets read it themselves, but a bare `ssh` does
not, so load it first:

```bash
source .env                      # $BOX, $SSH_PORT and $SSH_KEY in this shell
ssh -i $SSH_KEY $BOX -p $SSH_PORT nvidia-smi
```

**`-i $SSH_KEY` is not optional.** `ssh` does not read `.env` and will not
guess: without it, it tries its own default keys, fails, and falls back to
asking for a password — which looks exactly like the key never being installed,
and sends you to fix a problem you do not have.

(`make doctor` prints this whole line with your values already in it, if you
would rather copy it than source anything.)

**Gate: two A100s listed.** That is the box proved end to end — the address,
the port and the key all correct — before anything is synced to it.

Being asked for a **password even with `-i`** is the different failure, and it
means the key really is not on the box. RunPod injects keys at launch, so
adding one to your account afterwards does nothing for a pod already running.
Fix it from the pod's web terminal:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo '<paste the contents of your key's .pub half>' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Then add the key to Account Settings as well (step 5), or the next box you
launch will do the same thing.

### If you relaunch the pod, `.env` goes stale

A new pod gets a **new address and a new port**. Both are in that same Direct
TCP line, and the old port does not politely time out — it refuses, which reads
as the box being down rather than as the wrong port:

```
ssh: connect to host 154.54.102.40 port 19494: Connection refused
```

So after any relaunch, update `BOX` and `SSH_PORT` before anything else. The
key survives, as long as it was on your account before the new pod started.

And the laptop, which needs no GPU and no box at all:

```bash
make test
```

| Check | Means |
|---|---|
| `make doctor` prints `.env is complete` | Every key is set, and none is a placeholder |
| `make test` prints all `ok` | Laptop is ready |
| `nvidia-smi` prints a GPU | Box is ready |

`make test` compiles every module, checks every doc link, runs the suite, then
exercises ten gateway behaviours and six guardrails. It never touches the
cluster, so it works on a plane.

**You are done.** Bring-up is one of two commands, and your box decides which:

```bash
cat /proc/1/comm          # on the box, via the ssh line above
```

| It says | Run | Why |
|---|---|---|
| anything | `make fleet-runpod` | processes — no orchestrator, no systemd needed |

There is nothing to guess any more: this branch runs on a container or a host alike. Guessing wrong
costs seconds rather than a part-finished install.

Teardown is `make clean` and then **destroying the box** — the second half is
the one that stops the bill.

---

## Optional · Superlinked

The overflow path: a capacity refusal (`503`/`529`) *may* leave for an external
provider. Get a key at [superlinked.com](https://superlinked.com) to watch that
fire; leave `OVERFLOW_API_KEY` blank and overflow simply never leaves, which is
also what a cluster that is keeping up does.

## Optional · Opik

AgentShip builds a span tree for every turn whether or not anything collects it.
With no exporter configured the spans ship **nowhere** and the run never dials
out, so leaving this blank is a working configuration rather than a degraded
one.

Setting `AGENTSHIP_OTEL_EXPORTERS=opik` with a [Comet](https://comet.com)
workspace shows the agent's span tree — tool calls, model latency, where a
30-second research turn actually went — beside Grafana's view of the same turn
on the GPU. It sends spans off the machine, which is the one exception to the
rule the rest of this project keeps, so it is opt-in twice: once by naming the
exporter, again with `AGENTSHIP_OTEL_ALLOW_SAAS=1`.

---

## When it goes wrong

| Symptom | What it actually is | Fix |
|---|---|---|
| Cannot launch a pod at all | Zero balance, or a credit code that did not apply | Step 3 — check the balance, not the code |
| Any command: "No `.env`" | The file is a copy you make, not one that ships | Step 2 |
| `No API credentials for model 'openai/Qwen...'` | LiteLLM wants a key for any `openai/*` model, even our own gateway. `make app` sets a fake one for you | Only seen running the agent outside `make app` |
| `Permission denied (publickey)` | Key added to RunPod *after* the box launched | Step 5, then destroy and relaunch — keys inject at start |
| `Connection refused` | Using port 22 on RunPod | Use the port from Direct TCP |
| The tunnel connects but `127.0.0.1` is dead | Using the `ssh.runpod.io` proxy | Use Direct TCP |
| `make fleet-runpod-ssh` says "Address already in use" | A tunnel is already open, or something local holds :8080 | Close the other terminal, or `lsof -nP -iTCP:8080` |
| `make fleet-runpod` says it worked but nothing changed | Fixed — it stops a running fleet first now. If you see this on an old checkout, `make fleet-runpod-down` first | `make fleet-runpod-status` |
| Grafana loads but every panel is empty | The tunnel is closed, or Prometheus is scraping ports the fleet is not on | `make fleet-runpod-smoke-test` names which |
| The agent says it will look something up, then answers from memory | vLLM was started without the tool-call parser, so the call was dropped with no error | `make fleet-runpod` to relaunch the workers |
| `make app` will not start | No `DEEPGRAM_API_KEY` | Step 8 |
| The agent answers but cites nothing | `web_search` has no key | Step 9 |

Every command individually, and what each failure means:
[**COMMANDS.md**](docs/COMMANDS.md) · [**RUNBOOK.md**](docs/RUNBOOK.md)
