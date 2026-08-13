"""Typed validation-gate contracts for tests.

Gates are constructed from a :class:`ValidationGateContract`, never from a
free-form command plus a separately chosen suite (#25). These helpers keep
that shape readable in tests without letting a test build a contract whose
kind and command disagree — which is the defect under test.
"""

from __future__ import annotations

from issue_orchestrator.domain.validation_profile import (
    DEFAULT_VALIDATION_PROFILE,
    ValidationGateKind,
)
from issue_orchestrator.infra.validation_profiles import ValidationGateContract


def quick_contract(
    *,
    cmd: str | None = None,
    timeout_seconds: int = 300,
    profile: str = DEFAULT_VALIDATION_PROFILE,
) -> ValidationGateContract:
    """The ``validation.quick`` contract of a profile."""
    return ValidationGateContract(
        kind=ValidationGateKind.QUICK,
        profile=profile,
        cmd=cmd,
        timeout_seconds=timeout_seconds,
    )


def publish_contract(
    *,
    cmd: str | None = None,
    timeout_seconds: int = 1800,
    profile: str = DEFAULT_VALIDATION_PROFILE,
) -> ValidationGateContract:
    """The ``validation.publish`` contract of a profile."""
    return ValidationGateContract(
        kind=ValidationGateKind.PUBLISH,
        profile=profile,
        cmd=cmd,
        timeout_seconds=timeout_seconds,
    )
