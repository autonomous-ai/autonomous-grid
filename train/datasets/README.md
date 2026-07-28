# `train/datasets/` — corpora you can train on today

A task pack (`train/packs/`) is a *recipe*: config, data preparation, rewards, and a handful of
sample rows to show the shape. It deliberately ships almost no data, because the data is meant to
be yours.

That leaves a gap. Someone who wants to see the training plane actually work has nothing to run it
on until they export a year of tickets, and `prepare_data.py` will tell them — correctly — that
eight sample rows are not enough to train anything. A dataset here closes that gap: a corpus large
enough to produce a real model, with a grader that says whether the model is any good.

## What is committed, and what is not

**Committed:** the generator, the grader, and a `DATASET.md` recording where the corpus came from
and what it cannot teach. About 50 KB.

**Not committed:** every `.jsonl`. They are generated — the generators are seeded, so
`python make_tickets.py` reproduces the corpus byte-identically, and committing two megabytes of
derived text to save one command is a bad trade.

The `.gitignore` on `*.jsonl` earns its place a second way. These folders are exactly where someone
will drop a real customer export to try a run against it. Ignored by default, that export cannot be
committed without a deliberate `git add -f`, and real support tickets are not a thing to put in a
public repository by accident.

## Using one

```bash
cd train/datasets/autonomous-support
python make_tickets.py                       # -> tickets.jsonl + facts.jsonl
python ../../packs/support_replies/prepare_data.py --input tickets.jsonl
grid train doctor && grid train sft
python eval_facts.py <adapter-dir>           # is it actually right?
```

## Grade for correctness, not for loss

Every dataset here ships a grader, and that is the point of the folder rather than a nicety.

The first run on this corpus took its loss from 3.88 to 0.587 on a clean monotonic curve, and the
model it produced told a customer that "E12 is the factory calibration" and gave the wrong reset
procedure for E01. Loss measures agreement with the training distribution. It cannot see that a
fluent answer is false, and a support model that sounds authoritative while being wrong is worse
than no model at all.

So a dataset carries an answer key — `facts.jsonl`, the strings a correct reply must contain — and
`eval_facts.py` scores held-out tickets on whether the reply states the documented fix, cites the
order id, speaks as the agent rather than the customer, and invents no history. Those four are what
"working" means here. The loss curve is not evidence.

| dataset | rows | teaches |
|---|---|---|
| `autonomous-support` | 1,070 | drafting a support reply in house voice, with the correct documented fix |
