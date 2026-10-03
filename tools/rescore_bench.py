"""Re-score an existing benchmark work folder with the current metric.

    python tools/rescore_bench.py tmp/cso_nexon --work tmp/bench/work --out tmp/bench_A
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from valve_qc_merger.handswap.bench import compare, is_native, write_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    weapons = sorted({qc.parent for qc in args.corpus.rglob("*.qc")
                      if qc.parent.name.startswith("v_") and is_native(qc.parent)})
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    for done, weapon in enumerate(weapons, 1):
        for mode in ("identity", "roundtrip"):
            result_dir = args.work / mode / weapon.name
            if result_dir.is_dir():
                try:
                    results.append(compare(weapon, result_dir, mode))
                except Exception as exc:  # noqa: BLE001 - keep scoring the corpus
                    print(f"{weapon.name} {mode}: {exc}", flush=True)
        if done % 20 == 0:
            print(f"[{done}/{len(weapons)}]", flush=True)
    write_report(results, args.out / "report")
    print(f"-> {args.out / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
