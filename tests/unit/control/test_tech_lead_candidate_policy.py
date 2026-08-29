"""The one owner of tech_lead watch-set membership, both directions (#345).

Entry already had an owner and it stays proven where it always was
(``test_tech_lead_manifest_builder``). What is new here is the EXIT half and
the property that binds them: an outcome the run could reach must leave the
candidate in a state ``is_candidate`` answers False for, or the batch that
produced the outcome fires again over unchanged evidence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.tech_lead_candidate_policy import (
    TechLeadCandidatePolicy,
)
from issue_orchestrator.domain.tech_lead_candidate import CandidateOutcome
from issue_orchestrator.infra.config import Config


def _config(tmp_path: Path) -> Config:
    return Config(repo="acme/repo", repo_root=tmp_path)


class TestTheWatchLabelHasOneOwner:
    def test_the_selecting_label_is_the_configured_watch_label(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        config.tech_lead_review_label = "awaiting-tech-lead"
        config.code_reviewed_label = "code-reviewed"

        policy = TechLeadCandidatePolicy.from_config(config)

        assert policy.watch_label == config.tech_lead_watch_label == "awaiting-tech-lead"

    def test_the_review_marker_is_resolved_through_the_label_registry(
        self, tmp_path: Path
    ) -> None:
        """A prefixed deployment writes the prefixed marker; clear THAT one."""
        config = _config(tmp_path)
        config.label_prefix = "bot"
        config.code_reviewed_label = "code-reviewed"

        policy = TechLeadCandidatePolicy.from_config(config)

        assert policy.review_approved_label == "bot:code-reviewed"

    def test_the_rework_exit_clears_the_watch_label_not_a_local_derivation(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        config.tech_lead_review_label = "awaiting-tech-lead"
        config.code_reviewed_label = "code-reviewed"

        exit_rule = TechLeadCandidatePolicy.from_config(config).settle(
            CandidateOutcome.REWORK
        )

        assert "awaiting-tech-lead" in exit_rule.remove
        assert exit_rule.add == ()

    def test_the_default_configuration_asks_to_remove_one_label_once(
        self, tmp_path: Path
    ) -> None:
        """Watch label and review marker coincide by default; do not say it twice."""
        exit_rule = TechLeadCandidatePolicy.from_config(_config(tmp_path)).settle(
            CandidateOutcome.REWORK
        )

        assert exit_rule.remove == ("code-reviewed",)


class TestEveryOutcomeSettlesOrDeliberatelyDoesNot:
    @pytest.mark.parametrize(
        ("outcome", "expected_add"),
        [
            (CandidateOutcome.AUTHORITY, ("tech-lead-reviewed",)),
            (CandidateOutcome.HUMAN, ("tech-lead-failed",)),
            (CandidateOutcome.UNSETTLED, ("tech-lead-failed",)),
        ],
    )
    def test_a_settled_candidate_receives_a_terminal_label(
        self, tmp_path: Path, outcome: CandidateOutcome, expected_add: tuple[str, ...]
    ) -> None:
        exit_rule = TechLeadCandidatePolicy.from_config(_config(tmp_path)).settle(
            outcome
        )

        assert exit_rule.add == expected_add
        assert exit_rule.keeps_membership is False

    def test_a_deferred_candidate_keeps_its_membership_and_changes_nothing(
        self, tmp_path: Path
    ) -> None:
        exit_rule = TechLeadCandidatePolicy.from_config(_config(tmp_path)).settle(
            CandidateOutcome.DEFERRED
        )

        assert exit_rule.add == ()
        assert exit_rule.remove == ()
        assert exit_rule.keeps_membership is True

    @pytest.mark.parametrize("outcome", list(CandidateOutcome))
    def test_the_owner_answers_for_every_outcome_that_exists(
        self, tmp_path: Path, outcome: CandidateOutcome
    ) -> None:
        """A new outcome must state its exit rather than inheriting silence."""
        exit_rule = TechLeadCandidatePolicy.from_config(_config(tmp_path)).settle(
            outcome
        )

        assert exit_rule.keeps_membership is not outcome.settles_membership

    @pytest.mark.parametrize("outcome", list(CandidateOutcome))
    def test_the_exit_and_the_entry_predicate_agree(
        self, tmp_path: Path, outcome: CandidateOutcome
    ) -> None:
        """The invariant, replayed the way the next batch actually asks it.

        A batch selects on the watch label FIRST and only then applies
        ``is_candidate``. So a settled candidate is one that either loses the
        selecting label or answers False to the predicate — and a deferred one
        must survive both, or it is never re-audited.
        """
        config = _config(tmp_path)
        policy = TechLeadCandidatePolicy.from_config(config)
        exit_rule = policy.settle(outcome)
        after = [
            label
            for label in (policy.watch_label, *exit_rule.add)
            if label not in exit_rule.remove
        ]
        selected = policy.watch_label in after

        assert (selected and policy.is_candidate(after)) is exit_rule.keeps_membership


class TestTerminalLabels:
    def test_both_terminal_labels_are_named(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        config.tech_lead_reviewed_label = "my-reviewed"
        config.tech_lead_failed_label = "my-failed"

        policy = TechLeadCandidatePolicy.from_config(config)

        assert policy.terminal_labels == ("my-reviewed", "my-failed")

    def test_either_terminal_label_ends_candidacy(self, tmp_path: Path) -> None:
        policy = TechLeadCandidatePolicy.from_config(_config(tmp_path))

        assert policy.is_candidate(["code-reviewed", "tech-lead-reviewed"]) is False
        assert policy.is_candidate(["code-reviewed", "tech-lead-failed"]) is False
