"""Per-call cost ledger with hard caps (Procedure step 2).

Every model call is appended to a JSONL ledger with input tokens, output tokens
and dollar cost. Two caps are enforced *before* each call:

* per-run cap    -> the run ends gracefully with exit_status "RunBudgetExceeded"
* global cap     -> GlobalBudgetExceeded is raised and the whole experiment stops

A call's cost is only known after it returns, so each check adds
``reserve_per_call_usd`` (a worst-case estimate for one call) to the running
total. Overshoot is bounded by how much one call can exceed that reserve.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from minisweagent.exceptions import FormatError, LimitsExceeded


class GlobalBudgetExceeded(Exception):
    """Raised when the experiment-wide budget ceiling would be crossed."""


class CostTracker:
    def __init__(self, ledger_path: Path, global_cap_usd: float, per_run_cap_usd: float,
                 reserve_per_call_usd: float = 0.10):
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.global_cap = global_cap_usd
        self.per_run_cap = per_run_cap_usd
        self.reserve = reserve_per_call_usd
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}
        self.global_cost = 0.0
        # Resume support: restore global spend from an existing ledger.
        if self.ledger_path.exists():
            for line in self.ledger_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self.global_cost += json.loads(line).get("cost_usd", 0.0)

    # ------------------------------------------------------------------ caps
    def check(self, run_id: str) -> None:
        with self._lock:
            if self.global_cost + self.reserve > self.global_cap:
                raise GlobalBudgetExceeded(
                    f"global cap ${self.global_cap:.2f} reached (spent ${self.global_cost:.2f})")
            spent = self._runs.get(run_id, {}).get("cost", 0.0)
            if spent + self.reserve > self.per_run_cap:
                raise LimitsExceeded({
                    "role": "exit", "content": "RunBudgetExceeded",
                    "extra": {"exit_status": "RunBudgetExceeded", "submission": ""},
                })

    # ---------------------------------------------------------------- record
    def record(self, run_id: str, model: str, input_tokens: int, output_tokens: int,
               cost_usd: float, cached_input_tokens: int = 0) -> None:
        with self._lock:
            r = self._runs.setdefault(run_id, {"cost": 0.0, "in": 0, "out": 0, "calls": 0})
            r["cost"] += cost_usd
            r["in"] += input_tokens
            r["out"] += output_tokens
            r["calls"] += 1
            self.global_cost += cost_usd
            entry = {
                "ts": time.time(), "run_id": run_id, "call": r["calls"], "model": model,
                "input_tokens": input_tokens, "output_tokens": output_tokens,
                "cached_input_tokens": cached_input_tokens, "cost_usd": cost_usd,
                "run_cost_usd": r["cost"], "global_cost_usd": self.global_cost,
            }
            with self.ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")

    def totals(self, run_id: str) -> dict:
        with self._lock:
            r = self._runs.get(run_id, {"cost": 0.0, "in": 0, "out": 0, "calls": 0})
            return {"cost_usd": r["cost"], "input_tokens": r["in"],
                    "output_tokens": r["out"], "calls": r["calls"]}


def _usage_from_extra(extra: dict) -> tuple[int, int, int]:
    resp = extra.get("response")
    usage = resp.get("usage") if isinstance(resp, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0
    inp = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    out = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    details = usage.get("prompt_tokens_details") or {}
    cached = (details.get("cached_tokens") if isinstance(details, dict) else 0) \
        or usage.get("cache_read_input_tokens", 0) or 0
    return int(inp), int(out), int(cached)


class TrackedModel:
    """Transparent proxy around a mini-swe-agent model that feeds the CostTracker."""

    def __init__(self, model, tracker: CostTracker, run_id: str):
        self._model = model
        self._tracker = tracker
        self._run_id = run_id

    def query(self, messages, **kwargs):
        self._tracker.check(self._run_id)
        try:
            message = self._model.query(messages, **kwargs)
        except FormatError as e:  # the call was billed even though parsing failed
            self._log(e.messages[0].get("extra", {}))
            raise
        self._log(message.get("extra", {}))
        return message

    def _log(self, extra: dict) -> None:
        inp, out, cached = _usage_from_extra(extra)
        name = getattr(getattr(self._model, "config", None), "model_name", "unknown")
        self._tracker.record(self._run_id, name, inp, out, float(extra.get("cost", 0.0)), cached)

    def __getattr__(self, name):
        return getattr(self._model, name)
