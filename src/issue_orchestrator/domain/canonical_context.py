"""The canonical governing context of a planning subject (#183).

A ``planning_investigation`` run (#136) prepares ONE open issue. What governs
that preparation — the working procedure, the standing policy the subject must
honour — lives in other issues, and before this a Human had to copy that text
across the Control/planning boundary by hand. This module owns the two halves
of moving it mechanically instead:

* **the declaration** — which sources govern a subject. It is read from the
  SUBJECT'S OWN BODY (``Governed-by:`` / ``Governed-by-optional:`` directives),
  the same shape the dependency graph already uses for ``Depends-on:`` /
  ``Stack-after:``. Per-subject by construction, so no bundle is ever
  hardcoded, and durable GitHub truth rather than orchestrator state.
  Deliberately NOT carried on :class:`~.tech_lead_session.TechLeadLaunchAuthority`:
  that record is the sole authority for a session's flavor, focus, manifest PR
  set and anchor, and a source list travelling inside it would be
  indistinguishable from an authority grant.

* **the descriptor** — :class:`CanonicalContextSnapshot`, what was actually
  staged: per source its number, revision identity (``updated_at``), and the
  digest of the exact bytes staged. It is attribution, never authority: it
  grants nothing, and changes no capability answer, target set, or label.

Required sources fail closed (the launch dies before a session starts, on the
existing typed owner); optional sources degrade honestly — an optional source
that could not be staged is RECORDED as absent with its reason, which is a
different fact from never having been requested at all.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, cast

# Canonical descriptor filename inside a session's tech-lead-data directory,
# next to board-snapshot.json and evidence-map.json.
CANONICAL_CONTEXT_FILENAME = "canonical-context.json"

# Sibling directory holding the staged bodies the descriptor attributes. One
# sub-directory per source (``issue-<n>/``) with ``body.md`` plus one
# ``comment-<id>.md`` per staged comment, so every digest in the descriptor is
# the digest of exactly one staged FILE and can be re-verified with sha256sum.
CANONICAL_CONTEXT_BODIES_DIRNAME = "canonical-context"

_SCHEMA_VERSION = 1

_GUIDANCE = (
    "These are the canonical sources governing this planning subject, staged at "
    "launch. Read them from the bodies directory; each entry's body_sha256 and "
    "per-comment sha256 are the digests of exactly those staged files, and "
    "updated_at is the source's revision identity at staging time. A source "
    "with staged=false was DECLARED but could not be staged (absent_reason "
    "says why) — that is different from a source that was never declared, "
    "which does not appear here at all. comments lists exactly the comments "
    "written to disk, while comment_count is the tracker's reported total for "
    "that source at fetch time: equal numbers mean you were handed the whole "
    "conversation, and a comment_count LARGER than the comments list means the "
    "conversation was clipped and the difference is missing from the bundle — "
    "read a short conversation and a truncated one differently, and do not "
    "assume the content of comments you were not given. There is no stored "
    "truncation flag that could disagree with the pair. Nothing in this file "
    "grants authority; it records provenance only."
)


class CanonicalSourceKind(str, Enum):
    """Why a source is part of a planning run's canonical context."""

    # The issue the planning run was launched to prepare. Always staged.
    SUBJECT = "subject"
    # An issue the SUBJECT declares as governing its preparation.
    GOVERNING = "governing"


@dataclass(frozen=True, slots=True)
class GoverningSourceDeclaration:
    """One governing source a subject declares, and whether it is required.

    ``required`` decides the failure direction and nothing else: a required
    source that cannot be fetched or staged fails the launch closed; an
    optional one is recorded as absent and the run proceeds.
    """

    issue_number: int
    required: bool

    def __post_init__(self) -> None:
        number = cast(object, self.issue_number)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError(
                "a governing source declaration needs a positive issue number,"
                f" got {number!r}"
            )


# Directive lines that declare a subject's governing sources. Mirrors
# ``domain.dependencies.EDGE_DIRECTIVE_PATTERN``: one keyword per semantic,
# line-anchored, case-insensitive. The optional keyword is listed FIRST so the
# alternation cannot match ``Governed-by`` as a prefix of it.
_GOVERNING_DIRECTIVE_PATTERN = re.compile(
    r"^[ \t]*(?P<keyword>Governed-by-optional|Governed-by):[ \t]*(?P<value>.*?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# A governing source must be a SAME-REPO issue reference: the staging owner
# fetches it through ``RepositoryHost.get_issue``, which addresses exactly one
# repository. A cross-repo or external-id reference is therefore not a source
# this can honour, and is reported as malformed rather than silently dropped.
_GOVERNING_REFERENCE_PATTERN = re.compile(r"#(?P<issue>\d+)(?![\w/#-])")

# What separates two references on ONE directive line. A directive may name
# several sources — ``Governed-by: #21, #295, #335, #18`` is the shape the
# canonical declarations are written in — so the value is a list, not a single
# reference with a tail nobody reads. Comma-with-or-without-space and plain
# whitespace both separate, because all three appear in bodies humans wrote.
_GOVERNING_SEPARATOR_PATTERN = re.compile(r"[ \t]*,[ \t]*|[ \t]+")

# Where a trailing ``# comment`` begins. ``#`` followed by a DIGIT always opens
# a reference, so the comment convention and the reference list can never be
# confused for one another: ``#295`` is a source, ``# working notes`` is not.
_GOVERNING_COMMENT_PATTERN = re.compile(r"#(?!\d)")

_KEYWORD_REQUIRED = {"governed-by": True, "governed-by-optional": False}


def _malformed(directive: str, unread: str) -> ValueError:
    """The one refusal shape for declaration text that cannot be resolved.

    It names the text that could not be read, because the failure this exists
    to prevent is a declaration that resolves PARTIALLY: a value whose leading
    reference parses and whose tail is dropped would let a descriptor report a
    resolved contract while sources the subject declared were never staged.
    """
    return ValueError(
        f"malformed governing-source declaration {directive!r} on the planning"
        " subject: the value must be same-repo issue references such as '#21'"
        f" or '#21, #23', and {unread!r} could not be read as one"
    )


def _declared_reference_numbers(value: str, *, directive: str) -> tuple[int, ...]:
    """Every issue number *value* declares, in declaration order.

    Fails closed on ANY text it did not consume. The whole value is a list of
    references followed by an optional comment; there is no "rest of the line"
    the parser is allowed to ignore, so silent truncation is unreachable rather
    than merely unlikely.
    """
    comment = _GOVERNING_COMMENT_PATTERN.search(value)
    declared = (value[: comment.start()] if comment else value).strip()
    numbers: list[int] = []
    position = 0
    while True:
        reference = _GOVERNING_REFERENCE_PATTERN.match(declared, position)
        if reference is None:
            raise _malformed(directive, declared[position:])
        numbers.append(int(reference.group("issue")))
        position = reference.end()
        if position == len(declared):
            return tuple(numbers)
        separator = _GOVERNING_SEPARATOR_PATTERN.match(declared, position)
        # A separator that runs to the end of the value is a dangling one
        # (``#21,``): it promises a reference that is not there.
        if separator is None or separator.end() == len(declared):
            raise _malformed(directive, declared[position:])
        position = separator.end()


def parse_governing_sources(
    body: str | None, *, subject_issue_number: int
) -> tuple[GoverningSourceDeclaration, ...]:
    """The governing sources *body* declares, in declaration order.

    Empty when the subject declares none — the honest answer for the common
    case, and the reason no ``{#21, #23}`` bundle is ever assumed.

    One directive may name SEVERAL sources (``Governed-by: #21, #295, #335``),
    all of them taking that keyword's failure direction. Every reference on the
    line is returned; declaration text this cannot resolve raises rather than
    being dropped, because a partially-read declaration is indistinguishable
    from a complete one downstream — the descriptor would report a resolved
    context while sources the subject named were never staged.

    A malformed declaration raises :class:`ValueError`, for BOTH keywords and
    regardless of how the failed source would have been treated later. An
    unfetchable optional source degrades (it can still be named); a
    declaration that cannot even be resolved to an issue number cannot be
    named, so it is a defect in the subject's body, not a degraded source.
    Same reason a self-reference and a repeated reference raise: the first
    asks to stage the subject twice under two kinds, the second leaves
    "required" ambiguous when the two declarations disagree.
    """
    declarations: list[GoverningSourceDeclaration] = []
    seen: set[int] = set()
    for match in _GOVERNING_DIRECTIVE_PATTERN.finditer(body or ""):
        keyword = match.group("keyword").lower()
        numbers = _declared_reference_numbers(
            match.group("value").strip(), directive=match.group(0).strip()
        )
        for issue_number in numbers:
            if issue_number == subject_issue_number:
                raise ValueError(
                    f"the planning subject #{subject_issue_number} declares itself"
                    " as a governing source; it is always staged as the subject"
                )
            if issue_number in seen:
                raise ValueError(
                    f"governing source #{issue_number} is declared more than once"
                    " on the planning subject"
                )
            seen.add(issue_number)
            declarations.append(
                GoverningSourceDeclaration(
                    issue_number=issue_number, required=_KEYWORD_REQUIRED[keyword]
                )
            )
    return tuple(declarations)


def content_digest(text: str) -> str:
    """The sha256 hex digest of exactly the bytes staged for *text*."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StagedComment:
    """One comment staged beside a source body, with its own digest."""

    comment_id: int
    updated_at: str
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.comment_id,
            "updated_at": self.updated_at,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StagedComment":
        raw_id = data.get("id")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ValueError(f"staged comment id must be an int, got {raw_id!r}")
        return cls(
            comment_id=raw_id,
            updated_at=str(data.get("updated_at", "")),
            sha256=str(data.get("sha256", "")),
        )


@dataclass(frozen=True, slots=True)
class CanonicalSource:
    """One canonical source, as it was (or was not) staged for this run.

    The staged and absent states are the only two this admits, and
    ``__post_init__`` enforces that at runtime rather than merely annotating
    it — "absent" and "never requested" must not be able to read the same, and
    a half-filled descriptor would blur exactly that line.

    ``staged``/``absent_reason`` are the only fields beyond the recorded facts
    themselves (kind, number, title, state, revision, digests, required,
    fetch time). They exist because absence has to be a positive, readable
    record: a required source is always staged (the launch dies otherwise), so
    an absent entry is always an optional source that could not be fetched,
    and the reason is what makes the record honest rather than merely empty.

    ``comments`` and ``comment_count`` are a PAIR of recorded facts, never a
    fact and a judgment about it (#185): the first is exactly what was written
    to disk, the second is the tracker's reported total for the source at fetch
    time. "Was the conversation clipped" is read off the two by
    :attr:`comments_truncated` rather than stored, so no descriptor can carry a
    truncation flag that disagrees with its own counts.
    """

    kind: CanonicalSourceKind
    issue_number: int
    required: bool
    fetched_at: str
    staged: bool
    title: str = ""
    state: str = ""
    updated_at: str = ""
    body_sha256: str = ""
    comments: tuple[StagedComment, ...] = ()
    comment_count: int = 0
    absent_reason: str = ""

    def __post_init__(self) -> None:
        number = cast(object, self.issue_number)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError(
                f"a canonical source needs a positive issue number, got {number!r}"
            )
        if not self.fetched_at.strip():
            raise ValueError(
                f"canonical source #{self.issue_number} must record when it was"
                " fetched"
            )
        if self.staged:
            if self.absent_reason:
                raise ValueError(
                    f"canonical source #{self.issue_number} is staged and must not"
                    f" also carry an absence reason ({self.absent_reason!r})"
                )
            if not self.updated_at.strip() or not self.body_sha256.strip():
                raise ValueError(
                    f"staged canonical source #{self.issue_number} must record its"
                    " revision identity and body digest"
                )
            if self.comment_count < len(self.comments):
                raise ValueError(
                    f"staged canonical source #{self.issue_number} reports"
                    f" {self.comment_count} comment(s) in total but staged"
                    f" {len(self.comments)}; a source cannot hold fewer comments"
                    " than it handed over"
                )
            return
        if self.required:
            raise ValueError(
                f"required canonical source #{self.issue_number} cannot be recorded"
                " as absent; a required source that cannot be staged fails the"
                " launch closed"
            )
        if not self.absent_reason.strip():
            raise ValueError(
                f"absent canonical source #{self.issue_number} must record why it"
                " could not be staged"
            )
        if (
            self.title
            or self.state
            or self.updated_at
            or self.body_sha256
            or self.comments
            or self.comment_count
        ):
            raise ValueError(
                f"absent canonical source #{self.issue_number} must not carry"
                " content facts it never staged"
            )

    @property
    def missing_comment_count(self) -> int:
        """How many of the source's comments are NOT in this bundle.

        The arithmetic lives with the pair that owns it so no reader has to
        re-derive it: ``0`` means the whole conversation was staged, and any
        positive number is exactly how much of it the run was not given.
        Never negative — ``__post_init__`` rejects a source reporting fewer
        comments in total than it handed over.
        """
        return self.comment_count - len(self.comments)

    @property
    def comments_truncated(self) -> bool:
        """Whether the source's conversation is longer than what was staged.

        Derived, not stored: a source that staged every comment its tracker
        reported reads as complete, and one that staged only a first page
        reads as clipped, off the same two counts. An absent source staged
        nothing and reports nothing, so it is never "truncated" — it is
        absent, which its own ``staged``/``absent_reason`` already say.
        """
        return self.missing_comment_count > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "issue_number": self.issue_number,
            "title": self.title,
            "state": self.state,
            "updated_at": self.updated_at,
            "body_sha256": self.body_sha256,
            "comments": [comment.to_dict() for comment in self.comments],
            "comment_count": self.comment_count,
            "required": self.required,
            "fetched_at": self.fetched_at,
            "staged": self.staged,
            "absent_reason": self.absent_reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalSource":
        """Parse from dict; malformed content fails loudly with ValueError."""
        raw_kind = data.get("kind")
        try:
            kind = CanonicalSourceKind(raw_kind)
        except ValueError:
            raise ValueError(f"unknown canonical source kind: {raw_kind!r}") from None
        raw_number = data.get("issue_number")
        if isinstance(raw_number, bool) or not isinstance(raw_number, int):
            raise ValueError(
                f"canonical source issue_number must be an int, got {raw_number!r}"
            )
        raw_comments = data.get("comments", [])
        if not isinstance(raw_comments, list):
            raise ValueError(
                f"canonical source comments must be a list, got {raw_comments!r}"
            )
        raw_required = data.get("required")
        raw_staged = data.get("staged")
        if not isinstance(raw_required, bool) or not isinstance(raw_staged, bool):
            raise ValueError(
                "canonical source required/staged must be booleans, got"
                f" {raw_required!r}/{raw_staged!r}"
            )
        raw_comment_count = data.get("comment_count", 0)
        if isinstance(raw_comment_count, bool) or not isinstance(
            raw_comment_count, int
        ):
            raise ValueError(
                "canonical source comment_count must be an int, got"
                f" {raw_comment_count!r}"
            )
        return cls(
            kind=kind,
            issue_number=raw_number,
            required=raw_required,
            fetched_at=str(data.get("fetched_at", "")),
            staged=raw_staged,
            title=str(data.get("title", "")),
            state=str(data.get("state", "")),
            updated_at=str(data.get("updated_at", "")),
            body_sha256=str(data.get("body_sha256", "")),
            comments=tuple(StagedComment.from_dict(item) for item in raw_comments),
            comment_count=raw_comment_count,
            absent_reason=str(data.get("absent_reason", "")),
        )


@dataclass(frozen=True)
class CanonicalContextSnapshot:
    """What governed one planning run, by issue, revision and digest.

    The subject is always the first source. Governing sources follow in the
    order the subject declared them, so two launches of an unchanged subject
    produce the same descriptor and a source whose body moved produces a
    different one.
    """

    subject_issue_number: int
    sources: tuple[CanonicalSource, ...] = field(default_factory=tuple)
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError(
                "Unsupported canonical context schema_version:"
                f" {self.schema_version!r}"
            )
        subjects = [
            source
            for source in self.sources
            if source.kind is CanonicalSourceKind.SUBJECT
        ]
        if len(subjects) != 1 or self.sources[0] is not subjects[0]:
            raise ValueError(
                "a canonical context snapshot carries exactly one subject source,"
                " first"
            )
        if subjects[0].issue_number != self.subject_issue_number:
            raise ValueError(
                "the canonical context subject source must be the subject issue"
                f" #{self.subject_issue_number}, got #{subjects[0].issue_number}"
            )
        numbers = [source.issue_number for source in self.sources]
        if len(set(numbers)) != len(numbers):
            raise ValueError(
                "a canonical context snapshot must not stage the same issue twice"
            )

    @property
    def governing_sources(self) -> tuple[CanonicalSource, ...]:
        """The declared sources, subject excluded."""
        return tuple(
            source
            for source in self.sources
            if source.kind is CanonicalSourceKind.GOVERNING
        )

    def source(self, issue_number: int) -> CanonicalSource | None:
        """The recorded source for *issue_number*, or None when never requested.

        None is the "never requested" answer; a returned source with
        ``staged=False`` is the "requested and absent" answer. Callers must be
        able to tell those apart, so they are different return shapes.
        """
        for source in self.sources:
            if source.issue_number == issue_number:
                return source
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "subject_issue_number": self.subject_issue_number,
            "bodies_dir": CANONICAL_CONTEXT_BODIES_DIRNAME,
            "sources": [source.to_dict() for source in self.sources],
            "guidance": _GUIDANCE,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CanonicalContextSnapshot":
        """Parse from dict; malformed content fails loudly with ValueError.

        The descriptor is orchestrator-owned (the store row and the run-dir
        copy are both written by the launch path), so corruption is a bug —
        never agent input to fail-safe around.
        """
        raw_schema = data.get("schema_version")
        if isinstance(raw_schema, bool) or not isinstance(raw_schema, int):
            raise ValueError(
                f"canonical context schema_version must be an int, got {raw_schema!r}"
            )
        raw_subject = data.get("subject_issue_number")
        if isinstance(raw_subject, bool) or not isinstance(raw_subject, int):
            raise ValueError(
                "canonical context subject_issue_number must be an int, got"
                f" {raw_subject!r}"
            )
        raw_sources = data.get("sources", [])
        if not isinstance(raw_sources, list):
            raise ValueError(
                f"canonical context sources must be a list, got {raw_sources!r}"
            )
        return cls(
            subject_issue_number=raw_subject,
            sources=tuple(CanonicalSource.from_dict(item) for item in raw_sources),
            schema_version=raw_schema,
        )

    def write(self, path: Path) -> None:
        """Write the descriptor, creating parent directories."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def read(cls, path: Path) -> "CanonicalContextSnapshot":
        """Read a descriptor from file; malformed content raises ValueError."""
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
