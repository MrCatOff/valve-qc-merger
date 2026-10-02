"""canonicalize as a service: bring a view model's bones to the canonical rig.

The model keeps its OWN hands; only the skeleton changes — the same step
merge-v applies to every input (docs/merge-v.md, "The pipeline"), written out
as a standalone decompiled model: hand bones renamed to the reference names,
the shared ``Bip01`` root added, the reference parentage enforced, ``*Nub``
bones removed. Every edit is FK-exact and checked by the pose gate.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from valve_qc_merger.merge_view.canonicalize import canonicalize_model
from valve_qc_merger.merge_view.discovery import (
    MergeViewError,
    _resolve_smd,
    load_model,
    sanitize_model_dir,
)
from valve_qc_merger.merge_view.hands import (
    collision_guard,
    hand_bone_names,
    load_reference_rig,
    match_hands,
)
from valve_qc_merger.parsers.smd import parse_smd_file
from valve_qc_merger.resources import resource_path
from valve_qc_merger.retarget.config import DEFAULT_REFERENCE
from valve_qc_merger.retarget.correspondence import CorrespondenceError
from valve_qc_merger.services.base import (
    EXIT_DISCOVERY,
    EXIT_FAIL,
    EXIT_OK,
    Reporter,
    ServiceResult,
)
from valve_qc_merger.writers.smd import write_smd_file

POSE_TOLERANCE = 1e-4


@dataclass
class CanonicalizeOptions:
    model_dir: Path
    out: Path
    reference: Path | None = None  # None: merge-v's default reference hands
    prune: bool = False


def run_canonicalize(opts: CanonicalizeOptions,
                     reporter: Reporter | None = None) -> ServiceResult:
    """Copy ``opts.model_dir`` to ``opts.out`` with a canonical skeleton."""
    reporter = reporter or Reporter()
    result = ServiceResult()
    if not opts.model_dir.is_dir():
        reporter.log(f"error: model dir not found: {opts.model_dir}")
        result.exit_code = EXIT_DISCOVERY
        return result
    reference_path = resource_path(opts.reference or Path(DEFAULT_REFERENCE))
    try:
        reference = load_reference_rig(reference_path)
    except (OSError, CorrespondenceError) as exc:
        reporter.log(f"error: reference hands unusable: {exc}")
        result.exit_code = EXIT_DISCOVERY
        return result

    out = opts.out
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(opts.model_dir, out)
    sanitize_model_dir(out)
    try:
        model = load_model(out, require_anims=False)
        fullest = max(model.meshes.values(), key=lambda m: len(m.nodes))
        include = hand_bone_names(model.meshes, model.bodygroups)
        match = match_hands(fullest, reference, include)
        conflicts = collision_guard(fullest, match.renames)
        if conflicts:
            raise CorrespondenceError(f"rename collisions: {'; '.join(conflicts)}")
    except (MergeViewError, CorrespondenceError, ValueError) as exc:
        shutil.rmtree(out, ignore_errors=True)
        reporter.log(f"error: {exc}")
        result.failures.append(str(exc))
        result.exit_code = EXIT_DISCOVERY
        return result

    canon = canonicalize_model(model, match, parse_smd_file(reference_path).nodes,
                               prune=opts.prune)
    if canon.max_pose_deviation > POSE_TOLERANCE:
        shutil.rmtree(out, ignore_errors=True)
        message = f"pose NOT preserved (deviation {canon.max_pose_deviation:.6f}u)"
        reporter.log(f"error: {message}")
        result.failures.append(message)
        result.exit_code = EXIT_FAIL
        return result

    # Write every SMD back where the QC already points, then the QC itself.
    for stem, smd in model.meshes.items():
        write_smd_file(smd, _resolve_smd(out, stem))
    for seq in model.sequences:
        if seq.smd is not None and seq.name in model.anims:
            write_smd_file(model.anims[seq.name], _resolve_smd(out, seq.smd))
    model.qc_path.write_text(model.qc_text, encoding="latin-1")

    reporter.log(f"  {model.name}: {canon.renamed} bone(s) renamed, "
                 f"{len(canon.reparented)} reparented, {len(canon.nubs_removed)} Nub(s) "
                 f"removed, {len(canon.grafted)} grafted, pose deviation "
                 f"{canon.max_pose_deviation:.2e}u")
    for warning in canon.warnings:
        reporter.log(f"  warn: {warning}")
    result.warnings.extend(canon.warnings)
    result.outputs.append(model.qc_path)
    result.data["canonical"] = {
        "renamed": canon.renamed, "reparented": canon.reparented,
        "nubs_removed": canon.nubs_removed, "grafted": canon.grafted,
        "pruned": canon.pruned, "max_pose_deviation": canon.max_pose_deviation,
    }
    result.exit_code = EXIT_OK
    return result


__all__ = ["CanonicalizeOptions", "run_canonicalize"]
