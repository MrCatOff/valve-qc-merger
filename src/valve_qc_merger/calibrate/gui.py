"""Interactive weapon-offset + wrist/finger calibrator (retarget stage 2).

A human seats the weapon into OUR hands and corrects the wrist/fingers while a
live penetration readout shows how deep the hand is inside the weapon, then
exports the correction. The heavy lifting (skinning, penetration, tweaks) is in
:mod:`.scene`; this module is only the pyvista/Qt view and is imported lazily so
the core pipeline never needs the ``[calibrate]`` GUI extra.

Two backends:
    launch(scene, out_path)              # Qt: two views + Blender-style sidebar
    launch(scene, out_path, "vtk")       # bare pyvista window (sliders + hotkeys)
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from valve_qc_merger.calibrate.scene import CalibScene


def launch(scene: CalibScene, out_path: Path, backend: str = "qt") -> None:
    """Open the calibrator on ``scene``; write the correction to ``out_path`` on
    export. ``backend`` is "qt" (default, two views + sidebar) or "vtk" (bare
    pyvista window). Raises ImportError if the GUI extra is not installed."""
    if backend == "vtk":
        _run_vtk(scene, out_path)
    else:
        _run_qt(scene, out_path)


# --------------------------------------------------------------------------- vtk
def _run_vtk(scene: CalibScene, out_path: Path):
    import pyvista as pv

    LIVE_STRIDE = 4  # subsample hand verts for the live penetration estimate
    state = dict(anim=scene.anim_names()[0], frame=0, off=[0.0, 0.0, 0.0], pen={})
    pl = pv.Plotter(window_size=(1300, 900))
    pl.set_background("white")
    actors = {}
    txt = pl.add_text("", position="upper_left", font_size=11, name="hud")

    def hud():
        p = state["pen"]
        stale = "  (stale — press [p])" if state.get("pen_stale") else ""
        txt.SetText(0,
            f"anim {state['anim']}  frame {state['frame']}/{scene.frames(state['anim'])-1}\n"
            f"weapon offset  X {state['off'][0]:+.2f}  "
            f"Y {state['off'][1]:+.2f}  Z {state['off'][2]:+.2f}\n"
            f"PENETRATION  max {p.get('pen_max',0):.2f}u  sum {p.get('pen_sum',0):.1f}  "
            f"verts {p.get('pen_count',0)}{stale}\n"
            f"[p] update penetration   [e] export (exact)   [s] auto-seed")
        pl.render()

    def update_pen(stride=LIVE_STRIDE):
        s = scene.pose(state["anim"], state["frame"])
        state["pen"] = scene.penetration(s["base_weapon"], s["hand_points"],
                                         tuple(state["off"]), stride=stride)
        state["pen_stale"] = False

    def reskin():
        s = scene.pose(state["anim"], state["frame"])
        wpts, wf = s["weapon"]
        actors["weapon"] = pl.add_mesh(pv.PolyData(wpts, wf), color="#8899aa", name="weapon")
        actors["weapon"].SetPosition(*state["off"])
        hp, hf = s["hands"][0]  # first hand variant
        actors["hand"] = pl.add_mesh(pv.PolyData(hp, hf), color="#e8b48c",
                                     opacity=0.85, name="hand")
        update_pen()
        hud()

    # offset: move the weapon actor live every tick (cheap GPU transform); the
    # penetration is left stale until [p] (probing all verts is the only slow part).
    def set_off(axis):
        def cb(val):
            state["off"][axis] = val
            actors["weapon"].SetPosition(*state["off"])
            state["pen_stale"] = True
            hud()
        return cb
    for axis, label in ((0, "off X"), (1, "off Y"), (2, "off Z")):
        pl.add_slider_widget(set_off(axis), [-8.0, 8.0], value=0.0, title=label,
                             pointa=(0.02, 0.92 - axis * 0.11), pointb=(0.30, 0.92 - axis * 0.11),
                             style="modern", interaction_event="always")

    nf = scene.frames(state["anim"])
    if nf > 1:
        def set_frame(val):
            state["frame"] = int(round(val))
            reskin()
        pl.add_slider_widget(set_frame, [0, nf - 1], value=0, title="frame",
                             pointa=(0.02, 0.59), pointb=(0.30, 0.59),
                             style="modern", interaction_event="end")

    def export():
        update_pen(stride=1)  # exact figure for the record
        out_path.write_text(json.dumps({"weapon_offset": state["off"],
                                        "penetration": state["pen"]}, indent=1))
        rounded = [round(x, 2) for x in state["off"]]
        txt.SetText(0, f"exported weapon_offset={rounded} -> {out_path.name}")
        pl.render()

    def auto_seed():
        # nudge the weapon out along the palm normal by the current max penetration
        update_pen(stride=1)
        state["off"][1] -= state["pen"].get("pen_max", 0.0) * 0.6
        actors["weapon"].SetPosition(*state["off"])
        update_pen()
        hud()

    def probe_now():
        update_pen(stride=2)
        hud()

    pl.add_key_event("p", probe_now)
    pl.add_key_event("e", export)
    pl.add_key_event("s", auto_seed)
    reskin()
    pl.camera_position = "xz"
    pl.show()


# --------------------------------------------------------------------------- qt
def _run_qt(scene: CalibScene, out_path: Path):
    """Qt window: embedded VTK view + scrollable sidebar of Blender-style number
    fields (- [value] +). Weapon offset moves the actor live; finger bends re-skin
    the hand; penetration + export on buttons."""
    import pyvista as pv
    from PySide6 import QtWidgets
    from pyvistaqt import QtInteractor

    st = dict(anim=scene.anim_names()[0], frame=0, off=[0.0, 0.0, 0.0])

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    win = QtWidgets.QMainWindow()
    win.setWindowTitle("weapon/grip calibrator")
    central = QtWidgets.QWidget()
    win.setCentralWidget(central)
    hbox = QtWidgets.QHBoxLayout(central)

    # two independent views (left / right of the model), sharing one scene
    plotL = QtInteractor(central)
    plotL.set_background("white")
    plotR = QtInteractor(central)
    plotR.set_background("white")
    hbox.addWidget(plotL.interactor, stretch=2)
    hbox.addWidget(plotR.interactor, stretch=2)
    plots = [plotL, plotR]

    side = QtWidgets.QScrollArea()
    side.setWidgetResizable(True)
    side.setFixedWidth(340)
    panel = QtWidgets.QWidget()
    form = QtWidgets.QVBoxLayout(panel)
    side.setWidget(panel)
    hbox.addWidget(side, stretch=1)

    # --- render state (shared PolyData drawn in both views) ---
    hand_pd = weapon_pd = None
    weapon_actors: list = []
    pen_label = QtWidgets.QLabel("penetration: —")

    def repose():
        nonlocal hand_pd, weapon_pd
        s = scene.pose(st["anim"], st["frame"])
        wpts, wf = s["weapon"]
        hp, hf = s["hands"][0]
        if hand_pd is None:
            weapon_pd = pv.PolyData(wpts, wf)
            hand_pd = pv.PolyData(hp, hf)
            for pl, azim in ((plotL, 0), (plotR, 180)):  # opposite sides of the model
                weapon_actors.append(pl.add_mesh(weapon_pd, color="#8899aa", name="weapon"))
                pl.add_mesh(hand_pd, color="#e8b48c", opacity=0.9, name="hand")
                pl.camera_position = "xz"
                if azim:
                    pl.camera.Azimuth(azim)  # VTK method (capital); .azimuth is a float property
                pl.reset_camera()  # refit bounds; keeps the (rotated) view direction
        else:
            weapon_pd.points = wpts  # shared -> updates both views
            hand_pd.points = hp
        for wa in weapon_actors:
            wa.SetPosition(*st["off"])
        for pl in plots:
            pl.render()

    def update_pen(stride=2):
        s = scene.pose(st["anim"], st["frame"])
        p = scene.penetration(s["base_weapon"], s["hand_points"], tuple(st["off"]), stride=stride)
        pen_label.setText(f"penetration:  max {p['pen_max']:.2f}u   sum {p['pen_sum']:.1f}   "
                          f"verts {p['pen_count']}")
        return p

    # --- Blender-style number row: label  [-] [value] [+] ---
    def spin_row(label, lo, hi, step, val, on_change):
        row = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(QtWidgets.QLabel(label), stretch=2)
        minus = QtWidgets.QPushButton("−")
        minus.setFixedWidth(26)
        sp = QtWidgets.QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setSingleStep(step)
        sp.setDecimals(1 if step < 1 else 0)
        sp.setValue(val)
        sp.setFixedWidth(70)
        plus = QtWidgets.QPushButton("+")
        plus.setFixedWidth(26)
        minus.clicked.connect(lambda: sp.setValue(sp.value() - step))
        plus.clicked.connect(lambda: sp.setValue(sp.value() + step))
        sp.valueChanged.connect(on_change)
        for wdg in (minus, sp, plus):
            h.addWidget(wdg)
        return row, sp

    def group(title):
        g = QtWidgets.QGroupBox(title)
        v = QtWidgets.QVBoxLayout(g)
        form.addWidget(g)
        return v

    # --- animation ---
    gv = group("animation")
    combo = QtWidgets.QComboBox()
    combo.addItems(scene.anim_names())
    gv.addWidget(combo)
    frame_row = QtWidgets.QSpinBox()
    frame_row.setRange(0, scene.frames(st["anim"]) - 1)
    gv.addWidget(frame_row)

    def on_anim(name):
        st["anim"] = name
        st["frame"] = 0
        frame_row.setRange(0, scene.frames(name) - 1)
        frame_row.setValue(0)
        repose()
    def on_frame(f):
        st["frame"] = int(f)
        repose()
    combo.currentTextChanged.connect(on_anim)
    frame_row.valueChanged.connect(on_frame)

    # --- weapon offset (moves the actor live) ---
    gv = group("weapon offset")
    off_spins = []
    def off_cb(axis):
        def cb(val):
            st["off"][axis] = float(val)
            for wa in weapon_actors:
                wa.SetPosition(*st["off"])
            for pl in plots:
                pl.render()
        return cb
    for ax, lbl in ((0, "X"), (1, "Y"), (2, "Z")):
        row, sp = spin_row(lbl, -8.0, 8.0, 0.1, 0.0, off_cb(ax))
        off_spins.append(sp)
        gv.addWidget(row)

    # --- wrist + finger controls, driven by the GEOMETRICALLY discovered hands
    # (works on our ValveBiped rig and foreign CSO/Valve rigs alike) ---
    def chan_cb(bones, channel):  # set one anatomical channel on each bone, keep the rest
        def cb(val):
            for b in bones:
                cur = list(scene.tweaks.get(b, (0.0, 0.0, 0.0)))
                cur[channel] = float(val)
                scene.set_tweak(b, cur[0], cur[1], cur[2])
            repose()
        return cb

    for hand in scene.hand_struct:
        gv = group(f"{'left' if hand['side'] == 'L' else 'right'} hand — wrist + fingers")
        wb = hand["wrist"]
        gv.addWidget(spin_row("wrist TWIST", -60.0, 60.0, 5.0, 0.0, chan_cb([wb], 2))[0])
        gv.addWidget(spin_row("wrist bend", -40.0, 40.0, 5.0, 0.0, chan_cb([wb], 0))[0])
        for lbl, chain in hand["fingers"]:
            gv.addWidget(spin_row(f"{lbl} curl", -50.0, 90.0, 5.0, 0.0,
                                  chan_cb(tuple(chain), 0))[0])
            gv.addWidget(spin_row(f"{lbl} spread", -30.0, 30.0, 5.0, 0.0,
                                  chan_cb((chain[0],), 1))[0])

    # --- actions ---
    gv = group("actions")
    gv.addWidget(pen_label)

    def do_seed():
        new_off, p = scene.auto_seed(st["anim"], st["frame"], list(st["off"]))
        for i, sp in enumerate(off_spins):
            sp.setValue(new_off[i])  # fires off_cb -> moves weapon actor
        pen_label.setText(f"auto-seed:  max {p['pen_max']:.2f}u   sum {p['pen_sum']:.1f}   "
                          f"verts {p['pen_count']}   off={[round(x,2) for x in new_off]}")
    b_seed = QtWidgets.QPushButton("auto-seed (push weapon out of palm)")
    b_seed.clicked.connect(do_seed)
    gv.addWidget(b_seed)

    b_pen = QtWidgets.QPushButton("update penetration")
    b_pen.clicked.connect(lambda: update_pen())
    b_exp = QtWidgets.QPushButton("export offset + tweaks")
    def do_export():
        p = update_pen(stride=1)
        out_path.write_text(json.dumps({"weapon_offset": st["off"], "tweaks": scene.tweaks,
                                        "penetration": p}, indent=1))
        pen_label.setText(f"exported -> {out_path.name}   (max {p['pen_max']:.2f}u)")
    b_exp.clicked.connect(do_export)
    for b in (b_pen, b_exp):
        gv.addWidget(b)
    form.addStretch(1)

    repose()
    update_pen()
    win.resize(1800, 950)
    win.show()
    if os.environ.get("CALIB_BUILDTEST"):
        app.processEvents()
        print("BUILDTEST ok: window + sidebar constructed, repose+pen ran")
        return
    app.exec()


__all__ = ["launch"]
