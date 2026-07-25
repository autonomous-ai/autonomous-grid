"""Repro (temporary): after a PASSING prove_candidate, does the node still answer <name>?"""
from __future__ import annotations

import httpx

from tests.test_train_prove import OneSlotEngine, setup  # noqa: F401
from train.evaluate import prove_candidate


def test_pass_leaves_customer_name_404(setup):
    engine = OneSlotEngine()
    engine.weights, engine.name = "night-1", "support-v1"   # what customers use today
    transport = httpx.MockTransport(engine.handler)

    with httpx.Client(transport=transport, base_url="http://mac.local") as c:
        r = c.post("/v1/completions", json={"model": "support-v1", "prompt": "hi"})
        print("BEFORE CHECK:", r.status_code, r.text)
        assert r.status_code == 200

    result = prove_candidate(setup["cfg"], setup["run_dir"], setup["adapter"]("night-2"),
                             "support-v1", transport=transport)
    print("PASSED:", result["passed"], "delta", result["delta"])
    print("verdict:", result["verdict"])
    print("engine holds weights=%r under name=%r" % (engine.weights, engine.name))
    print("result keys:", sorted(result))

    with httpx.Client(transport=transport, base_url="http://mac.local") as c:
        r = c.post("/v1/completions", json={"model": "support-v1", "prompt": "hi"})
        print("AFTER CHECK :", r.status_code, r.text)
    assert r.status_code == 404
