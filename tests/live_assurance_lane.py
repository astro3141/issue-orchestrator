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

    def file(self, store: LiveAssuranceStore, head_sha: str) -> LiveAssuranceRecord:
        """Persist this run's outcome against the artifact it ran on."""
        record = LiveAssuranceRecord(
            head_sha=head_sha, outcome=self.outcome, detail=self.detail
        )
        store.record(record)
        return record


def _first_line(text: object) -> str:
    rendered = str(text).strip()
    if not rendered:
        return "(no detail)"
    return rendered.splitlines()[0][:300]


class _LaneCollector:
    """Bridges pytest's hooks onto :class:`LiveAssuranceLane`."""

    def __init__(
        self, lane: LiveAssuranceLane, store: LiveAssuranceStore, head_sha: str
    ) -> None:
        self._lane = lane
        self._store = store
        self._head_sha = head_sha
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
            self._lane.record_breach(nodeid, _first_line(excinfo.value))
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
        record = self._lane.file(self._store, self._head_sha)
        writer = session.config.get_terminal_writer()
        writer.line("")
        writer.line(
            f"[live-assurance] outcome={record.outcome.value} "
            f"artifact={record.head_sha} detail={record.detail}"
        )


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("live-assurance")
    group.addoption(
        "--live-assurance-root",
        default=None,
        help=(
            "Checkout under which .issue-orchestrator/live-assurance/<sha>.json "
            "is written. Enables the lane's record."
        ),
    )
    group.addoption(
        "--live-assurance-head-sha",
        default=None,
        help="Full 40-character SHA of the artifact this lane run is about.",
    )


def pytest_configure(config: pytest.Config) -> None:
    root = config.getoption("--live-assurance-root")
    head_sha = config.getoption("--live-assurance-head-sha")
    if root is None and head_sha is None:
        # Plugin loaded for an ad-hoc run of the probes. Nothing to file, and
        # inventing an artifact identity would be worse than filing nothing.
        return
    if root is None or head_sha is None:
        raise pytest.UsageError(
            "--live-assurance-root and --live-assurance-head-sha must be given "
            "together: a record with no artifact identity proves nothing."
        )
    collector = _LaneCollector(
        LiveAssuranceLane(), live_assurance_store_for(Path(str(root))), str(head_sha)
    )
    config.pluginmanager.register(collector, "live-assurance-collector")


__all__ = ["LiveAssuranceLane"]
