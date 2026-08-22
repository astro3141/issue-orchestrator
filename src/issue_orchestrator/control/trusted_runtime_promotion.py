"""The gate a trusted-runtime promotion has to pass (#194).

Self-hosting has one rule that outranks the rest: a trusted pinned runtime
orchestrates this repository, and the candidate's own possibly-modified source
never runs itself. Moving that pin — *promotion* — is therefore the single act
that lets unreviewed behaviour become the thing doing the reviewing.

Until now the discipline was documentary. The procedure lived in prose (issue
#18, ``docs/selfhosting/SELF_HOSTING_READY.md``: "ship the runtime you
verified"), and nothing in the codebase could refuse a promotion that skipped
it. This is its first machine-checkable artifact: a promotion is admitted only
when the live-assurance lane recorded ``PASS`` **for that exact artifact**.

Why assurance evidence and not publication evidence. They answer different
questions, and only one of them is about the boundary a promotion puts into
production. A publication receipt says a candidate passed the publication
contract — which, by the same #194 migration that created this gate, no longer
runs the live provider probes at all. Admitting a promotion on it would
certify the runtime's sandbox boundary with evidence that never touched the
sandbox. So the gate reads :class:`~..ports.live_assurance_store.
LiveAssuranceStore` and nothing else, and the record type it gets back cannot
be constructed from a validation suite label (see
:mod:`..domain.live_assurance`).

Fail-fast, not advisory: :meth:`TrustedRuntimePromotion.admit` raises. A gate
that returned a boolean would let a caller forget to check it, and "forgot to
check" is exactly the failure mode the prose procedure already had.
"""

from __future__ import annotations

from ..domain.commit_sha import normalize_commit_sha
from ..ports.live_assurance_store import LiveAssuranceStore


class TrustedRuntimePromotionRefused(Exception):
    """A promotion was asked for without live-assurance ``PASS`` on the artifact."""


class TrustedRuntimePromotion:
    """Admits or refuses one artifact as a trusted runtime."""

    def __init__(self, assurance: LiveAssuranceStore) -> None:
        self._assurance = assurance

    def admit(self, head_sha: str) -> str:
        """Allow ``head_sha`` to be promoted, or raise naming what is missing.

        Four refusals, kept distinguishable because they call for different
        operator actions:

        * **No record** — the lane never ran for this artifact. Run it.
        * **``INCONCLUSIVE``** — it ran and proved nothing (provider
          unavailable, timed out, or the model never issued the required
          operation). Re-running the *assurance lane* is the remedy, and it is
          the only re-run this design admits anywhere; it is emphatically not
          a retry of any candidate's validation.
        * **``SECURITY_FAIL``** — the boundary was exercised and it did not
          hold. Nothing to re-run; the artifact must not ship.
        * **Recorded from a modified working tree** — the probes exercised
          something this commit does not name. Commit, then run the lane.

        Which of those applies is :class:`~..domain.live_assurance.
        LiveAssuranceRecord`'s to say, not this gate's: asking the record why
        keeps one rule with one implementation, where restating it here would
        be a second enumeration free to drift.

        Returns the normalised artifact SHA it admitted, so a caller reporting
        success names the same string the record is keyed by.
        """
        artifact = normalize_commit_sha(head_sha, field_name="head_sha")
        record = self._assurance.for_artifact(artifact)
        if record is None:
            raise TrustedRuntimePromotionRefused(
                f"no live-assurance record for artifact {artifact}; "
                "the live-assurance lane has not run against this build"
            )
        reason = record.why_not_assuring(artifact)
        if reason is not None:
            raise TrustedRuntimePromotionRefused(
                f"live-assurance for artifact {artifact} does not assure it: "
                f"{reason}: {record.detail}"
            )
        return artifact


__all__ = ["TrustedRuntimePromotion", "TrustedRuntimePromotionRefused"]
