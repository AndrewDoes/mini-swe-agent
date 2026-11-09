#!/usr/bin/env python3
"""Compute evaluation metrics and statistical tests (Sections III-I & III-J).

Per policy (Section III-I):
  * Task success rate (fraction and %)
  * Cost per successful task (undefined / null when 0 successes)
  * False-completion rate (runs reported complete that fail final checks / runs reported complete)
  * Regression rate (share of evaluated runs breaking previously passing PASS_TO_PASS tests,
    plus in-loop visible regression rate)
  * Budget-exhaustion rate (runs exhausting budget/ceiling without verified success)
  * Mean cost, total cost, tokens, rounds, steps, empty-patch rate, stop-reason mix

Paired statistical analysis (Section III-J, controller vs. fixed on identical tasks):
  * McNemar's test on paired binary task success (exact binomial p-value, chi2, Cohen's g)
  * Wilcoxon signed-rank test on paired task cost, step count, and round count
    (W statistic, two-sided p-value, matched-pairs rank-biserial r, and Cohen's d)
  * Seeded 10,000-resample bootstrap 95% confidence intervals
  * Categorized task lists (controller_helped, controller_hurt, both_resolved, neither_resolved)
    for manual failure analysis.

    python scripts/analyze.py --stage pilot
"""
import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]


def bootstrap_ci(x: np.ndarray, seed: int, n: int = 10_000) -> tuple[float, float]:
    if len(x) == 0:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(n, len(x)), replace=True).mean(axis=1)
    return float(np.percentile(means, 2.5)), float(np.percentile(means, 97.5))


def mcnemar_test(ctrl_res: np.ndarray, fix_res: np.ndarray) -> dict:
    """McNemar's test for paired binary success outcomes (Section III-J).

    For tasks with multiple repeats, a task is treated as resolved by a policy if its
    majority/mean resolution rate across repeats is >= 0.5.
    """
    c_bin = ctrl_res >= 0.5
    f_bin = fix_res >= 0.5
    both = int(np.sum(c_bin & f_bin))
    b = int(np.sum(c_bin & ~f_bin))      # controller succeeded, fixed failed
    c = int(np.sum(~c_bin & f_bin))      # fixed succeeded, controller failed
    neither = int(np.sum(~c_bin & ~f_bin))
    n_discordant = b + c
    if n_discordant == 0:
        p_exact = 1.0
        chi2 = 0.0
        cohens_g = 0.0
    else:
        k = min(b, c)
        tail = sum(math.comb(n_discordant, i) for i in range(k + 1)) / (2 ** n_discordant)
        p_exact = min(1.0, 2.0 * tail)
        chi2 = float(((abs(b - c) - 1.0) ** 2) / n_discordant) if n_discordant > 0 else 0.0
        cohens_g = float((b / n_discordant) - 0.5)
    return {
        "n_both_resolved": both,
        "n_controller_only": b,
        "n_fixed_only": c,
        "n_neither_resolved": neither,
        "chi2_continuity_corrected": chi2,
        "p_value_exact": float(p_exact),
        "effect_size_cohens_g": cohens_g,
    }


def wilcoxon_signed_rank(diffs: np.ndarray) -> dict:
    """Two-sided Wilcoxon signed-rank test with tie correction and rank-biserial effect size."""
    nz = np.asarray([d for d in diffs if abs(d) > 1e-12], dtype=float)
    n = len(nz)
    if n == 0:
        return {"n_nonzero": 0, "w_stat": 0.0, "w_plus": 0.0, "w_minus": 0.0,
                "p_value": 1.0, "effect_size_rank_biserial_r": 0.0}
    abs_d = np.abs(nz)
    order = np.argsort(abs_d)
    ranks = np.empty(n, dtype=float)
    tie_groups = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and abs(abs_d[order[j + 1]] - abs_d[order[i]]) <= 1e-12:
            j += 1
        avg_rank = 0.5 * (i + 1 + j + 1)
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        if j > i:
            tie_groups.append(j - i + 1)
        i = j + 1

    w_plus = float(np.sum(ranks[nz > 0]))
    w_minus = float(np.sum(ranks[nz < 0]))
    w_stat = min(w_plus, w_minus)
    total_rank_sum = n * (n + 1) / 2.0
    rank_biserial_r = float((w_plus - w_minus) / total_rank_sum)

    if n <= 15 and not tie_groups:
        # Exact enumeration over all 2^n sign assignments
        le_count = 0
        for mask in range(1 << n):
            s = sum(ranks[bit] for bit in range(n) if (mask >> bit) & 1)
            if s <= w_stat + 1e-9:
                le_count += 1
        p_val = min(1.0, 2.0 * le_count / (1 << n))
    else:
        mean_w = n * (n + 1) / 4.0
        tie_corr = sum(t * (t * t - 1) for t in tie_groups) / 48.0
        var_w = (n * (n + 1) * (2 * n + 1) / 24.0) - tie_corr
        if var_w <= 0:
            p_val = 1.0
        else:
            z = (abs(w_stat - mean_w) - 0.5) / math.sqrt(var_w)
            p_val = float(math.erfc(max(0.0, z) / math.sqrt(2.0)))

    return {
        "n_nonzero": n,
        "w_stat": w_stat,
        "w_plus": w_plus,
        "w_minus": w_minus,
        "p_value": float(p_val),
        "effect_size_rank_biserial_r": rank_biserial_r,
    }


def _is_reported_complete(r: dict) -> bool:
    """Whether the agent or controller claimed task completion (Section III-I)."""
    return r.get("exit_status") == "Submitted" or r.get("stop_reason") == "tests_pass"


def _is_budget_exhausted(r: dict) -> bool:
    """Whether the run hit a budget or step cap without verified success (Section III-I)."""
    hit_cap = (
        r.get("exit_status") in ("RunBudgetExceeded", "LimitsExceeded")
        or r.get("stop_reason") in ("budget_guard", "step_ceiling")
    )
    return hit_cap and not r.get("resolved", False)


def _had_in_loop_regression(r: dict) -> bool:
    return any(
        rd.get("decision", {}).get("reason") == "regression_detected"
        for rd in r.get("rounds_log", [])
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(ROOT / "config" / "experiment.yaml"))
    ap.add_argument("--stage", choices=["pilot", "main"], required=True)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    out = Path(args.out) if args.out else (ROOT / "results" / cfg["experiment"])
    resolved_path = out / "eval" / "resolved.json"
    resolved = json.loads(resolved_path.read_text(encoding="utf-8")) if resolved_path.exists() else {}

    runs = []
    for f in sorted((out / args.stage).glob("*/*/rep*/result.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        key = f"{args.stage}:{r['policy']}_rep{r['rep']}"
        r["resolved"] = r["instance_id"] in set(resolved.get(key, []))
        r["regressed"] = r["instance_id"] in set(resolved.get(f"{key}:regressed", []))
        runs.append(r)
    if not runs:
        raise SystemExit("no results found")

    by_policy = defaultdict(list)
    for r in runs:
        by_policy[r["policy"]].append(r)

    summary = {"stage": args.stage, "policies": {}}
    for pol, rs in by_policy.items():
        cost = np.array([r["cost_usd"] for r in rs], dtype=float)
        res = np.array([r["resolved"] for r in rs], dtype=float)
        reported_complete = [r for r in rs if _is_reported_complete(r)]
        agent_submitted = [r for r in rs if r.get("exit_status") == "Submitted"]
        false_completions = [r for r in reported_complete if not r["resolved"]]
        false_submissions = [r for r in agent_submitted if not r["resolved"]]

        summary["policies"][pol] = {
            "n_runs": len(rs),
            "n_resolved": int(res.sum()),
            "resolve_rate": float(res.mean()),
            "success_rate_pct": float(res.mean() * 100.0),
            "mean_cost_usd": float(cost.mean()),
            "total_cost_usd": float(cost.sum()),
            "cost_per_resolved_usd": float(cost.sum() / res.sum()) if res.sum() > 0 else None,
            "n_reported_complete": len(reported_complete),
            "false_completion_rate": (
                float(len(false_completions) / len(reported_complete)) if reported_complete else None
            ),
            "agent_false_submission_rate": (
                float(len(false_submissions) / len(agent_submitted)) if agent_submitted else None
            ),
            "regression_rate": float(np.mean([r["regressed"] for r in rs])),
            "in_loop_regression_rate": float(np.mean([_had_in_loop_regression(r) for r in rs])),
            "budget_exhaustion_rate": float(np.mean([_is_budget_exhausted(r) for r in rs])),
            "mean_input_tokens": float(np.mean([r["input_tokens"] for r in rs])),
            "mean_output_tokens": float(np.mean([r["output_tokens"] for r in rs])),
            "mean_rounds": float(np.mean([r["rounds"] for r in rs])),
            "mean_steps": float(np.mean([r["total_steps"] for r in rs])),
            "empty_patch_rate": float(np.mean([not r["patch"].strip() for r in rs])),
            "stop_reasons": dict(Counter(r["stop_reason"] for r in rs)),
        }

    # Paired comparison at task level (average repeats first so tasks are the unit).
    per_task = defaultdict(
        lambda: defaultdict(lambda: {"res": [], "cost": [], "steps": [], "rounds": []})
    )
    for r in runs:
        d = per_task[r["instance_id"]][r["policy"]]
        d["res"].append(float(r["resolved"]))
        d["cost"].append(float(r["cost_usd"]))
        d["steps"].append(float(r["total_steps"]))
        d["rounds"].append(float(r["rounds"]))

    tasks = sorted(t for t, d in per_task.items() if "fixed" in d and "controller" in d)
    if tasks:
        ctrl_res = np.array([np.mean(per_task[t]["controller"]["res"]) for t in tasks])
        fix_res = np.array([np.mean(per_task[t]["fixed"]["res"]) for t in tasks])
        d_res = ctrl_res - fix_res

        ctrl_cost = np.array([np.mean(per_task[t]["controller"]["cost"]) for t in tasks])
        fix_cost = np.array([np.mean(per_task[t]["fixed"]["cost"]) for t in tasks])
        d_cost = ctrl_cost - fix_cost

        ctrl_steps = np.array([np.mean(per_task[t]["controller"]["steps"]) for t in tasks])
        fix_steps = np.array([np.mean(per_task[t]["fixed"]["steps"]) for t in tasks])
        d_steps = ctrl_steps - fix_steps

        ctrl_rounds = np.array([np.mean(per_task[t]["controller"]["rounds"]) for t in tasks])
        fix_rounds = np.array([np.mean(per_task[t]["fixed"]["rounds"]) for t in tasks])
        d_rounds = ctrl_rounds - fix_rounds

        cost_sd = float(d_cost.std(ddof=1)) if len(d_cost) > 1 else 0.0
        steps_sd = float(d_steps.std(ddof=1)) if len(d_steps) > 1 else 0.0

        summary["paired_controller_minus_fixed"] = {
            "n_tasks": len(tasks),
            "resolve_rate_diff": float(d_res.mean()),
            "resolve_rate_diff_ci95": bootstrap_ci(d_res, cfg["seed"]),
            "mcnemar_test": mcnemar_test(ctrl_res, fix_res),
            "cost_diff_usd": float(d_cost.mean()),
            "cost_diff_ci95": bootstrap_ci(d_cost, cfg["seed"] + 1),
            "cost_effect_size_cohens_d": float(d_cost.mean() / cost_sd) if cost_sd > 1e-12 else 0.0,
            "cost_wilcoxon": wilcoxon_signed_rank(d_cost),
            "steps_diff": float(d_steps.mean()),
            "steps_diff_ci95": bootstrap_ci(d_steps, cfg["seed"] + 2),
            "steps_effect_size_cohens_d": float(d_steps.mean() / steps_sd) if steps_sd > 1e-12 else 0.0,
            "steps_wilcoxon": wilcoxon_signed_rank(d_steps),
            "rounds_diff": float(d_rounds.mean()),
            "rounds_diff_ci95": bootstrap_ci(d_rounds, cfg["seed"] + 3),
            "rounds_wilcoxon": wilcoxon_signed_rank(d_rounds),
            "failure_analysis_cases": {
                "controller_helped": [t for t, cr, fr in zip(tasks, ctrl_res, fix_res) if cr > fr],
                "controller_hurt": [t for t, cr, fr in zip(tasks, ctrl_res, fix_res) if cr < fr],
                "both_resolved": [t for t, cr, fr in zip(tasks, ctrl_res, fix_res) if cr >= 0.5 and fr >= 0.5],
                "neither_resolved": [t for t, cr, fr in zip(tasks, ctrl_res, fix_res) if cr < 0.5 and fr < 0.5],
            },
        }
        # Variability across repeats (pilot input for sizing the main run).
        var = {}
        for pol in ("fixed", "controller"):
            sds = [np.std(per_task[t][pol]["cost"], ddof=1) for t in tasks if len(per_task[t][pol]["cost"]) > 1]
            flips = [0.0 < np.mean(per_task[t][pol]["res"]) < 1.0 for t in tasks if len(per_task[t][pol]["res"]) > 1]
            var[pol] = {
                "mean_within_task_cost_sd": float(np.mean(sds)) if sds else None,
                "share_tasks_with_mixed_outcomes": float(np.mean(flips)) if flips else None,
                "sd_of_task_cost_diff": cost_sd if len(d_cost) > 1 else None,
            }
        summary["variability"] = var

    (out / f"summary_{args.stage}.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    print(json.dumps(summary, indent=2, default=float))


if __name__ == "__main__":
    main()
