# Train your first model on a Mac (about 20 minutes, most of it waiting)

The shortest real path from nothing to a model your team can use, on one Apple-Silicon Mac. No
NVIDIA card, no vLLM, no cloud account, and nothing leaves the machine.

Everything here has been exercised end to end over HTTP in `tests/e2e_train.py`, and the imitation
rung has been run for real on an Intel Mac (140 tickets, 48 seconds on CPU). **What no one has run
yet is the MLX path on Apple Silicon** — the code is verified against mlx-lm's source and its wire
contract is tested, but the first person to do this is doing it first. If something here is wrong,
it will be wrong in this file.

## 0. Install (2 minutes)

```bash
curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-grid/main/install.sh | sh
pip install "grid[train]"          # torch, transformers, trl, peft
pip install mlx-lm                 # the Apple-Silicon trainer and rollout engine
grid train doctor                  # what this computer can do right now
```

`doctor` answers for two rungs separately. Expect the first to be ready and the second to say it
needs an engine — that is correct on a fresh machine, and stage one is what you want first.

## 1. The five-minute proof (optional, but do it once)

Before any of your own data, watch a model actually learn:

```bash
python -m train.mlx.grpo_hello
```

A small model is asked to reverse the words in a sentence, scored on how close it gets, and
updated. On an Intel iMac's CPU the same task climbed **0.220 → 0.674 in 5.6 minutes**. You should
see the score rise and a verdict at the end. If it doesn't rise, stop here and say so — everything
downstream assumes this works.

## 2. Your own data (5 minutes)

```bash
grid train web            # opens http://127.0.0.1:8322
```

Four jobs to pick from: draft support replies · prioritise leads · sort work into your own
categories · anything else your team answers in writing. Pick one and upload an export — a CSV or
JSONL with two columns. If you're not sure what to export, the page has the menu path for Zendesk,
Intercom, Freshdesk, Help Scout, HubSpot, Salesforce and Pipedrive.

**A few hundred rows is a real set. Forty is not**, and the page will say so rather than let you
spend an evening finding out.

The report tells you which columns it read. Check that line — reading the answer as the question
trains the model backwards, and it is the one mistake nothing downstream can catch.

## 3. Teach it (10–30 minutes, unattended)

Tick what a good answer looks like, pick this Mac, press start. Stage one imitates the answers your
team already wrote; it needs nothing but this computer. You can close the tab — the run is an
ordinary subprocess and survives it.

From a terminal, the same thing is:

```bash
grid train sft --config ~/.grid/train-workspaces/<your-model>/grid-train.toml
```

## 4. Is it better? (2 minutes)

Press **Check it on held-back work**. Both models answer the same examples the trained one never
saw, scored by the checks you ticked, and you get a table plus real answers side by side.

**If it lost, that is a normal outcome and the button to serve it does not appear.** The usual fix
is more history, not more training time.

## 5. Use it

"Start using this model" loads it onto this machine and shows you the two lines to point any tool
at — the address of the engine you chose on the machines step, and the model's name:

```
OPENAI_BASE_URL=http://127.0.0.1:8090/v1      # your grid's own endpoint (`grid up`)
model: <your-model>
```

Copy them from the page rather than from here: if you pointed training at a different machine, that
is the address the page will show. Anything that speaks the OpenAI API works.

## 6. Let it keep getting better

```bash
grid train collect --on      # keep the work the grid does — local files, redacted, pruned
grid train schedule on       # a LaunchAgent that runs the cycle at 23:00
grid train schedule          # confirm: on, where, and at what time
```

Then `http://127.0.0.1:8322/w/<your-model>/overnight` shows what has accumulated, what tonight will do,
and every past night — including the ones that trained and were refused, which is most of them
early on.

Two things it will not do: train on the model's own unjudged output (that is how a model drifts
into agreeing with itself), and train while you are using the machine or on battery.

## When something goes wrong

| What you see | What it means |
|---|---|
| `doctor` says stage one is not ready | `pip install "grid[train]"`, or on Apple Silicon `pip install mlx-lm` |
| "only 12 usable tickets" | the export has the wrong columns, or too little history — the report says which |
| the run stops with "out of memory" | pick a smaller starting model on the machines step |
| "no meaningful gain" | it trained and lost. More history helps; more steps usually don't |
| the overnight page says "nothing will happen tonight" | collecting is on but no job is installed — `grid train schedule on` |
| a scheduled night never appears in the history | check `autopilot.log` beside the model; the job writes its reason there |

## What this does not cover

- **More than one machine.** See `two-node-training.md` — one Mac generates attempts, another
  trains, and the weights sync between them. Worth doing once you have a second machine idle.
- **The feedback rung** (`grid train run`, GRPO). It needs an engine that returns sampled token ids
  and their logprobs: `grid train serve` on a Mac, or vLLM on an NVIDIA box.
- **Anything outside your network.** There is no account, no upload, and no vendor in this path.
