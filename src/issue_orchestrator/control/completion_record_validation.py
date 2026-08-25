"""Completion record loading and worktree validation."""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..domain.models import (
    COMPLETION_STRING_MAX_BYTES,
    CompletionRecord,
    RequestedAction,
    completion_record_path,
    fits_record_string_bound,
    sanitize_agent_label,
)
from ..infra.runtime_artifacts import filter_runtime_managed_dirty_paths
from ..infra.validation_state import truncate_with_tail

if TYPE_CHECKING:
    from ..infra.config import Config

logger = logging.getLogger(__name__)
_DIRTY_FILES_REASON_LIMIT = 8

# Hard cap on the completion record file size before we call ``json.load``.
# Real records are <= a few KB; anything approaching this cap is almost
# certainly abusive or broken. Checking the file size first prevents a
# hostile agent from exhausting memory / CPU by writing, say, a 500 MB
# JSON blob and forcing the orchestrator's parser to walk it. Matches the
# per-field cap in CompletionRecord.from_dict so a well-formed record
# cannot exceed a small multiple of this.
#
# 2 MiB is roughly two orders of magnitude above the largest legitimate
# completion we have seen; tighten further if we ever shrink per-field
# caps.
_MAX_COMPLETION_FILE_BYTES = 2 * 1024 * 1024

# The producer's own retry naming. ``write_completion_record`` appends
# ``-2``, ``-3``, ... to the stem when the canonical path is already
# occupied (``agent_done.py``), so those names — and only those names —
# can hold a retry of this run's completion. An arbitrary neighbouring
# file is not a candidate.
_PRODUCER_RETRY_MIN_INDEX = 2

# Both of the placeholder's fields are untrusted agent output bounded
# only by the file-size gate above, and both travel into logs and event
# payloads. Bound them here rather than at every consumer.
#
# ``error`` is truncated because a long one is still evidence worth
# keeping. ``session_id`` is not truncated but REJECTED past the record
# parser's own field cap: it is matched, not read, and a value the parser
# would refuse cannot be the session_id of any valid retry, so a
# placeholder carrying one can never hand authority to anything. Keeping
# a mangled copy of it would only put unbounded agent text on the
# per-sibling log lines below.
_PRODUCER_ERROR_MAX_CHARS = 2000
_PRODUCER_ERROR_TAIL_CHARS = 500


class WorktreeValidationFailure(Enum):
    """Typed classification for publish-precondition failures."""

    CURRENT_BRANCH_UNKNOWN = "current_branch_unknown"
    PROTECTED_BRANCH = "protected_branch"
    DIRTY_POLICY = "dirty_policy"


class CompletionRecordLoadFailure(str, Enum):
    """Typed reason a completion record could not be loaded."""

    MISSING = "missing"
    UNREADABLE = "unreadable"
    OVERSIZED = "oversized"
    INVALID_JSON = "invalid_json"
    INVALID_SCHEMA = "invalid_schema"


@dataclass(frozen=True)
class ProducerErrorPlaceholder:
    """What ``write_error_completion`` leaves where a record belongs.

    Not a completion: it is the record of a ``coding-done`` that crashed
    while *building* one. It parses, and it carries the session it was
    writing for plus the error that stopped it, but by construction it
    has no ``summary`` and no action payload — so it can never satisfy
    the :class:`CompletionRecord` contract.

    Typed rather than a raw dict because exactly two fields of the
    placeholder are load-bearing, and both are untrusted agent output:
    ``session_id`` is what a retry must match to be considered the same
    run's, and ``error`` is the evidence a repaired retry must never
    silently erase (#264).

    Both are bounded before they get here — see
    ``_producer_error_placeholder`` — so holding one of these cannot put
    unbounded agent text on a per-tick log line or in an event payload.
    """

    session_id: str
    error: str


@dataclass(frozen=True)
class CompletionRecordLoadResult:
    """Result of parsing an untrusted completion record file."""

    path: Path
    record: CompletionRecord | None = None
    failure: CompletionRecordLoadFailure | None = None
    error: str | None = None
    exists: bool = False
    size: int | None = None
    producer_error: ProducerErrorPlaceholder | None = None

    @classmethod
    def missing(cls, record_path: Path) -> "CompletionRecordLoadResult":
        """The one shape for "there is nothing at this path to parse"."""
        return cls(
            path=record_path,
            failure=CompletionRecordLoadFailure.MISSING,
            error="Completion record not found",
        )

    @property
    def ok(self) -> bool:
        return self.record is not None

    @property
    def invalid(self) -> bool:
        return self.failure not in (None, CompletionRecordLoadFailure.MISSING)


@dataclass(frozen=True)
class WorktreeValidationResult:
    ok: bool
    reason: str = ""
    failure: WorktreeValidationFailure | None = None
    blocking_paths: tuple[str, ...] = ()

    @classmethod
    def pass_(cls) -> "WorktreeValidationResult":
        return cls(ok=True)

    @classmethod
    def fail(
        cls,
        failure: WorktreeValidationFailure,
        reason: str,
        *,
        blocking_paths: Sequence[str] = (),
    ) -> "WorktreeValidationResult":
        return cls(
            ok=False,
            reason=reason,
            failure=failure,
            blocking_paths=tuple(blocking_paths),
        )

    @classmethod
    def dirty_policy_failure(
        cls,
        reason: str,
        *,
        blocking_paths: Sequence[str],
    ) -> "WorktreeValidationResult":
        return cls.fail(
            WorktreeValidationFailure.DIRTY_POLICY,
            reason,
            blocking_paths=blocking_paths,
        )


def load_completion_record(record_path: Path) -> CompletionRecord | None:
    """Read and validate a single completion record file.

    Compatibility wrapper for call sites that only need record-or-none.
    New control paths should use ``load_completion_record_result`` so
    missing files and invalid records stay distinguishable.
    """
    return load_completion_record_result(record_path).record


def load_completion_record_result(record_path: Path) -> CompletionRecordLoadResult:
    """Read and validate a single completion record file.

    This is the ONE entry point for parsing an untrusted completion
    record: applies the per-file size gate BEFORE ``json.load`` runs,
    then delegates to ``CompletionRecord.from_dict`` for field-level
    bounds. All call sites (the publish-path validator, the observer
    that scans sessions for completions) must route through this
    function so an agent cannot bypass the gate by hitting a
    duplicate reader — that was the bug flagged in #6017 re-review-2
    P3. Returns a typed result so callers can distinguish a genuinely
    missing completion record from one that was present but rejected.

    Every branch here logs at DEBUG, rejections included. This function
    is polled on the hot path — the observer calls it through
    ``select_completion_record`` on every ``observe_session`` for every
    live session — and a record that is unreadable, oversized, or
    malformed keeps being unreadable, oversized, or malformed on every
    subsequent tick until the session ends. Logging that at ERROR turns
    one torn write into an ERROR line per tick for the whole window,
    which is exactly the signal a real fault would produce.

    Nothing is lost by the demotion: a rejection only becomes a fault
    once a decision has to be made on it, and the decision site says so
    loudly. ``SessionController`` routes an invalid record to
    ``report_invalid_completion_record``, which logs the failure and its
    error at ERROR, emits ``SESSION_INVALID_COMPLETION_RECORD``, and
    writes a diagnostic file — once, when the session terminates.
    """
    if not record_path.exists():
        logger.debug("No completion record found at %s", record_path)
        return CompletionRecordLoadResult.missing(record_path)

    try:
        size = record_path.stat().st_size
    except OSError as exc:
        logger.debug("Could not stat completion record %s: %s", record_path, exc)
        return CompletionRecordLoadResult(
            path=record_path,
            failure=CompletionRecordLoadFailure.UNREADABLE,
            error=f"Could not stat completion record: {exc}",
            exists=True,
        )
    if size > _MAX_COMPLETION_FILE_BYTES:
        error = (
            f"Completion record is {size} bytes, exceeds max "
            f"{_MAX_COMPLETION_FILE_BYTES}"
        )
        logger.debug("%s: %s", record_path, error)
        return CompletionRecordLoadResult(
            path=record_path,
            failure=CompletionRecordLoadFailure.OVERSIZED,
            error=error,
            exists=True,
            size=size,
        )

    # Bound before the parse so the rejection branch can inspect what
    # was actually on disk even when ``json.load`` itself raised.
    raw: object = None
    try:
        with open(record_path) as f:
            data = json.load(f)
        raw = data
        record = CompletionRecord.from_dict(data)
        # DEBUG, not INFO: the observer polls this every tick for every
        # live session, and a session parked in a deferred state (a
        # background review exchange) polls for a long time. The one
        # place a successful read is worth an INFO line is the decision
        # that acts on it, and the controller's completion lookup
        # already logs exactly that — with the chosen path and why.
        logger.debug(
            "Read completion record: outcome=%s session=%s path=%s",
            record.outcome.value,
            record.session_id,
            record_path,
        )
        return CompletionRecordLoadResult(
            path=record_path,
            record=record,
            exists=True,
            size=size,
        )
    except json.JSONDecodeError as exc:
        logger.debug("Invalid JSON in completion record %s: %s", record_path, exc)
        return CompletionRecordLoadResult(
            path=record_path,
            failure=CompletionRecordLoadFailure.INVALID_JSON,
            error=f"Invalid JSON: {exc}",
            exists=True,
            size=size,
        )
    except OSError as exc:
        # The file was there a moment ago. It can stop being readable
        # between the existence check and the read — post-processing
        # cleanup removes it — and this function is polled every tick
        # for every live session, so that race is a load failure to
        # report, never an exception to raise at the caller.
        logger.debug("Could not read completion record %s: %s", record_path, exc)
        return CompletionRecordLoadResult(
            path=record_path,
            failure=CompletionRecordLoadFailure.UNREADABLE,
            error=f"Could not read completion record: {exc}",
            exists=True,
            size=size,
        )
    except ValueError as exc:
        logger.debug("Invalid completion record %s: %s", record_path, exc)
        return CompletionRecordLoadResult(
            path=record_path,
            failure=CompletionRecordLoadFailure.INVALID_SCHEMA,
            error=str(exc),
            exists=True,
            size=size,
            producer_error=_producer_error_placeholder(raw),
        )


def _producer_error_placeholder(raw: object) -> ProducerErrorPlaceholder | None:
    """Classify rejected JSON as a producer-error placeholder, or not.

    Only reached for a file that PARSED and then failed the record
    contract, which is the only shape ``write_error_completion`` can
    leave behind. Both fields are required: without ``agent_done_error``
    the file is some other broken record, and without a ``session_id``
    that a valid record could also carry, nothing can be matched against
    it — either way this is not a placeholder and the caller must leave
    the canonical path in charge.
    """
    if not isinstance(raw, dict):
        return None
    error = raw.get("agent_done_error")
    written_for = raw.get("session_id")
    if not isinstance(error, str) or not error.strip():
        return None
    if not isinstance(written_for, str) or not written_for.strip():
        return None
    if not fits_record_string_bound(written_for):
        # DEBUG for the same reason the other rejection branches are:
        # this is polled every tick for every live session.
        logger.debug(
            "Not a producer-error placeholder: session_id is %d bytes, "
            "past the %d a valid record may carry, so no retry can match it",
            len(written_for.encode("utf-8")),
            COMPLETION_STRING_MAX_BYTES,
        )
        return None
    return ProducerErrorPlaceholder(
        session_id=written_for,
        error=truncate_with_tail(
            error,
            _PRODUCER_ERROR_MAX_CHARS,
            _PRODUCER_ERROR_TAIL_CHARS,
        ),
    )


class CompletionPathChoice(str, Enum):
    """Which file the selection owner made authoritative, and why."""

    CANONICAL = "canonical"
    PRODUCER_ERROR_RETRY = "producer_error_retry"
    AMBIGUOUS_PRODUCER_ERROR_RETRY = "ambiguous_producer_error_retry"


@dataclass(frozen=True)
class CompletionRecordSelection:
    """The authoritative record for a run, plus why that file won."""

    canonical_path: Path
    path: Path
    load_result: CompletionRecordLoadResult
    choice: CompletionPathChoice
    producer_error: str | None = None
    unresolved_candidates: tuple[Path, ...] = ()

    @property
    def record(self) -> CompletionRecord | None:
        return self.load_result.record

    @property
    def superseded_path(self) -> Path | None:
        """The placeholder the selected retry took over from, if any.

        Descriptive only. This owner answers WHICH file speaks for the run;
        it does not decide which files may be removed, and #264 removes
        nothing it did not already remove — both records stay on disk except
        for the canonical file the pre-existing cleanup has always unlinked.
        Cleanup reads this to say in its log which record it deliberately
        LEFT behind, never to widen what it deletes.
        """
        if self.path == self.canonical_path:
            return None
        return self.canonical_path

    def lookup_fields(self) -> dict[str, object]:
        """Explain the choice to a log line or a trace event payload.

        Carries ``completion_producer_error`` whenever a placeholder was
        found, including when nothing replaced it: a repaired retry must
        not erase the fact that the first ``coding-done`` failed (#264).

        Every path in the payload is resolved, so a consumer comparing
        two of these fields — or one of them against the
        ``full_path``/``worktree_path`` the lookup event already resolves
        — is comparing the same form on both sides.
        """
        return {
            "completion_selected_path": str(self.path.resolve()),
            "completion_path_choice": self.choice.value,
            "completion_producer_error": self.producer_error,
            "completion_unresolved_candidates": [
                str(path.resolve()) for path in self.unresolved_candidates
            ],
        }


def select_completion_record(
    worktree: Path, completion_path: str | None
) -> CompletionRecordSelection:
    """Choose WHICH completion record file speaks for a run.

    The ONE owner of that question. The observer that watches sessions,
    the controller that decides their outcome, and the run-scoped audit
    copy all route here, so a record one of them acts on can never be a
    record another cannot see — the split that left a valid completion
    invisible on disk while its session ran to timeout (#264).

    It decides authority, not lifetime. Nothing here moves, renames,
    overwrites, or deletes any file, and #264 leaves record cleanup
    exactly as it found it: the pre-existing cleanup unlinks the
    canonical path and no other. Cleanup consults this only to report
    which record it left behind.

    Takes ``(worktree, completion_path)`` rather than a resolved file so
    that callers cannot re-derive the join themselves and drift; it asks
    ``completion_record_path`` for the canonical location and returns
    both that and its verdict.

    The rule is deliberately narrow:

    * Canonical missing, or canonical valid -> canonical. A VALID
      canonical record wins even with suffixed siblings present, because
      the suffix has a legitimate second meaning (a second review after
      rework, ``agent_done.py``). Re-assigning authority between two
      valid completions would change which intent governs, and this
      owner does not make that decision.
    * Canonical is a producer-error placeholder -> the placeholder is
      not a completion at all, so a successful retry may take over. It
      must be exactly one valid sibling, named the way the producer
      names retries, in the same run directory, carrying the same
      ``session_id`` the placeholder was written for.
    * Anything else -> canonical, unchanged. No valid sibling leaves the
      placeholder in charge; more than one is ambiguity this owner
      refuses to resolve — there is no newest-wins, lowest-suffix-wins,
      or mtime rule anywhere in it — so it fails closed onto the
      placeholder and the existing rejected-record path it already
      travels.

    Every file this function reads goes through
    ``load_completion_record_result``, so the size gate and field bounds
    apply to siblings exactly as they apply to the canonical record.

    Like the loader it calls, this function explains itself at DEBUG.
    It runs on every ``observe_session`` for every live session, so a
    placeholder that sits there unresolved would otherwise re-log the
    same explanation — once per non-matching sibling — on every tick
    until the session ends. The verdict is reported once, where it is
    acted on: ``SessionController._log_completion_lookup`` logs the
    chosen path, the choice, the preserved producer error, and any
    unresolved candidates, and puts the same fields on the
    ``COMPLETION_LOOKUP`` event.
    """
    canonical_path = completion_record_path(worktree, completion_path)
    if not canonical_path.exists():
        # Polled every tick for every live session, so the nothing-there
        # case must not pay for a stat and an open it does not need.
        return _canonical_selection(
            canonical_path, CompletionRecordLoadResult.missing(canonical_path)
        )

    canonical_result = load_completion_record_result(canonical_path)
    placeholder = canonical_result.producer_error
    if canonical_result.ok or placeholder is None:
        return _canonical_selection(canonical_path, canonical_result)

    candidates = _valid_producer_retries(canonical_path, placeholder.session_id)
    if not candidates:
        logger.debug(
            "Completion producer error at %s with no valid retry beside it: %s",
            canonical_path,
            placeholder.error,
        )
        return _canonical_selection(
            canonical_path, canonical_result, producer_error=placeholder.error
        )

    if len(candidates) > 1:
        logger.debug(
            "Ambiguous completion retry beside %s: %d valid records for "
            "session %s (%s); refusing to choose. Producer error was: %s",
            canonical_path,
            len(candidates),
            placeholder.session_id,
            ", ".join(str(candidate.path) for candidate in candidates),
            placeholder.error,
        )
        return CompletionRecordSelection(
            canonical_path=canonical_path,
            path=canonical_path,
            load_result=canonical_result,
            choice=CompletionPathChoice.AMBIGUOUS_PRODUCER_ERROR_RETRY,
            producer_error=placeholder.error,
            unresolved_candidates=tuple(
                candidate.path for candidate in candidates
            ),
        )

    chosen = candidates[0]
    logger.debug(
        "Completion retry at %s takes over from the producer error at %s. "
        "The first completion command failed with: %s",
        chosen.path,
        canonical_path,
        placeholder.error,
    )
    return CompletionRecordSelection(
        canonical_path=canonical_path,
        path=chosen.path,
        load_result=chosen,
        choice=CompletionPathChoice.PRODUCER_ERROR_RETRY,
        producer_error=placeholder.error,
    )


def _canonical_selection(
    canonical_path: Path,
    load_result: CompletionRecordLoadResult,
    *,
    producer_error: str | None = None,
) -> CompletionRecordSelection:
    return CompletionRecordSelection(
        canonical_path=canonical_path,
        path=canonical_path,
        load_result=load_result,
        choice=CompletionPathChoice.CANONICAL,
        producer_error=producer_error,
    )


def _valid_producer_retries(
    canonical_path: Path, written_for: str
) -> list[CompletionRecordLoadResult]:
    """Load every valid same-session retry beside the canonical path.

    Returns them in a stable order for reporting only. Order never
    decides anything: one candidate is selected, several are ambiguity.
    """
    parent = canonical_path.parent
    stem = canonical_path.stem
    suffix = canonical_path.suffix
    found: list[CompletionRecordLoadResult] = []
    for sibling in sorted(parent.glob(f"*{suffix}")):
        if not _has_producer_suffix(sibling, stem, suffix):
            continue
        loaded = load_completion_record_result(sibling)
        if loaded.record is None:
            continue
        if loaded.record.session_id != written_for:
            logger.debug(
                "Ignoring %s beside %s: written for session %s, not %s",
                sibling,
                canonical_path,
                loaded.record.session_id,
                written_for,
            )
            continue
        found.append(loaded)
    return found


def _has_producer_suffix(sibling: Path, stem: str, suffix: str) -> bool:
    """Whether ``sibling`` carries the producer's own numeric suffix."""
    if sibling.suffix != suffix or not sibling.is_file():
        return False
    if not sibling.stem.startswith(f"{stem}-"):
        return False
    index = sibling.stem[len(stem) + 1:]
    if not index.isdigit():
        return False
    return int(index) >= _PRODUCER_RETRY_MIN_INDEX


class CompletionValidationGitAdapter(Protocol):
    def get_current_branch(self, worktree: Path) -> str | None: ...
    def has_uncommitted_changes(self, worktree: Path) -> bool: ...
    def has_tracked_changes(self, worktree: Path, include_staged: bool = True) -> bool: ...

    def list_dirty_files(self, worktree: Path, mode: str) -> list[str] | None:
        """Enumerate dirty file paths for the given mode.

        Return the enumerated paths on success, ``None`` when
        enumeration itself failed (git error, etc.). Callers MUST treat
        ``None`` as fail-closed; an empty list ``[]`` is a valid "all
        dirty entries were filtered" result that callers may pass.

        The boolean ``has_*_changes`` helpers in this protocol
        intentionally fail closed by returning ``True`` on error;
        ``list_dirty_files`` needs the same fail-closed semantics, but a
        bare ``list`` return type would collapse "filtered to empty" and
        "could not enumerate" into the same value — hence ``None``.
        """
        ...


class CompletionRecordValidator:
    """Loads completion records and validates publish preconditions."""

    def __init__(
        self,
        *,
        config: "Config | None",
        git_adapter: CompletionValidationGitAdapter,
    ) -> None:
        self._config = config
        self._git_adapter = git_adapter

    def read_completion_record(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecord | None:
        """Read and validate a completion record from a worktree."""
        return self.read_completion_record_result(worktree, completion_path).record

    def read_completion_record_result(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecordLoadResult:
        """Read and validate a completion record from a worktree."""
        return self.select_completion_record(worktree, completion_path).load_result

    def select_completion_record(
        self, worktree: Path, completion_path: str | None = None
    ) -> CompletionRecordSelection:
        """Resolve a worktree-relative hint to the run's authoritative record.

        The instance-level door onto the module owner, for the consumers
        that hold a validator — the controller's lookup, the processor's
        re-read on the publish path — so every one of them acts on the
        same file (#264).
        """
        return select_completion_record(worktree, completion_path)

    def resolve_agent_label_from_completion_path(
        self, completion_path: str | None
    ) -> tuple[str | None, str | None]:
        if completion_path is None or self._config is None:
            return None, None
        filename = Path(completion_path).name
        if not (filename.startswith("completion-") and filename.endswith(".json")):
            return None, None
        safe_name = filename[len("completion-"):-len(".json")]
        matches = [
            label
            for label in self._config.agents.keys()
            if sanitize_agent_label(label) == safe_name
        ]
        if not matches:
            return None, None
        if len(matches) > 1:
            return (
                None,
                "Multiple agent labels map to completion file "
                f"{filename}: {', '.join(matches)}",
            )
        return matches[0], None

    def validate_worktree_state(
        self, worktree: Path, record: CompletionRecord
    ) -> WorktreeValidationResult:
        """Validate worktree state before executing requested publish actions."""
        branch = self._git_adapter.get_current_branch(worktree)
        if not branch:
            return WorktreeValidationResult.fail(
                WorktreeValidationFailure.CURRENT_BRANCH_UNKNOWN,
                "Could not determine current branch",
            )

        if RequestedAction.PUSH_BRANCH in record.requested_actions:
            if branch in ("main", "master"):
                return WorktreeValidationResult.fail(
                    WorktreeValidationFailure.PROTECTED_BRANCH,
                    f"Cannot push: on protected branch '{branch}'",
                )

            dirty_policy = self.check_dirty_policy(worktree)
            if not dirty_policy.ok:
                return dirty_policy

        return WorktreeValidationResult.pass_()

    def check_dirty_policy(self, worktree: Path) -> WorktreeValidationResult:
        """Apply validation.publish.dirty_check policy before push actions."""
        mode = (
            self._config.validation.publish.dirty_check
            if self._config is not None
            else "off"
        )

        if mode == "off":
            logger.info("Dirty-check skipped for %s: mode=off", worktree)
            return WorktreeValidationResult.pass_()
        list_mode = mode
        if mode == "tracked":
            dirty = self._git_adapter.has_tracked_changes(worktree, include_staged=True)
        elif mode == "unstaged":
            dirty = self._git_adapter.has_tracked_changes(worktree, include_staged=False)
        elif mode == "all":
            dirty = self._git_adapter.has_uncommitted_changes(worktree)
        else:
            return WorktreeValidationResult.fail(
                WorktreeValidationFailure.DIRTY_POLICY,
                (
                    "Invalid validation.publish.dirty_check value: "
                    f"{mode!r} (expected tracked|unstaged|all|off)"
                ),
            )

        logger.debug(
            "Dirty-check evaluated for %s: mode=%s dirty=%s",
            worktree,
            mode,
            dirty,
        )
        if dirty:
            dirty_files = self._git_adapter.list_dirty_files(worktree, list_mode)
            if dirty_files is None:
                # ``has_*_changes`` said the worktree is dirty, but the
                # enumeration call failed. Without the file list we
                # cannot tell whether the dirty state is the
                # planted/runtime-only kind that's safe to push or a
                # real blocking change. The boolean helpers fail closed
                # by returning ``True`` on git error; preserve that
                # invariant here by treating "unknown dirty state" as a
                # blocking failure rather than collapsing it to
                # "blocking_files == [] -> pass" (#6159).
                logger.warning(
                    "Dirty-check enumeration failed for %s (mode=%s); "
                    "failing closed",
                    worktree,
                    mode,
                )
                return WorktreeValidationResult.fail(
                    WorktreeValidationFailure.DIRTY_POLICY,
                    (
                        "Could not enumerate dirty files "
                        f"(validation.publish.dirty_check={mode!r}); "
                        "fail-closed because dirty state is unknown."
                    ),
                )
            blocking_files = filter_runtime_managed_dirty_paths(dirty_files, worktree)
            logger.info(
                "Dirty-check files for %s: mode=%s total=%d blocking=%d files=%s",
                worktree,
                mode,
                len(dirty_files),
                len(blocking_files),
                ", ".join(blocking_files[:_DIRTY_FILES_REASON_LIMIT])
                if blocking_files
                else "<runtime-only>",
            )
            if not blocking_files:
                # Bool short-circuit (has_uncommitted_changes / has_tracked_changes)
                # can fire on paths that ``list_dirty_files`` then filters out:
                # orchestrator-planted untracked files in mode=all (filtered
                # inside list_dirty_files) and runtime-managed metadata
                # (filtered here). Either way, ``blocking_files`` is the
                # authoritative gate — empty means nothing to block on.
                if dirty_files:
                    logger.info(
                        "Dirty-check ignored runtime-only files for %s: %s",
                        worktree,
                        ", ".join(dirty_files),
                    )
                else:
                    logger.info(
                        "Dirty-check found no blocking files for %s "
                        "(planted/runtime entries filtered)",
                        worktree,
                    )
                return WorktreeValidationResult.pass_()
            reason = (
                "Working tree is dirty; commit/add/stash before pushing. "
                "Override with validation.publish.dirty_check."
            )
            if blocking_files:
                preview = ", ".join(blocking_files[:_DIRTY_FILES_REASON_LIMIT])
                remaining = len(blocking_files) - _DIRTY_FILES_REASON_LIMIT
                suffix = f" (+{remaining} more)" if remaining > 0 else ""
                reason = f"{reason} Dirty files: {preview}{suffix}."
            return WorktreeValidationResult.dirty_policy_failure(
                reason,
                blocking_paths=blocking_files,
            )

        return WorktreeValidationResult.pass_()
