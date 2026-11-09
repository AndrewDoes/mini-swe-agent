#!/usr/bin/env python3
"""Pre-pull the Docker images for a task list so the first run is not slowed (or timed out) by
image downloads, and so any Docker problem shows its real error message.

    python scripts/prepull.py --stage pilot
    python scripts/prepull.py --stage pilot --platform linux/amd64     # Apple Silicon
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from minisweagent.run.benchmarks.swebench import get_swebench_docker_image_name


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default=str(ROOT / "results" / "tasks.json"))
    ap.add_argument("--stage", choices=["pilot", "main"], required=True)
    ap.add_argument("--n-main", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0, help="optional cap on number of tasks to pull (e.g. 1 for a test run)")
    ap.add_argument("--platform", default="", help="e.g. linux/amd64")
    args = ap.parse_args()

    t = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    ids = t["pilot_ids"] if args.stage == "pilot" else t["main_ids_ordered"][: args.n_main]
    if args.limit > 0:
        ids = ids[: args.limit]
    failed = []
    for i, iid in enumerate(ids, 1):
        image = get_swebench_docker_image_name({"instance_id": iid})
        cmd = ["docker", "pull", *(["--platform", args.platform] if args.platform else []), image]
        print(f"[{i}/{len(ids)}] {' '.join(cmd)}", flush=True)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            failed.append(iid)
            print("    FAILED:", (r.stderr or r.stdout).strip()[:600], flush=True)
    print(f"done: {len(ids) - len(failed)}/{len(ids)} pulled" + (f"; failed: {failed}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()