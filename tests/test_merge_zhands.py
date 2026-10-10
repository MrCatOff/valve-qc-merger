"""merge-zhands: zombie knife/grenade hands -> one model, shared grenade."""

from __future__ import annotations

import configparser
from pathlib import Path

from valve_qc_merger.merge_view.discovery import load_model
from valve_qc_merger.merge_zhands.merger import merge_zhands
from valve_qc_merger.models.geometry import Vector2, Vector3
from valve_qc_merger.models.smd import BonePose, Frame, Node, Smd, Triangle, Vertex
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.writers.smd import write_smd_text

ZERO = Vector3(0.0, 0.0, 0.0)


def _tri(material: str, bone: int, x: float) -> Triangle:
    def v(dx: float, dy: float) -> Vertex:
        return Vertex(bone=bone, position=Vector3(x + dx, dy, 0.0),
                      normal=Vector3(0.0, 0.0, 1.0), uv=Vector2(dx, dy))
    return Triangle(material, (v(0, 0), v(1, 0), v(0, 1)))


def _write_model(root: Path, name: str, *, grenade: bool, hand_root: str,
                 render: str = "") -> None:
    # Hands: <hand_root> -> Bone_Lefthand; grenade rig Bone_Root under the hand.
    directory = root / name
    (directory / "anims").mkdir(parents=True)
    nodes = [Node(0, hand_root, -1), Node(1, "Bone_Lefthand", 0)]
    poses = [BonePose(0, ZERO, ZERO), BonePose(1, Vector3(5.0, 0.0, 0.0), ZERO)]
    triangles = [_tri("hand.bmp", 1, 0.0)]
    if grenade:
        nodes.append(Node(2, "Bone_Root", 1))
        poses.append(BonePose(2, Vector3(1.0, 0.0, 0.0), ZERO))
        triangles.append(_tri("frogbomb.bmp", 2, 20.0))
    mesh = Smd(nodes=nodes, frames=[Frame(0, tuple(poses))], triangles=triangles)
    (directory / "ref.smd").write_text(write_smd_text(mesh), encoding="latin-1")
    moved = [BonePose(p.bone, Vector3(p.position.x, p.position.y + 1.0, 0.0), ZERO)
             for p in poses]
    anim = Smd(nodes=nodes, frames=[Frame(0, tuple(poses)), Frame(1, tuple(moved))])
    (directory / "anims" / "idle.smd").write_text(write_smd_text(anim),
                                                  encoding="latin-1")
    for texture in ("hand.bmp", "frogbomb.bmp"):
        (directory / texture).write_bytes(b"BM" + texture.encode())
    qc = [f"$modelname {name}.mdl", render, '$body studio "ref"',
          '$sequence idle {', '    "./anims/idle"', "    fps 30", "    loop", "}"]
    (directory / f"{name}.qc").write_text("\n".join(qc) + "\n", encoding="latin-1")


def test_knife_and_grenade_share_hands_and_one_grenade(tmp_path: Path) -> None:
    src = tmp_path / "in"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    _write_model(src, "v_alpha_knife_invisible", grenade=False,
                 hand_root="Bone01", render="$texrendermode hand.bmp additive")
    models = [load_model(d) for d in sorted(src.iterdir())]
    out = tmp_path / "out"
    report = merge_zhands(models, out, "v_zh")

    assert all(passed for _c, passed, _d in report.gate), report.gate
    # knife + grenade hands are one mesh; the additive variant is its own entry
    assert report.hands == ["alpha", "alpha_knife_invisible"]
    qc = (out / "v_zh.qc").read_text(encoding="latin-1")
    assert '$bodygroup "grenade"' in qc and '\tstudio "grenade/grenade"' in qc
    assert '$texrendermode "hand_additive.bmp" additive' in qc
    assert "\tloop" in qc
    ini = configparser.ConfigParser()
    ini.read(out / "models.ini")
    assert qc.index('$bodygroup "grenade"') < qc.index('$bodygroup "hands"')
    assert ini["v_alpha_knife"]["pev_body"] == "0"
    assert ini["v_alpha_grenade"]["pev_body"] == "1"  # grenade is the low bit
    assert ini["v_alpha_knife_invisible"]["pev_body"] == "2"  # hands 1 * 2
    # the grenade subtree is namespaced so it can't collide with hand roots
    grenade = parse_smd_file(out / "grenade" / "grenade.smd")
    assert "gren_root" in {n.name for n in grenade.nodes}


def test_grenade_root_name_clash_with_a_hand_root(tmp_path: Path) -> None:
    # One rig's HAND root is called Bone_Root (CSO ghost knife) while every
    # grenade rig roots the frog at Bone_Root too: they must stay distinct.
    src = tmp_path / "in"
    _write_model(src, "v_beta_knife", grenade=False, hand_root="Bone_Root")
    _write_model(src, "v_beta_grenade", grenade=True, hand_root="Bone01")
    models = [load_model(d) for d in sorted(src.iterdir())]
    report = merge_zhands(models, tmp_path / "out", "v_zh")
    assert all(passed for _c, passed, _d in report.gate), report.gate
    grenade = parse_smd_file(tmp_path / "out" / "grenade" / "grenade.smd")
    names = {n.index: n.name for n in grenade.nodes}
    parent = {n.name: names.get(n.parent) for n in grenade.nodes}
    assert parent["gren_root"] == "Bone_Lefthand"
    assert parent["Bone_Root"] is None  # beta knife's hand root, untouched


def test_split_keeps_a_zombies_models_together(tmp_path: Path) -> None:
    from valve_qc_merger.merge_view.discovery import load_model
    from valve_qc_merger.merge_zhands.merger import planned_bones, split_zombies
    src = tmp_path / "src"
    for name, grenade in (("v_alpha_knife", False), ("v_alpha_grenade", True),
                          ("v_beta_knife", False), ("v_beta_grenade", True)):
        _write_model(src, name, grenade=grenade, hand_root="Bone01")
    models = [load_model(src / n) for n in ("v_alpha_knife", "v_alpha_grenade",
                                            "v_beta_knife", "v_beta_grenade")]
    one = planned_bones(models[:2])
    assert planned_bones(models) == one  # one rig: the bones are shared
    parts = split_zombies(models, limit=one - 1)  # nothing fits: a zombie a part
    assert [[m.name for m in part] for part in parts] == [
        ["v_alpha_knife", "v_alpha_grenade"], ["v_beta_knife", "v_beta_grenade"]]
    assert len(split_zombies(models)) == 1


def test_project_build_stages_claws_under_zhands_names(tmp_path: Path) -> None:
    import configparser

    from valve_qc_merger.project import Build, Project
    from valve_qc_merger.services.base import CollectingReporter
    src = tmp_path / "src"
    _write_model(src, "v_smoker", grenade=False, hand_root="Bone01")  # not v_<z>_knife
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    project = Project.create(tmp_path / "pack")
    project.import_decompiled(src, kind="zhands")
    project.add_build(Build("zh", "merge-zhands"))
    reporter = CollectingReporter()
    result = project.run_build("zh", reporter)
    assert result.outputs, reporter.lines
    manifest = configparser.ConfigParser()
    manifest.read(project.build_dir("zh") / "output" / "models.ini")
    assert "v_smoker" in manifest and "v_smoker_knife" not in manifest
    assert any("v_smoker: no grenade" in n or "v_smoker_grenade" in n
               for n in reporter.lines + result.warnings)
