"""A Tech Lead batch candidate is an OPEN pull request (#352).

The watch label is never removed on merge, so a repository accumulates one
watch-labelled pull request per merged branch, forever. Asking the repository
host for ``state="all"`` therefore answered "which pull requests need a tech
lead review" with the whole merged history: a threshold of 1 tripped on a
100-pull-request manifest spanning years, none of which could ever merge again.

The proof directions of this leaf, one class each:

* **A. Historical contamination** — closed and merged watch-labelled pull
  requests reach neither the threshold count nor the manifest, and the query
  that produced them asked GitHub for open pull requests only. Restoring
  ``state="all"`` fails the query assertions; dropping the lifecycle predicate
  fails the set assertions.
* **B. No open candidate** — a repository whose watch label survives only on
  history admits nothing at all.
* **C. Lifecycle race** — a candidate counted while open and merged before the
  manifest is built is gone from the manifest. Threshold-time openness is never
  carried forward as authority, so it is excluded even by an observation that
  hands it back.
* **D. Existing policy preserved** — the configured watch label still narrows
  which OPEN pull requests are candidates, and is still not required in order
  to exclude closed history.
* **E. Post-manifest lifecycle race** — a candidate the manifest bound while
  open, which then merges at the exact commit that was audited, receives no
  candidate-bound effect when the batch completes. This is the direction a
  head-only completion re-read cannot see, because the head is unchanged.

All three seams are exercised because the point of the repair is that they are
ONE rule: ``FactGatherer`` counts toward the threshold,
``TechLeadManifestBuilder`` writes the set the batch audits, and
``tech_lead_candidate_disposition`` settles it — and none of them may hold a
lifecycle rule of its own.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    RemoveLabelAction,
)
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.tech_lead_candidate_disposition import (
    candidate_standing,
    plan_candidate_dispositions,
    repository_candidate_observations,
)
from issue_orchestrator.control.tech_lead_candidate_policy import (
    TechLeadCandidatePolicy,
)
from issue_orchestrator.control.tech_lead_manifest_builder import (
    TechLeadManifestBuilder,
)
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.tech_lead_artifacts import TechLeadDecision
from issue_orchestrator.domain.tech_lead_candidate import (
    CandidateStanding,
    TechLeadCandidate,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import PRInfo

WATCH = "code-reviewed"


def _pr(number: int, *, state: str = "open", labels: list[str] | None = None) -> PRInfo:
    return PRInfo(
        number=number,
        title=f"PR {number}",
        url=f"https://example.invalid/pull/{number}",
        branch=f"branch-{number}",
        body="",
        state=state,
        labels=[WATCH] if labels is None else labels,
        head_sha=f"{number:040d}",
    )


class FakeRepositoryHost:
    """Answers ``state`` the way GitHub does, and records what it was asked."""

    def __init__(self, prs: list[PRInfo]) -> None:
        self.prs = list(prs)
        self.queries: list[tuple[str, str]] = []

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        self.queries.append((label, state))
        return [
            pr
            for pr in self.prs
            if label in pr.labels and state in ("all", pr.state)
        ]

    def get_pr(self, number: int) -> PRInfo | None:
        """The single-pull-request read the completion-time observation makes."""
        for pr in self.prs:
            if pr.number == number:
                return pr
        return None

    def merge(self, number: int) -> None:
        """The pull request merges between two observations.

        At the head it already had: ``_pr`` derives the head from the number,
        so a merged pull request keeps the exact commit that was audited. That
        is what makes the completion-time direction below a real proof rather
        than a restatement of the moved-head one.
        """
        self.settle(number, "merged")

    def close(self, number: int) -> None:
        """The pull request is closed unmerged between two observations."""
        self.settle(number, "closed")

    def settle(self, number: int, state: str) -> None:
        self.prs = [
            _pr(pr.number, state=state, labels=pr.labels)
            if pr.number == number
            else pr
            for pr in self.prs
        ]


class LifecycleBlindHost(FakeRepositoryHost):
    """A host that hands back every watch-labelled PR whatever it was asked.

    Stands in for the observation the repair must survive on its own: the
    manifest may not audit a merged pull request because a query answered
    loosely, and it may not carry threshold-time openness forward instead of
    reading what it was actually handed.
    """

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        self.queries.append((label, state))
        return [pr for pr in self.prs if label in pr.labels]


def _config(watch_label: str | None = None) -> Config:
    config = Config()
    config.repo = "owner/repo"
    config.tech_lead_review_agent = "agent:tech-lead"
    config.tech_lead_review_threshold = 1
    config.code_reviewed_label = WATCH
    config.tech_lead_review_label = watch_label
    return config


def _threshold_facts(config: Config, host: FakeRepositoryHost):
    """The batch facts, gathered the way a tick gathers them."""
    repository_host = MagicMock()
    repository_host.list_issues.return_value = []
    repository_host.get_issue.return_value = None
    repository_host.get_prs_with_label.side_effect = host.get_prs_with_label
    facts = FactGatherer(
        config=config, repository_host=repository_host
    ).gather_tech_lead_facts(OrchestratorState())
    assert facts is not None
    return facts


def _manifest(config: Config, host: FakeRepositoryHost):
    builder = TechLeadManifestBuilder(
        host, candidate_policy=TechLeadCandidatePolicy.from_config(config)
    )
    return builder.build(data_dir="tech-lead-data")


class TestHistoricalContamination:
    """A: merged history carrying the watch label is not a candidate set."""

    @staticmethod
    def _repository() -> FakeRepositoryHost:
        """One open candidate among a repository's worth of settled history."""
        return FakeRepositoryHost(
            [
                *(_pr(number, state="merged") for number in range(1, 20)),
                *(_pr(number, state="closed") for number in range(20, 30)),
                _pr(30),
            ]
        )

    def test_only_the_open_pull_request_trips_the_threshold(self) -> None:
        config = _config()
        host = self._repository()

        facts = _threshold_facts(config, host)

        assert facts.pr_count == 1
        assert [number for number, _ in facts.prs] == [30]

    def test_only_the_open_pull_request_is_audited(self) -> None:
        config = _config()
        host = self._repository()

        manifest = _manifest(config, host)

        assert [pr.number for pr in manifest.prs] == [30]

    def test_the_threshold_asks_github_for_open_pull_requests_only(self) -> None:
        config = _config()
        host = self._repository()

        _threshold_facts(config, host)

        assert host.queries == [(WATCH, "open")]

    def test_the_manifest_asks_github_for_open_pull_requests_only(self) -> None:
        config = _config()
        host = self._repository()

        _manifest(config, host)

        assert host.queries == [(WATCH, "open")]

    def test_a_lifecycle_blind_observation_still_audits_nothing_settled(self) -> None:
        """The predicate is the backstop, not a restatement of the query."""
        config = _config()
        host = LifecycleBlindHost(self._repository().prs)

        manifest = _manifest(config, host)

        assert [pr.number for pr in manifest.prs] == [30]


class TestNoOpenCandidate:
    """B: a watch label that survives only on history admits nothing."""

    @staticmethod
    def _repository() -> FakeRepositoryHost:
        return FakeRepositoryHost(
            [_pr(1, state="merged"), _pr(2, state="closed"), _pr(3, state="merged")]
        )

    def test_no_batch_threshold_admission(self) -> None:
        facts = _threshold_facts(_config(), self._repository())

        assert facts.pr_count == 0
        assert facts.prs == ()

    def test_no_manifest_containing_them(self) -> None:
        manifest = _manifest(_config(), self._repository())

        assert manifest.prs == []


class TestLifecycleRace:
    """C: the later observation wins; openness is not carried forward."""

    def test_a_candidate_that_merged_since_it_was_counted_is_not_audited(
        self,
    ) -> None:
        config = _config()
        host = FakeRepositoryHost([_pr(1), _pr(2)])

        counted = _threshold_facts(config, host)
        host.merge(1)
        manifest = _manifest(config, host)

        assert [number for number, _ in counted.prs] == [1, 2]
        assert [pr.number for pr in manifest.prs] == [2]

    def test_it_is_not_audited_even_when_the_observation_hands_it_back(self) -> None:
        config = _config()
        host = LifecycleBlindHost([_pr(1), _pr(2)])

        _threshold_facts(config, host)
        host.merge(1)
        manifest = _manifest(config, host)

        assert [pr.number for pr in manifest.prs] == [2]

    def test_no_candidate_bound_effect_can_reach_the_merged_pull_request(self) -> None:
        """A disposition is bound to a manifest candidate (#345), so omission
        from the manifest is what makes the terminal pull request unreachable.
        """
        config = _config()
        host = FakeRepositoryHost([_pr(1)])

        _threshold_facts(config, host)
        host.merge(1)
        manifest = _manifest(config, host)

        assert manifest.prs == []


class TestConfiguredWatchLabelIsPreserved:
    """D: a custom watch label narrows OPEN candidates, nothing more."""

    @staticmethod
    def _repository() -> FakeRepositoryHost:
        return FakeRepositoryHost(
            [
                _pr(1, labels=[WATCH]),
                _pr(2, labels=["ready-for-tech-lead"]),
                _pr(3, state="merged", labels=["ready-for-tech-lead"]),
            ]
        )

    def test_the_configured_label_still_selects_among_open_pull_requests(
        self,
    ) -> None:
        config = _config(watch_label="ready-for-tech-lead")
        host = self._repository()

        manifest = _manifest(config, host)

        assert [pr.number for pr in manifest.prs] == [2]
        assert host.queries == [("ready-for-tech-lead", "open")]

    def test_the_default_label_excludes_closed_history_without_one(self) -> None:
        """A dedicated label is not the mechanism that keeps history out."""
        config = _config()
        host = FakeRepositoryHost([_pr(1), _pr(2, state="merged")])

        manifest = _manifest(config, host)

        assert [pr.number for pr in manifest.prs] == [1]

    def test_a_terminal_label_still_ends_candidacy_on_an_open_pull_request(
        self,
    ) -> None:
        config = _config()
        host = FakeRepositoryHost(
            [_pr(1), _pr(2, labels=[WATCH, "tech-lead-reviewed"])]
        )

        manifest = _manifest(config, host)

        assert [pr.number for pr in manifest.prs] == [1]


RUN_IDENTITY = "20260829T000000Z/tech-lead-1"


def _bound_authority(config: Config, host: FakeRepositoryHost):
    """The launch authority a batch built from THIS manifest carries (#345).

    Built from the manifest rather than hand-written, so the candidates whose
    dispositions are planned below are exactly the ones the open-only
    observation admitted — including the head each was bound at.
    """
    manifest = _manifest(config, host)
    candidates = tuple(
        TechLeadCandidate(pr.number, pr.head_sha) for pr in manifest.prs
    )
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=tuple(candidate.pr_number for candidate in candidates),
        manifest_candidates=candidates,
        reviewed_candidates=candidates,
        contracted_candidates=candidates,
        diffed_candidates=candidates,
    )


def _decision(pr_number: int, disposition: str) -> TechLeadDecision:
    return TechLeadDecision.from_agent_payload(
        {
            "schema_version": 1,
            "summary": "Contract review of the batch.",
            "findings": [],
            "proposed_actions": [],
            "candidate_verdicts": [
                {
                    "pr_number": pr_number,
                    "candidate_sha": f"{pr_number:040d}",
                    "disposition": disposition,
                    "rationale": "Stated reason.",
                }
            ],
        }
    )


def _dispositions(
    config: Config, host: FakeRepositoryHost, authority, decision: TechLeadDecision
) -> list[object]:
    """Completion-time planning, through the reader production builds."""
    return plan_candidate_dispositions(
        config,
        authority,
        decision,
        expected=None,
        labels=LabelManager(config),
        observations=repository_candidate_observations(host),  # type: ignore[arg-type]
        run_identity=RUN_IDENTITY,
    )


class TestPostManifestLifecycleRace:
    """E: a candidate that settles AFTER the manifest bound it settles nothing.

    The direction the first repair missed. Threshold and manifest are open-only
    now, but a batch review runs for as long as it runs, and the completion-time
    re-read that applies each verdict asked only whether the head was still the
    audited one. A pull request MERGES at that head, so the answer was yes and
    merge-facing effects projected onto a pull request that had already merged.
    """

    def test_the_manifest_binds_the_candidate_while_it_is_open(self) -> None:
        """The premise: the pull request really was a candidate (#345 binding)."""
        manifest = _manifest(_config(), FakeRepositoryHost([_pr(1)]))

        assert [(pr.number, pr.head_sha) for pr in manifest.prs] == [
            (1, f"{1:040d}")
        ]

    def test_the_completion_read_asks_the_lifecycle_not_only_the_head(self) -> None:
        """The mutation this leaf rests on, stated as an assertion.

        The head is UNCHANGED — ``covers`` says so — and the standing is still
        not CURRENT. Restore the head-only completion observation and the
        standing becomes CURRENT, which is what fails here.
        """
        config = _config()
        host = FakeRepositoryHost([_pr(1)])
        authority = _bound_authority(config, host)
        [candidate] = authority.manifest_candidates
        host.merge(1)

        standing, observed = candidate_standing(
            candidate,
            repository_candidate_observations(host),  # type: ignore[arg-type]
        )

        assert candidate.covers(observed) is True
        assert standing is CandidateStanding.TERMINAL

    @pytest.mark.parametrize("settle", ["merge", "close"])
    def test_a_pass_produces_no_merge_facing_authority(self, settle: str) -> None:
        config = _config()
        host = FakeRepositoryHost([_pr(1)])
        authority = _bound_authority(config, host)
        getattr(host, settle)(1)

        actions = _dispositions(config, host, authority, _decision(1, "pass"))

        assert [
            action
            for action in actions
            if isinstance(action, AddLabelAction)
            and action.label == "tech-lead-reviewed"
        ] == []

    @pytest.mark.parametrize("disposition", ["pass", "rework", "human_a"])
    def test_no_candidate_bound_effect_of_any_kind_is_applied(
        self, disposition: str
    ) -> None:
        """REWORK admission and HUMAN_A escalation are authority too.

        So is a terminal or watch-set label: writing one onto a merged pull
        request settles history that this batch may not settle. The receipt is
        the only thing that may land, and it asserts nothing about lifecycle
        that the observation did not show.
        """
        config = _config()
        host = FakeRepositoryHost([_pr(1)])
        authority = _bound_authority(config, host)
        host.merge(1)

        actions = _dispositions(config, host, authority, _decision(1, disposition))

        assert not [a for a in actions if isinstance(a, AddLabelAction)]
        assert not [a for a in actions if isinstance(a, RemoveLabelAction)]
        [receipt] = [a for a in actions if isinstance(a, AddCommentAction)]
        assert receipt.number == 1
        assert "no longer open" in receipt.comment

    def test_an_open_sibling_in_the_same_batch_is_unaffected(self) -> None:
        """The refusal is per candidate, not per batch (#345 independence)."""
        config = _config()
        host = FakeRepositoryHost([_pr(1), _pr(2)])
        authority = _bound_authority(config, host)
        host.merge(1)
        decision = TechLeadDecision.from_agent_payload(
            {
                "schema_version": 1,
                "summary": "Contract review of the batch.",
                "findings": [],
                "proposed_actions": [],
                "candidate_verdicts": [
                    {
                        "pr_number": number,
                        "candidate_sha": f"{number:040d}",
                        "disposition": "pass",
                        "rationale": "Stated reason.",
                    }
                    for number in (1, 2)
                ],
            }
        )

        actions = _dispositions(config, host, authority, decision)

        assert [
            action.issue_number
            for action in actions
            if isinstance(action, AddLabelAction)
            and action.label == "tech-lead-reviewed"
        ] == [2]
