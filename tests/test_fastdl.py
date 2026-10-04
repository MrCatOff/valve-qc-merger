"""FastDL: every file of a folder on the mirror, with its size."""

from __future__ import annotations

import functools
import os
import threading
from collections.abc import Iterator
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from valve_qc_merger.server import fastdl


class _NoHead(SimpleHTTPRequestHandler):
    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self.send_error(405)

    def log_message(self, *_args) -> None:  # noqa: ANN002
        pass


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:  # noqa: ANN002
        pass


def _serve(folder: Path, handler: type) -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 functools.partial(handler, directory=str(folder)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/cstrike"


@pytest.fixture
def folders(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    local = tmp_path / "local"
    mirror = tmp_path / "www" / "cstrike"
    for root in (local, mirror):
        (root / "models").mkdir(parents=True)
        (root / "sound" / "weapons").mkdir(parents=True)
    (local / "models" / "v_ak47.mdl").write_bytes(b"x" * 100)
    (local / "models" / "v_m4a1.mdl").write_bytes(b"x" * 50)
    (local / "sound" / "weapons" / "ak 1.wav").write_bytes(b"x" * 10)
    (local / "cs_custom.wad").write_bytes(b"x" * 7)
    (mirror / "models" / "v_ak47.mdl").write_bytes(b"x" * 100)
    (mirror / "models" / "v_m4a1.mdl").write_bytes(b"x" * 40)  # stale
    (mirror / "sound" / "weapons" / "ak 1.wav").write_bytes(b"x" * 10)  # space: quoted
    yield local, mirror


def test_file_url() -> None:
    assert fastdl.file_url("http://h/cs/", "sound/a b.wav") == "http://h/cs/sound/a%20b.wav"
    assert fastdl.file_url("http://h/cs", "models\\x.mdl") == "http://h/cs/models/x.mdl"


@pytest.mark.parametrize("handler", [_Quiet, _NoHead])
def test_check_against_a_mirror(folders: tuple[Path, Path], handler: type) -> None:
    local, mirror = folders
    server, base = _serve(mirror.parent, handler)
    try:
        files = fastdl.local_files(local)
        assert "cs_custom.wad" in files  # top-level WADs are downloads too
        results = fastdl.check(base, files, workers=4)
    finally:
        server.shutdown()
    states = {r.path: r.state for r in results}
    assert states == {"cs_custom.wad": "missing", "models/v_m4a1.mdl": "size",
                      "models/v_ak47.mdl": "ok", "sound/weapons/ak 1.wav": "ok"}
    assert results[0].state == "missing"  # worst first
    stale = next(r for r in results if r.state == "size")
    assert (stale.local_size, stale.remote_size) == (50, 40)
    assert fastdl.summary(results) == {"missing": 1, "size": 1, "error": 0, "ok": 2}
    text = fastdl.report(base, results)
    assert "[missing] cs_custom.wad" in text and "[ok]" not in text


def test_unreachable_mirror() -> None:
    result = fastdl.check_one("http://127.0.0.1:9", "models/x.mdl", 1, timeout=2)
    assert result.state == "error" and "no answer" in result.detail


QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_fastdl_tab(folders: tuple[Path, Path], tmp_path: Path) -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from valve_qc_merger.project import Project
    from valve_qc_merger.studio.server_window import ServerWindow
    local, mirror = folders
    server, base = _serve(mirror.parent, _Quiet)
    (local / "server.cfg").write_text(f'hostname "x"\nsv_downloadurl "{base}/"\n',
                                      encoding="utf-8")
    project = Project.create(tmp_path / "pack")
    project.settings.game_dir = str(local)
    window = ServerWindow(project)
    try:
        panel = window.fastdl
        assert panel.url_edit.text() == f"{base}/"  # from server.cfg
        results = panel.run(workers=2)
        assert len(results) == 4 and panel.table.rowCount() == 2  # problems only
        assert "✕ 1 missing" in panel.summary_label.text()
        panel.problems_box.setChecked(False)
        assert panel.table.rowCount() == 4
        panel.url_edit.setText("ftp://nope")
        assert panel.run() == [] and "http://" in panel.summary_label.text()
    finally:
        server.shutdown()
        window.close()
