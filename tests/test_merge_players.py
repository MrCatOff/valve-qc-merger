"""merge-players tests: grouping, sub-bone collapse + prune, placeholder, e2e.

Fixtures are built programmatically into ``tmp_path`` (a full CS 1.6 donor ships
641 SMDs; a synthetic 5-bone donor exercises every code path). A CSO body adds a
non-donor sub-bone (collapsed) and omits the donor's mouth ``Bone01`` (pruned as
a vertexless leaf, so its ``$controller`` is dropped).
"""

from __future__ import annotations

from pathlib import Path

from valve_qc_merger.cli import main
from valve_qc_merger.merge_players.discovery import (
    PlayerModel,
    load_donor,
    load_player_body,
)
from valve_qc_merger.merge_players.grouping import group_models
from valve_qc_merger.merge_players.sequences import PLACEHOLDER_STEM, build_sequences
from valve_qc_merger.merge_players.skeleton import reduce_body
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import (
    BonePose,
    Frame,
    Node,
    Smd,
    Triangle,
    Vertex,
)
from valve_qc_merger.writers.smd import write_smd_file

ORIGIN = Vector3(0.0, 0.0, 0.0)
NORMAL = Vector3(0.0, 0.0, 1.0)
UV = Vector2(0.0, 0.0)


def _skel(nodes: list[tuple[int, str, int]], frames: int = 1) -> tuple[list[Node], list[Frame]]:
    node_objs = [Node(i, name, parent) for i, name, parent in nodes]
    frame_objs = [
        Frame(t, tuple(BonePose(i, Vector3(float(i), 0.0, 0.0), ORIGIN) for i, _, _ in nodes))
        for t in range(frames)
    ]
    return node_objs, frame_objs


def _tri(material: str, bones: tuple[int, int, int], base: float) -> Triangle:
    return Triangle(material, tuple(  # type: ignore[arg-type]
        Vertex(b, Vector3(base + k, float(b), 0.0), NORMAL, UV)
        for k, b in enumerate(bones)
    ))


def _write_smd(path: Path, nodes, tris=None, frames=1) -> None:
    n, f = _skel(nodes, frames)
    write_smd_file(Smd(nodes=n, frames=f, triangles=list(tris or [])), path)


# 5-bone donor rig: Bip01 / Spine / Head / R Hand / Bone01 (mouth, leaf).
_DONOR_NODES = [
    (0, "Bip01", -1), (1, "Bip01 Spine", 0), (2, "Bip01 Head", 1),
    (3, "Bip01 R Hand", 0), (4, "Bone01", 2),
]
_DONOR_QC = """$modelname "donor.mdl"
$bodygroup "studio" { studio "donor" }
$flags 0
$attachment 0 "Bip01 R Hand" 1 2 3
$hbox 1 "Bip01 Head" -1 -1 -1 1 1 1
$controller Mouth "Bone01" ZR 0 30
$sequence "idle" {
\t"anims/idle"
\tfps 15
\tloop
}
$sequence "walk" {
\t"anims/walk"
\tfps 30
\tloop
}
$sequence "ref_aim_shieldgun" {
\t"anims/ref_aim_shieldgun"
\tfps 20
\tloop
}
$sequence "I_am_a_stupid_placeholder" {
\t"anims/I_am_a_stupid_placeholder"
\tfps 30
\tloop
}
""".replace("\\t", "\t")


def _make_donor(root: Path) -> Path:
    d = root / "donor"
    (d / "anims").mkdir(parents=True)
    # Donor body weights every bone incl the mouth (so donor is self-consistent).
    _write_smd(d / "donor.smd", _DONOR_NODES,
               [_tri("skin.bmp", (0, 1, 2), 0.0), _tri("skin.bmp", (2, 3, 4), 5.0)])
    (d / "skin.bmp").write_bytes(b"BM")  # placeholder texture bytes
    for seq in ("idle", "walk", "ref_aim_shieldgun", "I_am_a_stupid_placeholder"):
        _write_smd(d / "anims" / f"{seq}.smd", _DONOR_NODES, frames=2)
    (d / "donor.qc").write_text(_DONOR_QC)
    return d


def _make_cso(root: Path, name: str, *, sub_bone: bool = True) -> Path:
    """A CSO body: core bones + optional non-donor sub-bone; NO mouth Bone01."""
    d = root / "nexon" / name
    d.mkdir(parents=True)
    nodes = [(0, "Bip01", -1), (1, "Bip01 Spine", 0), (2, "Bip01 Head", 1),
             (3, "Bip01 R Hand", 0)]
    tris = [_tri(f"{name}.bmp", (0, 1, 2), 0.0), _tri(f"{name}.bmp", (2, 3, 3), 5.0)]
    if sub_bone:
        nodes.append((4, "Chest_Sub", 1))  # child of Spine, carries verts
        tris.append(_tri(f"{name}.bmp", (4, 4, 1), 9.0))
    _write_smd(d / f"{name}.smd", nodes, tris)
    (d / f"{name}.bmp").write_bytes(b"BM")
    (d / f"{name}.qc").write_text(
        f'$modelname "{name}.mdl"\n$bodygroup "studio" {{ studio "{name}" }}\n'
        '$hbox 1 "Bip01 Head" -1 -1 -1 1 1 1\n'
        '$sequence "cso_extra" { "cso/cso_extra" fps 30 }\n'
    )
    return d


# --------------------------------------------------------------------------- #
def test_reduce_body_collapses_subbones_and_prunes_leaves(tmp_path):
    donor = load_donor(_make_donor(tmp_path))
    _make_cso(tmp_path, "trooper")
    model = load_player_body(tmp_path / "nexon" / "trooper")
    mesh = model.body_meshes[0]
    before = {(v.position, v.normal) for t in mesh.triangles for v in t.vertices}

    collapsed = reduce_body(mesh, donor)

    assert collapsed == ["Chest_Sub"]
    names = {n.name for n in mesh.nodes}
    assert "Chest_Sub" not in names  # sub-bone collapsed
    assert names <= {name for name, _ in donor.table}  # only donor bones remain
    # Positions/normals never move (only bone bindings change).
    after = {(v.position, v.normal) for t in mesh.triangles for v in t.vertices}
    assert after == before
    # Every leaf is weighted (no vertexless leaf studiomdl would prune).
    used = {v.bone for t in mesh.triangles for v in t.vertices}
    has_child = {n.parent for n in mesh.nodes}
    assert all(n.index in has_child or n.index in used for n in mesh.nodes)


def test_build_sequences_voids_shields_keeps_order(tmp_path):
    donor = load_donor(_make_donor(tmp_path))
    out = tmp_path / "out"
    plan = build_sequences(donor, out, placeholder_globs=("*shield*",))

    assert plan.names == ["idle", "walk", "ref_aim_shieldgun", "I_am_a_stupid_placeholder"]
    assert plan.placeholdered == ["ref_aim_shieldgun"]
    # The voided slot points at the placeholder; kept slots copied their SMDs.
    joined = "\n".join(plan.qc_lines)
    assert f'"anims/{PLACEHOLDER_STEM}"' in joined
    assert (out / "anims" / "idle.smd").exists()
    assert (out / "anims" / f"{PLACEHOLDER_STEM}.smd").exists()


def test_group_by_sex_splits_female_male(tmp_path):
    def pm(name: str) -> PlayerModel:
        return PlayerModel(name, tmp_path, tmp_path / "x.qc", "", [], [],
                           hitbox_sig="sig", proportion_sig="p", height=69.0)

    groups = dict(group_models(
        [pm("gign"), pm("marinegirl"), pm("terror")], mode="sex"))
    assert set(groups) == {"male", "female"}
    assert [m.name for m in groups["female"]] == ["marinegirl"]
    assert sorted(m.name for m in groups["male"]) == ["gign", "terror"]


def test_end_to_end_merge_passes_gate(tmp_path):
    donor = _make_donor(tmp_path)
    _make_cso(tmp_path, "trooper")
    _make_cso(tmp_path, "raider", sub_bone=False)
    out = tmp_path / "out"

    code = main([
        "merge-players", str(tmp_path / "nexon"), "--base", str(donor),
        "--out", str(out), "--name", "pl", "--group-by", "size",
    ])
    assert code == 0  # gate passed for every part

    qcs = list(out.rglob("pl_*.qc"))
    assert qcs, "no QC emitted"
    qc = qcs[0].read_text()
    # Canonical sequence order preserved; mouth controller dropped (Bone01 pruned).
    assert qc.index('"idle"') < qc.index('"walk"') < qc.index('"ref_aim_shieldgun"')
    assert "$controller" not in qc
    assert '$bodygroup "body0"' in qc


def test_max_skins_splits_into_parts(tmp_path):
    donor = _make_donor(tmp_path)
    for name in ("a_body", "b_body", "c_body"):
        _make_cso(tmp_path, name)
    out = tmp_path / "out"

    code = main([
        "merge-players", str(tmp_path / "nexon"), "--base", str(donor),
        "--out", str(out), "--name", "pl", "--group-by", "size", "--max-skins", "1",
    ])
    assert code == 0
    assert len(list(out.rglob("pl_*.qc"))) >= 3  # one part per skin
