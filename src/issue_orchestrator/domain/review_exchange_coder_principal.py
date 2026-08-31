"""Which principal occupies the review-exchange CODER side (#388).

The exchange has two sides and names them by lane: ``coder`` and ``reviewer``.
The lane name is a position in the protocol, not an authority — and until #388
it was read as both. A Tech Lead whose completion offers a change for review
enters the coder side (``control/completion_review_exchange`` sets
``coder_label = agent_label``, and ``Config.get_reviewer_for_agent`` resolves a
reviewer for a tech-lead agent like any other), and the lane then handed it the
Actor's completion protocol: ``resources/review_exchange_coder.md``, whose step
3 makes ``prepush-check --dirty-only -v`` mandatory. That command writes
``<git-common-dir>/issue-orchestrator/validate-timings.jsonl``, outside a
bounded Tech Lead's sandbox write roots — the same wall #383 measured and #385
moved off the primary lane.

This enum is the answer to "whose completion contract does the coder side run
under", and it carries BOTH consequences of that answer, because they were
never two questions:

* :attr:`~ReviewExchangeCoderPrincipal.task_kind` — the completion protocol
  document the session is handed and the sandbox role it resolves to;
* :attr:`~ReviewExchangeCoderPrincipal.files_its_own_turn_validation` — whether
  the model session's own ``coding-done`` produces the round's validation
  evidence, or a trusted owner outside that sandbox does.

A document swap alone would not have been a repair: the exchange does not
merely *instruct* the coder to validate, it *requires the artifact* — a passing
``validation-record.json`` naming current HEAD — and only ``coding-done
completed`` writes one. Splitting the answer across two places is how a lane
could hand out the Tech Lead document and still fail the turn for the record
that document no longer asks for.

It lives in ``domain`` with no I/O so producer and reader share one vocabulary:
the launcher declares it into the session env, and ``coding-done`` reads that
declaration back to route its own gate.
"""

from __future__ import annotations

import enum

from .sandbox_scope import (
    REVIEW_EXCHANGE_CODER_TASK_KIND,
    REVIEW_EXCHANGE_TECH_LEAD_TASK_KIND,
)

__all__ = [
    "EXCHANGE_CODER_PRINCIPAL_ENV_SUFFIX",
    "ReviewExchangeCoderPrincipal",
]

#: Env var (after ``infra.env.ENV_PREFIX``) the exchange launcher declares the
#: coder side's principal into. Named here, beside the enum, so the producer
#: (``execution.persistent_session_exchange``) and the reader
#: (``entrypoints.cli_tools.coding_done``) cannot drift on the spelling.
EXCHANGE_CODER_PRINCIPAL_ENV_SUFFIX = "EXCHANGE_CODER_PRINCIPAL"


class ReviewExchangeCoderPrincipal(enum.Enum):
    """Who is sitting on the coder side of one review exchange."""

    #: An ordinary coding agent. Everything about this lane is unchanged: it
    #: runs the Actor's exchange protocol and files its own turn validation.
    ACTOR = "actor"

    #: The configured Tech Lead agent, reworking a candidate it authored. It
    #: keeps the completion contract #385 gave it on the primary lane: it is
    #: asked for a committed, clean checkout, and never for a host or
    #: shared-repository write.
    TECH_LEAD = "tech_lead"

    @property
    def task_kind(self) -> str:
        """The task kind the coder side launches under.

        Selects the completion protocol document
        (``resources.get_completion_instructions``) and the sandbox role
        (``domain.sandbox_scope``). Those two selections must agree, which is
        why one value drives both rather than each site deciding.
        """
        if self is ReviewExchangeCoderPrincipal.TECH_LEAD:
            return REVIEW_EXCHANGE_TECH_LEAD_TASK_KIND
        return REVIEW_EXCHANGE_CODER_TASK_KIND

    @property
    def files_its_own_turn_validation(self) -> bool:
        """Whether this principal's own ``coding-done`` produces the evidence.

        ``True`` for an Actor: its ``coding-done completed`` runs the
        code-candidate quick gate and writes the ``validation-record.json`` the
        round is judged on, exactly as before #388.

        ``False`` for a Tech Lead: the quick gate needs the same
        host/shared-repository effects the pre-push step does (#364/#370), so
        the round's evidence is produced by the trusted owner outside the model
        sandbox instead. Nothing is skipped; the owner changed.
        """
        return self is ReviewExchangeCoderPrincipal.ACTOR

    @classmethod
    def declared(cls, raw: str | None) -> "ReviewExchangeCoderPrincipal":
        """Read an owner-injected declaration, failing SAFE to ``ACTOR``.

        Absent, empty, or unrecognised text means "nobody declared a special
        principal", and the safe answer to that is the ordinary lane: an Actor
        runs its own gate. The unsafe direction would be inferring TECH_LEAD
        from noise, which would route a real coder's validation away from the
        session that owes it — so only the exact recorded value does that.
        """
        if not raw:
            return cls.ACTOR
        try:
            return cls(raw.strip())
        except ValueError:
            return cls.ACTOR
