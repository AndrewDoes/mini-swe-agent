"""Environment proxy that records every agent action of the current round."""
from __future__ import annotations

import time


class RecordingEnv:
    def __init__(self, env):
        self._env = env
        self.records: list[dict] = []

    def execute(self, action, *args, **kwargs):
        command = action.get("command", "") if isinstance(action, dict) else str(action)
        t0 = time.time()
        try:
            out = self._env.execute(action, *args, **kwargs)
        except Exception as e:  # e.g. Submitted: the agent ended the task
            self.records.append({"command": command, "returncode": None, "output": "",
                                 "raised": type(e).__name__, "secs": time.time() - t0})
            raise
        self.records.append({"command": command, "returncode": out.get("returncode"),
                             "output": out.get("output", ""), "secs": time.time() - t0})
        return out

    def drain(self) -> list[dict]:
        records, self.records = self.records, []
        return records

    def raw(self, command: str) -> dict:
        """Harness-side command: not shown to the agent, not recorded."""
        return self._env.execute({"command": command})

    def __getattr__(self, name):
        return getattr(self._env, name)
