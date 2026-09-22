"""The eleven sentences this CLI prints for a bare 404 from a project or task route.

A bare framework 404 is what an OLD relay (one that predates the route) and a deliberately FROZEN
one (the hosted fleet since PRD `grid-scale-phase-a` issue 09 — grid-src does not mount the task
plane's routers) both answer. From here the two are indistinguishable, so the sentence has to name
both and assert neither.

⚠️ **The old wording asserted the wrong one, twice.** It said the relay *predates* the feature and
told the user to *ask its operator to update it* — on a frozen grid the relay is current, and
"update it" sends somebody to make the grid MORE frozen, not less. Every sentence below was written
before the freeze existed and was true then; this file is what stops them being "fixed" back to
that confident form, one at a time, by somebody reading only the old-relay case.
"""

import re

import pytest

from remote import relay

#: Every missing-route sentence in `remote/relay.py`, by name, so a twelfth is caught by the count.
SENTENCES = {
    name: getattr(relay, name)
    for name in dir(relay)
    if name.startswith("_OLD_RELAY") and isinstance(getattr(relay, name), str)
}


def test_there_are_exactly_eleven_and_this_file_knows_them():
    assert set(SENTENCES) == {
        "_OLD_RELAY", "_OLD_RELAY_NO_ARCHIVE", "_OLD_RELAY_NO_CANCEL", "_OLD_RELAY_NO_COMMIT",
        "_OLD_RELAY_NO_LEAVE", "_OLD_RELAY_NO_READS", "_OLD_RELAY_NO_RENAME", "_OLD_RELAY_NO_SEND",
        "_OLD_RELAY_NO_STREAM", "_OLD_RELAY_NO_UNDO", "_OLD_RELAY_NO_VISIBILITY",
    }, "a sentence was added or removed — teach this file about it"


@pytest.mark.parametrize("name", sorted(SENTENCES))
def test_each_sentence_names_the_frozen_case_and_asserts_neither(name):
    text = SENTENCES[name]
    # names BOTH cases …
    assert "either" in text, f"{name} asserts one cause where two are possible"
    assert "switched off" in text, f"{name} does not name the frozen-plane case"
    # … and sends nobody to "update" a relay that may be current and deliberately so
    assert not re.search(r"update (it|the relay)", text), f"{name} still says to update the relay"
    assert "Ask its operator" in text, f"{name} lost the one remedy that is right in both cases"


def test_the_general_sentence_is_the_only_one_that_says_the_grid_serves_no_projects():
    """`_OLD_RELAY` covers a relay with NO project routes. Its ten siblings exist because their
    routes arrived later than the project routes: a relay missing only `cancel` plainly HAS
    projects, and saying otherwise sends somebody to check a feature that is working. The tests in
    `test_local_cli.py` pin that distinction by this phrase, so it must stay unique to `_OLD_RELAY`."""
    assert "does not serve projects" in relay._OLD_RELAY
    for name, text in SENTENCES.items():
        if name != "_OLD_RELAY":
            assert "does not serve projects" not in text, name
