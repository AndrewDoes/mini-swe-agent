#!/usr/bin/env python3
"""Draw the pilot and main task lists from SWE-bench Verified (seeded, disjoint).

The full shuffled order is saved. Pilot = first `pilot_size` ids. The main run
takes a prefix of the remaining ids, so its final size can be chosen *after* the
pilot without changing which tasks come first (replication = seed + size).

    python scripts/select_tasks.py --config config/experiment.yaml --out results/tasks.json
"""
import argparse
import hashlib
import json
import random
import re
import time
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "experiment.yaml"))
    ap.add_argument("--out", default=str(ROOT / "results" / "tasks.json"))
    ap.add_argument("--pilot-size", type=int, default=10)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    from datasets import load_dataset

    ds = load_dataset(cfg["dataset"]["name"], split=cfg["dataset"]["split"])
    rows = list(ds)
    inc = cfg.get("inclusion") or {}
    if inc.get("difficulty"):
        rows = [r for r in rows if r.get("difficulty") in inc["difficulty"]]
    if inc.get("exclude_regex"):
        rows = [r for r in rows if not re.search(inc["exclude_regex"], r["instance_id"])]

    ids = sorted(r["instance_id"] for r in rows)          # canonical order before shuffling
    order = ids.copy()
    random.Random(cfg["seed"]).shuffle(order)

    pilot, main_ordered = order[: args.pilot_size], order[args.pilot_size:]
    assert not set(pilot) & set(main_ordered), "pilot and main must be disjoint"

    out = {
        "seed": cfg["seed"],
        "dataset": cfg["dataset"],
        "inclusion": inc,
        "n_pool": len(ids),
        "pool_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pilot_ids": pilot,
        "main_ids_ordered": main_ordered,
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"pool={len(ids)} pilot={len(pilot)} main_candidates={len(main_ordered)} -> {args.out}")
    print("pilot ids:", *pilot, sep="\n  ")


if __name__ == "__main__":
    main()
