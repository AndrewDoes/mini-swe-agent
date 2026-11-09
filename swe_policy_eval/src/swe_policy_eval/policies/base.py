"""Common interface for the two policies compared in the experiment.

A *round* is a burst of up to N agent steps (model call + action execution).
Between rounds the harness builds a ``RoundState`` from visible signals only and
asks the policy for a ``Decision``: stop, or continue with a given step budget
and an optional nudge message appended to the conversation.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class PatchInfo:
    nonempty: bool
    sha: str
    n_lines: int
    files: list[str] = field(default_factory=list)


@dataclass
class CheckRun:
    """One test-runner command the agent executed during the round."""
    command: str
    returncode: int | None
    ok: bool | None          # True = passed, False = failed, None = unknown / nothing ran
    passed: int = 0
    failed: int = 0
    errors: int = 0
    ran: int = 0
    tail: str = ""


@dataclass
class RoundState:
    round_idx: int                 # 1-based index of the round that just finished
    steps_this_round: int
    total_steps: int
    step_ceiling: int              # same hard step ceiling for both policies
    cum_cost: float
    round_cost: float
    run_cost_cap: float
    cum_input_tokens: int
    cum_output_tokens: int
    patch: PatchInfo               # source-only diff at the end of the round
    prev_patch_shas: list[str]     # patch hashes at the end of earlier rounds
    checks: list[CheckRun]         # test runs the agent made during this round
    n_actions: int
    history: list[dict] = field(default_factory=list)

    @property
    def remaining_steps(self) -> int:
        return max(0, self.step_ceiling - self.total_steps)


@dataclass
class Decision:
    action: Literal["continue", "stop"]
    next_steps: int = 0
    nudge: str = ""
    reason: str = ""
    revert_to_best: bool = False


class Policy(ABC):
    name: str = "base"

    @abstractmethod
    def first_round_steps(self) -> int: ...

    @abstractmethod
    def decide(self, state: RoundState) -> Decision: ...

    def describe(self) -> dict:
        return {"name": self.name}
