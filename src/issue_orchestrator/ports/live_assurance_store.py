"""Port for durable live-agent assurance evidence (#194).

Keyed by the **artifact commit alone**, not by ``(issue, commit)``. A
:class:`~..domain.attempt.AttemptKey` names a candidate somebody proposed for
an issue; what the assurance lane proves is a property of a *build* — the
runtime a promotion would ship — and the same commit can be reached without
ever having been a candidate. Keying by the artifact is what makes "for that
exact artifact" a storage property rather than a field two writers could
disagree about.

The store is deliberately small: record and read. There is no "latest", no
scan and no per-issue enumeration, because every question this evidence
answers names the artifact it is about.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.live_assurance import LiveAssuranceRecord


@runtime_checkable
class LiveAssuranceStore(Protocol):
    """Durable read/write of the live-assurance lane's result for one artifact."""

    def record(self, record: LiveAssuranceRecord) -> None:
        """Persist ``record`` under the artifact it names.

        Implementations MUST use ``record.head_sha`` as the key rather than
        accepting one separately: a record filed under an artifact it does not
        describe is the stale-SHA defect wearing a different hat.
        """
        ...

    def for_artifact(self, head_sha: str) -> LiveAssuranceRecord | None:
        """The recorded result for ``head_sha``, or ``None`` if none exists.

        ``None`` means "the lane never ran for this artifact". A record that
        does not parse raises instead, so a gate cannot read corruption as
        absence — nor absence as a pass.
        """
        ...


__all__ = ["LiveAssuranceStore"]
