"""Which released clients a conformance run tests (`conformance/clients.py`).

grid-platform's `conformance.yml` runs `GRID_CONFORMANCE_CLIENTS=field` on every pull request and `all` once a week, so
`IN_THE_FIELD` decides what a PR proves. The newest release must always be in it: a release left out would be tested
only by the weekly run, days after the change that broke it merged.
"""
import pytest

from conformance import clients


@pytest.fixture(autouse=True)
def _pin(monkeypatch):
    """A fixed harness pin: no manifest read, and `harness_pin`'s cache never carries one test's pin into another."""
    monkeypatch.setenv("GRID_HARNESS_PIN", "0.3.59")
    clients.harness_pin.cache_clear()
    yield
    clients.harness_pin.cache_clear()


def test_the_field_is_releases_only_and_holds_the_newest():
    assert set(clients.IN_THE_FIELD) <= set(clients.RELEASED)
    assert max(clients.RELEASED, key=clients.parse) in clients.IN_THE_FIELD
    assert len(clients.IN_THE_FIELD) < len(clients.RELEASED)  # the control: a field equal to RELEASED saves nothing


def test_unset_is_every_release(monkeypatch):
    monkeypatch.delenv("GRID_CONFORMANCE_CLIENTS", raising=False)
    assert clients.released() == clients.RELEASED


@pytest.mark.parametrize("name, expected", [("all", clients.RELEASED), ("field", clients.IN_THE_FIELD)])
def test_a_named_set_is_that_set(monkeypatch, name, expected):
    monkeypatch.setenv("GRID_CONFORMANCE_CLIENTS", name)
    assert clients.released() == expected


@pytest.mark.parametrize("name", ["feild", "FIELD", "none"])
def test_a_misspelt_set_fails_rather_than_running_every_release(monkeypatch, name):
    monkeypatch.setenv("GRID_CONFORMANCE_CLIENTS", name)
    with pytest.raises(RuntimeError, match="GRID_CONFORMANCE_CLIENTS"):
        clients.released()


def test_the_harness_pin_runs_once_whether_or_not_the_set_holds_it(monkeypatch):
    monkeypatch.setenv("GRID_CONFORMANCE_CLIENTS", "field")
    assert clients.versions() == list(clients.IN_THE_FIELD)  # 0.3.59 is in the field: not added twice
    monkeypatch.setenv("GRID_HARNESS_PIN", "0.3.47")
    clients.harness_pin.cache_clear()
    assert clients.versions() == [*clients.IN_THE_FIELD, "0.3.47"]  # a pin PROD did not see still runs
