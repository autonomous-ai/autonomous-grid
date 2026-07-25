"""Turn whatever a manager exports out of their tools into tasks, and report honestly on it.

The CLI packs ship editable `prepare_data.py` scripts on purpose — an engineer should own that
code. The web path can't assume anyone will edit anything, so this module does the same job
defensively: guess the columns, keep what's usable, and produce a report that says plainly
whether there is enough to train on and what to do if not.

Never raises on messy input. A file with the wrong shape produces a report that says so.
"""
from __future__ import annotations

import csv
import dataclasses
import io
import json
import re

# Column names seen in real exports (Zendesk, Intercom, HubSpot, Freshdesk, spreadsheets).
# Order matters: the first alias that matches wins, so the strongest evidence goes first and
# anything ambiguous goes last (see _WEAK below).
_ALIASES = {
    # The generic pair: whatever the work was, and whatever your team answered.
    "work": ("work", "input", "question", "request", "text", "prompt", "task", "description",
             "message", "body", "subject", "content", "enquiry", "inquiry", "item"),
    "answer": ("answer", "output", "reply", "response", "resolution", "reply body", "agent reply",
               "completion", "result", "summary", "notes", "outcome text"),
    # A label, for the sorting jobs: which queue, which category, which priority.
    "label": ("label", "category", "queue", "group", "form", "tag", "topic", "class",
              "department", "team", "disposition", "reason", "bucket", "priority", "type",
              "status", "state"),
    "subject": ("subject", "title", "ticket subject", "summary"),
    "body": ("body", "description", "message", "question", "ticket body",
             "first message", "customer message", "content", "text"),
    "reply": ("reply", "response", "answer", "agent reply", "resolution",
              "public reply", "comment", "agent response"),
    "lead": ("lead", "message", "inquiry", "notes", "description", "form message", "body", "text"),
    "outcome": ("outcome", "status", "stage", "deal stage", "disposition", "result", "won/lost"),
    "context": ("context", "source", "company", "company size", "channel", "segment"),
    "resolved": ("resolved", "solved", "status", "state"),
}

OUTCOME_LABELS = {
    "hot": ("closed won", "closed-won", "won", "demo booked", "demo-booked", "purchased"),
    "warm": ("qualified", "meeting held", "meeting-held", "closed lost", "closed-lost",
             "negotiation", "proposal", "engaged"),
    "cold": ("no response", "no-response", "disqualified", "spam", "unqualified",
             "junk", "closed", "lost"),
}


@dataclasses.dataclass
class Report:
    ok: bool                     # is there enough to train on
    level: str                   # "good" | "thin" | "blocked"
    headline: str                # one sentence for the page
    detail: str                  # what to do about it
    rows_seen: int = 0
    rows_usable: int = 0
    samples: list[dict] = dataclasses.field(default_factory=list)
    columns_used: dict[str, str] = dataclasses.field(default_factory=dict)
    distribution: dict[str, int] = dataclasses.field(default_factory=dict)


def _rows(text: str, filename: str) -> list[dict]:
    """Parse CSV or JSONL into lower-cased-key dicts. Unparseable → empty list."""
    text = text.lstrip("﻿")
    is_json = filename.lower().endswith((".jsonl", ".ndjson", ".json")) or text.lstrip().startswith("{")
    out: list[dict] = []
    if is_json:
        for line in text.splitlines():
            line = line.strip().rstrip(",")
            if not line or line in ("[", "]"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                out.append(row)
        if not out:  # maybe a single JSON array
            try:
                data = json.loads(text)
                out = [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []
            except json.JSONDecodeError:
                out = []
    else:
        try:
            out = list(csv.DictReader(io.StringIO(text)))
        except csv.Error:
            out = []
    return [{str(k).strip().lower(): v for k, v in row.items() if k} for row in out]


# Aliases that name the right thing often enough to keep, and the wrong thing often enough that
# anything else should win first:
#   "sent"   — in a mailbox export this is the TIMESTAMP, not the reply. Read as the answer, the
#              model is trained to emit "2026-07-01 10:04:11 +0700" for every request.
#   "status" — present in essentially every helpdesk export, and almost never the field a team
#              sorts by. A routing model trained on it learns to predict "solved".
_WEAK = {"sent", "final", "status", "state", "result", "type", "priority"}


def _pick(rows: list[dict], field: str) -> str | None:
    """Which column in this export is our `field`?

    Strong aliases beat weak ones, exact beats substring, and within a tier the alias order above
    decides. A wrong guess here is not a crash: it is a night of training on the wrong column and
    a model that confidently answers with a timestamp — so the page also prints what was chosen.
    """
    if not rows:
        return None
    keys = list(rows[0].keys())
    strong = [a for a in _ALIASES[field] if a not in _WEAK]
    weak = [a for a in _ALIASES[field] if a in _WEAK]
    for aliases in (strong, weak):
        for alias in aliases:
            if alias in keys:
                return alias
        for alias in aliases:
            for key in keys:
                if alias in key:
                    return key
    return None


def _text(row: dict, column: str | None) -> str:
    value = row.get(column) if column else None
    return str(value).strip() if value not in (None, "") else ""


def prepare_support(rows: list[dict], min_reply_words: int = 8) -> tuple[Report, list[dict]]:
    """Tickets → [{prompt, reference}] plus a report."""
    body_col, reply_col = _pick(rows, "body"), _pick(rows, "reply")
    subject_col, resolved_col = _pick(rows, "subject"), _pick(rows, "resolved")
    if not rows:
        return Report(False, "blocked", "We couldn't read that file.",
                      "Export as CSV or JSONL — one row per ticket, with the customer's message "
                      "and the reply your team sent."), []
    if not reply_col or not (body_col or subject_col):
        found = ", ".join(list(rows[0].keys())[:8]) or "none"
        return Report(False, "blocked", "That export is missing the reply column.",
                      "We need the customer's message and the reply your team sent. "
                      f"Columns we found: {found}.", rows_seen=len(rows)), []

    kept: list[dict] = []
    for row in rows:
        body, subject, reply = _text(row, body_col), _text(row, subject_col), _text(row, reply_col)
        if resolved_col and str(row.get(resolved_col, "")).strip().lower() in ("false", "0", "no", "open", "pending"):
            continue
        if not reply or len(reply.split()) < min_reply_words or not (body or subject):
            continue
        ticket = f"Subject: {subject}\n\n{body}" if subject and body else (body or subject)
        kept.append({"prompt": ticket, "reference": reply})

    columns = {"ticket": body_col or subject_col or "", "reply": reply_col}
    samples = [{"prompt": k["prompt"][:400], "reference": k["reference"][:400]} for k in kept[:3]]
    if len(kept) < 40:
        return Report(False, "blocked",
                      f"Only {len(kept)} usable tickets — not enough to learn from.",
                      "Aim for a few hundred. Export a longer date range, or include tickets "
                      "you'd been filtering out.",
                      len(rows), len(kept), samples, columns), kept
    if len(kept) < 250:
        return Report(True, "thin", f"{len(kept)} usable tickets — enough to start.",
                      "This is a reasonable first pass. More history makes a bigger difference "
                      "than more training time.", len(rows), len(kept), samples, columns), kept
    return Report(True, "good", f"{len(kept)} usable tickets — a strong set.",
                  "Only tickets your team actually resolved are used, so the model learns from "
                  "your good work rather than everything.", len(rows), len(kept), samples, columns), kept


def _label_for(outcome: str) -> str | None:
    text = outcome.strip().lower()
    if not text:
        return None
    for label, needles in OUTCOME_LABELS.items():
        if any(needle in text for needle in needles):
            return label
    return None


def prepare_leads(rows: list[dict]) -> tuple[Report, list[dict]]:
    """Leads → [{prompt, label}] plus a report; classes balanced so 'cold' can't be farmed."""
    lead_col, outcome_col = _pick(rows, "lead"), _pick(rows, "outcome")
    context_col = _pick(rows, "context")
    if not rows:
        return Report(False, "blocked", "We couldn't read that file.",
                      "Export as CSV or JSONL — one row per lead, with the enquiry text and "
                      "what happened to it."), []
    if not lead_col or not outcome_col:
        found = ", ".join(list(rows[0].keys())[:8]) or "none"
        return Report(False, "blocked", "That export is missing what happened to each lead.",
                      "We need the enquiry text and its outcome (won, lost, no response…). "
                      f"Columns we found: {found}.", rows_seen=len(rows)), []

    buckets: dict[str, list[dict]] = {"hot": [], "warm": [], "cold": []}
    unmapped = 0
    for row in rows:
        lead, outcome = _text(row, lead_col), _text(row, outcome_col)
        label = _label_for(outcome)
        if not lead:
            continue
        if label is None:
            unmapped += 1
            continue
        context = _text(row, context_col)
        prompt = f"Lead: {lead}" + (f"\nContext: {context}" if context else "")
        buckets[label].append({"prompt": prompt, "label": label})

    counts = {k: len(v) for k, v in buckets.items()}
    floor = min((c for c in counts.values() if c), default=0)
    kept = [row for rows_ in buckets.values() for row in rows_[:floor]]
    samples = [{"prompt": k["prompt"][:300], "reference": k["label"]} for k in kept[:3]]
    columns = {"lead": lead_col, "outcome": outcome_col}
    detail_tail = f" {unmapped} rows had an outcome we didn't recognise." if unmapped else ""

    if floor < 10:
        return Report(False, "blocked",
                      f"Too few in the smallest group ({counts}).",
                      "We keep the groups balanced so the model can't win by guessing the "
                      "commonest answer, which caps everything at the smallest group. "
                      "Aim for 30+ of each." + detail_tail,
                      len(rows), len(kept), samples, columns, counts), kept
    if floor < 30:
        return Report(True, "thin", f"{len(kept)} leads, balanced at {floor} per group.",
                      "Enough to try. Also compare the result against your own judgement before "
                      "trusting it." + detail_tail,
                      len(rows), len(kept), samples, columns, counts), kept
    return Report(True, "good", f"{len(kept)} leads, balanced at {floor} per group.",
                  "Your closed-won and lost history is the answer key here — that makes this the "
                  "most reliable kind of training we can do." + detail_tail,
                  len(rows), len(kept), samples, columns, counts), kept


def prepare_generic(rows: list[dict]) -> tuple[Report, list[dict]]:
    """Any job where your team writes an answer: two columns, the work and what they sent.

    This is the pack that makes the product work for departments nobody planned for — procurement
    replying to suppliers, HR answering policy questions, ops writing shipping notes. If a team has
    a spreadsheet of "here is what came in, here is what we said", it can train a model.
    """
    if not rows:
        return Report(False, "blocked", "We couldn't read that file.",
                      "Export as CSV or JSONL with two columns: the work that came in, and what "
                      "your team wrote back."), []
    work_col, answer_col = _pick(rows, "work"), _pick(rows, "answer")
    if not work_col or not answer_col or work_col == answer_col:
        found = ", ".join(list(rows[0].keys())[:8]) or "none"
        return Report(False, "blocked", "We need two columns to learn from.",
                      "One with the work that came in, one with what your team wrote back. "
                      f"Columns we found: {found}.", rows_seen=len(rows)), []

    kept = [
        {"prompt": _text(row, work_col), "reference": _text(row, answer_col)}
        for row in rows
        if _text(row, work_col) and len(_text(row, answer_col).split()) >= 3
    ]
    columns = {"the work": work_col, "what your team wrote": answer_col}
    samples = [{"prompt": k["prompt"][:400], "reference": k["reference"][:400]} for k in kept[:3]]
    detail_columns = (f"Reading “{work_col}” as the work and “{answer_col}” as your team's answer. "
                      "If that is the wrong way round, rename the columns and upload again.")
    if len(kept) < 40:
        return Report(False, "blocked", f"Only {len(kept)} usable pairs.",
                      f"{detail_columns} Aim for a few hundred.",
                      len(rows), len(kept), samples, columns), kept
    level = "good" if len(kept) >= 250 else "thin"
    return Report(True, level, f"{len(kept)} usable pairs — {'a strong set' if level == 'good' else 'enough to start'}.",
                  detail_columns, len(rows), len(kept), samples, columns), kept


def prepare_classify(rows: list[dict]) -> tuple[Report, list[dict]]:
    """Sorting work into your own categories, checked against what your team actually chose.

    The reward here is exact: either it picked the category your team picked, or it did not. That
    makes it the most reliable kind of training available, and it fits a surprising amount of what
    a business does all day — routing, tagging, triage, quality flags.
    """
    if not rows:
        return Report(False, "blocked", "We couldn't read that file.",
                      "Export as CSV or JSONL with the text and the category your team chose."), []
    text_col, label_col = _pick(rows, "work"), _pick(rows, "label")
    if not text_col or not label_col or text_col == label_col:
        found = ", ".join(list(rows[0].keys())[:8]) or "none"
        return Report(False, "blocked", "We need the text and the category your team chose.",
                      f"Columns we found: {found}.", rows_seen=len(rows)), []

    buckets: dict[str, list[dict]] = {}
    for row in rows:
        text, label = _text(row, text_col), _text(row, label_col).strip()
        if not text or not label or len(label) > 60:
            continue
        buckets.setdefault(label.lower(), []).append({"prompt": text, "label": label.lower()})

    counts = {label: len(items) for label, items in buckets.items()}
    columns = {"the text": text_col, "the category": label_col}
    if len(counts) < 2:
        return Report(False, "blocked", "Only one category found.",
                      "A sorting model needs at least two categories to choose between.",
                      len(rows), 0, [], columns, counts), []
    if len(counts) > 24:
        return Report(False, "blocked", f"{len(counts)} different categories.",
                      "That is more like free text than a set of categories. Group them into a "
                      "dozen or fewer, or use the “anything your team answers” option instead.",
                      len(rows), 0, [], columns, counts), []

    # Balanced, so the model cannot win by always guessing the commonest category.
    floor = min(counts.values())
    kept = [row for items in buckets.values() for row in items[:floor]]
    samples = [{"prompt": k["prompt"][:300], "reference": k["label"]} for k in kept[:3]]
    spread = ", ".join(f"{label} {count}" for label, count in sorted(counts.items(),
                                                                     key=lambda kv: -kv[1])[:6])
    if floor < 10:
        return Report(False, "blocked",
                      f"The smallest category has only {floor} examples.",
                      f"Categories are balanced so the model can't win by guessing the commonest "
                      f"one, which caps every category at the smallest. Found: {spread}. Aim for "
                      "30+ each.", len(rows), len(kept), samples, columns, counts), kept
    level = "good" if floor >= 30 else "thin"
    return Report(True, level,
                  f"{len(kept)} examples across {len(counts)} categories, balanced at {floor} each.",
                  f"Found: {spread}. Your team's own choices are the answer key, which makes this "
                  "the most reliable kind of training there is.",
                  len(rows), len(kept), samples, columns, counts), kept


PREPARERS = {
    "support-replies": prepare_support,
    "sales-triage": prepare_leads,
    "any-task": prepare_generic,
    "sort-into-categories": prepare_classify,
}


def prepare(pack: str, text: str, filename: str) -> tuple[Report, list[dict]]:
    preparer = PREPARERS.get(pack)
    if preparer is None:
        return Report(False, "blocked", "Unknown kind of training.", f"No preparer for {pack!r}."), []
    return preparer(_rows(text, filename))


# The instruction each pack's model works under. Kept identical to the CLI packs' SYSTEM_PROMPT so
# a model started in the browser and one started from a terminal learn the same job.
SYSTEM_PROMPTS = {
    "any-task": "Answer the way this team answers. Be specific and concise.",
    "sort-into-categories": (
        "Sort the text into exactly one category. Reply with ONLY this line:\n"
        "CATEGORY: <one of the categories you were trained on>"
    ),
    "support-replies": (
        "You are the support agent. Read the ticket and draft the reply to send. "
        "Be specific to this ticket, resolve the issue, and keep the reply under 160 words."
    ),
    "sales-triage": (
        "You triage inbound sales leads. Reply in EXACTLY this shape and nothing else:\n"
        "PRIORITY: <hot|warm|cold>\nNEXT: <one concrete next action>"
    ),
}


def write_task_files(pack: str, kept: list[dict], dest) -> None:
    """Write what the trainer and the graders read.

    Four files, because the ladder has two rungs: `prompts.jsonl` + `refs/labels.jsonl` feed the
    RL pass, and **`sft.jsonl` feeds stage one** — imitation of the answers the team already wrote.
    Stage one is the rung that runs on a Mac with no rollout server and no NVIDIA card, so leaving
    it out of the browser path would have meant the interface could only offer the harder half.
    """
    from pathlib import Path

    dest = Path(dest)
    (dest / "prompts.jsonl").write_text(
        "".join(json.dumps({"prompt": row["prompt"]}) + "\n" for row in kept), encoding="utf-8"
    )
    system = SYSTEM_PROMPTS.get(pack, "")
    if pack == "sort-into-categories":
        (dest / "labels.jsonl").write_text(
            "".join(json.dumps({"prompt": r["prompt"], "label": r["label"]}) + "\n" for r in kept),
            encoding="utf-8",
        )
        answers = [(r["prompt"], f"CATEGORY: {r['label']}") for r in kept]
    elif pack in ("support-replies", "any-task"):
        (dest / "refs.jsonl").write_text(
            "".join(json.dumps({"prompt": r["prompt"], "reference": r["reference"]}) + "\n"
                    for r in kept),
            encoding="utf-8",
        )
        answers = [(r["prompt"], r["reference"]) for r in kept]
    else:
        (dest / "labels.jsonl").write_text(
            "".join(json.dumps({"prompt": r["prompt"], "label": r["label"]}) + "\n" for r in kept),
            encoding="utf-8",
        )
        # The lead pack's ideal answer is the shape the graders check, with the true priority in it.
        answers = [(r["prompt"], f"PRIORITY: {r['label']}\nNEXT: ") for r in kept]

    (dest / "sft.jsonl").write_text(
        "".join(
            json.dumps({"messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": answer},
            ]}) + "\n"
            for prompt, answer in answers
        ),
        encoding="utf-8",
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug[:48] or "model"
