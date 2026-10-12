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
                 render: str = "", mouth: float | None = None) -> None:
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
        if mouth is not None:  # a second grenade bone: where it sits is the rig
            nodes.append(Node(3, "Bone_Mouth", 2))
            poses.append(BonePose(3, Vector3(mouth, 0.0, 0.0), ZERO))
            triangles.append(_tri("frogbomb.bmp", 3, 30.0))
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
    # the hands group leads with a blank: pev_body 0 draws nothing
    assert '$bodygroup "hands"\n{\n\tblank' in qc
    # grenade group {blank, grenade, blank}: body 2 draws nothing too (the
    # client draws it by itself on a knife hit), stride 3
    assert '$bodygroup "grenade"\n{\n\tblank\n\tstudio "grenade/grenade"\n\tblank\n}' in qc
    assert ini["v_alpha_knife"]["pev_body"] == "3"
    assert ini["v_alpha_grenade"]["pev_body"] == "4"  # grenade is the low digit
    assert ini["v_alpha_knife_invisible"]["pev_body"] == "6"  # hands (1 + 1) * 3
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
    # the hand bones are pooled into slots: the frog hangs off the left-hand
    # slot, under the hand root's slot — never the hand root itself
    hand = parent["gren_root"]
    assert hand is not None and not hand.startswith("gren_")
    assert parent[hand] is not None and parent[parent[hand]] is None


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


def test_grenades_of_different_rigs_get_an_entry_each(tmp_path: Path) -> None:
    # v_boomer_grenade: the shared frog mesh drawn by another rig's animation
    # was torn apart — each grenade rig is its own entry of the grenade group
    src = tmp_path / "in"
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01", mouth=1.0)
    _write_model(src, "v_beta_grenade", grenade=True, hand_root="Bone01", mouth=3.0)
    _write_model(src, "v_gamma_grenade", grenade=True, hand_root="Bone01", mouth=1.0)
    models = [load_model(d) for d in sorted(src.iterdir())]
    out = tmp_path / "out"
    report = merge_zhands(models, out, "v_zh")
    assert all(passed for _c, passed, _d in report.gate), report.gate
    qc = (out / "v_zh.qc").read_text(encoding="latin-1")
    assert '\tstudio "grenade/grenade"' in qc and '\tstudio "grenade/grenade2"' in qc
    assert '\tstudio "grenade/grenade3"' not in qc  # alpha and gamma share a rig
    # grenade group {blank, rig 1, blank, rig 2} is the low digit: stride 4
    hands = {stem: (body // 4) - 1 for stem, body in report.pev_body.items()}
    grenade = {stem: body % 4 for stem, body in report.pev_body.items()}
    assert grenade == {"v_alpha_grenade": 1, "v_beta_grenade": 3, "v_gamma_grenade": 1}
    assert min(hands.values()) >= 0


def test_one_frog_for_all_redraws_the_other_rigs(tmp_path: Path) -> None:
    # beta's frog sits on another rig (its mouth bone elsewhere): with one frog
    # for all its grenade animation is redrawn for alpha's frog — one entry
    from valve_qc_merger.merge_zhands.merger import canonical_grenade
    src = tmp_path / "in"
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01", mouth=1.0)
    _write_model(src, "v_beta_grenade", grenade=True, hand_root="Bone01", mouth=3.0)
    _write_model(src, "v_gamma_grenade", grenade=True, hand_root="Bone01", mouth=1.0)
    models = [load_model(d) for d in sorted(src.iterdir())]
    frog = canonical_grenade(models, "frogbomb", "frogbomb.bmp")
    assert frog is not None and frog.name in ("v_alpha_grenade", "v_gamma_grenade")
    out = tmp_path / "out"
    report = merge_zhands(models, out, "v_zh", frog=frog, grenade_skins=[])
    assert all(passed for _c, passed, _d in report.gate), report.gate
    qc = (out / "v_zh.qc").read_text(encoding="latin-1")
    assert '\tstudio "grenade/grenade"' in qc and "grenade2" not in qc
    assert any("redrawn" in w for w in report.warnings)
    # grenade group {blank, frog, blank}: every grenade model shows entry 1
    assert {stem: body % 3 for stem, body in report.pev_body.items()} == {
        "v_alpha_grenade": 1, "v_beta_grenade": 1, "v_gamma_grenade": 1}


def test_grenade_skins_are_entries_of_their_own(tmp_path: Path) -> None:
    src = tmp_path / "in"
    _write_model(src, "v_alpha_grenade", grenade=True, hand_root="Bone01")
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    skin = tmp_path / "Red Frog.bmp"
    skin.write_bytes(b"BM red")
    models = [load_model(d) for d in sorted(src.iterdir())]
    out = tmp_path / "out"
    report = merge_zhands(models, out, "v_zh", grenade_skins=[str(skin)])
    qc = (out / "v_zh.qc").read_text(encoding="latin-1")
    assert '\tstudio "grenade/grenade_red_frog"' in qc
    skinned = (out / "grenade" / "grenade_red_frog.smd").read_text(encoding="latin-1")
    assert "Red_Frog.bmp" in skinned or "red_frog" in skinned.lower()
    entry = report.manifest["v_alpha_grenade"]
    # grenade group {blank, frog, blank, red frog}: stride 4
    assert entry["pev_body"] % 4 == 1 and entry["pev_body_red_frog"] % 4 == 3
    assert entry["pev_body_red_frog"] // 4 == entry["pev_body"] // 4  # the same hands
    assert "pev_body_red_frog" not in report.manifest["v_alpha_knife"]


def test_a_mirrored_frog_is_drawn_on_our_frog() -> None:
    # v_china's frog is ours in a mirror: paired across (our left half with
    # its right), it is drawn on our own frog by rotations — no extra frog
    from valve_qc_merger.merge_zhands.frog import can_redraw, redraw_grenade

    def frog(side: float) -> Smd:
        # left/right twins share a UV; a lopsided part (unique UVs) on one side
        twins = [(1.0, 0.0, 0.0), (2.0, 1.0, 0.0), (1.5, 0.0, 1.0), (1.0, 2.0, 0.5)]
        lopsided = [(0.3, 0.0, 0.0), (0.5, 1.0, 0.0), (0.3, 0.5, 1.5), (0.4, 2.0, 0.2)]

        def vertex(x: float, y: float, z: float, uv: float) -> Vertex:
            return Vertex(0, Vector3(side * x, y, z), Vector3(0.0, 0.0, 1.0),
                          Vector2(uv, uv))

        triangles = []
        for n, (x, y, z) in enumerate(twins):
            uv = n / 10
            triangles.append(Triangle("frogbomb.bmp", (
                vertex(x, y, z, uv), vertex(-x, y, z, uv), vertex(0.0, y + 3.0, z, 0.5 + uv))))
        for n, (x, y, z) in enumerate(lopsided):
            uv = 0.9 + n / 100
            triangles.append(Triangle("frogbomb.bmp", (
                vertex(x, y, z, uv), vertex(x, y + 0.1, z, uv + 0.001),
                vertex(x, y, z + 0.1, uv + 0.002))))
        return Smd(nodes=[Node(0, "gren_root", -1)],
                   frames=[Frame(0, (BonePose(0, ZERO, ZERO),))], triangles=triangles)

    ours, theirs = frog(1.0), frog(-1.0)
    assert can_redraw(ours, theirs, "gren_")
    anim = Smd(nodes=list(theirs.nodes), frames=list(theirs.frames), triangles=[])
    assert redraw_grenade(ours, theirs, {"idle": anim}, "gren_") is not None


def test_knives_alone_keep_body_two_blank(tmp_path: Path) -> None:
    # no grenades: the hands are the low group, {blank, h0, blank, h1}
    src = tmp_path / "in"
    _write_model(src, "v_alpha_knife", grenade=False, hand_root="Bone01")
    _write_model(src, "v_beta_knife", grenade=False, hand_root="Bone01",
                 render="$texrendermode hand.bmp additive")
    models = [load_model(d) for d in sorted(src.iterdir())]
    report = merge_zhands(models, tmp_path / "out", "v_zh")
    qc = (tmp_path / "out" / "v_zh.qc").read_text(encoding="latin-1")
    hands = qc.split('$bodygroup "hands"\n{\n')[1].split("}")[0].split("\n")
    assert hands[0] == hands[2] == "\tblank"
    assert sorted(report.pev_body.values()) == [1, 3]
