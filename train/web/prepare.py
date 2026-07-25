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
_ALIASES = {
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


def _pick(rows: list[dict], field: str) -> str | None:
    """Which column in this export is our `field`? Exact alias first, then substring."""
    if not rows:
        return None
    keys = list(rows[0].keys())
    for alias in _ALIASES[field]:
        if alias in keys:
            return alias
    for alias in _ALIASES[field]:
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


PREPARERS = {"support-replies": prepare_support, "sales-triage": prepare_leads}


def prepare(pack: str, text: str, filename: str) -> tuple[Report, list[dict]]:
    preparer = PREPARERS.get(pack)
    if preparer is None:
        return Report(False, "blocked", "Unknown kind of training.", f"No preparer for {pack!r}."), []
    return preparer(_rows(text, filename))


# The instruction each pack's model works under. Kept identical to the CLI packs' SYSTEM_PROMPT so
# a model started in the browser and one started from a terminal learn the same job.
SYSTEM_PROMPTS = {
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
    if pack == "support-replies":
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
