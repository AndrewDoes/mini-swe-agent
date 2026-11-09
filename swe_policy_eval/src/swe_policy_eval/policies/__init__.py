from .base import CheckRun, Decision, PatchInfo, Policy, RoundState
from .controller import ControllerPolicy
from .fixed import FixedPolicy

REGISTRY = {"fixed": FixedPolicy, "controller": ControllerPolicy}


def build_policy(name: str, params: dict | None = None) -> Policy:
    return REGISTRY[name](**(params or {}))


__all__ = ["CheckRun", "Decision", "PatchInfo", "Policy", "RoundState",
           "FixedPolicy", "ControllerPolicy", "build_policy", "REGISTRY"]
