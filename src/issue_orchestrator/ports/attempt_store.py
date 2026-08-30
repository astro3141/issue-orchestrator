"""Port for attempt-scoped state persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..domain.attempt import Attempt, AttemptKey
from ..domain.issue_key import IssueKey


@runtime_checkable
class AttemptStore(Protocol):
    """Persistence boundary for #6130 attempt state.

    Damage is part of this contract, not an implementation detail (#378). A
    record that exists but cannot be read raises
    :class:`~..domain.attempt.CorruptAttemptEvidence`, which names the file,
    the attempt asked for, and why — never ``None``, never a default record.
    "Nothing is recorded" and "the record of what was decided is damaged" have
    different remedies, and a caller that cannot tell them apart will grant
    authority to a candidate on the strength of a broken instrument.

    ``AttemptKey`` refuses an identity that names no repository scope or no
    stable issue id, so no implementation is ever asked to derive a path or a
    payload from one.
    """

    def for_key(self, key: AttemptKey) -> Attempt | None:
        """Return an attempt record for ``key`` if one exists.

        ``None`` means no record — never a record that could not be read.
        """
        ...

    def update(self, key: AttemptKey, mutate: Callable[[Attempt], Attempt]) -> Attempt:
        """Persist ``mutate`` applied to the attempt at ``key``.

        The only write on this port, and it is a *mutation* rather than a
        whole-record replacement on purpose. An attempt now carries durable
        Foundation admission evidence about one ``(issue, commit)`` — the
        validation record path, and (#34) the execution identities §4's I2c is
        read from. A writer that builds a fresh ``Attempt`` to set one field
        erases the rest, and the erasure is silent: the record still parses,
        it just no longer says who reviewed the candidate. Handing the writer
        the current record makes preserving the other facts the shape of the
        call rather than a convention each caller re-implements.

        Creates the record when absent, so no caller distinguishes a first
        write from a later one. Implementations reject a ``mutate`` that
        returns an attempt filed under a different key, and return the
        persisted record.

        What this removes is the *caller-side* erasure — a writer that never
        saw the other fields. It is not a concurrency primitive: the
        read-modify-write is not required to be atomic, so two writers racing
        on one ``(issue, commit)`` can still lose the earlier write. Today's
        writers do not race — validation completes and files its record path
        before the review exchange that files the identities starts — and this
        port relies on that ordering rather than on locking.
        """
        ...

    def for_issue(self, issue_key: IssueKey) -> tuple[Attempt, ...]:
        """Every durable attempt recorded for ``issue_key``, in key order.

        Read-only, and the one way a caller that holds an *issue* can find the
        *candidates* recorded under it. Continuation needs exactly that (#149):
        the candidate whose publication failed is named by a commit nothing on
        the board still points at, so there is no issue-shaped route to it —
        the durable record is the only index.

        Ordered deterministically by the attempt key so two readers of the same
        directory derive the same live set. Damage raises rather than being
        skipped, for the reason :meth:`for_key` does: a caller deciding what is
        live from a partial read would silently conclude that an operation
        stopped, and releasing a lease on that is the one direction a scheduler
        reader may never be moved.
        """
        ...

    def supersede_issue(self, issue_key: IssueKey) -> int:
        """Drop all cached attempts for an issue.

        Returns the number of sidecars removed. This is used by scratch reset:
        correctness comes from new SHAs missing by construction; proactive
        cleanup keeps old attempt sidecars from accumulating.
        """
        ...
