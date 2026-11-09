"""Detect test-runner commands among the agent's actions and parse their output.

Only *visible* signals are used (tests the agent itself ran). The hidden
FAIL_TO_PASS / PASS_TO_PASS tests are never executed or shown during a run.
"""
from __future__ import annotations

import re

from .policies.base import CheckRun

_TEST_CMD = re.compile(
    r"(?:^|\s)(?:pytest|py\.test)\b|-m\s+pytest\b|runtests\.py|manage\.py\s+test|\btox\b"
    r"|-m\s+unittest\b|\bnosetests\b|(?:^|\s)bin/test\b"
)
_SKIP_VERB = re.compile(r"^\s*(grep|rg|cat|sed|head|tail|less|find|ls|git|echo|pip|which)\b")
_SPLIT = re.compile(r"&&|;|\|\||\|")


def is_test_command(command: str) -> bool:
    for piece in _SPLIT.split(command):
        if _TEST_CMD.search(piece) and not _SKIP_VERB.match(piece):
            return True
    return False


def _last_int(pattern: str, text: str) -> int:
    found = re.findall(pattern, text)
    return int(found[-1]) if found else 0


def parse_check_output(command: str, output: str, returncode: int | None) -> CheckRun:
    passed = _last_int(r"(\d+) passed", output)
    failed = _last_int(r"(\d+) failed", output)
    errors = _last_int(r"(\d+) errors?\b", output)
    ran = _last_int(r"Ran (\d+) tests?", output)
    m = re.findall(r"FAILED \(([^)]*)\)", output)
    if m:  # Django / unittest style: FAILED (failures=2, errors=1)
        failed = max(failed, _last_int(r"failures=(\d+)", m[-1]))
        errors = max(errors, _last_int(r"errors=(\d+)", m[-1]))
    nothing_ran = returncode == 5 or "no tests ran" in output.lower() or "collected 0 items" in output
    if returncode is None or nothing_ran:
        ok = None
    elif returncode == 0 and failed + errors == 0 and (passed + ran) > 0:
        ok = True
    elif returncode == 0 and failed + errors == 0:
        ok = None  # exit 0 but nothing countable: unknown
    else:
        ok = False
    tail = "\n".join(output.strip().splitlines()[-25:])[-1800:]
    return CheckRun(command=command[:300], returncode=returncode, ok=ok, passed=passed,
                    failed=failed, errors=errors, ran=ran, tail=tail)


def extract_checks(records: list[dict]) -> list[CheckRun]:
    return [parse_check_output(r["command"], r.get("output", ""), r.get("returncode"))
            for r in records if is_test_command(r["command"])]
