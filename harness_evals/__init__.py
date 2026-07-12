"""Reusable A/B evaluation system for agent skills and instruction bundles."""

from .manifest import ManifestError, ProviderConfig, SuiteSpec, load_suite
from .providers import (
    ClaudeCliProvider,
    FakeProvider,
    ProviderError,
    ProviderExecutionPolicy,
    ProviderResult,
    execution_policy_for,
)
from .runner import EvalRunner, RunSelection, RunnerError

__version__ = "0.2.0"

__all__ = [
    "ClaudeCliProvider",
    "EvalRunner",
    "FakeProvider",
    "ManifestError",
    "ProviderConfig",
    "ProviderError",
    "ProviderExecutionPolicy",
    "ProviderResult",
    "RunSelection",
    "RunnerError",
    "SuiteSpec",
    "execution_policy_for",
    "load_suite",
    "__version__",
]
