#!/usr/bin/env python3
"""Run both policies on the selected tasks (Procedure steps 3-6).

    # pilot (10 tasks), then inspect cost/variability and freeze parameters
    python scripts/run_experiment.py --stage pilot

    # main run on the first N non-pilot tasks, with repeats
    python scripts/run_experiment.py --stage main --n-main 50 --repeats 2 --workers 4

Every (task, policy, repeat) runs in its own fresh Docker container started from
the same SWE-bench image, so neither policy sees the other's changes. Jobs are
interleaved (both policies of a task run back to back, in seeded random order)
so that if the global budget runs out, both arms are equally covered.
The run is resumable: finished runs (result.json present) are skipped.
"""
import argparse
import json
import random
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv()
sys.path.insert(0, str(ROOT / "src"))

from minisweagent.config import get_config_from_spec
from minisweagent.models import get_model
from minisweagent.run.benchmarks.swebench import DEFAULT_CONFIG_FILE, get_sb_environment
from minisweagent.utils.serialize import recursive_merge

from swe_policy_eval.cost_tracker import CostTracker, GlobalBudgetExceeded, TrackedModel
from swe_policy_eval.policies import build_policy
from swe_policy_eval.rounds_agent import RoundsAgent

STOP = threading.Event()


def build_config(cfg: dict, run_dir: Path) -> dict:
    base = get_config_from_spec(str(DEFAULT_CONFIG_FILE))
    lim = cfg["limits"]
    model_cfg = {"model_name": cfg["model"]["name"], "model_kwargs": cfg["model"].get("kwargs", {})}
    if cfg["model"].get("class"):
        model_cfg["model_class"] = cfg["model"]["class"]      # e.g. "openrouter"
    override = {
        "model": model_cfg,
        "agent": {
            "step_limit": lim["step_ceiling"] + 10,     # safety net only; policies enforce the ceiling
            "cost_limit": lim["per_run_cap_usd"] + 1.0,   # backstop only; CostTracker is the authority
            "output_path": run_dir / "trajectory.json",
        },
    }
    if cfg.get("environment"):
        override["environment"] = cfg["environment"]          # e.g. pull_timeout, run_args
    merged = recursive_merge(base, override)
    if cfg["model"].get("class", "").startswith("openrouter"):
        # mini's default swebench config sets litellm-only kwargs; OpenRouterModel posts
        # model_kwargs straight into the JSON payload, so drop them.
        merged["model"]["model_kwargs"].pop("drop_params", None)
    return merged


def run_one(job: dict, cfg: dict, instances: dict, tracker: CostTracker, out: Path) -> None:
    if STOP.is_set():
        return
    iid, pol, rep, stage = job["instance_id"], job["policy"], job["rep"], job["stage"]
    run_id = f"{stage}/{pol}/{iid}/rep{rep}"
    run_dir = out / stage / pol / iid / f"rep{rep}"
    if (run_dir / "result.json").exists():
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "error.json").unlink(missing_ok=True)

    config = build_config(cfg, run_dir)
    policy = build_policy(pol, cfg["policies"][pol])
    env, t0 = None, time.time()
    try:
        env = get_sb_environment(config, instances[iid])            # fresh container
        model = TrackedModel(get_model(config=config["model"]), tracker, run_id)
        agent = RoundsAgent(
            model, env, policy=policy, tracker=tracker, run_id=run_id,
            rounds_log_path=run_dir / "rounds.jsonl",
            run_cost_cap=cfg["limits"]["per_run_cap_usd"],
            step_ceiling=cfg["limits"]["step_ceiling"],
            **config["agent"],
        )
        res = agent.run_policy(instances[iid]["problem_statement"])
        res |= {"instance_id": iid, "policy": pol, "rep": rep, "stage": stage,
                "run_id": run_id, "wall_secs": time.time() - t0, "policy_params": policy.describe(),
                "rounds_log": agent.round_logs}
        (run_dir / "result.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"[done] {run_id}: {res['stop_reason']} rounds={res['rounds']} "
              f"cost=${res['cost_usd']:.3f} patch={'yes' if res['patch'].strip() else 'EMPTY'}", flush=True)
    except GlobalBudgetExceeded as e:
        STOP.set()
        print(f"[budget] {e}; stopping the experiment", flush=True)
    except Exception as e:   # infrastructure error: retried on resume
        (run_dir / "error.json").write_text(json.dumps(
            {"run_id": run_id, "error": repr(e), "stderr": getattr(e, "stderr", None),
             "stdout": getattr(e, "stdout", None), "traceback": traceback.format_exc()}, indent=2), encoding="utf-8")
        if getattr(e, "stderr", None):
            print(f"        docker/stderr: {str(e.stderr).strip()[:500]}", flush=True)
        print(f"[error] {run_id}: {e!r}", flush=True)
    finally:
        if env is not None:
            try:
                env.cleanup()
            except Exception:
                pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "experiment.yaml"))
    ap.add_argument("--tasks", default=str(ROOT / "results" / "tasks.json"))
    ap.add_argument("--stage", choices=["pilot", "main"], required=True)
    ap.add_argument("--n-main", type=int, default=0, help="size of the main subset (prefix of the ordered list)")
    ap.add_argument("--limit", type=int, default=0, help="optional cap on number of tasks to run (e.g. 1 for a quick test run)")
    ap.add_argument("--repeats", type=int, default=None)
    ap.add_argument("--policies", nargs="+", default=["fixed", "controller"])
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--out", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    tasks = json.loads(Path(args.tasks).read_text(encoding="utf-8"))
    assert tasks["seed"] == cfg["seed"], "tasks.json was drawn with a different seed than the config"
    ids = tasks["pilot_ids"] if args.stage == "pilot" else tasks["main_ids_ordered"][: args.n_main]
    if args.limit > 0:
        ids = ids[: args.limit]
    if args.stage == "main" and not ids:
        sys.exit("--n-main must be > 0 for the main stage")
    repeats = args.repeats or (1 if args.stage == "pilot" else cfg.get("repeats", 1))
    out = Path(args.out) if args.out else (ROOT / "results" / cfg["experiment"])

    jobs = []
    for rep in range(repeats):
        for iid in ids:
            order = list(args.policies)
            random.Random(f"{cfg['seed']}-{rep}-{iid}").shuffle(order)
            jobs += [{"instance_id": iid, "policy": p, "rep": rep, "stage": args.stage} for p in order]

    cap = cfg["limits"]["per_run_cap_usd"]
    print(f"{len(ids)} tasks x {len(args.policies)} policies x {repeats} repeats = {len(jobs)} runs; "
          f"worst case ${len(jobs) * cap:.0f} vs global cap ${cfg['limits']['global_cap_usd']:.0f}")
    if args.dry_run:
        return

    out.mkdir(parents=True, exist_ok=True)
    (out / f"run_config_{args.stage}.json").write_text(json.dumps(
        {"config": cfg, "ids": ids, "repeats": repeats, "policies": args.policies,
         "started": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2), encoding="utf-8")

    from datasets import load_dataset
    wanted = set(ids)
    instances = {r["instance_id"]: r for r in load_dataset(cfg["dataset"]["name"], split=cfg["dataset"]["split"])
                 if r["instance_id"] in wanted}
    lim = cfg["limits"]
    tracker = CostTracker(out / "cost_ledger.jsonl", lim["global_cap_usd"], lim["per_run_cap_usd"],
                          lim.get("reserve_per_call_usd", 0.10))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(lambda j: run_one(j, cfg, instances, tracker, out), jobs))
    print(f"total spend ${tracker.global_cost:.2f}")


if __name__ == "__main__":
    main()