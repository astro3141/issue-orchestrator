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

Both seams are exercised because the point of the repair is that they are ONE
rule: ``FactGatherer`` counts toward the threshold, ``TechLeadManifestBuilder``
writes the set the batch audits, and neither may hold a lifecycle rule of its
own.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.tech_lead_candidate_policy import (
    TechLeadCandidatePolicy,
)
from issue_orchestrator.control.tech_lead_manifest_builder import (
    TechLeadManifestBuilder,
)
from issue_orchestrator.domain.models import OrchestratorState
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

    def merge(self, number: int) -> None:
        """The pull request merges between two observations."""
        self.prs = [
            _pr(pr.number, state="merged", labels=pr.labels)
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
