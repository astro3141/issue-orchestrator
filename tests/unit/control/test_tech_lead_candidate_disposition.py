"""What a Tech Lead verdict does to ONE exact candidate (#345).

The proof directions of the leaf, one class each:

* **A. Exact candidate movement** — a disposition rendered against ``A`` may not
  reach ``B``. Removing the completion-time head re-read makes these fail by
  observing the authority transfer.
* **C. PASS** — only a still-current candidate receives the merge-facing label,
  and the receipt names the exact commit and the run that decided it.
* **D. REWORK** — actionable, candidate-bound feedback lands BEFORE the
  ``needs-rework`` projection, through the existing lane.
* **E. HUMAN_A** — no merge and no rework authority; the existing escalation
  surface, with the question preserved.
* **F. Multi-candidate isolation** — two candidates in one batch reach
  independent answers.
* **G. Watch-set exit** — every candidate this run could audit stops awaiting a
  tech-lead answer, including the outcomes that produce no merge authority.
  Removing the exit makes the batch re-fire over unchanged evidence forever.

Plus the prerequisite B rests on at THIS seam: a PASS is refused unless the
launch authority records an independent reviewer approval of that exact commit,
so the rule holds against an agent that renders one anyway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.tech_lead_candidate_disposition import (
    candidate_effects,
    plan_candidate_dispositions,
    repository_candidate_heads,
)
from issue_orchestrator.domain.tech_lead_artifacts import TechLeadDecision
from issue_orchestrator.control.tech_lead_candidate_policy import (
    TechLeadCandidatePolicy,
)
from issue_orchestrator.domain.tech_lead_candidate import (
    CandidateOutcome,
    CandidateStanding,
    TechLeadCandidate,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config

CANDIDATE_A = "a" * 40
CANDIDATE_B = "b" * 40
RUN_IDENTITY = "20260603T000000Z/issue-7"


def _config(tmp_path: Path) -> Config:
    config = Config(repo="acme/repo", repo_root=tmp_path)
    config.tech_lead_review_agent = "agent:tech-lead"
    return config


def _authority(
    *candidates: TechLeadCandidate, reviewed: tuple[TechLeadCandidate, ...] | None = None
) -> TechLeadLaunchAuthority:
    """A batch authority whose candidates arrived independently reviewed.

    ``reviewed`` defaults to ALL of them: these tests are about what a verdict
    does to a candidate, and the prerequisite has its own class below.
    """
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=tuple(candidate.pr_number for candidate in candidates),
        manifest_candidates=candidates,
        reviewed_candidates=candidates if reviewed is None else reviewed,
    )


def _decision(*verdicts: dict[str, object]) -> TechLeadDecision:
    return TechLeadDecision.from_agent_payload(
        {
            "schema_version": 1,
            "summary": "Contract review of the batch.",
            "findings": [],
            "proposed_actions": [],
            "candidate_verdicts": list(verdicts),
        }
    )


def _verdict(
    pr_number: int,
    disposition: str,
    *,
    sha: str = CANDIDATE_A,
    rationale: str = "Because the contract says so.",
) -> dict[str, object]:
    return {
        "pr_number": pr_number,
        "candidate_sha": sha,
        "disposition": disposition,
        "rationale": rationale,
    }


def _plan(
    tmp_path: Path,
    authority: TechLeadLaunchAuthority,
    decision: TechLeadDecision,
    heads: dict[int, str | None],
) -> list[object]:
    config = _config(tmp_path)
    return plan_candidate_dispositions(
        config,
        authority,
        decision,
        expected=None,
        labels=LabelManager(config),
        heads=lambda pr_number: heads.get(pr_number),
        run_identity=RUN_IDENTITY,
    )


def _labels(actions: list[object], label: str) -> list[AddLabelAction]:
    return [
        action
        for action in actions
        if isinstance(action, AddLabelAction) and action.label == label
    ]


def _comments(actions: list[object], pr_number: int) -> list[AddCommentAction]:
    return [
        action
        for action in actions
        if isinstance(action, AddCommentAction) and action.number == pr_number
    ]


class TestExactCandidateMovement:
    """A: PASS(A) cannot project authority onto B."""

    def test_a_moved_candidate_receives_no_reviewed_label(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_B},
        )

        assert _labels(actions, "tech-lead-reviewed") == []

    def test_the_refusal_is_explicit_and_durable_on_the_pull_request(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_B},
        )

        [receipt] = _comments(actions, 101)
        assert CANDIDATE_A in receipt.comment
        assert CANDIDATE_B in receipt.comment
        assert RUN_IDENTITY in receipt.comment
        assert "no longer current" in receipt.comment

    def test_a_moved_candidate_receives_no_rework_authority_either(
        self, tmp_path: Path
    ) -> None:
        """A REWORK is authority too: it consumes the rework budget."""
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_B},
        )

        assert _labels(actions, "needs-rework") == []
        assert not any(isinstance(action, RemoveLabelAction) for action in actions)

    def test_an_unreadable_head_is_not_an_unchanged_one(self, tmp_path: Path) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "pass")),
            {101: None},
        )

        assert _labels(actions, "tech-lead-reviewed") == []
        assert "could not be read" in _comments(actions, 101)[0].comment

    def test_a_candidate_that_never_bound_a_commit_cannot_pass(
        self, tmp_path: Path
    ) -> None:
        authority = _authority(TechLeadCandidate(101, ""))
        actions = _plan(
            tmp_path,
            authority,
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_A},
        )

        assert _labels(actions, "tech-lead-reviewed") == []

    def test_the_head_reader_asks_the_repository_for_the_live_head(
        self, tmp_path: Path
    ) -> None:
        """The mutation the proof rests on: remove this read and A becomes B."""

        class Host:
            def __init__(self) -> None:
                self.asked: list[int] = []

            def get_pr(self, pr_number: int) -> object:
                self.asked.append(pr_number)
                return type("PR", (), {"head_sha": CANDIDATE_B})()

        host = Host()
        reader = repository_candidate_heads(host)  # type: ignore[arg-type]

        assert reader(101) == CANDIDATE_B
        assert host.asked == [101]

    def test_an_unreadable_repository_answers_unknown_not_unchanged(
        self, tmp_path: Path
    ) -> None:
        class ExplodingHost:
            def get_pr(self, pr_number: int) -> object:
                raise RuntimeError("transport is down")

        reader = repository_candidate_heads(ExplodingHost())  # type: ignore[arg-type]

        assert reader(101) is None


class TestPassVerdict:
    """C: a PASS on a still-current candidate, and nothing wider."""

    def test_pass_projects_the_merge_facing_label_for_that_candidate(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_A},
        )

        assert [label.issue_number for label in _labels(actions, "tech-lead-reviewed")] == [101]

    def test_the_receipt_names_the_exact_candidate_and_the_run(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "pass", rationale="Conforms to the TD.")),
            {101: CANDIDATE_A},
        )

        [receipt] = _comments(actions, 101)
        assert CANDIDATE_A in receipt.comment
        assert RUN_IDENTITY in receipt.comment
        assert "Conforms to the TD." in receipt.comment

    def test_a_candidate_with_no_verdict_receives_nothing(
        self, tmp_path: Path
    ) -> None:
        """Silence is not a PASS: no verdict, no merge-facing effect."""
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(),
            {101: CANDIDATE_A},
        )

        assert actions == []


class TestReworkVerdict:
    """D: bounded rework, through the lane that already exists."""

    def test_rework_never_projects_the_reviewed_label(self, tmp_path: Path) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_A},
        )

        assert _labels(actions, "tech-lead-reviewed") == []

    def test_the_feedback_is_published_before_the_rework_projection(
        self, tmp_path: Path
    ) -> None:
        """#295: a bare ``needs-rework`` with nothing actionable is forbidden."""
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_A},
        )

        feedback = next(
            index
            for index, action in enumerate(actions)
            if isinstance(action, AddCommentAction)
        )
        projection = next(
            index
            for index, action in enumerate(actions)
            if isinstance(action, AddLabelAction) and action.label == "needs-rework"
        )
        assert feedback < projection
        assert "Extract the owner." in actions[feedback].comment
        assert CANDIDATE_A in actions[feedback].comment

    def test_the_rework_reaches_the_existing_admission_path_exactly_once(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_A},
        )

        assert len(_labels(actions, "needs-rework")) == 1

    def test_the_watch_label_comes_off_so_the_batch_is_not_re_tripped(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_A},
        )

        removals = [
            action for action in actions if isinstance(action, RemoveLabelAction)
        ]
        assert [action.label for action in removals] == ["code-reviewed"]

    def test_rework_escalates_no_human(self, tmp_path: Path) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_A},
        )

        assert _labels(actions, "needs-human") == []


class TestHumanAuthorityVerdict:
    """E: the existing escalation boundary, and no other authority."""

    def test_human_a_produces_neither_merge_nor_rework_authority(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "human_a", rationale="Whose call is this?")),
            {101: CANDIDATE_A},
        )

        assert _labels(actions, "tech-lead-reviewed") == []
        assert _labels(actions, "needs-rework") == []

    def test_it_uses_the_existing_needs_human_surface_with_the_question(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "human_a", rationale="Whose call is this?")),
            {101: CANDIDATE_A},
        )

        [label] = _labels(actions, "needs-human")
        assert label.issue_number == 101
        assert label.needs_human_cause is not None
        [comment] = _comments(actions, 101)
        assert "Whose call is this?" in comment.comment
        assert CANDIDATE_A in comment.comment


class TestMultiCandidateIsolation:
    """F: one run, two candidates, two independent answers."""

    def test_pass_and_rework_in_one_batch_do_not_leak_into_each_other(
        self, tmp_path: Path
    ) -> None:
        authority = _authority(
            TechLeadCandidate(101, CANDIDATE_A),
            TechLeadCandidate(102, CANDIDATE_B),
        )
        decision = _decision(
            _verdict(101, "pass"),
            _verdict(102, "rework", sha=CANDIDATE_B, rationale="Missing tests."),
        )

        actions = _plan(
            tmp_path, authority, decision, {101: CANDIDATE_A, 102: CANDIDATE_B}
        )

        assert [l.issue_number for l in _labels(actions, "tech-lead-reviewed")] == [101]
        assert [l.issue_number for l in _labels(actions, "needs-rework")] == [102]

    def test_one_moved_sibling_does_not_refuse_the_other(
        self, tmp_path: Path
    ) -> None:
        authority = _authority(
            TechLeadCandidate(101, CANDIDATE_A),
            TechLeadCandidate(102, CANDIDATE_B),
        )
        decision = _decision(
            _verdict(101, "pass"), _verdict(102, "pass", sha=CANDIDATE_B)
        )

        effects = candidate_effects(
            _config(tmp_path),
            authority,
            decision,
            expected=None,
            labels=LabelManager(_config(tmp_path)),
            heads=lambda pr_number: {101: CANDIDATE_A, 102: "c" * 40}[pr_number],
            run_identity=RUN_IDENTITY,
        )

        by_pr = {item.candidate.pr_number: item for item in effects}
        assert by_pr[101].standing is CandidateStanding.CURRENT
        assert by_pr[101].projected_reviewed_label is True
        assert by_pr[102].standing is CandidateStanding.MOVED
        assert by_pr[102].projected_reviewed_label is False


class TestIndependentReviewPrerequisite:
    """A PASS rests on evidence the orchestrator itself established (#345).

    The prompt tells the tech lead not to pass a candidate whose staged
    evidence carries a gap. These fix what happens when it does anyway — the
    check the agent cannot reach.
    """

    def test_a_pass_on_an_unreviewed_candidate_projects_nothing(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A), reviewed=()),
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_A},
        )

        assert _labels(actions, "tech-lead-reviewed") == []

    def test_the_refusal_says_which_prerequisite_was_missing(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A), reviewed=()),
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_A},
        )

        [receipt] = _comments(actions, 101)
        assert "not independently" in receipt.comment
        assert CANDIDATE_A in receipt.comment

    def test_rework_and_human_a_do_not_need_the_prerequisite(
        self, tmp_path: Path
    ) -> None:
        """Neither claims the candidate is mergeable, so neither is gated on it."""
        rework = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A), reviewed=()),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            {101: CANDIDATE_A},
        )
        human = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A), reviewed=()),
            _decision(_verdict(101, "human_a", rationale="Whose call?")),
            {101: CANDIDATE_A},
        )

        assert len(_labels(rework, "needs-rework")) == 1
        assert len(_labels(human, "needs-human")) == 1

    def test_a_sibling_that_is_reviewed_still_passes(self, tmp_path: Path) -> None:
        reviewed = TechLeadCandidate(101, CANDIDATE_A)
        unreviewed = TechLeadCandidate(102, CANDIDATE_B)
        actions = _plan(
            tmp_path,
            _authority(reviewed, unreviewed, reviewed=(reviewed,)),
            _decision(
                _verdict(101, "pass"), _verdict(102, "pass", sha=CANDIDATE_B)
            ),
            {101: CANDIDATE_A, 102: CANDIDATE_B},
        )

        assert [l.issue_number for l in _labels(actions, "tech-lead-reviewed")] == [101]


@pytest.mark.parametrize(
    "disposition", ["pass", "rework", "human_a"]
)
def test_every_disposition_leaves_a_receipt_naming_its_candidate(
    tmp_path: Path, disposition: str
) -> None:
    """Labels are projections; the receipt is what says what was decided."""
    actions = _plan(
        tmp_path,
        _authority(TechLeadCandidate(101, CANDIDATE_A)),
        _decision(_verdict(101, disposition, rationale="Stated reason.")),
        {101: CANDIDATE_A},
    )

    [comment] = _comments(actions, 101)
    assert CANDIDATE_A in comment.comment
    assert RUN_IDENTITY in comment.comment


class TestWatchSetExit:
    """G: every candidate this run COULD audit stops awaiting an answer.

    Before #345 a landed batch review projected ``tech-lead-reviewed`` onto
    every manifest pull request. That was wrong as merge authority, but it was
    also the only thing that ever removed a pull request from the tech-lead
    watch set. These fix its replacement: the disposition that produces no
    merge authority still has to settle the candidate, or the same batch fires
    again on the next tick over the same unchanged evidence, forever.
    """

    def test_human_a_stops_the_candidate_counting_toward_the_threshold(
        self, tmp_path: Path
    ) -> None:
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "human_a", rationale="Whose call is this?")),
            {101: CANDIDATE_A},
        )

        assert [l.issue_number for l in _labels(actions, "tech-lead-failed")] == [101]

    def test_a_refused_pass_stops_the_candidate_too(self, tmp_path: Path) -> None:
        """Otherwise the refusal comment is re-posted every batch, forever."""
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A), reviewed=()),
            _decision(_verdict(101, "pass")),
            {101: CANDIDATE_A},
        )

        assert [l.issue_number for l in _labels(actions, "tech-lead-failed")] == [101]
        assert _labels(actions, "tech-lead-reviewed") == []

    def test_a_moved_candidate_deliberately_stays_in_the_watch_set(
        self, tmp_path: Path
    ) -> None:
        """The one keep: it must be re-audited at what it NOW proposes."""
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "human_a", rationale="Whose call?")),
            {101: CANDIDATE_B},
        )

        assert not [a for a in actions if isinstance(a, AddLabelAction)]
        assert not [a for a in actions if isinstance(a, RemoveLabelAction)]

    def test_the_rework_removal_targets_the_configured_watch_label(
        self, tmp_path: Path
    ) -> None:
        """F1: the batch is keyed on the watch label, not on ``code_reviewed``.

        Removing a locally-derived label leaves the real watch label in place,
        so the candidate re-trips the very threshold the rework just left.
        """
        config = _config(tmp_path)
        config.tech_lead_review_label = "awaiting-tech-lead"
        config.code_reviewed_label = "code-reviewed"

        actions = plan_candidate_dispositions(
            config,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "rework", rationale="Extract the owner.")),
            expected=None,
            labels=LabelManager(config),
            heads=lambda pr_number: CANDIDATE_A,
            run_identity=RUN_IDENTITY,
        )

        removed = {
            action.label
            for action in actions
            if isinstance(action, RemoveLabelAction)
        }
        assert removed == {"awaiting-tech-lead", "code-reviewed"}

    def test_a_settled_candidate_is_no_longer_a_batch_candidate(
        self, tmp_path: Path
    ) -> None:
        """The invariant, stated end to end through the owner that holds it."""
        config = _config(tmp_path)
        policy = TechLeadCandidatePolicy.from_config(config)
        actions = _plan(
            tmp_path,
            _authority(TechLeadCandidate(101, CANDIDATE_A)),
            _decision(_verdict(101, "human_a", rationale="Whose call?")),
            {101: CANDIDATE_A},
        )
        settled_labels = [config.tech_lead_watch_label] + [
            action.label for action in actions if isinstance(action, AddLabelAction)
        ]

        assert policy.is_candidate([config.tech_lead_watch_label]) is True
        assert policy.is_candidate(settled_labels) is False

    @pytest.mark.parametrize(
        ("disposition", "reviewed", "expected"),
        [
            ("pass", True, CandidateOutcome.AUTHORITY),
            ("pass", False, CandidateOutcome.UNSETTLED),
            ("rework", True, CandidateOutcome.REWORK),
            ("human_a", True, CandidateOutcome.HUMAN),
        ],
    )
    def test_the_outcome_is_what_the_orchestrator_concluded(
        self,
        tmp_path: Path,
        disposition: str,
        reviewed: bool,
        expected: CandidateOutcome,
    ) -> None:
        candidate = TechLeadCandidate(101, CANDIDATE_A)
        config = _config(tmp_path)
        effects = candidate_effects(
            config,
            _authority(candidate, reviewed=(candidate,) if reviewed else ()),
            _decision(_verdict(101, disposition, rationale="Stated reason.")),
            expected=None,
            labels=LabelManager(config),
            heads=lambda pr_number: CANDIDATE_A,
            run_identity=RUN_IDENTITY,
        )

        [planned] = effects
        assert planned.outcome is expected
        assert planned.leaves_watch_set is True
