"""The Map Workshop's state, Qt-free: the open maps, each with its check
report and its plan of changes (undo / redo), nothing written until Apply."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from valve_qc_merger.mapkit import bsp as B
from valve_qc_merger.mapkit.analyze import MapReport, check_map, game_folder
from valve_qc_merger.mapkit.fix import FixPlan, Operation, SaveResult, next_name, save_plan
from valve_qc_merger.mapkit.rules import Rules
from valve_qc_merger.server.entities import parse


@dataclass
class MapState:
    path: Path
    report: MapReport | None = None
    error: str = ""
    plan: FixPlan = field(init=False)
    _undo: list[list[Operation]] = field(default_factory=list, repr=False)
    _redo: list[list[Operation]] = field(default_factory=list, repr=False)
    _bsp: B.Bsp | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.plan = FixPlan(self.path)

    @property
    def name(self) -> str:
        return self.path.stem

    @property
    def bsp(self) -> B.Bsp:
        if self._bsp is None:
            self._bsp = B.Bsp.read(self.path)
        return self._bsp

    def entities(self) -> list[dict[str, str]]:
        """The map's entities as read."""
        return parse(self.bsp.entity_text)

    def planned_entities(self) -> list[dict[str, str]]:
        """The entities with the plan applied (what Apply would write)."""
        return self.plan.apply(self.entities())

    # -- the plan, undoable ----------------------------------------------------

    def _snapshot(self) -> None:
        self._undo.append(copy.copy(self.plan.operations))
        self._redo.clear()

    def add(self, operations: list[Operation]) -> None:
        if not operations:
            return
        self._snapshot()
        self.plan.operations = self.plan.operations + list(operations)

    def remove(self, operations: list[Operation]) -> None:
        drop = {id(op) for op in operations}
        if not drop:
            return
        self._snapshot()
        self.plan.operations = [op for op in self.plan.operations if id(op) not in drop]

    def set_enabled(self, operation: Operation, enabled: bool) -> None:
        if operation.enabled == enabled:
            return
        self._snapshot()
        operation.enabled = enabled

    def clear(self) -> None:
        if self.plan.operations:
            self._snapshot()
            self.plan.operations = []

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        if self._undo:
            self._redo.append(copy.copy(self.plan.operations))
            self.plan.operations = self._undo.pop()

    def redo(self) -> None:
        if self._redo:
            self._undo.append(copy.copy(self.plan.operations))
            self.plan.operations = self._redo.pop()

    def output_name(self, out_dir: Path | None = None) -> str:
        """The name Apply writes: the map's, or the next ``_sp`` one."""
        if not self.plan.needs_new_name:
            return self.name
        return next_name((out_dir or game_folder(self.path)) / "maps", self.name)


@dataclass
class WorkshopState:
    folders: list[Path] = field(default_factory=list)  # the base game (Game install)
    rules: Rules = field(default_factory=Rules)
    maps: dict[Path, MapState] = field(default_factory=dict)
    out_dir: Path | None = None  # write into another game folder
    ent_file: bool = False  # a ReHLDS .ent instead of the BSP

    def open(self, path: Path) -> MapState:
        path = Path(path).resolve()
        state = self.maps.get(path)
        if state is None:
            state = self.maps[path] = MapState(path)
            self.check(state)
        return state

    def close(self, path: Path) -> None:
        self.maps.pop(Path(path).resolve(), None)

    def check(self, state: MapState) -> MapReport | None:
        try:
            state._bsp = None
            state.report = check_map(state.path, self.folders, self.rules)
            state.error = ""
        except (B.BspError, OSError) as exc:
            state.report, state.error = None, str(exc)
        return state.report

    def apply(self, state: MapState) -> SaveResult:
        """Write the plan (see :func:`fix.save_plan`), then start a fresh
        plan on the written map and check it."""
        result = save_plan(state.plan, out_dir=self.out_dir, ent=self.ent_file,
                           folders=self.folders)
        target = (self.out_dir or game_folder(state.path)) / "maps" / f"{result.map_name}.bsp"
        if target.resolve() != state.path:
            self.maps.pop(state.path, None)
            state = self.open(target)
        else:
            state.plan = FixPlan(state.path)
            state._undo.clear()
            state._redo.clear()
            self.check(state)
        return result


__all__ = ["MapState", "WorkshopState"]
