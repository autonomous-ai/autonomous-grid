import asyncio

import pytest

from local.inflight import InflightTable, TransactionState


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_claim_is_exclusive_and_only_matches_advertised_models():
    clock = FakeClock()
    table = InflightTable(clock=clock)
    txn = table.create(model="m1", body=b"{}", is_stream=False)

    assert table.claim(node_id="n1", models=("other",)) is None
    first = table.claim(node_id="n1", models=("m1",))
    assert first is not None and first.id == txn.id
    assert first.state is TransactionState.CLAIMED
    assert table.claim(node_id="n2", models=("m1",)) is None


def test_publish_after_finish_is_ignored_not_an_error():
    table = InflightTable(clock=FakeClock())
    txn = table.create(model="m1", body=b"{}", is_stream=True)
    table.claim(node_id="n1", models=("m1",))
    table.finish(txn.id, b"done")
    assert table.publish(txn.id, b"late chunk") is False


def test_sweep_expires_a_claim_that_never_reported():
    clock = FakeClock()
    table = InflightTable(clock=clock, result_deadline=60.0)
    txn = table.create(model="m1", body=b"{}", is_stream=False)
    table.claim(node_id="n1", models=("m1",))
    clock.now += 61.0
    expired = table.sweep()
    assert [item.id for item in expired] == [txn.id]
    assert table.get(txn.id) is None


@pytest.mark.asyncio
async def test_consumer_receives_chunks_in_order_then_the_terminal_result():
    table = InflightTable(clock=FakeClock())
    txn = table.create(model="m1", body=b"{}", is_stream=True)
    table.claim(node_id="n1", models=("m1",))
    table.publish(txn.id, b"a")
    table.publish(txn.id, b"b")
    table.finish(txn.id, None)
    received = [chunk async for chunk in table.stream(txn.id)]
    assert received == [b"a", b"b"]


def test_cancel_after_finish_evicts_the_transaction():
    table = InflightTable(clock=FakeClock())
    txn = table.create(model="m1", body=b"{}", is_stream=False)
    table.claim(node_id="n1", models=("m1",))
    table.finish(txn.id, b"result")
    table.cancel(txn.id, "consumer finished")
    assert table.get(txn.id) is None


@pytest.mark.asyncio
async def test_stream_on_unknown_id_yields_nothing():
    table = InflightTable(clock=FakeClock())
    received = [chunk async for chunk in table.stream("does-not-exist")]
    assert received == []
