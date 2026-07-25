"""Workspaces: one folder per model someone is teaching.

    ~/.grid/train-workspaces/<slug>/
        meta.json          what it is, which pack, the data report, chosen checks
        upload.<ext>       exactly what was uploaded, kept verbatim
        prompts.jsonl      the tasks
        refs.jsonl | labels.jsonl   what the graders compare against
        rewards.py         the checks, generated from the plain-language choices
        grid-train.toml    the run config the CLI reads

Everything is a plain file the user owns and an engineer can inspect — the web interface writes
the same inputs `grid train run` takes from a hand-written config, so nothing about a run started
from a browser is special.
"""
from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path

from shared import paths

from . import prepare


def workspaces_root() -> Path:
    return paths.grid_home() / "train-workspaces"


@dataclasses.dataclass
class Workspace:
    slug: str
    path: Path
    meta: dict

    @property
    def name(self) -> str:
        return self.meta.get("name") or self.slug

    @property
    def pack(self) -> str:
        return self.meta.get("pack", "")

    @property
    def report(self) -> dict:
        return self.meta.get("report") or {}

    @property
    def stage(self) -> str:
        """Where the user is: data → checks → machines → running → done."""
        return self.meta.get("stage", "data")

    def save(self) -> None:
        self.meta["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        (self.path / "meta.json").write_text(json.dumps(self.meta, indent=2) + "\n", encoding="utf-8")


def create(name: str, pack: str) -> Workspace:
    slug = prepare.slugify(name)
    root = workspaces_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / slug
    suffix = 2
    while path.exists():
        path = root / f"{slug}-{suffix}"
        suffix += 1
    path.mkdir(parents=True)
    workspace = Workspace(
        slug=path.name,
        path=path,
        meta={
            "name": name,
            "slug": path.name,
            "pack": pack,
            "stage": "data",
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        },
    )
    workspace.save()
    return workspace


def load(slug: str) -> Workspace:
    path = workspaces_root() / slug
    meta_file = path / "meta.json"
    if not meta_file.is_file():
        raise KeyError(slug)
    return Workspace(slug=slug, path=path, meta=json.loads(meta_file.read_text(encoding="utf-8")))


def list_all() -> list[Workspace]:
    root = workspaces_root()
    if not root.is_dir():
        return []
    out = []
    for child in sorted(root.iterdir(), reverse=True):
        try:
            out.append(load(child.name))
        except (KeyError, json.JSONDecodeError):
            continue
    return out


def attach_data(workspace: Workspace, filename: str, content: str) -> prepare.Report:
    """Save the upload verbatim, prepare the tasks, and record the report."""
    suffix = Path(filename).suffix or ".csv"
    (workspace.path / f"upload{suffix}").write_text(content, encoding="utf-8")
    report, kept = prepare.prepare(workspace.pack, content, filename)
    if kept:
        prepare.write_task_files(workspace.pack, kept, workspace.path)
    workspace.meta["upload"] = {"filename": filename, "bytes": len(content.encode("utf-8"))}
    workspace.meta["report"] = dataclasses.asdict(report)
    workspace.meta["stage"] = "checks" if report.ok else "data"
    workspace.save()
    return report


# --- the checks, in the words a manager would use -----------------------------------------
# Each entry generates one reward function. `default` decides what is pre-ticked; `cost` warns
# where a check makes a run slower.
CHECKS: dict[str, list[dict]] = {
    "support-replies": [
        {"id": "similarity", "label": "Sounds like the replies your team actually sent",
         "help": "Compares each draft with how your team answered that ticket.",
         "default": True},
        {"id": "grounding", "label": "Uses the specifics of the ticket",
         "help": "Rewards drafts that pick up the order number, product or dates in the message "
                 "instead of answering generically.",
         "default": True},
        {"id": "format", "label": "Ready to send",
         "help": "Sensible length, no [placeholders] left in, none of the marketing words we ban.",
         "default": True},
        {"id": "judge", "label": "Have a local model rate the tone",
         "help": "Slower, and needs a capable model on your grid. Your tickets stay on your "
                 "network — the rating happens on a machine you own.",
         "default": False, "cost": "about 2× slower"},
    ],
    "sales-triage": [
        {"id": "correct_priority", "label": "Gets the priority right",
         "help": "Checked against what actually happened to each lead — won, lost, ignored.",
         "default": True, "locked": True},
        {"id": "parseable", "label": "Answers in a shape your tools can read",
         "help": "One priority line and one next action, nothing rambling.",
         "default": True},
    ],
}

_REWARD_SOURCES = {
    "support-replies": {
        "header": '''"""Checks for this model, generated from the options chosen in the web interface.
Edit freely — this is a plain Python file and `grid train run` reads it as-is."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

_REFS = {}
_p = Path(__file__).parent / "refs.jsonl"
if _p.is_file():
    for _line in _p.read_text(encoding="utf-8").splitlines():
        _row = json.loads(_line)
        _REFS[_row["prompt"]] = _row["reference"]

_BANNED = {"revolutionary", "empower", "supercharge", "unlock", "transform", "leverage",
           "synergy", "delve", "elevate"}
_PLACEHOLDER = re.compile(r"\\[(?:name|order|tracking|date|link|company)[^\\]]*\\]|\\{\\{[^}]*\\}\\}|xxx+",
                          re.IGNORECASE)


def _texts(completions, completions_text):
    return completions_text if completions_text is not None else completions
''',
        "similarity": '''

def reward_similarity(prompts, completions=None, completions_text=None, **kwargs):
    """Closeness to the reply your team actually sent (0.5 when we have no reference)."""
    out = []
    for prompt, text in zip(prompts, _texts(completions, completions_text)):
        reference = _REFS.get(prompt)
        if not reference:
            out.append(0.5)
            continue
        out.append(difflib.SequenceMatcher(None, text.lower().split(),
                                           reference.lower().split()).ratio())
    return out
''',
        "grounding": '''

def reward_grounding(prompts, completions=None, completions_text=None, **kwargs):
    """Does the draft engage with THIS ticket — its ids, numbers, product names?"""
    out = []
    for prompt, text in zip(prompts, _texts(completions, completions_text)):
        distinctive = set(re.findall(r"[A-Z]{2,}[-\\d]*\\d|\\d{4,}|[A-Z][a-z]+[A-Z]\\w+", prompt))
        if not distinctive:
            out.append(0.5)
            continue
        out.append(sum(1 for token in distinctive if token in text) / len(distinctive))
    return out
''',
        "format": '''

def reward_format(prompts, completions=None, completions_text=None, **kwargs):
    """Ready to send: sane length, no leftover placeholders, no banned words."""
    out = []
    for text in _texts(completions, completions_text):
        words = text.split()
        score = 1.0
        if not 20 <= len(words) <= 180:
            score -= 0.5
        if _PLACEHOLDER.search(text):
            score -= 0.5
        if any(word.lower().strip(".,!") in _BANNED for word in words):
            score -= 0.3
        out.append(max(score, 0.0))
    return out
''',
        "judge": '''

def reward_judge(prompts, completions=None, completions_text=None, **kwargs):
    """A model on YOUR grid rates the reply 0-10. Never point this at a vendor API: the
    tickets would leave your network, which is the whole thing this avoids."""
    import os

    import httpx

    url = os.environ.get("GRID_JUDGE_URL", "http://127.0.0.1:8000/v1").rstrip("/")
    model = os.environ.get("GRID_JUDGE_MODEL", "auto")
    out = []
    with httpx.Client(timeout=90.0) as client:
        for prompt, text in zip(prompts, _texts(completions, completions_text)):
            try:
                response = client.post(f"{url}/chat/completions", json={
                    "model": model, "max_tokens": 4,
                    "messages": [{"role": "user", "content":
                        "Rate this support reply 0-10 for: resolves the issue, specific to the "
                        f"ticket, professional tone.\\n\\nTICKET:\\n{prompt}\\n\\nREPLY:\\n{text}\\n\\n"
                        "Answer with ONLY the number."}]})
                match = re.search(r"\\d+", response.json()["choices"][0]["message"]["content"])
                out.append(min(int(match.group()) if match else 0, 10) / 10)
            except Exception:      # a judge that is down must not fail the run
                out.append(0.5)
    return out
''',
    },
    "sales-triage": {
        "header": '''"""Checks for this model, generated from the options chosen in the web interface."""
from __future__ import annotations

import json
import re
from pathlib import Path

_LABELS = {}
_p = Path(__file__).parent / "labels.jsonl"
if _p.is_file():
    for _line in _p.read_text(encoding="utf-8").splitlines():
        _row = json.loads(_line)
        _LABELS[_row["prompt"]] = _row["label"]

_PRIORITY = re.compile(r"^PRIORITY:\\s*(hot|warm|cold)\\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT = re.compile(r"^NEXT:\\s*(\\S.*)$", re.IGNORECASE | re.MULTILINE)


def _texts(completions, completions_text):
    return completions_text if completions_text is not None else completions
''',
        "correct_priority": '''

def reward_correct_priority(prompts, completions=None, completions_text=None, **kwargs):
    """1.0 when the priority matches what actually happened to that lead."""
    out = []
    for prompt, text in zip(prompts, _texts(completions, completions_text)):
        truth = _LABELS.get(prompt)
        match = _PRIORITY.search(text)
        predicted = match.group(1).lower() if match else None
        out.append(0.5 if truth is None else (1.0 if predicted == truth else 0.0))
    return out
''',
        "parseable": '''

def reward_parseable(prompts, completions=None, completions_text=None, **kwargs):
    """The answer must be machine-readable: one priority line, one next action, no rambling."""
    out = []
    for text in _texts(completions, completions_text):
        score = 0.0
        if len(_PRIORITY.findall(text)) == 1:
            score += 0.6
        if _NEXT.search(text):
            score += 0.3
        if len([line for line in text.strip().splitlines() if line.strip()]) <= 4:
            score += 0.1
        out.append(score)
    return out
''',
    },
}

_MAX_TOKENS = {"support-replies": 220, "sales-triage": 60}


def write_checks(workspace: Workspace, chosen: list[str]) -> Path:
    """Generate rewards.py from the ticked boxes."""
    sources = _REWARD_SOURCES[workspace.pack]
    locked = [c["id"] for c in CHECKS[workspace.pack] if c.get("locked")]
    ids = [c["id"] for c in CHECKS[workspace.pack] if c["id"] in set(chosen) | set(locked)]
    body = sources["header"] + "".join(sources[i] for i in ids)
    path = workspace.path / "rewards.py"
    path.write_text(body, encoding="utf-8")
    workspace.meta["checks"] = ids
    workspace.meta["stage"] = "machines"
    workspace.save()
    return path


def write_config(workspace: Workspace, *, model: str, endpoint: str, steps: int,
                 sync_nodes: list[str] | None = None) -> Path:
    """Write the same grid-train.toml an engineer would hand-write."""
    nodes = sync_nodes or [endpoint]
    toml = f'''# Generated by the grid train web interface for "{workspace.name}".
# This is an ordinary run config — `grid train run --config <this file>` works from a terminal.

[model]
name = "{model}"

[rollout]
base_url = "{endpoint}"
api_key_env = "GRID_TRAIN_API_KEY"
max_tokens = {_MAX_TOKENS.get(workspace.pack, 200)}
temperature = 1.0
sync_every = 2
sync_nodes = [{", ".join(f'"{n}"' for n in nodes)}]

[data]
prompts_jsonl = "{(workspace.path / "prompts.jsonl").as_posix()}"

[rewards]
python_file = "{(workspace.path / "rewards.py").as_posix()}"

[trainer]
group_size = 8
steps = {steps}
learning_rate = 1e-5
lora_rank = 16
lora_alpha = 32
save_every_steps = 25
output_dir = "{(workspace.path / "run").as_posix()}"

[deploy]
adapter_name = "{workspace.slug}"
nodes = [{", ".join(f'"{n}"' for n in nodes)}]
'''
    path = workspace.path / "grid-train.toml"
    path.write_text(toml, encoding="utf-8")
    workspace.meta["config"] = {"model": model, "endpoint": endpoint, "steps": steps}
    workspace.save()
    return path
