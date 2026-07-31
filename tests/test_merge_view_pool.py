"""merge-view bone pooling and bodygroup collapse tests (spec §3.6-3.7)."""

from __future__ import annotations

from valve_qc_merger.merge_view.bonepool import plan_pool


def test_pool_reuses_slots_across_models_and_keeps_one_parent_per_slot() -> None:
    shared = {"Bip01", "Bip01 R Hand"}
    models = {
        "big": {"Bip01": None, "Bip01 R Hand": "Bip01",
                "gun": "Bip01 R Hand", "slide": "gun", "mag": "gun"},
        "small": {"Bip01": None, "Bip01 R Hand": "Bip01",
                  "frame": "Bip01 R Hand", "cyl": "frame"},
    }
    plan = plan_pool(models, shared)

    # Big model shapes the pool (3 slots); small reuses two of them.
    assert plan.size == 3
    big, small = plan.assignments["big"], plan.assignments["small"]
    assert big["gun"] == small["frame"]      # same parent key: Bip01 R Hand
    assert small["cyl"] in {big["slide"], big["mag"]}  # under the gun slot
    # One parent per slot, by construction.
    assert plan.slot_parent[big["gun"]] == "Bip01 R Hand"
    assert plan.slot_parent[small["cyl"]] == big["gun"]
    # A model never uses one slot twice.
    assert len(set(big.values())) == len(big)
    assert len(set(small.values())) == len(small)


def test_pool_grows_only_when_no_compatible_slot_is_free() -> None:
    shared = {"Bip01"}
    models = {
        "a": {"Bip01": None, "x": "Bip01", "y": "Bip01"},   # two siblings
        "b": {"Bip01": None, "p": "Bip01"},                 # one: reuses
        "c": {"Bip01": None, "q": "Bip01", "r": "Bip01", "s": "Bip01"},  # three
    }
    plan = plan_pool(models, shared)
    assert plan.size == 3  # max sibling fan-out under one parent key
    for name in ("a", "b", "c"):
        slots = plan.assignments[name].values()
        assert len(set(slots)) == len(list(slots))
