"""One spelling for a project id (ADR 0033 D-a, issue 28).

A project id used to be spelled positionally in the `grid project …` group and as `--project` in the
`grid task …` group, so moving between them meant changing hands and being refused by argparse in
both directions. Every command that takes a project id now accepts **both**, and this module is the
one place they meet.

Client-only. Nothing here reaches the wire: the relay is posted the same `project_id` it always was,
so there is no lockstep value and no rollout order.

Registration happens next to the argument (`add_project` / `add_member` in `cli/parser.py`) and
records what resolution needs on the subparser itself via `set_defaults`, rather than in a table
keyed by command name — a table would rot the first time a command was renamed.
"""
from __future__ import annotations

import argparse

# Where `--project` / `--member` park. Deliberately NOT `project` / `member_key`: argparse would then
# have the flag and the positional overwrite each other in an order nothing controls, and "both were
# given" — the case that must be refused — would be unobservable by the time we looked.
_PROJECT_FLAG_DEST = "project_flag"
_MEMBER_FLAG_DEST = "member_flag"
_CONVERSATION_FLAG_DEST = "conversation_flag"

# The marker `resolve` keys on. Absent ⇒ this command takes no project id, so resolution is a no-op
# and the namespace passes through untouched (`grid project create`, `grid project list`, and every
# command outside these two groups).
_SHAPE = "_project_shape"
_PROG = "_project_prog"
_REQUIRED = "_project_required"

# The two ways to type THIS command, accumulated as its arguments are registered. Grown rather than
# templated, because a refusal that offers `grid project member remove <project-id>` drops the
# `--member` the caller already typed, and `grid project import <project-id>` is worse still: it
# PARSES, with the id landing in the `<path>` slot. Both shipped in the first draft and were found
# in review, so the forms are now derived from the command's real shape and can only be wrong if the
# registration order is (which argparse would break first).
_FORMS = "_project_forms"

_LIST_HINT = "`grid project list` shows the ids you are a member of."
_MEMBER_HINT = "`grid project member list` prints the key for each member."
_CONVERSATION_HINT = ("`grid task get <turn-id>` prints the conversation a turn belongs to, and "
                      "`grid task create` prints the one it opened.")


def add_project(parser: argparse.ArgumentParser, *, required: bool = True,
                help: str = "Project id from `grid project list`.") -> None:
    """Register both spellings of the project id on `parser`."""
    # Whatever this command already takes positionally comes first and must stay first — today only
    # `import`'s required `<path>`.
    leading = "".join(f"<{action.dest.replace('_', '-')}> "
                      for action in parser._actions if not action.option_strings)
    parser.add_argument("project_id", nargs="?", default=None, help=help)
    parser.add_argument(
        "--project", dest=_PROJECT_FLAG_DEST, default=None, metavar="ID",
        help="The same project id, named instead of given positionally.")
    parser.set_defaults(**{
        _SHAPE: "plain", _PROG: parser.prog, _REQUIRED: required,
        _FORMS: (f"{leading}<project-id>", f"{leading}--project <project-id>")})


# What each TRAILING-OPTIONAL shape's second slot is called — the dest to shift into, and the word a
# refusal uses for it. A table for `_SECOND`'s reason, restated because this one was a single
# hard-coded shape until ADR 0034 D-m (issue 45) needed a second: `directory` and `path` differ only
# in their words, and two copies of the same slot arithmetic is how one of them comes to behave
# differently. `grid project files --project P src` parked `src` in `project_id` and listed the top
# of a project called `src` — measured, before this table existed.
# `(dest, word, unset)` — the slot to shift into, the word a refusal uses for it, and the value
# argparse leaves there when the caller gave none. The third element is not decoration: `directory`
# defaults to `None` and `path` to `""`, so a truthiness test would quietly change what "already
# filled" means for one of them, which is precisely the drift this table exists to prevent.
_TRAILING = {
    "directory": ("directory", "directory", None),
    "path": ("path", "path", ""),
}

# What each two-positional shape's SECOND value is called, in every place a refusal has to name it.
# A table rather than a branch per shape: `member` and `conversation` differ only in their words, and
# three copies of the same slot arithmetic is how one of them comes to behave differently.
_SECOND = {
    "member": ("member_key", _MEMBER_FLAG_DEST, "--member", "member key", _MEMBER_HINT),
    "conversation": ("conversation_id", _CONVERSATION_FLAG_DEST, "--conversation",
                     "conversation id", _CONVERSATION_HINT),
}


def _forms_so_far(parser: argparse.ArgumentParser, caller: str) -> tuple[str, str]:
    """The forms `add_project` recorded, or a developer error naming the fix.

    Registering a second optional positional BEFORE the project id would silently swap what a
    caller's first word means, because argparse fills consecutive optional positionals left to
    right. That is a mistake to make loudly at parser-build time, not a `TypeError` from unpacking
    `None` three frames away.
    """
    forms = parser.get_default(_FORMS)
    if forms is None:
        raise ValueError(
            f"{caller}({parser.prog}) was called before add_project(). The project id has to be "
            f"registered first, or argparse fills this positional with the caller's project id.")
    return forms


def add_directory(parser: argparse.ArgumentParser, *, help: str) -> None:
    """Register `clone`/`refresh`'s trailing optional directory, and mark the shape.

    Call it AFTER `add_project`, for the reason `add_member` documents.
    """
    positional, flag = _forms_so_far(parser, "add_directory")
    parser.add_argument("directory", nargs="?", default=None, help=help)
    parser.set_defaults(**{_SHAPE: "directory",
                           _FORMS: (f"{positional} [<directory>]", f"{flag} [<directory>]")})


def add_path(parser: argparse.ArgumentParser, *, help: str) -> None:
    """Register `files`' trailing OPTIONAL path, and mark the shape (ADR 0034 D-m, issue 45).

    Call it AFTER `add_project`, for the reason `add_member` documents.

    ⚠️ **Only for an OPTIONAL trailing positional.** `grid project file`'s path is REQUIRED, so
    argparse gives a lone positional to it rather than to the optional project id and the ambiguity
    this shape exists for cannot arise — measured, and the same reason `wip reset`'s conversation id
    needs no rule of its own.
    """
    positional, flag = _forms_so_far(parser, "add_path")
    parser.add_argument("path", nargs="?", default="", help=help)
    parser.set_defaults(**{_SHAPE: "path",
                           _FORMS: (f"{positional} [<path>]", f"{flag} [<path>]")})


def add_conversation(parser: argparse.ArgumentParser, *,
                     help: str = "Conversation id from `grid task get <turn-id>`.") -> None:
    """Register both spellings of a conversation id, and mark the parser as a two-positional shape.

    The third shape beside `add_member` and `add_directory`, added by ADR 0034 D-e (issue 41) when
    `wip reset` stopped naming a member and started naming the conversation whose branch it moves.

    Call it AFTER `add_project`, for `add_member`'s reason: argparse fills consecutive optional
    positionals left to right, so the registration order is the order a caller types them.

    ⚠️ **It has to go through this module rather than a bare `add_argument`, and a test says so.**
    The forms every refusal in here offers are derived from what was registered — so a second
    REQUIRED positional added behind this module's back makes every refusal offer a command that no
    longer parses, and `test_a_refusal_only_ever_offers_a_command_that_really_works` is what caught
    exactly that. It was written as a bare `add_argument` first.
    """
    positional, flag = _forms_so_far(parser, "add_conversation")
    parser.add_argument("conversation_id", nargs="?", default=None, help=help)
    parser.add_argument(
        "--conversation", dest=_CONVERSATION_FLAG_DEST, default=None, metavar="ID",
        help="The same conversation id, named instead of given positionally.")
    parser.set_defaults(**{_SHAPE: "conversation",
                           _FORMS: (f"{positional} <conversation-id>",
                                    f"{flag} --conversation <conversation-id>")})


def add_member(parser: argparse.ArgumentParser, *,
               help: str = "Member key from `grid project member list`.") -> None:
    """Register both spellings of a member key, and mark the parser as the two-positional shape.

    Call it AFTER `add_project`: argparse fills consecutive optional positionals left to right, so
    the registration order is the order a caller types them.

    `--member` is the flag issue 29 later teaches to accept `me` and an email address.
    """
    positional, flag = _forms_so_far(parser, "add_member")
    parser.add_argument("member_key", nargs="?", default=None, help=help)
    parser.add_argument(
        "--member", dest=_MEMBER_FLAG_DEST, default=None, metavar="KEY",
        help="The same member key, named instead of given positionally.")
    parser.set_defaults(**{_SHAPE: "member",
                           _FORMS: (f"{positional} <member-key>",
                                    f"{flag} --member <member-key>")})


def resolve(args: argparse.Namespace) -> argparse.Namespace:
    """Settle the two spellings into one, returning a NEW namespace.

    A new one rather than a mutated one because every caller downstream reads it as a record, and a
    handler that saw `args.project_id` change under it would be the sort of bug this slice exists to
    remove. Commands with no project id are returned unchanged.
    """
    shape = getattr(args, _SHAPE, None)
    if shape is None:
        return args

    prog = getattr(args, _PROG, "grid")
    forms = _forms(args, prog=prog)
    required = bool(getattr(args, _REQUIRED, True))
    # Before any of the shape helpers: they compare and move these values, and a blank one makes
    # every message downstream describe the wrong problem.
    _refuse_a_blank_value(args, prog=prog, forms=forms, required=required)
    if shape in _TRAILING:
        args = _shift_the_trailing_slot(args, shape, prog=prog, forms=forms)
    if shape in _SECOND:
        args = _place_a_lone_positional(args, shape, prog=prog, forms=forms)

    # The project first, so a command missing both is told about its FIRST argument first, the way
    # argparse reports a missing positional.
    settled = _merge(
        prog=prog, label="project id", positional=getattr(args, "project_id", None),
        flag=getattr(args, _PROJECT_FLAG_DEST, None), spelling="--project",
        required=required, forms=forms, hint=_LIST_HINT)

    changed: dict[str, str | None] = {}
    if shape in _SECOND:
        dest, flag_dest, spelling, label, hint = _SECOND[shape]
        changed[dest] = _merge(
            prog=prog, label=label, positional=getattr(args, dest, None),
            flag=getattr(args, flag_dest, None), spelling=spelling, required=True,
            # The same whole-command forms, not the project's with a word swapped: the project id
            # comes first, so `<prog> <member-key>` is not a form this command has. Only the hint
            # differs — it is `member list`, not `list`, that prints a key.
            forms=forms, hint=hint)
    # Written to BOTH names: the `project` group's handlers read `project_id` and the `task` group's
    # read `project`, and neither had to change for this slice.
    return argparse.Namespace(
        **{**vars(args), **changed, "project_id": settled, "project": settled})


def _refuse_a_blank_value(args: argparse.Namespace, *, prog: str, forms: tuple[str, str],
                          required: bool) -> None:
    """A value that was TYPED but is blank is neither a project id nor a member key.

    Blank is not the same as absent, and must not be read as it: `--project ""` falling through to
    the caller's `default` project is the substitution this whole slice exists to remove — something
    was named, and something else was used. So it is refused, never treated as "not given".

    What it fixes: an empty id used to be passed on verbatim, so `grid project status ""` asked for
    `/relay/v1/projects//status`, matched no route, and got a bare framework 404 — which
    `missing_route_hint` reports as "this grid's relay does not have projects yet, ask its operator
    to update it". The relay is fine. Sending somebody to chase a working feature is exactly what
    `_OLD_RELAY_NO_CANCEL` was split out to avoid.

    Pre-existing rather than introduced by the two-spelling work: the old required positional
    accepted `""` just as happily, and argparse's own `required=True` checks presence, never
    emptiness. `remote_task._resolve_project` has caught the `task create` case since issue 26; this
    is the same rule for the other seventeen commands, in the one place that now decides whether a
    value was given at all.
    """
    for label, value, hint in (
            ("project id", getattr(args, "project_id", None), _LIST_HINT),
            ("project id", getattr(args, _PROJECT_FLAG_DEST, None), _LIST_HINT),
            ("member key", getattr(args, "member_key", None), _MEMBER_HINT),
            ("member key", getattr(args, _MEMBER_FLAG_DEST, None), _MEMBER_HINT),
            ("conversation id", getattr(args, "conversation_id", None), _CONVERSATION_HINT),
            ("conversation id", getattr(args, _CONVERSATION_FLAG_DEST, None), _CONVERSATION_HINT)):
        if value is None or value.strip():
            continue
        # Only `task create` has somewhere to fall back to, and its refusal has to name it —
        # otherwise the advice is "pass a real id" to somebody whose next move is to pass none.
        fallback = ("" if required or label != "project id" else
                    "\n…or leave it off entirely to use your own 'default' project.")
        raise SystemExit(f"{prog}: the {label} given is empty.\nPass a real one, either way:\n"
                         f"    {forms[0]}\n    {forms[1]}{fallback}\n{hint}")


def _forms(args: argparse.Namespace, *, prog: str) -> tuple[str, str]:
    """The two whole-command forms this command accepts, as `grid …` lines ready to print.

    Every refusal in this module offers these two and nothing hand-written, so a command whose shape
    changes cannot leave a message behind describing the shape it used to have.
    """
    positional, flag = getattr(args, _FORMS, ("<project-id>", "--project <project-id>"))
    return f"{prog} {positional}", f"{prog} {flag}"


def _shift_the_trailing_slot(args: argparse.Namespace, shape: str, *, prog: str,
                             forms: tuple[str, str]) -> argparse.Namespace:
    """`<project-id> [<second>]` commands: with `--project` given, the slots shift.

    Measured: argparse fills consecutive optional positionals left to right, so
    `clone --project abc mydir` parks `mydir` in `project_id` and leaves `directory` unset — a
    clone into a directory named after the project, which is not what anybody typing that meant.
    Naming the id frees its slot, and a remaining positional then has exactly one reading.

    Two of them do not: with the id named there is no second slot to shift into, so that is refused
    rather than guessed at.
    """
    dest, word, unset = _TRAILING[shape]
    flag = getattr(args, _PROJECT_FLAG_DEST, None)
    positional = getattr(args, "project_id", None)
    if flag is None or positional is None:
        return args
    if positional == flag:
        # One project id said both ways — the documented "both spellings, same value, is fine". NOT
        # a second slot. `clone` hid this because its own default directory is named after the
        # project id, so shifting made no difference there; `refresh` defaults to the CURRENT
        # directory, and a member who restated the id out of habit would silently refresh `./<id>`
        # instead. Found in review. Saying it twice AND naming a second value still works:
        # `clone --project P P dir`.
        return args
    second = getattr(args, dest, unset)
    if second != unset:
        raise SystemExit(
            f"{prog}: {positional!r} and {second!r} were both given after --project {flag}, and "
            f"only one of them can be the {word}.\nGive the project id once, either way:\n"
            f"    {forms[0]}\n    {forms[1]}")
    return argparse.Namespace(
        **{**vars(args), "project_id": None, dest: positional})


def _place_a_lone_positional(args: argparse.Namespace, shape: str, *, prog: str,
                             forms: tuple[str, str]) -> argparse.Namespace:
    """One positional on a two-positional command — decide which of the two it is, or refuse.

    argparse gives it to `project_id` whatever the caller meant (measured), so on its own
    `member remove --project P abc` would quietly act on a project called `abc`.

    But it is only AMBIGUOUS when neither flag was given. Name either value and the remaining slot
    is the only place the positional can go, so `member remove P1 --member KEY` and
    `member remove --project P1 KEY` both have exactly one reading and are taken — the same slot shift
    `_shift_the_directory` does for `clone`/`refresh`, and the reason the two flags compose with the
    positionals instead of being a separate dialect. Refusing them (the first draft, caught in
    review) meant a refusal that claimed an ambiguity the flag had already settled.

    With BOTH flags given a positional is a stray; `_merge` refuses it, naming both values.
    """
    dest, flag_dest, _spelling, label, _hint = _SECOND[shape]
    positional = getattr(args, "project_id", None)
    if positional is None or getattr(args, dest, None) is not None:
        return args  # zero or two positionals — nothing to place

    project_flag = getattr(args, _PROJECT_FLAG_DEST, None)
    member_flag = getattr(args, flag_dest, None)
    if project_flag is not None and member_flag is None:
        # The project is named, so the free slot is the second value.
        return argparse.Namespace(
            **{**vars(args), "project_id": None, dest: positional})
    if member_flag is not None or project_flag is not None:
        # The member is named (the positional is already in the right slot), or both are and the
        # positional is a stray for `_merge` to refuse.
        return args

    raise SystemExit(
        f"{prog}: {positional!r} on its own could be either the project id or the {label}.\n"
        f"Give both, or name them:\n"
        f"    {forms[0]}\n    {forms[1]}\n"
        f"{_LIST_HINT}")


def _merge(*, prog: str, label: str, positional: str | None, flag: str | None, spelling: str,
           required: bool, forms: tuple[str, str], hint: str) -> str | None:
    """One value, two spellings. Refuses a disagreement rather than preferring either silently.

    `forms` is passed in rather than built from `label`, because the two spellings of one value are
    not the whole command line: a command with a second positional has to show both of them, or it
    offers a form that does not parse.
    """
    if positional is not None and flag is not None:
        if positional != flag:
            raise SystemExit(
                f"{prog}: two different values for the {label} — {positional!r} as an argument and "
                f"{flag!r} as {spelling}.\nGive it once, either way.")
        return positional
    settled = positional if positional is not None else flag
    if settled is None and required:
        raise SystemExit(f"{prog}: no {label} given.\nPass it either way:\n"
                         f"    {forms[0]}\n    {forms[1]}\n{hint}")
    return settled
