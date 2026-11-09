"""Docker-free smoke tests. Run:  python -m pytest tests -q   (or python tests/test_smoke.py)

A scripted model drives a throwaway git repo through RoundsAgent so the round
loop, both policies, the cost cap and patch extraction are exercised without
Docker or an API key.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output

from swe_policy_eval.check_parsing import is_test_command, parse_check_output
from swe_policy_eval.cost_tracker import CostTracker, TrackedModel
from swe_policy_eval.policies import ControllerPolicy, FixedPolicy
from swe_policy_eval.rounds_agent import RoundsAgent

TEMPLATES = {"system_template": "You are an agent.", "instance_template": "{{task}}"}


def make_repo() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "tests").mkdir()
    (d / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (d / "tests" / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    for cmd in ("git init -q", "git add -A", "git -c user.email=a@b -c user.name=t commit -qm init"):
        subprocess.run(cmd, shell=True, cwd=d, check=True)
    return d


def act(cmd: str, cost: float = 1.0) -> dict:
    return make_output("thinking", [{"command": cmd}], cost=cost)


PY = f'"{sys.executable}"'
FIX = f'{PY} -c "open(\'calc.py\', \'w\').write(\'def add(a, b):\\n    return a + b\\n\')"'
TEST = f"{PY} -m pytest -q tests"
NOOP = "git status -s"


def run(policy, outputs, repo, tmp, cap=100.0, **kw):
    tracker = CostTracker(tmp / "ledger.jsonl", 1000.0, cap, reserve_per_call_usd=0.1)
    env = LocalEnvironment(cwd=str(repo), timeout=60)
    model = TrackedModel(DeterministicModel(outputs=outputs), tracker, "t/run")
    agent = RoundsAgent(model, env, policy=policy, tracker=tracker, run_id="t/run",
                        rounds_log_path=tmp / "rounds.jsonl", run_cost_cap=cap, step_ceiling=12,
                        diff_cmd='git diff -- . ":(exclude,glob)**/tests/**" ":(exclude)patch.txt"',
                        output_path=tmp / "traj.json", step_limit=50, cost_limit=cap + 1.0, **TEMPLATES, **kw)
    res = agent.run_policy("fix add()")
    return res, agent


def test_check_parsing():
    assert is_test_command("cd /testbed && python -m pytest -x tests/")
    assert is_test_command("./tests/runtests.py admin_views")
    assert not is_test_command("grep -rn pytest .")
    out = "===== 3 passed in 0.1s ====="
    assert parse_check_output("pytest", out, 0).ok is True
    assert parse_check_output("pytest", "1 failed, 2 passed", 1).ok is False
    assert parse_check_output("pytest", "no tests ran", 5).ok is None
    assert parse_check_output("runtests", "Ran 4 tests in 0.1s\n\nFAILED (failures=1)", 1).failed == 1


def test_controller_stops_when_tests_pass():
    repo, tmp = make_repo(), Path(tempfile.mkdtemp())
    pol = ControllerPolicy(steps_per_round=2, min_steps=1, max_steps=2, min_rounds_before_stop=2)
    res, _ = run(pol, [act(NOOP), act(FIX), act(TEST), act("never reached")], repo, tmp)
    assert res["stop_reason"] == "tests_pass", res["stop_reason"]
    assert res["rounds"] == 2 and res["total_steps"] == 3
    assert "a + b" in res["patch"] and res["patch_source"] == "harness_diff"
    rounds = [json.loads(l) for l in (tmp / "rounds.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rounds[0]["decision"]["reason"] == "unverified_patch"
    assert rounds[1]["tests"][0]["ok"] is True
    assert rounds[1]["cost"]["cum_usd"] == 3.0


def test_fixed_runs_full_schedule_even_if_tests_pass():
    repo, tmp = make_repo(), Path(tempfile.mkdtemp())
    pol = FixedPolicy(rounds=2, steps_per_round=2)
    res, _ = run(pol, [act(FIX), act(TEST), act(NOOP), act(NOOP)], repo, tmp)
    assert res["stop_reason"] == "fixed_schedule_complete"
    assert res["rounds"] == 2 and res["total_steps"] == 4 and res["cost_usd"] == 4.0


def test_agent_submission_is_used_and_policy_cannot_veto():
    repo, tmp = make_repo(), Path(tempfile.mkdtemp())
    submit = f'echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT&& {PY} -c "print(open(\'patch.txt\').read(), end=\'\')"'
    outs = [act(FIX), act("git diff -- calc.py > patch.txt"), act(submit)]
    res, _ = run(FixedPolicy(rounds=3, steps_per_round=5), outs, repo, tmp)
    assert res["exit_status"] == "Submitted" and res["patch_source"] == "agent_submission"
    assert res["stop_reason"].startswith("agent_exit")
    assert "a + b" in res["patch"]


def test_per_run_cost_cap_is_hard():
    repo, tmp = make_repo(), Path(tempfile.mkdtemp())
    outs = [act(NOOP, cost=1.0) for _ in range(10)]
    res, _ = run(FixedPolicy(rounds=4, steps_per_round=5), outs, repo, tmp, cap=3.0)
    assert res["exit_status"] == "RunBudgetExceeded", res
    assert res["cost_usd"] <= 3.0
    ledger = [json.loads(l) for l in (tmp / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(ledger) == 3 and ledger[-1]["run_cost_usd"] == 3.0


def _state(**kw):
    from swe_policy_eval.policies.base import CheckRun, PatchInfo, RoundState
    base = dict(round_idx=2, steps_this_round=5, total_steps=10, step_ceiling=60, cum_cost=0.5,
                round_cost=0.2, run_cost_cap=3.0, cum_input_tokens=0, cum_output_tokens=0,
                patch=PatchInfo(True, "aaa", 10, ["x.py"]), prev_patch_shas=["bbb"], checks=[], n_actions=5)
    base.update(kw)
    return RoundState(**base), CheckRun


def test_controller_branches():
    from swe_policy_eval.policies.base import PatchInfo
    pol = ControllerPolicy(patience=2)
    s, C = _state(checks=[])
    assert pol.decide(s).reason == "unverified_patch"

    # 1st failure -> tests_failing
    s, C = _state()
    s.checks = [C("pytest", 1, False, failed=2, tail="FAILED test_a.py::test_1\nE assert 1 == 2")]
    d = pol.decide(s)
    assert d.reason == "tests_failing" and d.next_steps == pol.max_steps and "assert 1 == 2" in d.nudge

    # Table I: Same failing test/error repeats for 2 rounds -> repeated_failure_new_diagnosis
    s2, C = _state(
        history=[{"tests": [{"ok": False, "passed": 0, "failed": 2, "errors": 0, "ran": 2,
                             "tail": "FAILED test_a.py::test_1\nE assert 1 == 2"}]}]
    )
    s2.checks = [C("pytest", 1, False, failed=2, tail="FAILED test_a.py::test_1\nE assert 1 == 2")]
    d2 = pol.decide(s2)
    assert d2.reason == "repeated_failure_new_diagnosis" and "new diagnosis" in d2.nudge

    # Table I: Repository tests passing increases, no regressions -> progress_continue
    s_prog, C = _state(
        history=[{"tests": [{"ok": False, "passed": 1, "failed": 1, "errors": 0, "ran": 2, "tail": "FAILED x"}]}]
    )
    s_prog.checks = [C("pytest", 1, False, passed=3, failed=1, tail="FAILED x")]
    assert pol.decide(s_prog).reason == "progress_continue"

    # Table I: Regression detected -> regression_detected (revert_to_best=True)
    s_reg, C = _state(
        history=[{"tests": [{"ok": False, "passed": 4, "failed": 1, "errors": 0, "ran": 5, "tail": "FAILED x"}]}]
    )
    s_reg.checks = [C("pytest", 1, False, passed=2, failed=3, tail="FAILED x\nFAILED y")]
    d_reg = pol.decide(s_reg)
    assert d_reg.reason == "regression_detected" and d_reg.revert_to_best is True

    # Table I: No measurable progress for `patience` rounds -> patch_stagnant / no_progress
    s, C = _state(prev_patch_shas=["aaa", "aaa"])                       # unchanged for 2 rounds
    assert pol.decide(s).reason == "patch_stagnant"
    s, C = _state(patch=PatchInfo(False, "e", 0), prev_patch_shas=["e", "e"])
    assert pol.decide(s).reason == "no_progress"
    s, C = _state(patch=PatchInfo(False, "e", 0), prev_patch_shas=["f"])
    assert pol.decide(s).reason == "empty_patch"

    # Table I: Remaining budget below threshold -> 1 final attempt, then budget_guard
    s_low, C = _state(cum_cost=2.8, history=[])
    assert pol.decide(s_low).reason == "low_budget_final_attempt"
    s_exhaust, C = _state(cum_cost=2.8, history=[{"decision": {"reason": "low_budget_final_attempt"}}])
    assert pol.decide(s_exhaust).reason == "budget_guard"

    s, C = _state(total_steps=60)
    assert pol.decide(s).reason == "step_ceiling"
    s, C = _state(total_steps=55); s.checks = [C("pytest", 1, False, failed=1)]
    assert pol.decide(s).next_steps == 5                                 # clamped to remaining steps


def test_statistical_functions():
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from analyze import mcnemar_test, wilcoxon_signed_rank

    ctrl = np.array([1.0, 1.0, 1.0, 0.0, 1.0])
    fix = np.array([0.0, 0.0, 1.0, 0.0, 0.0])
    mc = mcnemar_test(ctrl, fix)
    assert mc["n_controller_only"] == 3 and mc["n_fixed_only"] == 0 and mc["n_both_resolved"] == 1
    assert 0.0 < mc["p_value_exact"] <= 0.25

    diffs = np.array([-0.4, -0.3, -0.5, -0.2, -0.6])
    wx = wilcoxon_signed_rank(diffs)
    assert wx["n_nonzero"] == 5 and wx["w_plus"] == 0.0 and wx["effect_size_rank_biserial_r"] == -1.0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
