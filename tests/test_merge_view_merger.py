"""merge-v sequence labels: what studiomdl and the file system both take."""

from __future__ import annotations

from types import SimpleNamespace

from valve_qc_merger.merge_view.merger import _sanitize_label, _unique_sequence_names


def test_labels_of_another_code_page_become_ascii() -> None:
    assert _sanitize_label("��� KakTyc�") == "____KakTyc_"
    assert _sanitize_label('a "b"') == "a__b_"
    assert _sanitize_label("") == "seq"
    assert _sanitize_label("idle1") == "idle1"


def test_unique_names_keep_the_original_key() -> None:
    models = [SimpleNamespace(name="v_a", anims={"ф idle": 0, "draw": 0}),
              SimpleNamespace(name="v_b", anims={"draw": 0})]
    names = _unique_sequence_names(models)  # type: ignore[arg-type]
    assert names[("v_a", "ф idle")] == "__idle"
    assert names[("v_a", "draw")] == "draw"
    assert names[("v_b", "draw")] == "v_b__draw"


def test_repeated_labels_of_a_long_model_name_fit_the_paths() -> None:
    long = "v_bloodhunter_left_d3a3_hands"
    models = [SimpleNamespace(name="v_a", anims={"shoot_left_empty": 0}),
              SimpleNamespace(name=long, anims={"shoot_left_empty": 0})]
    final = _unique_sequence_names(models)[(long, "shoot_left_empty")]  # type: ignore[arg-type]
    assert final.endswith("__shoot_left_empty")  # the label stays readable
    assert len(f"{long}/{final}.smd") <= 64 and len(final) <= 31


def test_repeated_labels_keep_every_sequence_in_order() -> None:
    from valve_qc_merger.merge_view.discovery import _unique_names
    from valve_qc_merger.retarget.qc_build import QcSequence
    seqs = [QcSequence(name, None, (), smd=f"a/{i}") for i, name in
            enumerate(["idle", "reload", "Reload", "reload", "draw"])]
    assert [s.name for s in _unique_names(seqs)] == ["idle", "reload", "Reload_2",
                                                     "reload_3", "draw"]
    assert [s.smd for s in _unique_names(seqs)] == [s.smd for s in seqs]
