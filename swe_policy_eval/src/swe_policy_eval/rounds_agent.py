"""mini-swe-agent DefaultAgent extended with a round loop driven by a Policy.

Everything inside a round is stock mini-swe-agent behaviour (same prompts, same
tools, same format-error handling). The only additions are:
  * after each round the harness snapshots the source diff and the test runs,
  * the Policy decides to stop or continue (and what to say / how long to run),
  * one JSON line per round is written to ``rounds.jsonl``.
Both policies go through this exact code path.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

from minisweagent.agents.default import DefaultAgent
from minisweagent.exceptions import FormatError, InterruptAgentFlow

from .check_parsing import extract_checks
from .cost_tracker import CostTracker
from .policies.base import Decision, PatchInfo, Policy, RoundState
from .recording_env import RecordingEnv

# Source-only diff of tracked files: tests, conftest and the agent's patch.txt are excluded.
_DIFF_CMD = (
    "cd /testbed && git diff -- . "
    "':(exclude,glob)**/tests/**' ':(exclude,glob)**/test_*.py' "
    "':(exclude,glob)**/*_test.py' ':(exclude,glob)**/conftest.py' ':(exclude)patch.txt'"
)


class RoundsAgent(DefaultAgent):
    def __init__(self, model, env, *, policy: Policy, tracker: CostTracker, run_id: str,
                 rounds_log_path: Path, run_cost_cap: float, step_ceiling: int,
                 diff_cmd: str = _DIFF_CMD, **agent_kwargs):
        self.renv = RecordingEnv(env)
        super().__init__(model, self.renv, **agent_kwargs)
        self.policy = policy
        self.tracker = tracker
        self.run_id = run_id
        self.rounds_log_path = Path(rounds_log_path)
        self.run_cost_cap = run_cost_cap
        self.step_ceiling = step_ceiling
        self.diff_cmd = diff_cmd
        self.round_logs: list[dict] = []

    # ------------------------------------------------------------ main loop
    def run_policy(self, task: str) -> dict:
        self.extra_template_vars |= {"task": task}
        self.messages = []
        self.add_messages(
            self.model.format_message(role="system", content=self._render_template(self.config.system_template)),
            self.model.format_message(role="user", content=self._render_template(self.config.instance_template)),
        )
        self.rounds_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.rounds_log_path.write_text("", encoding="utf-8")

        steps_budget = self.policy.first_round_steps()
        round_idx, total_steps, prev_shas = 0, 0, []
        last_cost = self.tracker.totals(self.run_id)["cost_usd"]
        last_decision = Decision("stop", reason="unset")
        best_patch_text = ""
        best_test_score: tuple[int, int] | None = None

        while True:
            round_idx += 1
            steps, exited = self._run_round(steps_budget)
            total_steps += steps
            records = self.renv.drain()
            checks = extract_checks(records)
            patch, patch_text = self._snapshot_patch()
            tot = self.tracker.totals(self.run_id)

            # Track best verified checkpoint (most passing tests, fewest failures/errors).
            known_checks = [c for c in checks if c.ok is not None]
            if patch.nonempty and known_checks:
                score = (max(c.passed + c.ran for c in known_checks), -sum(c.failed + c.errors for c in known_checks))
                if best_test_score is None or score >= best_test_score:
                    best_test_score = score
                    best_patch_text = patch_text
            elif patch.nonempty and not best_patch_text:
                best_patch_text = patch_text

            state = RoundState(
                round_idx=round_idx, steps_this_round=steps, total_steps=total_steps,
                step_ceiling=self.step_ceiling, cum_cost=tot["cost_usd"],
                round_cost=tot["cost_usd"] - last_cost, run_cost_cap=self.run_cost_cap,
                cum_input_tokens=tot["input_tokens"], cum_output_tokens=tot["output_tokens"],
                patch=patch, prev_patch_shas=list(prev_shas), checks=checks,
                n_actions=len(records), history=self.round_logs,
            )
            if exited:  # the agent submitted, or a hard limit fired: the policy cannot veto
                status = self.messages[-1].get("extra", {}).get("exit_status", "exit")
                decision = Decision("stop", reason=f"agent_exit:{status}")
            else:
                decision = self.policy.decide(state)
            last_decision = decision

            self._log_round(state, records, decision)
            prev_shas.append(patch.sha)
            last_cost = tot["cost_usd"]

            if decision.action == "stop":
                break
            if decision.revert_to_best:
                self._restore_checkpoint(best_patch_text)
            steps_budget = decision.next_steps
            if decision.nudge:
                self.add_messages(self.model.format_message(role="user", content=decision.nudge))

        return self._finalize(last_decision, round_idx, total_steps)

    # --------------------------------------------------------------- a round
    def _run_round(self, max_steps: int) -> tuple[int, bool]:
        steps = 0
        while steps < max_steps:
            try:
                self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as e:
                # Billed before parsing failed, so query() never charged it to self.cost.
                self.cost += e.messages[0].get("extra", {}).get("cost", 0.0)
                self.n_consecutive_format_errors += 1
                limit = self.config.max_consecutive_format_errors
                if 0 < limit <= self.n_consecutive_format_errors:
                    self.add_messages(*e.messages, {
                        "role": "exit", "content": "RepeatedFormatError",
                        "extra": {"exit_status": "RepeatedFormatError", "submission": ""}})
                else:
                    self.add_messages(*e.messages)
            except InterruptAgentFlow as e:  # Submitted, LimitsExceeded, TimeExceeded, ...
                self.add_messages(*e.messages)
            except Exception as e:
                self.handle_uncaught_exception(e)
                raise
            finally:
                self.save(self.config.output_path)
            steps += 1
            if self.messages[-1].get("role") == "exit":
                return steps, True
        return steps, False

    # ----------------------------------------------------------- snapshots
    def _snapshot_patch(self) -> tuple[PatchInfo, str]:
        try:
            text = self.renv.raw(self.diff_cmd).get("output", "")
        except Exception:
            text = ""
        files = re.findall(r"^diff --git a/(\S+)", text, flags=re.M)
        info = PatchInfo(nonempty=bool(text.strip()), sha=hashlib.sha1(text.encode()).hexdigest()[:12],
                         n_lines=len(text.splitlines()), files=files)
        return info, text

    def _restore_checkpoint(self, patch_text: str) -> None:
        prefix = "cd /testbed && " if self.diff_cmd.startswith("cd /testbed") else ""
        try:
            self.renv.raw(f"{prefix}git checkout -- .")
            if patch_text.strip():
                import base64
                b64 = base64.b64encode(patch_text.encode("utf-8")).decode("ascii")
                self.renv.raw(
                    f'{prefix}python3 -c "import base64,subprocess; '
                    f'subprocess.run([\'git\',\'apply\'], input=base64.b64decode(\'{b64}\'))"'
                )
        except Exception:
            pass

    def _log_round(self, s: RoundState, records: list[dict], d: Decision) -> None:
        entry = {
            "round": s.round_idx, "policy": self.policy.name, "run_id": self.run_id,
            "ts": time.time(), "steps_this_round": s.steps_this_round, "total_steps": s.total_steps,
            "actions": [{"command": r["command"][:500], "returncode": r["returncode"],
                         "output_tail": r.get("output", "")[-300:]} for r in records],
            "tests": [{k: (v[-400:] if k == "tail" else v) for k, v in c.__dict__.items()} for c in s.checks],
            "patch": {"sha": s.patch.sha, "nonempty": s.patch.nonempty,
                      "n_lines": s.patch.n_lines, "files": s.patch.files},
            "cost": {"round_usd": s.round_cost, "cum_usd": s.cum_cost,
                     "cum_input_tokens": s.cum_input_tokens, "cum_output_tokens": s.cum_output_tokens},
            "decision": {"action": d.action, "next_steps": d.next_steps,
                         "reason": d.reason, "revert_to_best": d.revert_to_best, "nudge": d.nudge[:600]},
        }
        self.round_logs.append(entry)
        with self.rounds_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    # ------------------------------------------------------------ finishing
    def _finalize(self, decision: Decision, rounds: int, total_steps: int) -> dict:
        last = self.messages[-1].get("extra", {}) if self.messages else {}
        exit_status = last.get("exit_status")
        if self.messages[-1].get("role") != "exit":  # policy stopped the run
            exit_status = "PolicyStop"
        submission = (last.get("submission") or "") if exit_status == "Submitted" else ""
        if submission.strip():
            patch, source = submission, "agent_submission"
        else:
            patch, source = self.renv.raw(self.diff_cmd).get("output", ""), "harness_diff"
        if self.messages[-1].get("role") != "exit":
            self.add_messages(self.model.format_message(
                role="exit", content=f"PolicyStop:{decision.reason}",
                extra={"exit_status": "PolicyStop", "submission": patch}))
        self.save(self.config.output_path)
        tot = self.tracker.totals(self.run_id)
        return {"exit_status": exit_status, "stop_reason": decision.reason, "patch": patch,
                "patch_source": source, "rounds": rounds, "total_steps": total_steps, **tot}

    def serialize(self, *extra_dicts) -> dict:
        info = {"info": {"policy": self.policy.describe(), "run_id": self.run_id,
                         "rounds": self.round_logs}}
        return super().serialize(info, *extra_dicts)
