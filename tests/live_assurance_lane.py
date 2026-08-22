"""The live-assurance lane, as a pytest plugin (#194).

Registered with ``-p tests.live_assurance_lane`` by the ``test-live-assurance``
Makefile target and by nothing else. It watches the selected ``live_agent``
tests and reduces the whole run to one of exactly three outcomes, which it
files against the artifact the lane ran on through
:class:`~issue_orchestrator.ports.live_assurance_store.LiveAssuranceStore`.

The reduction, in the order that matters:

* any :class:`~tests.live_assurance.SandboxBreach` → ``SECURITY_FAIL``
* otherwise any skip, timeout, non-breach failure, or an empty selection →
  ``INCONCLUSIVE``
* otherwise → ``PASS``

The precedence itself is not decided here — it lives in
:meth:`~issue_orchestrator.domain.live_assurance.LiveAssuranceOutcome.observed`
so the rule is stated once, in the vocabulary that also defines the outcomes.
What this module owns is the pytest-specific half: which *report* counts as a
breach and which as an incomplete observation.

The artifact the record is filed against comes from
:func:`~issue_orchestrator.execution.assured_artifact.artifact_under_assurance`
applied to ``--live-assurance-root``, not from a SHA the caller supplies
alongside it. One root, one artifact: the commit and the working-tree state are
read from the same checkout the record is written under, so a record cannot be
about a tree other than the one it lives in.

An **empty selection is ``INCONCLUSIVE``**, never a pass. A lane whose marker
expression stopped matching anything would otherwise certify a runtime having
executed no probe at all — the vacuous-pass shape the probe module already
guards against inside individual assertions.

The lane is serial by construction (the probes share authenticated provider
CLIs and provider account state), and this plugin depends on that: exception
classification happens in the process that ran the test, so a ``-n`` run would
classify in workers whose state never reaches ``pytest_sessionfinish`` here.
``tests/unit/test_makefile_validation_phases.py`` pins the target against
growing a ``-n``.

Re-running the lane after an ``INCONCLUSIVE`` is availability handling for
assurance evidence. It is not, and must never become, a retry of any
candidate's validation: nothing in this module re-runs a test, and no outcome
here is readable as a validation verdict.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.live_assurance import (
    LiveAssuranceOutcome,
    LiveAssuranceRecord,
)
from issue_orchestrator.execution.assured_artifact import (
    AssuredArtifact,
    AssuredArtifactUnresolvable,
    artifact_under_assurance,
)
from issue_orchestrator.execution.live_assurance_provider import (
    live_assurance_store_for,
)
from issue_orchestrator.ports.live_assurance_store import LiveAssuranceStore

from .live_assurance import SandboxBreach

_MAX_DETAIL_ENTRIES = 20


class LiveAssuranceLane:
    """Accumulates one lane run's observations and renders its outcome."""

    def __init__(self) -> None:
        self._breaches: list[str] = []
        self._incomplete: list[str] = []
        self._executed = 0

    @property
    def outcome(self) -> LiveAssuranceOutcome:
        """The single result this run reached."""
        return LiveAssuranceOutcome.observed(
            breached=bool(self._breaches),
            incomplete=bool(self._incomplete) or self._executed == 0,
        )

    @property
    def probes_executed(self) -> int:
        """How many probes completed a call phase, for the record's own field."""
        return self._executed

    @property
    def detail(self) -> str:
        """A short, ordered account of why, preserved rather than discarded.

        An ``INCONCLUSIVE`` whose reason was dropped is indistinguishable from
        one nobody looked at, and #109's whole complaint is that failed
        observations were being reinterpreted instead of kept.
        """
        reasons = [
            *(f"SECURITY_FAIL: {entry}" for entry in self._breaches),
            *(f"INCONCLUSIVE: {entry}" for entry in self._incomplete),
        ]
        if not reasons:
            if self._executed == 0:
                # The empty-selection INCONCLUSIVE. Saying "0 probes passed"
                # would be true and useless; the reader needs to know that the
                # lane selected nothing, which is a broken marker expression,
                # not an unavailable provider.
                return "no live-agent probe was selected, so nothing was proven"
            return f"{self._executed} live-agent probe(s) passed"
        kept = reasons[:_MAX_DETAIL_ENTRIES]
        if len(reasons) > _MAX_DETAIL_ENTRIES:
            kept.append(f"... and {len(reasons) - _MAX_DETAIL_ENTRIES} more")
        return " | ".join(kept)

    def record_breach(self, nodeid: str, reason: str) -> None:
        self._breaches.append(f"{nodeid}: {reason}")

    def record_incomplete(self, nodeid: str, reason: str) -> None:
        self._incomplete.append(f"{nodeid}: {reason}")

    def record_executed(self) -> None:
        self._executed += 1

    def file(
        self, store: LiveAssuranceStore, artifact: AssuredArtifact
    ) -> LiveAssuranceRecord:
        """Persist this run's outcome against the artifact it ran on."""
        record = LiveAssuranceRecord(
            head_sha=artifact.head_sha,
            outcome=self.outcome,
            detail=self.detail,
            working_tree_dirty=artifact.working_tree_dirty,
            probes_executed=self._executed,
        )
        store.record(record)
        return record


def _first_line(text: object) -> str:
    """An incomplete observation's reason, which is a sentence at most."""
    rendered = str(text).strip()
    if not rendered:
        return "(no detail)"
    return rendered.splitlines()[0][:300]


_MAX_BREACH_DETAIL = 4000


def _breach_detail(text: object) -> str:
    """A breach's evidence, kept whole rather than reduced to a first line.

    ``_first_line`` is right for an ``INCONCLUSIVE``: the reason is short and
    the rest is a traceback. A ``SECURITY_FAIL`` is the opposite case. Its
    ``detail`` is the durable artifact an operator reads when
    ``trusted-runtime-promote`` refuses — the probes are gone by then — and the
    breach assertions deliberately embed file contents and provider output
    across many lines. Truncating that to 300 characters would drop exactly
    what the record exists to preserve.
    """
    rendered = str(text).strip()
    if not rendered:
        return "(no detail)"
    if len(rendered) <= _MAX_BREACH_DETAIL:
        return rendered
    return rendered[:_MAX_BREACH_DETAIL] + " ... (truncated)"


class _LaneCollector:
    """Bridges pytest's hooks onto :class:`LiveAssuranceLane`."""

    def __init__(
        self,
        lane: LiveAssuranceLane,
        store: LiveAssuranceStore,
        artifact: AssuredArtifact,
    ) -> None:
        self._lane = lane
        self._store = store
        self._artifact = artifact
        self._classified: set[str] = set()

    def pytest_exception_interact(
        self,
        node: pytest.Item | pytest.Collector,
        call: pytest.CallInfo[object],
        report: pytest.TestReport | pytest.CollectReport,
    ) -> None:
        excinfo = call.excinfo
        if excinfo is None:
            return
        nodeid = getattr(node, "nodeid", str(node))
        self._classified.add(nodeid)
        if issubclass(excinfo.type, SandboxBreach):
            self._lane.record_breach(nodeid, _breach_detail(excinfo.value))
        else:
            self._lane.record_incomplete(nodeid, _first_line(excinfo.value))

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.skipped:
            # A skip is a provider/host availability statement, which is
            # exactly what INCONCLUSIVE is for. Recorded once: a setup-phase
            # skip reports under ``when == "setup"``.
            if report.nodeid not in self._classified:
                self._classified.add(report.nodeid)
                self._lane.record_incomplete(
                    report.nodeid, _first_line(report.longrepr)
                )
            return
        if report.when != "call":
            return
        if report.passed:
            self._lane.record_executed()
            return
        if report.nodeid not in self._classified:
            # A failure pytest never routed through ``exception_interact``.
            # Unclassified means unproven, and unproven is never a pass.
            self._classified.add(report.nodeid)
            self._lane.record_incomplete(report.nodeid, _first_line(report.longrepr))

    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        record = self._lane.file(self._store, self._artifact)
        writer = session.config.get_terminal_writer()
        writer.line("")
        writer.line(
            f"[live-assurance] outcome={record.outcome.value} "
            f"artifact={record.head_sha} "
            f"probes_executed={record.probes_executed} "
            f"working_tree_dirty={str(record.working_tree_dirty).lower()} "
            f"detail={record.detail}"
        )
        if record.working_tree_dirty:
            writer.line(
                "[live-assurance] this run observed uncommitted changes, so the "
                "record does not assure the commit it is filed under; commit and "
                "re-run the lane before promoting."
            )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("live-assurance")
    group.addoption(
        "--live-assurance-root",
        default=None,
        help=(
            "Checkout the lane is filing evidence about. Its commit and its "
            "working-tree state are the record's artifact identity, and "
            ".issue-orchestrator/live-assurance/<sha>.json is written under it. "
            "Enables the lane's record."
        ),
    )


def pytest_configure(config: pytest.Config) -> None:
    root = config.getoption("--live-assurance-root")
    if root is None:
        # Plugin loaded for an ad-hoc run of the probes. Nothing to file, and
        # inventing an artifact identity would be worse than filing nothing.
        return
    checkout = Path(str(root))
    try:
        # One root, one artifact. The SHA is resolved from the checkout the
        # record is filed under rather than passed alongside it, so "the record
        # is about a different tree than the one it lives in" is not a state
        # this lane can reach.
        artifact = artifact_under_assurance(checkout)
    except AssuredArtifactUnresolvable as exc:
        raise pytest.UsageError(str(exc)) from exc
    collector = _LaneCollector(
        LiveAssuranceLane(), live_assurance_store_for(checkout), artifact
    )
    config.pluginmanager.register(collector, "live-assurance-collector")


__all__ = ["LiveAssuranceLane"]
