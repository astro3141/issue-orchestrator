"""Staging each audited candidate's EXECUTABLE LEAF contract (#345, direction C).

The batch review is asked for a verdict against the governing contract. Before
this the staged inputs were the manifest, the candidate's diff/metadata and the
independent Reviewer's evidence — none of which is that contract. A bounded leaf
may narrow the work below the repository's Spec/TD ("A-C only; direct invocation
deferred"), and a stateless run that cannot read the leaf can only infer such a
constraint from PR prose or a previous session's memory.

The proof directions, one class each:

* **Identity and provenance** — the staged input names the executable issue,
  carries its current body, and resolves only the governing pointers that issue
  itself declares, each by revision and digest.
* **The leaf-only constraint** — a STOP condition that exists ONLY in the leaf
  reaches the run. The mutation is the point: drop the leaf staging and the
  decisive constraint is nowhere in the bundle.
* **Fail closed** — an unidentifiable, unreadable or unresolvable contract is a
  gap, the gap makes ``pass`` structurally unreachable, and the batch still runs
  for the candidate's siblings.
* **Stateless reconstruction** — a second run rebuilds the same contract from
  canonical sources, with no prior-session memory and no Human relay.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from issue_orchestrator.control.tech_lead_candidate_contract import (
    build_candidate_contracts,
    stage_candidate_contracts,
)
from issue_orchestrator.domain.canonical_context import (
    CanonicalSourceKind,
    content_digest,
)
from issue_orchestrator.domain.tech_lead_candidate import CandidatePassPrerequisite
from issue_orchestrator.domain.tech_lead_candidate_contract import (
    TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME,
    TECH_LEAD_CANDIDATE_CONTRACT_FILENAME,
)
from issue_orchestrator.domain.tech_lead_manifest import PRToReview, TechLeadManifest
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)

LEAF = 345
SPEC = 335
POLICY = 295
CANDIDATE_SHA = "a" * 40
OTHER_SHA = "b" * 40

# The kind of constraint that exists ONLY in the executable issue: the leaf
# admits part of the Spec and defers the rest. Nothing in the repository's
# tracked Spec/TD says it, and no previous session may be relied on to remember.
LEAF_ONLY_STOP = "STOP: direct candidate-scoped invocation is deferred; A-C only."


def _issue(
    number: int,
    *,
    body: str = "",
    title: str | None = None,
    state: str = "open",
    updated_at: str = "2026-08-28T09:00:00Z",
    comment_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title=title if title is not None else f"Issue {number}",
        body=body,
        state=state,
        updated_at=updated_at,
        labels=[],
        comment_count=comment_count,
    )


class _Host:
    """A repository host answering only the two existing issue fetch owners."""

    def __init__(
        self,
        issues: dict[int, SimpleNamespace],
        comments: dict[int, list[dict[str, Any]]] | None = None,
        errors: dict[int, Exception] | None = None,
    ) -> None:
        self._issues = issues
        self._comments = comments or {}
        self._errors = errors or {}
        self.issue_calls: list[int] = []

    def get_issue(self, issue_number: int) -> SimpleNamespace | None:
        self.issue_calls.append(issue_number)
        if issue_number in self._errors:
            raise self._errors[issue_number]
        return self._issues.get(issue_number)

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self._comments.get(issue_number, [])


def _entry(
    number: int = 346,
    *,
    branch: str = f"{LEAF}-r28-candidate",
    head_sha: str = CANDIDATE_SHA,
) -> PRToReview:
    return PRToReview(
        number=number,
        title="R28 candidate",
        url=f"https://example.invalid/pull/{number}",
        branch=branch,
        head_sha=head_sha,
    )


def _leaf_body(*, stop: str = LEAF_ONLY_STOP, governs: str = f"Governed-by: #{SPEC}") -> str:
    return f"{governs}\n\n## Bounded contract\n\n{stop}\n"


def _stage(
    tmp_path: Path, host: _Host, *entries: PRToReview
) -> tuple[Any, Path]:
    data_path = tmp_path / "tech-lead-data"
    contracts = stage_candidate_contracts(
        list(entries), repository_host=host, data_path=data_path
    )
    return contracts, data_path


def _authority(
    *, contracted: bool, entries: tuple[PRToReview, ...]
) -> TechLeadLaunchAuthority:
    manifest = TechLeadManifest(prs=list(entries))
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=tuple(entry.number for entry in entries),
        manifest_candidates=manifest.candidates(),
        reviewed_candidates=manifest.candidates(),
        contracted_candidates=manifest.contracted_candidates() if contracted else (),
    )


class TestIdentityAndProvenance:
    """The staged input names the leaf, its bytes, and its declared sources."""

    def test_the_leaf_and_only_its_declared_sources_are_staged(
        self, tmp_path: Path
    ) -> None:
        host = _Host(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body(), title="R28 candidate"),
                SPEC: _issue(SPEC, body="The settled gate contract."),
                # Declared by nobody: a project-wide crawler would take it.
                POLICY: _issue(POLICY, body="Unrelated standing policy."),
            }
        )

        contracts, data_path = _stage(tmp_path, host, _entry())

        (contract,) = contracts.entries
        assert contract.gap == ""
        assert contract.issue_number == LEAF
        leaf, governing = contract.sources
        assert leaf.kind is CanonicalSourceKind.SUBJECT
        assert leaf.issue_number == LEAF
        assert governing.kind is CanonicalSourceKind.GOVERNING
        assert governing.issue_number == SPEC
        assert [source.issue_number for source in contract.sources] == [LEAF, SPEC]
        assert POLICY not in host.issue_calls

    def test_every_digest_attributes_exactly_one_staged_file(
        self, tmp_path: Path
    ) -> None:
        host = _Host(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body(), comment_count=1),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            },
            comments={
                LEAF: [
                    {
                        "id": 5460662430,
                        "updated_at": "2026-08-29T05:31:00Z",
                        "body": "The canonical finding.",
                    }
                ]
            },
        )

        contracts, data_path = _stage(tmp_path, host, _entry())

        (contract,) = contracts.entries
        bundle = data_path / contract.sources_dir
        assert bundle.is_dir()
        leaf = contract.sources[0]
        body_file = bundle / f"issue-{LEAF}" / "body.md"
        assert body_file.read_text() == _leaf_body()
        assert leaf.body_sha256 == content_digest(body_file.read_text())
        assert leaf.updated_at == "2026-08-28T09:00:00Z"
        (comment,) = leaf.comments
        comment_file = bundle / f"issue-{LEAF}" / f"comment-{comment.comment_id}.md"
        assert comment.sha256 == content_digest(comment_file.read_text())
        assert leaf.comment_count == 1
        assert leaf.comments_truncated is False

    def test_the_bundle_is_scoped_to_the_exact_candidate_commit(
        self, tmp_path: Path
    ) -> None:
        """Two candidates sharing a governing source keep separate bundles.

        A shared directory would make one candidate's digests attributable to
        the other, and the audited commit is the only thing that makes a bundle
        this candidate's.
        """
        host = _Host(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body()),
                POLICY: _issue(POLICY, body=f"Governed-by: #{SPEC}\n"),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            }
        )
        first = _entry(346, branch=f"{LEAF}-r28", head_sha=CANDIDATE_SHA)
        second = _entry(
            347, branch=f"{POLICY}-feedback", head_sha=OTHER_SHA
        )

        contracts, data_path = _stage(tmp_path, host, first, second)

        dirs = {entry.sources_dir for entry in contracts.entries}
        assert dirs == {
            f"{TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME}/pr-346-{CANDIDATE_SHA[:12]}",
            f"{TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME}/pr-347-{OTHER_SHA[:12]}",
        }
        for entry in contracts.entries:
            assert (data_path / entry.sources_dir / f"issue-{SPEC}").is_dir()

    def test_the_descriptor_is_written_beside_the_manifest(
        self, tmp_path: Path
    ) -> None:
        host = _Host(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body()),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            }
        )

        _, data_path = _stage(tmp_path, host, _entry())

        payload = json.loads(
            (data_path / TECH_LEAD_CANDIDATE_CONTRACT_FILENAME).read_text()
        )
        (candidate,) = payload["candidates"]
        assert candidate["pr_number"] == 346
        assert candidate["candidate_sha"] == CANDIDATE_SHA
        assert candidate["issue_number"] == LEAF
        assert candidate["gap"] == ""
        assert [source["issue_number"] for source in candidate["sources"]] == [
            LEAF,
            SPEC,
        ]
        # The guidance tells the run what the file is FOR, so a session reading
        # it cannot mistake provenance for authority.
        assert "gap" in payload["guidance"]


class TestLeafOnlyConstraint:
    """The constraint that exists ONLY in the leaf reaches the run."""

    def test_a_leaf_only_stop_condition_is_in_the_staged_bytes(
        self, tmp_path: Path
    ) -> None:
        host = _Host(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body()),
                # The repository-wide contract says nothing about the leaf's
                # own narrowing — that is the whole point of a bounded leaf.
                SPEC: _issue(SPEC, body="The settled gate contract."),
            }
        )

        contracts, data_path = _stage(tmp_path, host, _entry())

        (contract,) = contracts.entries
        staged = (data_path / contract.sources_dir / f"issue-{LEAF}" / "body.md").read_text()
        assert LEAF_ONLY_STOP in staged
        governing = (
            data_path / contract.sources_dir / f"issue-{SPEC}" / "body.md"
        ).read_text()
        assert LEAF_ONLY_STOP not in governing

    def test_without_the_leaf_the_constraint_is_nowhere_in_the_bundle(
        self, tmp_path: Path
    ) -> None:
        """The mutation: stage the governing Spec only, and the leaf's own
        narrowing is unreachable — which is exactly the state PR #346 shipped
        in, and exactly what a run would have to invent.
        """
        host = _Host(
            issues={SPEC: _issue(SPEC, body="The settled gate contract.")},
            errors={LEAF: RuntimeError("issue not found")},
        )

        contracts, data_path = _stage(tmp_path, host, _entry())

        (contract,) = contracts.entries
        assert contract.gap
        staged_text = "".join(
            path.read_text()
            for path in (data_path / TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME).rglob("*.md")
        )
        assert LEAF_ONLY_STOP not in staged_text
        assert contract.establishes_leaf_contract is False


class TestFailClosed:
    """An unresolved contract cannot yield a merge-facing PASS."""

    @pytest.mark.parametrize(
        ("entry", "host", "expected"),
        [
            pytest.param(
                _entry(branch="chore/no-issue-here"),
                _Host(issues={}),
                "names no issue",
                id="no issue association",
            ),
            pytest.param(
                _entry(),
                _Host(issues={}, errors={LEAF: RuntimeError("boom")}),
                "could not be staged",
                id="unreadable leaf",
            ),
            pytest.param(
                _entry(),
                _Host(
                    issues={LEAF: _issue(LEAF, body=_leaf_body())},
                    errors={SPEC: RuntimeError("boom")},
                ),
                "could not resolve",
                id="unreadable required governing source",
            ),
            pytest.param(
                _entry(),
                _Host(
                    issues={
                        LEAF: _issue(LEAF, body="Governed-by: acme/other#5\n")
                    }
                ),
                "could not resolve",
                id="malformed governing declaration",
            ),
        ],
    )
    def test_an_unresolved_contract_is_a_gap(
        self, tmp_path: Path, entry: PRToReview, host: _Host, expected: str
    ) -> None:
        contracts, _ = _stage(tmp_path, host, entry)

        (contract,) = contracts.entries
        assert contract.establishes_leaf_contract is False
        assert expected in contract.gap
        assert entry.contract_established is False
        assert contracts.contracted_pr_numbers() == frozenset()

    def test_a_gap_makes_pass_structurally_unreachable(
        self, tmp_path: Path
    ) -> None:
        """The gap is carried into the launch authority, out of the agent's reach."""
        host = _Host(issues={}, errors={LEAF: RuntimeError("boom")})
        entry = _entry()

        _stage(tmp_path, host, entry)
        authority = _authority(contracted=True, entries=(entry,))

        candidate = entry.candidate()
        assert authority.unmet_pass_prerequisites(candidate) == (
            CandidatePassPrerequisite.LEAF_CONTRACT,
        )

    def test_an_optional_source_that_cannot_be_read_is_not_a_gap(
        self, tmp_path: Path
    ) -> None:
        """The leaf itself declared it as not load-bearing, and said so."""
        host = _Host(
            issues={
                LEAF: _issue(
                    LEAF,
                    body=f"Governed-by: #{SPEC}\nGoverned-by-optional: #{POLICY}\n",
                ),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            },
            errors={POLICY: RuntimeError("boom")},
        )
        entry = _entry()

        contracts, _ = _stage(tmp_path, host, entry)

        (contract,) = contracts.entries
        assert contract.establishes_leaf_contract is True
        assert entry.contract_established is True
        absent = contract.sources[2]
        assert absent.issue_number == POLICY
        assert absent.staged is False
        assert "boom" in absent.absent_reason

    def test_one_unresolved_candidate_does_not_refuse_its_siblings(
        self, tmp_path: Path
    ) -> None:
        host = _Host(
            issues={LEAF: _issue(LEAF, body="No governing sources declared.\n")},
            errors={POLICY: RuntimeError("boom")},
        )
        good = _entry(346, branch=f"{LEAF}-r28", head_sha=CANDIDATE_SHA)
        bad = _entry(347, branch=f"{POLICY}-x", head_sha=OTHER_SHA)

        contracts, _ = _stage(tmp_path, host, good, bad)

        assert contracts.contracted_pr_numbers() == frozenset({346})
        assert good.contract_established is True
        assert bad.contract_established is False

    def test_a_partial_bundle_is_not_left_attributed_to_nobody(
        self, tmp_path: Path
    ) -> None:
        """A half-written bundle is text the descriptor names no owner for."""
        host = _Host(
            issues={LEAF: _issue(LEAF, body=_leaf_body())},
            errors={SPEC: RuntimeError("boom")},
        )
        entry = _entry()

        _, data_path = _stage(tmp_path, host, entry)

        root = data_path / TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME
        assert list(root.rglob("*.md")) == []


class TestStatelessReconstruction:
    """A fresh run rebuilds the same contract from canonical sources."""

    def test_a_second_run_reconstructs_the_same_bundle(
        self, tmp_path: Path
    ) -> None:
        def host() -> _Host:
            return _Host(
                issues={
                    LEAF: _issue(LEAF, body=_leaf_body()),
                    SPEC: _issue(SPEC, body="The settled gate contract."),
                }
            )

        first = build_candidate_contracts(
            [_entry()], repository_host=host(), data_path=tmp_path / "run-1"
        )
        second = build_candidate_contracts(
            [_entry()], repository_host=host(), data_path=tmp_path / "run-2"
        )

        assert [source.body_sha256 for source in first.entries[0].sources] == [
            source.body_sha256 for source in second.entries[0].sources
        ]
        assert first.entries[0].issue_number == second.entries[0].issue_number == LEAF

    def test_an_edited_leaf_yields_a_different_bundle(self, tmp_path: Path) -> None:
        """Amending the issue changes what the next run is judged against."""
        before = _Host(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body(stop="STOP: A-C only.")),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            }
        )
        after = _Host(
            issues={
                LEAF: _issue(
                    LEAF,
                    body=_leaf_body(stop="STOP: A-C and F5."),
                    updated_at="2026-08-29T09:00:00Z",
                ),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            }
        )

        first = build_candidate_contracts(
            [_entry()], repository_host=before, data_path=tmp_path / "run-1"
        )
        second = build_candidate_contracts(
            [_entry()], repository_host=after, data_path=tmp_path / "run-2"
        )

        assert (
            first.entries[0].sources[0].body_sha256
            != second.entries[0].sources[0].body_sha256
        )
        assert (
            first.entries[0].sources[0].updated_at
            != second.entries[0].sources[0].updated_at
        )


class TestTheLaunchPathCarriesTheAnswer:
    """The staged answer reaches the record completion actually reads.

    Staging the bundle is half the contract; the other half is that the
    orchestrator's own answer about it lands on ``TechLeadLaunchAuthority``
    before the session spawns, where the agent cannot reach it. Driven through
    ``prepare_tech_lead_session_data`` — the one owner that stages a tech_lead
    session's inputs — rather than by constructing the record in the test.
    """

    @staticmethod
    def _config(tmp_path: Path):
        from issue_orchestrator.infra.config import Config

        config = Config(repo="test/repo")
        config.tech_lead_review_agent = "agent:tech-lead"
        config.repo_root = tmp_path / "repo"
        config.repo_root.mkdir(parents=True, exist_ok=True)
        return config

    class _BatchHost(_Host):
        """A host that also answers the manifest builder's PR query."""

        def __init__(self, issues, prs) -> None:
            super().__init__(issues)
            self._prs = prs

        def get_prs_with_label(self, label: str, state: str = "all"):
            return list(self._prs)

    def _prepare(self, tmp_path: Path, host):
        from types import SimpleNamespace as NS

        from issue_orchestrator.control.tech_lead_session_policy import (
            prepare_tech_lead_session_data,
        )
        from issue_orchestrator.domain.board_snapshot import BoardSnapshot
        from issue_orchestrator.ports.tech_lead_authority import (
            InMemoryTechLeadAuthorityStore,
        )

        worktree = tmp_path / "worktree"
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "run"
        run_dir.mkdir(parents=True)
        store = InMemoryTechLeadAuthorityStore()
        prepare_tech_lead_session_data(
            config=self._config(tmp_path),
            repository_host=host,
            manifest_downloader=NS(download=lambda manifest, path: manifest),
            tech_lead_authority=store,
            board_snapshot_provider=NS(
                snapshot=lambda focus, problems=(): BoardSnapshot(
                    generated_at="2026-08-29T00:00:00Z", orchestrator_paused=False
                )
            ),
            working_copy=NS(get_head_sha=lambda worktree: "e" * 40),
            planning_command_guard=NS(),
            issue=NS(
                number=7,
                title="Tech lead batch",
                agent_type="agent:tech-lead",
                labels=[],
            ),
            ctx=NS(
                run=NS(run_dir=run_dir, run_id="run-1", session_name="issue-7"),
                worktree_path=worktree,
                update_manifest=lambda entries: None,
            ),
            tech_lead_scope=None,
        )
        return store.load(run_id="run-1", session_name="issue-7"), worktree

    @staticmethod
    def _pr(number: int, branch: str, head_sha: str):
        from types import SimpleNamespace as NS

        return NS(
            number=number,
            title="candidate",
            url=f"https://example.invalid/pull/{number}",
            branch=branch,
            head_sha=head_sha,
            labels=["tech-lead-review"],
        )

    def test_a_resolved_contract_reaches_the_launch_authority(
        self, tmp_path: Path
    ) -> None:
        host = self._BatchHost(
            issues={
                LEAF: _issue(LEAF, body=_leaf_body()),
                SPEC: _issue(SPEC, body="The settled gate contract."),
            },
            prs=[self._pr(346, f"{LEAF}-r28", CANDIDATE_SHA)],
        )

        authority, worktree = self._prepare(tmp_path, host)

        assert authority is not None
        candidate = authority.manifest_candidates[0]
        # The staged bundle is in the run directory the agent reads.
        staged = list(worktree.rglob(TECH_LEAD_CANDIDATE_CONTRACT_FILENAME))
        assert len(staged) == 1
        payload = json.loads(staged[0].read_text())
        assert payload["candidates"][0]["issue_number"] == LEAF
        # The reviewer prerequisite is a separate axis and is NOT established
        # here, so the run still cannot pass this candidate.
        assert authority.unmet_pass_prerequisites(candidate) == (
            CandidatePassPrerequisite.INDEPENDENT_REVIEW,
        )

    def test_an_unresolved_contract_reaches_it_as_a_refusal(
        self, tmp_path: Path
    ) -> None:
        """The mutation: drop the leaf staging and `pass` becomes unreachable."""
        host = self._BatchHost(
            issues={SPEC: _issue(SPEC, body="The settled gate contract.")},
            prs=[self._pr(346, f"{LEAF}-r28", CANDIDATE_SHA)],
        )

        authority, _ = self._prepare(tmp_path, host)

        assert authority is not None
        candidate = authority.manifest_candidates[0]
        assert (
            CandidatePassPrerequisite.LEAF_CONTRACT
            in authority.unmet_pass_prerequisites(candidate)
        )
