"""Round-trip benchmark of the hand retarget over a corpus of native weapons.

    python tools/retarget_bench.py tmp/cso_nexon --out tmp/bench [--donor DIR]

Every view model wearing the CSO 2009 hand set (our hands) is retargeted to
ours directly (identity) and through foreign hands (default: the old Valve
hands of the corpus' classic v_deagle) and back; <out>/report.md / .json give
the per-weapon segment error (see valve_qc_merger.handswap.bench).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from valve_qc_merger.handswap.bench import is_native, run_weapon, write_report
from valve_qc_merger.handswap.foreign import asset_from_weapon, write_asset


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("corpus", type=Path, help="folder searched for decompiled view models")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--donor", type=Path, help="weapon whose hands are the foreign hands "
                    "(default: the corpus' v_deagle)")
    ap.add_argument("--only", nargs="*", default=[], help="weapon names to run")
    args = ap.parse_args(argv)

    weapons = sorted({qc.parent for qc in args.corpus.rglob("*.qc")
                      if qc.parent.name.startswith("v_") and is_native(qc.parent)})
    if args.only:
        weapons = [w for w in weapons if w.name in set(args.only)]
    donor = args.donor or next(p.parent for p in args.corpus.rglob("v_deagle.qc"))
    args.out.mkdir(parents=True, exist_ok=True)
    asset = write_asset(asset_from_weapon(str(donor)), str(args.out / "foreign_hands.json.gz"))
    texture = next((str(p) for p in donor.glob("*.[bB][mM][pP]") if "hand" in p.name.lower()),
                   None)
    print(f"{len(weapons)} native weapons; foreign hands from {donor.name}", flush=True)
    results = []
    started = time.time()
    for done, weapon in enumerate(weapons, 1):
        pair = run_weapon(weapon, asset, args.out / "work", texture)
        results.extend(pair)
        trip = next((r for r in pair if r.mode == "roundtrip"), None)
        note = trip.error if trip and trip.error else (f"{trip.mean:.3f}" if trip else "?")
        print(f"[{done}/{len(weapons)}] {weapon.name}: {note}", flush=True)
        if done % 10 == 0 or done == len(weapons):
            write_report(results, args.out / "report")
    print(f"done in {time.time() - started:.0f} s -> {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
