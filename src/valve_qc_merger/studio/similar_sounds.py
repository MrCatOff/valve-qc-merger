"""Sounds ▸ Find similar sounds: groups that could be one file, and the
sounds already shared."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from valve_qc_merger.project import Project
from valve_qc_merger.project import sounds as library
from valve_qc_merger.studio import audio, theme
from valve_qc_merger.studio import dialog_kit as kit
from valve_qc_merger.studio.icons import icon

LEVEL_TEXT = {"exact": "identical file", "same": "same sound, saved another way",
              "similar": "similar — listen first"}
LEVEL_TOKEN = {"exact": "success", "same": "success", "similar": "warning"}
ROLE_GROUP, ROLE_NAME = Qt.ItemDataRole.UserRole, Qt.ItemDataRole.UserRole + 1


class SimilarSoundsDialog(QDialog):
    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.groups: list = []
        self.kinds: dict[str, str] = {}
        self.keepers: dict[int, QComboBox] = {}
        self.setWindowTitle("Find similar sounds")
        layout = kit.dialog_layout(self)
        layout.addWidget(kit.header(
            "Find similar sounds", "Sounds that could be one file: identical copies, the "
            "same sound saved another way (rate, stereo, gain, silence), and similar ones. "
            "Sharing one changes no file — builds point the models' sound events at the "
            "kept sound; a stock sound the game already has costs nothing."))
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        found = QWidget()
        found_layout = QVBoxLayout(found)
        found_layout.setContentsMargins(4, 10, 4, 4)
        row = QHBoxLayout()
        self.similar_box = QCheckBox("Also look for similar (not identical) sounds")
        self.similar_box.setChecked(True)
        self.similar_box.toggled.connect(lambda _on: self.analyse())
        rescan = QPushButton(icon("refresh-cw"), "Search again")
        rescan.setAutoDefault(False)
        rescan.clicked.connect(self.analyse)
        row.addWidget(self.similar_box, 1)
        row.addWidget(rescan)
        found_layout.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Sound", "Match", "Played by", "Keep"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(3, 240)
        self.tree.setToolTip("Double-click a sound to play it; tick a group to share it")
        self.tree.itemDoubleClicked.connect(self._play)
        self.tree.itemChanged.connect(lambda *_a: self._update_summary())
        found_layout.addWidget(self.tree, 1)
        self.summary = QLabel()
        theme.set_role(self.summary, "success")
        bottom = QHBoxLayout()
        self.apply_button = QPushButton(icon("check", theme.TOKENS["on_accent"]),
                                        "Share the ticked groups")
        theme.set_primary(self.apply_button)
        self.apply_button.setAutoDefault(False)
        self.apply_button.clicked.connect(self.apply)
        bottom.addWidget(self.summary, 1)
        bottom.addWidget(self.apply_button)
        found_layout.addLayout(bottom)
        self.tabs.addTab(found, icon("search"), "Found")

        shared = QWidget()
        shared_layout = QVBoxLayout(shared)
        shared_layout.setContentsMargins(4, 10, 4, 4)
        shared_layout.addWidget(kit.hint("Sounds that play another one instead. Removing "
                                         "one gives the models their own file back."))
        self.shared_tree = QTreeWidget()
        self.shared_tree.setColumnCount(2)
        self.shared_tree.setHeaderLabels(["Sound", "Plays instead"])
        self.shared_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.shared_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        shared_layout.addWidget(self.shared_tree, 1)
        unshare = QPushButton(icon("x"), "Stop sharing the selected")
        unshare.setAutoDefault(False)
        unshare.clicked.connect(self.unshare)
        shared_layout.addWidget(unshare, 0, Qt.AlignmentFlag.AlignRight)
        self.tabs.addTab(shared, icon("layers"), "Shared")
        self.resize(900, 640)
        self.analyse()
        self._fill_shared()

    # -- found ------------------------------------------------------------------
    def analyse(self) -> None:
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.groups = library.find_similar(self.project,
                                               similar=self.similar_box.isChecked())
            texts = []
            from valve_qc_merger.project.qc_edit import qc_file
            for asset in self.project.assets:
                try:
                    texts.append(qc_file(self.project.asset_dir(asset))
                                 .read_text(encoding="latin-1"))
                except (OSError, ValueError):
                    continue
            self.kinds = library.precache_kinds(texts, self.project.settings.client_sounds,
                                                self.project.settings.sound_precache)
            self.users = library.sound_users(self.project)
        finally:
            QApplication.restoreOverrideCursor()
        self._fill()

    def _fill(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        self.keepers = {}
        for index, group in enumerate(self.groups):
            title = (f"{len(group.members)} sound(s)"
                     + (f" + stock {group.stock[0]}" if group.stock else ""))
            node = QTreeWidgetItem(self.tree, [title, f"{LEVEL_TEXT[group.level]} "
                                               f"({group.score:.0%})", "", ""])
            node.setData(0, ROLE_GROUP, index)
            node.setFlags(node.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            useful = len([m for m in group.members if self.played(m)]) > (
                0 if group.stock else 1)
            node.setCheckState(0, Qt.CheckState.Checked
                               if group.level != "similar" and useful
                               else Qt.CheckState.Unchecked)
            node.setForeground(1, theme.color(LEVEL_TOKEN[group.level]))
            for name in group.members + group.stock:
                stock = name in group.stock
                users = self.users.get(name.lower(), [])
                child = QTreeWidgetItem(node, [
                    name + ("  (stock)" if stock else ""), "",
                    f"{len(users)} model(s)" if users else
                    ("" if stock else "no model — your plugin names it"), ""])
                child.setData(0, ROLE_NAME, name)
                if users:
                    child.setToolTip(2, ", ".join(users))
                elif not stock:
                    child.setForeground(0, theme.color("muted"))
                    child.setForeground(2, theme.color("muted"))
                    child.setToolTip(2, "Sharing here changes only the sounds models play; "
                                     "point your plugin at the kept sound to drop this one")
            box = QComboBox()
            for name in group.stock + group.members:
                box.addItem(name + ("  (stock)" if name in group.stock else ""), name)
            box.setCurrentIndex(max(box.findData(group.keeper), 0))
            box.currentIndexChanged.connect(lambda _i: self._update_summary())
            self.tree.setItemWidget(node, 3, box)
            self.keepers[index] = box
            node.setExpanded(True)
        self.tree.blockSignals(False)
        self._update_summary()

    def played(self, name: str) -> bool:
        """A model plays it: sharing can change it (a plugin's own sound it cannot)."""
        return bool(self.users.get(name.lower()))

    def ticked(self) -> list[tuple[object, str]]:
        """(group, chosen keeper) of every ticked group."""
        out = []
        for row in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(row)
            if node.checkState(0) == Qt.CheckState.Checked:
                index = node.data(0, ROLE_GROUP)
                out.append((self.groups[index], self.keepers[index].currentData()))
        return out

    def savings(self) -> tuple[int, int, int]:
        """(files, precache_sound slots, generic slots) the ticked groups save."""
        files = sounds = generic = 0
        stock = library.stock_sounds() | set(map(library.sound_key,
                                                 library.stock_sound_files(self.project)))
        for group, keeper in self.ticked():
            dropped = [m for m in group.members
                       if m.lower() != keeper.lower() and self.played(m)]
            if library.sound_key(keeper) in stock:  # the game has the stock one already
                dropped = [m for m in group.members if self.played(m)]
            files += len(dropped)
            for name in dropped:
                kind = self.kinds.get(library.sound_key(name))
                sounds += kind == "sound"
                generic += kind == "generic"
        return files, sounds, generic

    def _update_summary(self) -> None:
        files, sounds, generic = self.savings()
        total = sum(g.saves for g in self.groups)
        self.summary.setText(
            f"Sharing the ticked groups saves {files} file(s): {sounds} precache_sound, "
            f"{generic} generic slot(s)" + (f" — {total} duplicate(s) in all, the rest named "
                                            "by plugins" if total != files else "")
            if self.groups else "Nothing to share: every sound is its own.")
        self.apply_button.setEnabled(bool(self.ticked()))

    def _play(self, item: QTreeWidgetItem, _column: int) -> None:
        name = item.data(0, ROLE_NAME)
        if not name:
            return
        path = library.resolve(self.project, name)
        if path is None:
            for group in self.groups:
                if name in group.stock:
                    path = library.stock_sound_files(self.project).get(name)
        if path is not None:
            audio.play(path)

    def apply(self) -> int:
        count = 0
        for group, keeper in self.ticked():
            members = [m for m in group.members if self.played(m)]
            count += len(library.share_sounds(self.project, members, keeper))
        self.summary.setText(f"{count} sound(s) now share — run the builds to apply")
        self.analyse()
        self._fill_shared()
        return count

    # -- shared -----------------------------------------------------------------
    def _fill_shared(self) -> None:
        self.shared_tree.clear()
        for name, target in sorted(self.project.settings.sound_aliases.items()):
            item = QTreeWidgetItem(self.shared_tree, [name, target])
            item.setData(0, ROLE_NAME, name)
        self.tabs.setTabText(1, f"Shared ({len(self.project.settings.sound_aliases)})")

    def unshare(self) -> None:
        names = [i.data(0, ROLE_NAME) for i in self.shared_tree.selectedItems()]
        if names:
            library.unshare_sounds(self.project, names)
            self._fill_shared()
            self.analyse()


__all__ = ["SimilarSoundsDialog"]
