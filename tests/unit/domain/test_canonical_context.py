"""The canonical-context value objects and the per-subject declaration (#183).

The declaration is what keeps a governing bundle from ever being hardcoded,
and the descriptor is what makes a staged bundle attributable. Both are pure
domain, so they are proven here without touching a repository host.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.canonical_context import (
    CanonicalContextSnapshot,
    CanonicalSource,
    CanonicalSourceKind,
    GoverningSourceDeclaration,
    StagedComment,
    content_digest,
    parse_governing_sources,
)

FETCHED_AT = "2026-08-21T00:00:00+00:00"


def _subject(number: int = 183, **overrides: object) -> CanonicalSource:
    fields: dict[str, object] = {
        "kind": CanonicalSourceKind.SUBJECT,
        "issue_number": number,
        "required": True,
        "fetched_at": FETCHED_AT,
        "staged": True,
        "title": "Stage canonical governing context",
        "state": "open",
        "updated_at": "2026-08-20T10:00:00Z",
        "body_sha256": content_digest("subject body"),
    }
    fields.update(overrides)
    return CanonicalSource(**fields)  # type: ignore[arg-type]


def _governing(number: int, **overrides: object) -> CanonicalSource:
    fields: dict[str, object] = {
        "kind": CanonicalSourceKind.GOVERNING,
        "issue_number": number,
        "required": True,
        "fetched_at": FETCHED_AT,
        "staged": True,
        "title": f"Governing #{number}",
        "state": "open",
        "updated_at": "2026-08-19T10:00:00Z",
        "body_sha256": content_digest(f"body {number}"),
    }
    fields.update(overrides)
    return CanonicalSource(**fields)  # type: ignore[arg-type]


def _comment(comment_id: int) -> StagedComment:
    return StagedComment(
        comment_id=comment_id,
        updated_at="2026-08-18T09:00:00Z",
        sha256=content_digest(f"comment {comment_id}"),
    )


class TestParseGoverningSources:
    def test_a_subject_declaring_nothing_declares_nothing(self) -> None:
        # Failure direction 7: no bundle is ever assumed for a planning
        # subject. #21/#23 appear only where a subject named them.
        assert parse_governing_sources("Plain body.", subject_issue_number=183) == ()
        assert parse_governing_sources(None, subject_issue_number=183) == ()

    def test_both_keywords_are_read_in_declaration_order(self) -> None:
        body = (
            "Prose first.\n"
            "Governed-by: #21\n"
            "Governed-by-optional: #23  # working notes\n"
        )

        assert parse_governing_sources(body, subject_issue_number=183) == (
            GoverningSourceDeclaration(issue_number=21, required=True),
            GoverningSourceDeclaration(issue_number=23, required=False),
        )

    def test_keyword_case_and_leading_space_are_tolerated(self) -> None:
        body = "  governed-BY:   #21\n\tGOVERNED-BY-OPTIONAL: #23\n"

        assert parse_governing_sources(body, subject_issue_number=1) == (
            GoverningSourceDeclaration(issue_number=21, required=True),
            GoverningSourceDeclaration(issue_number=23, required=False),
        )

    def test_a_mid_line_mention_is_not_a_declaration(self) -> None:
        body = "See the note (Governed-by: #21) for background.\n"

        assert parse_governing_sources(body, subject_issue_number=1) == ()

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            ("Governed-by: owner/repo#5", "same-repo"),
            ("Governed-by-optional: M1-010", "same-repo"),
            ("Governed-by:", "same-repo"),
            ("Governed-by: #7\nGoverned-by: #7", "more than once"),
            ("Governed-by: #7\nGoverned-by-optional: #7", "more than once"),
            ("Governed-by: #183", "declares itself"),
        ],
    )
    def test_an_unusable_declaration_is_loud(self, body: str, match: str) -> None:
        # A source that cannot even be NAMED is a defect in the subject, not a
        # degraded source — so this is loud for the optional keyword too.
        with pytest.raises(ValueError, match=match):
            parse_governing_sources(body, subject_issue_number=183)


class TestCanonicalSourceStates:
    def test_absent_is_a_recorded_fact_with_a_reason(self) -> None:
        absent = CanonicalSource(
            kind=CanonicalSourceKind.GOVERNING,
            issue_number=23,
            required=False,
            fetched_at=FETCHED_AT,
            staged=False,
            absent_reason="issue #23 was not found",
        )

        assert absent.staged is False
        assert absent.absent_reason
        assert absent.to_dict()["absent_reason"] == "issue #23 was not found"

    def test_a_required_source_can_never_be_recorded_as_absent(self) -> None:
        # Fail-closed is enforced by the type, not only by the staging path.
        with pytest.raises(ValueError, match="fails the launch closed"):
            CanonicalSource(
                kind=CanonicalSourceKind.GOVERNING,
                issue_number=21,
                required=True,
                fetched_at=FETCHED_AT,
                staged=False,
                absent_reason="boom",
            )

    @pytest.mark.parametrize(
        "content_fact",
        [
            {"title": "Looks staged"},
            {"state": "open"},
            {"updated_at": "2026-08-19T10:00:00Z"},
            {"body_sha256": content_digest("body")},
            {"comments": (_comment(1),)},
            # #185: the conversation TOTAL is a content fact too. A source
            # that staged nothing knows nothing about its conversation.
            {"comment_count": 5},
        ],
    )
    def test_an_absent_source_may_not_claim_content_it_never_staged(
        self, content_fact: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError, match="content facts"):
            CanonicalSource(
                kind=CanonicalSourceKind.GOVERNING,
                issue_number=23,
                required=False,
                fetched_at=FETCHED_AT,
                staged=False,
                absent_reason="unreachable",
                **content_fact,  # type: ignore[arg-type]
            )

    def test_an_absent_source_is_absent_rather_than_clipped(self) -> None:
        absent = CanonicalSource(
            kind=CanonicalSourceKind.GOVERNING,
            issue_number=23,
            required=False,
            fetched_at=FETCHED_AT,
            staged=False,
            absent_reason="unreachable",
        )

        assert absent.comment_count == 0
        assert absent.comments == ()
        assert absent.comments_truncated is False

    def test_an_absent_source_must_say_why(self) -> None:
        with pytest.raises(ValueError, match="must record why"):
            CanonicalSource(
                kind=CanonicalSourceKind.GOVERNING,
                issue_number=23,
                required=False,
                fetched_at=FETCHED_AT,
                staged=False,
            )

    def test_a_staged_source_needs_revision_identity_and_digest(self) -> None:
        with pytest.raises(ValueError, match="revision identity"):
            _governing(21, updated_at="")

    def test_fetch_time_is_always_recorded(self) -> None:
        with pytest.raises(ValueError, match="when it was"):
            _governing(21, fetched_at="")


class TestConversationCompleteness:
    """#185: a complete comment set and a clipped first page read differently.

    ``get_issue_comments`` answers with one page, so a staged conversation may
    be a prefix of the real one. The descriptor records the tracker's total
    beside what it staged, and "was it clipped" is READ OFF the pair rather
    than stored beside it.
    """

    def test_a_clipped_conversation_says_how_much_is_missing(self) -> None:
        clipped = _governing(
            21, comments=tuple(_comment(i) for i in range(100)), comment_count=137
        )

        assert clipped.comments_truncated is True
        assert len(clipped.comments) == 100
        assert clipped.comment_count == 137
        # Not merely "something is missing" - exactly 37 comments are.
        assert clipped.comment_count - len(clipped.comments) == 37

    def test_a_complete_conversation_reads_as_complete(self) -> None:
        complete = _governing(
            21, comments=(_comment(1), _comment(2)), comment_count=2
        )

        assert complete.comments_truncated is False
        assert complete.comment_count == len(complete.comments)

    def test_a_silent_source_is_not_mistaken_for_a_clipped_one(self) -> None:
        # Zero staged comments is the honest answer for an issue nobody
        # commented on, and must not read as "the page was clipped at zero".
        silent = _governing(21, comments=(), comment_count=0)

        assert silent.comments_truncated is False

    def test_no_stored_flag_can_disagree_with_the_counts(self) -> None:
        # The failure CompletedReviewExchange was reshaped to remove: two
        # stored values for one fact, free to drift apart. There is exactly
        # one representation here, and it is the pair.
        field_names = {field.name for field in dataclasses.fields(CanonicalSource)}
        assert not any("truncat" in name for name in field_names)
        assert "comment_count" in field_names
        with pytest.raises(TypeError):
            CanonicalSource(  # type: ignore[call-arg]
                kind=CanonicalSourceKind.GOVERNING,
                issue_number=21,
                required=True,
                fetched_at=FETCHED_AT,
                staged=True,
                updated_at="2026-08-19T10:00:00Z",
                body_sha256=content_digest("body"),
                truncated=True,
            )
        serialized = _governing(21, comments=(_comment(1),), comment_count=9).to_dict()
        assert not any("truncat" in key for key in serialized)
        assert serialized["comment_count"] == 9
        assert len(serialized["comments"]) == 1

    def test_a_source_may_not_claim_fewer_comments_than_it_staged(self) -> None:
        with pytest.raises(ValueError, match="fewer comments than it handed over"):
            _governing(21, comments=(_comment(1), _comment(2)), comment_count=1)

    def test_the_pair_survives_the_serialized_form(self, tmp_path: Path) -> None:
        snapshot = CanonicalContextSnapshot(
            subject_issue_number=183,
            sources=(
                _subject(comments=(_comment(1),), comment_count=1),
                _governing(21, comments=(_comment(2),), comment_count=42),
            ),
        )
        path = tmp_path / "canonical-context.json"

        snapshot.write(path)
        replayed = CanonicalContextSnapshot.read(path)

        assert replayed == snapshot
        assert replayed.sources[0].comments_truncated is False
        assert replayed.sources[1].comments_truncated is True
        payload = json.loads(path.read_text())
        assert payload["sources"][1]["comment_count"] == 42
        # The reader guidance explains the pair, so an agent handed only this
        # file knows a short conversation from a clipped one.
        assert "comment_count" in payload["guidance"]

    @pytest.mark.parametrize("raw", ["7", 7.0, True, None])
    def test_a_malformed_stored_total_is_loud(self, raw: object) -> None:
        payload = _governing(21).to_dict()
        payload["comment_count"] = raw

        with pytest.raises(ValueError, match="comment_count must be an int"):
            CanonicalSource.from_dict(payload)


class TestCanonicalContextSnapshot:
    def test_absent_and_never_requested_read_differently(self) -> None:
        # Failure direction 3: the distinction is structural, not a blank field.
        snapshot = CanonicalContextSnapshot(
            subject_issue_number=183,
            sources=(
                _subject(),
                CanonicalSource(
                    kind=CanonicalSourceKind.GOVERNING,
                    issue_number=23,
                    required=False,
                    fetched_at=FETCHED_AT,
                    staged=False,
                    absent_reason="issue #23 was not found",
                ),
            ),
        )

        requested = snapshot.source(23)
        assert requested is not None and requested.staged is False
        assert snapshot.source(21) is None

    def test_the_subject_is_the_first_and_only_subject(self) -> None:
        with pytest.raises(ValueError, match="exactly one subject source"):
            CanonicalContextSnapshot(
                subject_issue_number=183, sources=(_governing(21), _subject())
            )
        with pytest.raises(ValueError, match="exactly one subject source"):
            CanonicalContextSnapshot(subject_issue_number=183, sources=())

    def test_the_subject_source_must_be_the_subject(self) -> None:
        with pytest.raises(ValueError, match="subject source must be"):
            CanonicalContextSnapshot(
                subject_issue_number=183, sources=(_subject(number=999),)
            )

    def test_one_issue_is_never_staged_twice(self) -> None:
        with pytest.raises(ValueError, match="same issue twice"):
            CanonicalContextSnapshot(
                subject_issue_number=183,
                sources=(_subject(), _governing(21), _governing(21)),
            )

    def test_round_trips_through_its_serialized_form(self, tmp_path: Path) -> None:
        snapshot = CanonicalContextSnapshot(
            subject_issue_number=183,
            sources=(
                _subject(),
                _governing(
                    21,
                    comments=(
                        StagedComment(
                            comment_id=5365348920,
                            updated_at="2026-08-18T09:00:00Z",
                            sha256=content_digest("a comment"),
                        ),
                    ),
                    comment_count=1,
                ),
            ),
        )
        path = tmp_path / "canonical-context.json"

        snapshot.write(path)

        assert CanonicalContextSnapshot.read(path) == snapshot
        payload = json.loads(path.read_text())
        assert payload["sources"][1]["comments"][0]["id"] == 5365348920
        assert payload["guidance"]

    def test_governing_sources_exclude_the_subject(self) -> None:
        snapshot = CanonicalContextSnapshot(
            subject_issue_number=183, sources=(_subject(), _governing(21))
        )

        assert [s.issue_number for s in snapshot.governing_sources] == [21]

    @pytest.mark.parametrize(
        "payload",
        [
            {"schema_version": "1", "subject_issue_number": 1, "sources": []},
            {"schema_version": 1, "subject_issue_number": "1", "sources": []},
            {"schema_version": 1, "subject_issue_number": 1, "sources": {}},
            {"schema_version": 2, "subject_issue_number": 1, "sources": []},
        ],
    )
    def test_malformed_stored_content_is_loud(self, payload: dict) -> None:
        with pytest.raises(ValueError):
            CanonicalContextSnapshot.from_dict(payload)
