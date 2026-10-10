---
name: dodona-docs
description: >-
  Keep Dodona's documents from disagreeing with each other — which file is authoritative
  for what, the predicted/measured convention, and how to propagate a changed number. Use
  when editing README, PROJECT, SUBMISSION or docs/, when a measured value contradicts a
  written one, or when asked what is done.
---

# Keeping the documents honest

## Which file owns what

| File | Owns | Never contains |
|---|---|---|
| `README.md` | What it is, the problem, the config, quick start, status. **Crisp** | Depth. It is a front door |
| `PROJECT.md` | The problem in full, the decisions, the C4 levels, alternatives rejected, risks, scope | The brief's part structure |
| `SUBMISSION.md` | **The brief answered, parts 0–8**, a decision tree per part | Project motivation — that is `PROJECT.md` |
| `docs/project/01-capacity.md` | **Every number.** The authority | — |
| `docs/project/0N-*.md` | Per-part depth | Numbers that disagree with `01-capacity.md` |
| `docs/cluster/*.md` | How the code works, file by file | This project's decisions |
| `cluster/RUNBOOK.md` | The stages and their gates | Design rationale |

Two splits worth holding onto:

- **`docs/project/` is ours and carries the predictions. `docs/cluster/` documents code**
  and stays accurate as the code does.
- **`SUBMISSION.md` answers the brief. `PROJECT.md` explains the project. `README.md` is
  the front door and stays short.** When something belongs in two, put the reasoning in
  `PROJECT.md`, the answer in `SUBMISSION.md`, and at most a sentence in `README.md`.

## The predicted / measured convention

Every number is marked one or the other.

Predictions are written down **before** the run and checked afterwards. **They are not
edited to match what happened.** A prediction that was wrong by 2× tells the reader which
piece of arithmetic was misunderstood; one silently retrofitted teaches nothing and is
indistinguishable from having got it right.

`TBM` means to-be-measured. A `TBM` left in the final report is a hole we are **disclosing**
— which is fine. A number that was quietly guessed is not.

## Propagating a changed number

Capacity changes flow one way. See `dodona-capacity` for the order.

Anything else: fix the authoritative file, then grep for the old value.

```
grep -rn "<old number>" --include="*.md" .
```

Then verify nothing broke:

```
# every relative link and anchor resolves
python3 - <<'PY'
import os,re,glob
def slug(s):
    s=s.lower(); s=re.sub(r'[^\w\s-]','',s); return s.replace(' ','-')
def anchors(p):
    out=set(); fence=False
    for line in open(p):
        if line.lstrip().startswith('```'): fence=not fence; continue
        if fence: continue
        m=re.match(r'#{1,6}\s+(.*)',line)
        if m: out.add(slug(m.group(1).strip()))
    return out
bad=0
for f in glob.glob('**/*.md',recursive=True):
    d=os.path.dirname(f)
    for m in re.finditer(r'\]\(([^)#]*)(#[^)]+)?\)', open(f).read()):
        t,a=m.group(1).strip(),(m.group(2) or '')[1:]
        if t.startswith(('http','mailto')): continue
        tgt=os.path.normpath(os.path.join(d,t)) if t else f
        if t and not os.path.exists(tgt): print("LINK",f,"->",t); bad+=1; continue
        if a and os.path.isfile(tgt) and tgt.endswith('.md') and a not in anchors(tgt):
            print("ANCHOR",f,"->",t+"#"+a); bad+=1
print("broken:",bad)
PY
```

## Stale docs are declared, not hidden

`docs/README.md` carries a section naming which deep dives still hold numbers from an
earlier hardware target. **Keep it current.** A document that silently disagrees with its
own project is worse than one that says so — the reader has no way to know which to trust.

When a doc is reconciled, remove it from that list in the same commit.

## When asked "is this done?"

Check the tree, not the status table. The tables in `README.md` and `SUBMISSION.md` are
updated by hand and lag.

| Claim | How to check it |
|---|---|
| "the cluster is up" | `kubectl get pods`, `nvidia-smi` — three processes on index 0 |
| "admission works" | `metrics/*/decisions.jsonl` non-empty, `admitperf_gate_recording 1` |
| "the hop works" | `/hops` non-empty **and** `KV_BACKEND=mooncake` |
| "we measured X" | a scrape under `metrics/` that contains X |

If a claim has no artifact behind it, it is not done — say so and update the table.
