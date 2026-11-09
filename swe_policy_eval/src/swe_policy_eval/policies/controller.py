"""Progress-Aware Controller Policy (Section III-E, Table I).

Rule-based and deterministic on purpose: it makes no model calls, so every
dollar in the ledger belongs to the agent and the two arms are directly comparable.

Agent-visible signals inspected after each round (Section III-E & III-F):
  * Passing and failing repository test counts (from tests the agent itself ran)
  * Newly passing vs. newly broken tests across rounds (progress vs. regressions)
  * Repetition of identical failing tests or error signatures across rounds
  * Source-only patch diff state and stagnation across rounds
  * Cumulative cost vs. the per-run dollar ceiling

Table I Decision Rules (in priority order):
  1. Hard step ceiling reached                          -> stop ("step_ceiling")
  2. All agent-visible checks pass (and round >= min)   -> stop ("tests_pass")
  3. Remaining budget below threshold:
       - first time below threshold                     -> one final targeted attempt ("low_budget_final_attempt")
       - after final attempt                            -> stop ("budget_guard")
  4. Regression detected (passing dropped / newly broken)-> revert to best checkpoint + request narrower fix ("regression_detected")
  5. No measurable progress for `patience` (3) rounds   -> stop ("patch_stagnant" / "no_progress")
  6. Same failing test or error repeats for 2 rounds    -> replace repair prompt; request new diagnosis ("repeated_failure_new_diagnosis")
  7. Repository tests passing increases, no regressions -> continue with next round ("progress_continue")
  8. Otherwise continue with situation-sized step budget:
       - empty patch          -> steps_per_round, "make a minimal edit" ("empty_patch")
       - tests failing (1st)  -> max_steps, failing output attached ("tests_failing")
       - patch but no tests   -> min_steps, "verify it" ("unverified_patch")
       - tests ok, round < min-> min_steps, "check edge cases" ("confirm_before_stop")
"""
from __future__ import annotations

import re

from .base import CheckRun, Decision, Policy, RoundState

_FAIL_LINE = re.compile(r"(?:^|\s)(?:FAILED|ERROR)\s+(\S+)|^\s*E\s+([A-Za-z_]\w*Error.*)", re.M)


def _failure_sig_from_fields(failed: int, errors: int, tail: str) -> str:
    matches = _FAIL_LINE.findall(tail or "")
    if matches:
        tokens = [a or b.strip()[:80] for a, b in matches]
        return "|".join(tokens[:8])
    lines = [ln.strip() for ln in (tail or "").splitlines() if ln.strip()]
    last_line = lines[-1][:120] if lines else ""
    return f"f={failed},e={errors}:{last_line}"


def _summarize_checks(checks: list[CheckRun] | list[dict]) -> dict:
    norm = []
    for c in checks:
        if isinstance(c, CheckRun):
            norm.append({"ok": c.ok, "passed": c.passed, "failed": c.failed,
                         "errors": c.errors, "ran": c.ran, "tail": c.tail})
        elif isinstance(c, dict):
            norm.append(c)
    known = [c for c in norm if c.get("ok") is not None]
    any_fail = any(c.get("ok") is False for c in norm)
    all_ok = bool(known) and all(c.get("ok") is True for c in known)
    passed = max(((c.get("passed", 0) or c.get("ran", 0)) for c in known), default=0)
    bad = max(((c.get("failed", 0) + c.get("errors", 0)) for c in known), default=0)
    fail_sig = None
    if any_fail:
        f = next(c for c in reversed(norm) if c.get("ok") is False)
        fail_sig = _failure_sig_from_fields(f.get("failed", 0), f.get("errors", 0), f.get("tail", ""))
    return {
        "has_known": bool(known),
        "all_ok": all_ok,
        "any_fail": any_fail,
        "passed": passed,
        "bad": bad,
        "fail_sig": fail_sig,
    }


class ControllerPolicy(Policy):
    name = "controller"

    def __init__(
        self,
        steps_per_round: int = 15,
        min_steps: int = 6,
        max_steps: int = 20,
        min_rounds_before_stop: int = 2,
        patience: int = 3,
        repeat_failure_limit: int = 2,
        cost_stop_frac: float = 0.9,
        final_attempt_on_low_budget: bool = True,
        stop_on_empty_stagnation: bool = True,
    ):
        self.steps_per_round = steps_per_round
        self.min_steps = min_steps
        self.max_steps = max_steps
        self.min_rounds_before_stop = min_rounds_before_stop
        self.patience = patience
        self.repeat_failure_limit = repeat_failure_limit
        self.cost_stop_frac = cost_stop_frac
        self.final_attempt_on_low_budget = final_attempt_on_low_budget
        self.stop_on_empty_stagnation = stop_on_empty_stagnation

    def first_round_steps(self) -> int:
        return self.steps_per_round

    # ------------------------------------------------------------------
    def decide(self, s: RoundState) -> Decision:
        if s.remaining_steps <= 0:
            return Decision("stop", reason="step_ceiling")

        def go(steps: int, nudge: str, reason: str, revert: bool = False) -> Decision:
            return Decision(
                "continue",
                next_steps=max(1, min(steps, s.remaining_steps)),
                nudge=nudge,
                reason=reason,
                revert_to_best=revert,
            )

        p = s.patch
        curr = _summarize_checks(s.checks)
        tests_pass = curr["all_ok"]
        tests_fail = curr["any_fail"]

        # Table I Rule 6: All agent-visible checks pass -> Stop; submit for final evaluation
        if p.nonempty and tests_pass and s.round_idx >= self.min_rounds_before_stop:
            return Decision("stop", reason="tests_pass")

        # Table I Rule 5: Remaining budget below threshold -> One final targeted attempt, then stop
        if s.cum_cost >= self.cost_stop_frac * s.run_cost_cap:
            already_had_final = any(
                h.get("decision", {}).get("reason") == "low_budget_final_attempt" for h in s.history
            )
            if already_had_final or not self.final_attempt_on_low_budget:
                return Decision("stop", reason="budget_guard")
            return go(
                self.min_steps,
                "Remaining budget is below the low-budget threshold. Make one final, targeted "
                "attempt to fix and verify the issue, then submit immediately.",
                "low_budget_final_attempt",
            )

        # Prior rounds' test summaries (from history)
        hist_summaries = [_summarize_checks(h.get("tests", [])) for h in s.history]
        prev_known_rounds = [hs for hs in hist_summaries if hs["has_known"]]
        prev_test = prev_known_rounds[-1] if prev_known_rounds else None

        # Table I Rule 4: Regression detected -> Revert change; request narrower fix
        if curr["has_known"] and prev_test is not None:
            broke_passing_suite = prev_test["all_ok"] and tests_fail
            dropped_passing = (
                prev_test["passed"] > 0
                and curr["passed"] < prev_test["passed"]
                and curr["bad"] > prev_test["bad"]
            )
            if broke_passing_suite or dropped_passing:
                return go(
                    self.min_steps,
                    "Regression detected: repository test results worsened compared to the previous "
                    f"round (passing: {prev_test['passed']} -> {curr['passed']}, "
                    f"failures/errors: {prev_test['bad']} -> {curr['bad']}). "
                    "Your recent change has been reverted to the best checkpoint; apply a narrower, "
                    "more targeted fix and re-run the tests.",
                    "regression_detected",
                    revert=True,
                )

        # Table I Rule 3: No measurable progress for `patience` rounds -> Stop
        # (Wait `patience` consecutive rounds with identical patch SHA after the state was established)
        prev = s.prev_patch_shas[-self.patience:]
        stagnant = len(prev) >= self.patience and all(sha == p.sha for sha in prev)
        if p.nonempty and stagnant:
            return Decision("stop", reason="patch_stagnant")
        if (not p.nonempty) and stagnant and self.stop_on_empty_stagnation:
            return Decision("stop", reason="no_progress")

        # Table I Rule 1: Repository tests passing increases, no regressions -> Continue with next round
        if curr["has_known"] and prev_test is not None:
            if curr["passed"] > prev_test["passed"] and curr["bad"] <= prev_test["bad"]:
                if tests_fail:
                    failing = next(c for c in reversed(s.checks) if c.ok is False)
                    return go(
                        self.steps_per_round,
                        f"Progress detected: passing tests increased ({prev_test['passed']} -> "
                        f"{curr['passed']}) with no regressions, though {failing.failed} failed / "
                        f"{failing.errors} errors remain. Output tail:\n{failing.tail}\n"
                        "Continue with the next round to resolve the remaining failures.",
                        "progress_continue",
                    )
                return go(
                    self.min_steps,
                    f"Progress detected: passing tests increased ({prev_test['passed']} -> "
                    f"{curr['passed']}) with no regressions. Check edge cases and submit if ready.",
                    "progress_continue",
                )

        # Table I Rule 2: Same failing test or error repeats for `repeat_failure_limit` (2) rounds
        # -> Replace repair prompt; request new diagnosis
        if tests_fail and self.repeat_failure_limit >= 2 and len(hist_summaries) >= self.repeat_failure_limit - 1:
            recent = hist_summaries[-(self.repeat_failure_limit - 1):]
            if all(r["any_fail"] and r["fail_sig"] == curr["fail_sig"] for r in recent):
                failing = next(c for c in reversed(s.checks) if c.ok is False)
                return go(
                    self.max_steps,
                    f"The same failing test or error has repeated for {self.repeat_failure_limit} "
                    f"consecutive rounds ({failing.failed} failed, {failing.errors} errors). "
                    f"Output tail:\n{failing.tail}\n"
                    "Do NOT repeat your previous repair approach. Step back, form a new diagnosis "
                    "of the root cause, and change your repair strategy.",
                    "repeated_failure_new_diagnosis",
                )

        # Standard situation-sized continuation rules
        if not p.nonempty:
            return go(
                self.steps_per_round,
                "There are no source changes yet. Locate the root cause and make a minimal "
                "edit to non-test source files, then verify it.",
                "empty_patch",
            )
        if tests_fail:
            failing = next(c for c in reversed(s.checks) if c.ok is False)
            return go(
                self.max_steps,
                "Your last test run failed "
                f"({failing.failed} failed, {failing.errors} errors). Output tail:\n"
                f"{failing.tail}\nFix the cause in the source files, then re-run the tests.",
                "tests_failing",
            )
        if not s.checks:
            return go(
                self.min_steps,
                "You have a patch but have not run any tests this round. Run the most "
                "relevant existing tests and your reproduction script to verify it.",
                "unverified_patch",
            )
        return go(
            self.min_steps,
            "Tests passed. Do a short check of edge cases and nearby tests for regressions; "
            "if everything is fine, submit.",
            "confirm_before_stop",
        )

    def describe(self) -> dict:
        return {
            k: getattr(self, k)
            for k in (
                "steps_per_round",
                "min_steps",
                "max_steps",
                "min_rounds_before_stop",
                "patience",
                "repeat_failure_limit",
                "cost_stop_frac",
                "final_attempt_on_low_budget",
                "stop_on_empty_stagnation",
            )
        } | {"name": self.name}
