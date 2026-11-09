#!/usr/bin/env python3
"""Evaluate final patches with the hidden tests (Procedure step 7 & Section III-I).

Builds one predictions file per (policy, repeat) from result.json files, runs the
official swebench harness on each, and writes <out>/eval/resolved.json containing:
  * "<stage>:<policy>_rep<k>"           -> [resolved instance ids]
  * "<stage>:<policy>_rep<k>:regressed" -> [instance ids that broke PASS_TO_PASS tests]

    python scripts/evaluate.py --stage pilot --max-workers 4
"""
import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _find_regressed_ids(run_id: str, model_name: str, instance_ids: list[str]) -> list[str]:
    """Inspect per-instance swebench report.json files for PASS_TO_PASS failures (regressions)."""
    safe_model = model_name.replace("/", "__")
    regressed = []
    for base in (Path("logs/run_evaluation"), ROOT / "logs" / "run_evaluation"):
        for iid in instance_ids:
            rp = base / run_id / safe_model / iid / "report.json"
            if rp.exists():
                try:
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    p2p_fail = (
                        data.get(iid, {})
                        .get("tests_status", {})
                        .get("PASS_TO_PASS", {})
                        .get("failure", [])
                    )
                    if p2p_fail and iid not in regressed:
                        regressed.append(iid)
                except Exception:
                    pass
    return sorted(regressed)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "experiment.yaml"))
    ap.add_argument("--stage", choices=["pilot", "main"], required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--max-workers", type=int, default=4)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else (ROOT / "results" / cfg["experiment"])
    eval_dir = out / "eval"
    eval_dir.mkdir(parents=True, exist_ok=True)

    groups: dict[str, list[dict]] = defaultdict(list)
    for f in sorted((out / args.stage).glob("*/*/rep*/result.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        groups[f"{r['policy']}_rep{r['rep']}"].append(r)

    resolved_path = eval_dir / "resolved.json"
    resolved = json.loads(resolved_path.read_text(encoding="utf-8")) if resolved_path.exists() else {}
    for name, runs in groups.items():
        key = f"{args.stage}:{name}"
        preds_path = eval_dir / f"{args.stage}_{name}.jsonl"
        with preds_path.open("w", encoding="utf-8") as f:
            for r in runs:
                f.write(json.dumps({"instance_id": r["instance_id"], "model_name_or_path": name,
                                    "model_patch": r["patch"]}) + "\n")
        run_id = f"{cfg['experiment']}_{args.stage}"
        iids = [r["instance_id"] for r in runs]
        cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
               "--dataset_name", cfg["dataset"]["name"], "--split", cfg["dataset"]["split"],
               "--predictions_path", str(preds_path), "--max_workers", str(args.max_workers),
               "--run_id", run_id, "--report_dir", str(eval_dir),
               "--instance_ids", *iids]
        print("running:", " ".join(cmd[:8]), "...", flush=True)
        subprocess.run(cmd, check=True)
        report = json.loads((eval_dir / f"{name.replace('/', '__')}.{run_id}.json").read_text(encoding="utf-8"))
        resolved[key] = sorted(report.get("resolved_ids", []))
        resolved[f"{key}:regressed"] = _find_regressed_ids(run_id, name, iids)
        print(f"{key}: resolved {len(resolved[key])}/{len(runs)}, "
              f"regressed {len(resolved[f'{key}:regressed'])}/{len(runs)}")

    resolved_path.write_text(json.dumps(resolved, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
