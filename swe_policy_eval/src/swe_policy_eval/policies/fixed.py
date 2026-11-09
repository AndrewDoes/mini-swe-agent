"""Fixed policy: a pre-committed schedule that ignores everything it observes.

Always R rounds of K steps with the same neutral nudge between rounds. It never
stops early (unless the agent submits on its own or a hard cap fires), so its
cost is set by the schedule, not by how the run is going.
"""
from __future__ import annotations

from .base import Decision, Policy, RoundState

DEFAULT_NUDGE = (
    "Continue working on the task. Round {next_round} of {rounds} is starting. "
    "When you are confident the fix is complete, submit it as instructed."
)


class FixedPolicy(Policy):
    name = "fixed"

    def __init__(self, rounds: int = 4, steps_per_round: int = 15, nudge: str = DEFAULT_NUDGE):
        self.rounds = rounds
        self.steps_per_round = steps_per_round
        self.nudge = nudge

    def first_round_steps(self) -> int:
        return self.steps_per_round

    def decide(self, state: RoundState) -> Decision:
        if state.round_idx >= self.rounds:
            return Decision("stop", reason="fixed_schedule_complete")
        if state.remaining_steps <= 0:
            return Decision("stop", reason="step_ceiling")
        return Decision(
            "continue",
            next_steps=min(self.steps_per_round, state.remaining_steps),
            nudge=self.nudge.format(next_round=state.round_idx + 1, rounds=self.rounds),
            reason="fixed_schedule",
        )

    def describe(self) -> dict:
        return {"name": self.name, "rounds": self.rounds, "steps_per_round": self.steps_per_round}
