"""Port for attempt-scoped state persistence."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from ..domain.attempt import Attempt, AttemptKey
from ..domain.issue_key import IssueKey


@runtime_checkable
class AttemptStore(Protocol):
    """Persistence boundary for #6130 attempt state."""

    def for_key(self, key: AttemptKey) -> Attempt | None:
        """Return an attempt record for ``key`` if one exists."""
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

    def supersede_issue(self, issue_key: IssueKey) -> int:
        """Drop all cached attempts for an issue.

        Returns the number of sidecars removed. This is used by scratch reset:
        correctness comes from new SHAs missing by construction; proactive
        cleanup keeps old attempt sidecars from accumulating.
        """
        ...
