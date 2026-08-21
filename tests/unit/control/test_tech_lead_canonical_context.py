"""Staging a planning run's canonical governing context (#183).

Covers the failure directions the leaf is measured on: exact provenance,
fail-closed on a required source, honest degradation of an optional one, no
authority effect, durable replay after the worktree is reaped, a re-run that
moves without rewriting history, no hardcoded bundle, every other flavor
unchanged, and no Human step between Control and the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from issue_orchestrator.control.tech_lead_canonical_context import (
    stage_canonical_context,
)
from issue_orchestrator.control.tech_lead_session_policy import (
    prepare_tech_lead_session_data,
)
from issue_orchestrator.domain.board_snapshot import (
    BOARD_SNAPSHOT_FILENAME,
    BoardSnapshot,
)
from issue_orchestrator.domain.canonical_context import (
    CANONICAL_CONTEXT_BODIES_DIRNAME,
    CANONICAL_CONTEXT_FILENAME,
    CanonicalSourceKind,
    content_digest,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.ports.tech_lead_authority import (
    InMemoryTechLeadAuthorityStore,
    TechLeadAuthorityConflictError,
)

SUBJECT = 183
PROCEDURE = 21
POLICY = 23


def _issue(
    number: int,
    *,
    body: str | None = "",
    title: str | None = None,
    state: str = "open",
    updated_at: str = "2026-08-20T09:00:00Z",
    agent_type: str = "agent:tech-lead",
) -> SimpleNamespace:
    return SimpleNamespace(
        number=number,
        title=title if title is not None else f"Issue {number}",
        body=body,
        state=state,
        updated_at=updated_at,
        labels=[agent_type],
        agent_type=agent_type,
    )


class _Host:
    """A repository host that answers only the two existing fetch owners."""

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
        self.comment_calls: list[int] = []

    def get_issue(self, issue_number: int) -> SimpleNamespace | None:
        self.issue_calls.append(issue_number)
        if issue_number in self._errors:
            raise self._errors[issue_number]
        return self._issues.get(issue_number)

    def get_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        self.comment_calls.append(issue_number)
        return self._comments.get(issue_number, [])

    # Only reached by the best-effort evidence map, never by this owner.
    def get_default_branch(self) -> str:
        return "main"

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list:
        return []


def _ctx(run_dir: Path, run_id: str = "run-1") -> tuple[SimpleNamespace, dict]:
    manifest: dict = {}
    return (
        SimpleNamespace(
            run=SimpleNamespace(
                run_dir=run_dir, run_id=run_id, session_name=f"issue-{SUBJECT}"
            ),
            worktree_path=run_dir.parent,
            update_manifest=manifest.update,
        ),
        manifest,
    )


def _stage(
    run_dir: Path,
    host: _Host,
    *,
    subject: SimpleNamespace,
    store: InMemoryTechLeadAuthorityStore | None = None,
    flavor: TechLeadSessionFlavor = TechLeadSessionFlavor.PLANNING_INVESTIGATION,
    run_id: str = "run-1",
):
    ctx, manifest = _ctx(run_dir, run_id=run_id)
    snapshot = stage_canonical_context(
        repository_host=host,
        tech_lead_authority=store or InMemoryTechLeadAuthorityStore(),
        ctx=ctx,
        run_dir=run_dir,
        flavor=flavor,
        subject_issue=subject,
    )
    return snapshot, manifest


class TestExactProvenance:
    """Direction 1: name every staged source by number, revision and digest."""

    def test_descriptor_names_each_source_and_its_staged_bytes(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(SUBJECT, body=f"Plan this.\nGoverned-by: #{PROCEDURE}\n")
        host = _Host(
            issues={
                SUBJECT: subject,
                PROCEDURE: _issue(
                    PROCEDURE,
                    body="The working procedure.",
                    title="Working procedure",
                    updated_at="2026-08-19T08:00:00Z",
                ),
            },
            comments={
                PROCEDURE: [
                    {
                        "id": 5365348920,
                        "updated_at": "2026-08-19T09:00:00Z",
                        "body": "A clarification.",
                    }
                ]
            },
        )
        run_dir = tmp_path / "run"

        snapshot, manifest = _stage(run_dir, host, subject=subject)

        assert snapshot is not None
        subject_source, governing = snapshot.sources
        assert subject_source.kind is CanonicalSourceKind.SUBJECT
        assert subject_source.issue_number == SUBJECT
        assert governing.kind is CanonicalSourceKind.GOVERNING
        assert governing.issue_number == PROCEDURE
        assert governing.required is True
        assert governing.title == "Working procedure"
        assert governing.state == "open"
        # Revision identity comes from the tracker, fetch time from our clock.
        assert governing.updated_at == "2026-08-19T08:00:00Z"
        assert governing.fetched_at
        # Digests attribute exactly the bytes on disk.
        bodies = run_dir / "tech-lead-data" / CANONICAL_CONTEXT_BODIES_DIRNAME
        body_file = bodies / f"issue-{PROCEDURE}" / "body.md"
        assert body_file.read_text() == "The working procedure."
        assert governing.body_sha256 == content_digest(body_file.read_text())
        (comment,) = governing.comments
        comment_file = bodies / f"issue-{PROCEDURE}" / f"comment-{comment.comment_id}.md"
        assert comment.comment_id == 5365348920
        assert comment.updated_at == "2026-08-19T09:00:00Z"
        assert comment.sha256 == content_digest(comment_file.read_text())
        # The descriptor is on disk and recorded in the run manifest.
        descriptor_path = run_dir / "tech-lead-data" / CANONICAL_CONTEXT_FILENAME
        assert manifest["canonical_context"] == str(descriptor_path)
        assert json.loads(descriptor_path.read_text())["subject_issue_number"] == SUBJECT

    def test_an_edited_body_yields_a_different_descriptor(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(SUBJECT, body=f"Governed-by: #{PROCEDURE}\n")
        first_host = _Host(
            issues={
                SUBJECT: subject,
                PROCEDURE: _issue(PROCEDURE, body="v1", updated_at="2026-08-19T08:00:00Z"),
            }
        )
        second_host = _Host(
            issues={
                SUBJECT: subject,
                PROCEDURE: _issue(PROCEDURE, body="v2", updated_at="2026-08-20T08:00:00Z"),
            }
        )

        first, _ = _stage(tmp_path / "run-1", first_host, subject=subject)
        second, _ = _stage(
            tmp_path / "run-2", second_host, subject=subject, run_id="run-2"
        )

        assert first is not None and second is not None
        assert first.sources[1].body_sha256 != second.sources[1].body_sha256
        assert first.sources[1].updated_at != second.sources[1].updated_at


class TestFailClosedOnRequired:
    """Direction 2: a required source that cannot be staged kills the launch."""

    def test_unfetchable_required_source_raises_and_records_nothing(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(SUBJECT, body=f"Governed-by: #{PROCEDURE}\n")
        host = _Host(
            issues={SUBJECT: subject},
            errors={PROCEDURE: RuntimeError("github down")},
        )
        store = InMemoryTechLeadAuthorityStore()
        run_dir = tmp_path / "run"

        with pytest.raises(ValueError, match=f"required canonical source #{PROCEDURE}"):
            _stage(run_dir, host, subject=subject, store=store)

        assert not (run_dir / "tech-lead-data" / CANONICAL_CONTEXT_FILENAME).exists()
        assert (
            store.load_canonical_context(run_id="run-1", session_name=f"issue-{SUBJECT}")
            is None
        )

    def test_a_missing_required_source_is_not_silently_absent(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(SUBJECT, body=f"Governed-by: #{PROCEDURE}\n")
        host = _Host(issues={SUBJECT: subject})  # #21 resolves to None

        with pytest.raises(ValueError, match="was not found"):
            _stage(tmp_path / "run", host, subject=subject)

    def test_an_unfetchable_subject_fails_closed_too(self, tmp_path: Path) -> None:
        subject = _issue(SUBJECT, body="")
        host = _Host(issues={}, errors={SUBJECT: RuntimeError("github down")})

        with pytest.raises(ValueError, match=f"required canonical source #{SUBJECT}"):
            _stage(tmp_path / "run", host, subject=subject)


class TestOptionalDegradesHonestly:
    """Direction 3: absent is recorded; absent != never requested."""

    def test_unfetchable_optional_source_is_recorded_as_absent(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(SUBJECT, body=f"Governed-by-optional: #{POLICY}\n")
        host = _Host(
            issues={SUBJECT: subject}, errors={POLICY: RuntimeError("github down")}
        )
        run_dir = tmp_path / "run"

        snapshot, manifest = _stage(run_dir, host, subject=subject)

        assert snapshot is not None
        recorded = snapshot.source(POLICY)
        assert recorded is not None
        assert recorded.staged is False
        assert "github down" in recorded.absent_reason
        assert recorded.body_sha256 == ""
        # Never requested is a different answer, in the file as in the object.
        assert snapshot.source(PROCEDURE) is None
        payload = json.loads(
            (run_dir / "tech-lead-data" / CANONICAL_CONTEXT_FILENAME).read_text()
        )
        assert [entry["issue_number"] for entry in payload["sources"]] == [
            SUBJECT,
            POLICY,
        ]
        assert payload["sources"][1]["staged"] is False
        # Degradation is honest, not silent: the run still launches.
        assert manifest["canonical_context"]


class TestNoHardcodedBundle:
    """Direction 7: only what the subject declares is staged."""

    def test_a_subject_declaring_nothing_is_staged_alone(self, tmp_path: Path) -> None:
        subject = _issue(SUBJECT, body="No declarations here, only prose.")
        host = _Host(issues={SUBJECT: subject})

        snapshot, _ = _stage(tmp_path / "run", host, subject=subject)

        assert snapshot is not None
        assert [source.issue_number for source in snapshot.sources] == [SUBJECT]
        assert host.issue_calls == [SUBJECT]
        assert snapshot.source(PROCEDURE) is None
        assert snapshot.source(POLICY) is None

    def test_declared_sources_are_staged_in_declaration_order(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(
            SUBJECT,
            body=f"Governed-by: #{POLICY}\nGoverned-by-optional: #{PROCEDURE}\n",
        )
        host = _Host(
            issues={
                SUBJECT: subject,
                POLICY: _issue(POLICY, body="policy"),
                PROCEDURE: _issue(PROCEDURE, body="procedure"),
            }
        )

        snapshot, _ = _stage(tmp_path / "run", host, subject=subject)

        assert snapshot is not None
        assert [source.issue_number for source in snapshot.sources] == [
            SUBJECT,
            POLICY,
            PROCEDURE,
        ]
        assert [source.required for source in snapshot.sources] == [True, True, False]

    def test_a_malformed_declaration_fails_the_launch(self, tmp_path: Path) -> None:
        subject = _issue(SUBJECT, body="Governed-by: elsewhere\n")
        host = _Host(issues={SUBJECT: subject})
        store = InMemoryTechLeadAuthorityStore()
        run_dir = tmp_path / "run"

        with pytest.raises(ValueError, match="same-repo"):
            _stage(run_dir, host, subject=subject, store=store)

        # A source that cannot even be named is a defect in the subject, so it
        # fails closed like an unfetchable required source: no descriptor, no
        # durable row, no launch.
        assert not (run_dir / "tech-lead-data" / CANONICAL_CONTEXT_FILENAME).exists()
        assert (
            store.load_canonical_context(run_id="run-1", session_name=f"issue-{SUBJECT}")
            is None
        )

    def test_the_declaration_is_read_from_the_staged_revision(
        self, tmp_path: Path
    ) -> None:
        """The in-hand snapshot may be stale, or carry no body at all.

        Reading the declaration from the body this owner just staged keeps
        "what governs the run" and "what the run was handed" one revision.
        """
        stale = _issue(SUBJECT, body=None)
        host = _Host(
            issues={
                SUBJECT: _issue(SUBJECT, body=f"Governed-by: #{PROCEDURE}\n"),
                PROCEDURE: _issue(PROCEDURE, body="procedure"),
            }
        )

        snapshot, _ = _stage(tmp_path / "run", host, subject=stale)

        assert snapshot is not None
        assert [source.issue_number for source in snapshot.sources] == [
            SUBJECT,
            PROCEDURE,
        ]


class TestDurableReplayAndRerun:
    """Directions 5 and 6: provenance outlives the run and never rewrites."""

    @staticmethod
    def _subject() -> SimpleNamespace:
        return _issue(SUBJECT, body=f"Governed-by: #{PROCEDURE}\n")

    def test_descriptor_answers_after_the_worktree_is_reaped(
        self, tmp_path: Path
    ) -> None:
        import shutil

        subject = self._subject()
        host = _Host(
            issues={SUBJECT: subject, PROCEDURE: _issue(PROCEDURE, body="procedure")}
        )
        store = InMemoryTechLeadAuthorityStore()
        run_dir = tmp_path / "worktree" / "run"

        snapshot, _ = _stage(run_dir, host, subject=subject, store=store)

        shutil.rmtree(tmp_path / "worktree")
        assert not run_dir.exists()
        replayed = store.load_canonical_context(
            run_id="run-1", session_name=f"issue-{SUBJECT}"
        )
        assert replayed == snapshot
        assert replayed is not None
        assert [source.issue_number for source in replayed.sources] == [
            SUBJECT,
            PROCEDURE,
        ]

    def test_a_new_run_moves_without_rewriting_the_original(
        self, tmp_path: Path
    ) -> None:
        subject = self._subject()
        store = InMemoryTechLeadAuthorityStore()
        first_host = _Host(
            issues={
                SUBJECT: subject,
                PROCEDURE: _issue(PROCEDURE, body="v1", updated_at="2026-08-19T08:00:00Z"),
            }
        )
        second_host = _Host(
            issues={
                SUBJECT: subject,
                PROCEDURE: _issue(PROCEDURE, body="v2", updated_at="2026-08-20T08:00:00Z"),
            }
        )

        first, _ = _stage(tmp_path / "run-1", first_host, subject=subject, store=store)
        second, _ = _stage(
            tmp_path / "run-2",
            second_host,
            subject=subject,
            store=store,
            run_id="run-2",
        )

        original = store.load_canonical_context(
            run_id="run-1", session_name=f"issue-{SUBJECT}"
        )
        assert original == first
        assert original != second
        assert store.load_canonical_context(
            run_id="run-2", session_name=f"issue-{SUBJECT}"
        ) == second

    def test_a_run_never_rewrites_its_own_recorded_context(
        self, tmp_path: Path
    ) -> None:
        subject = self._subject()
        store = InMemoryTechLeadAuthorityStore()
        _stage(
            tmp_path / "run-1",
            _Host(
                issues={SUBJECT: subject, PROCEDURE: _issue(PROCEDURE, body="v1")}
            ),
            subject=subject,
            store=store,
        )

        with pytest.raises(TechLeadAuthorityConflictError):
            _stage(
                tmp_path / "run-1-again",
                _Host(
                    issues={SUBJECT: subject, PROCEDURE: _issue(PROCEDURE, body="v2")}
                ),
                subject=subject,
                store=store,
            )


class TestOtherFlavorsUnchanged:
    """Direction 8: only a planning run stages canonical context."""

    @pytest.mark.parametrize(
        "flavor",
        [
            TechLeadSessionFlavor.BATCH_REVIEW,
            TechLeadSessionFlavor.FAILURE_INVESTIGATION,
            TechLeadSessionFlavor.HEALTH_REVIEW,
        ],
    )
    def test_no_fetch_no_file_no_row(
        self, tmp_path: Path, flavor: TechLeadSessionFlavor
    ) -> None:
        subject = _issue(SUBJECT, body=f"Governed-by: #{PROCEDURE}\n")
        host = _Host(
            issues={SUBJECT: subject, PROCEDURE: _issue(PROCEDURE, body="procedure")}
        )
        store = InMemoryTechLeadAuthorityStore()
        run_dir = tmp_path / "run"

        snapshot, manifest = _stage(
            run_dir, host, subject=subject, store=store, flavor=flavor
        )

        assert snapshot is None
        assert host.issue_calls == [] and host.comment_calls == []
        assert not (run_dir / "tech-lead-data").exists()
        assert manifest == {}
        assert (
            store.load_canonical_context(run_id="run-1", session_name=f"issue-{SUBJECT}")
            is None
        )


class TestOrdinaryLaneThroughThePolicyOwner:
    """Directions 4 and 9, on the real staging path.

    ``prepare_tech_lead_session_data`` is the one owner that stages a tech_lead
    session's inputs. Driving it proves the bundle arrives with no Human step
    between Control and the run, and that nothing about the run's AUTHORITY
    moved because of it.
    """

    @staticmethod
    def _config(tmp_path: Path):
        from issue_orchestrator.infra.config import Config

        config = Config(repo="test/repo")
        config.tech_lead_review_agent = "agent:tech-lead"
        config.repo_root = tmp_path / "repo"
        config.repo_root.mkdir(parents=True, exist_ok=True)
        return config

    @staticmethod
    def _board_provider():
        class _Provider:
            def __init__(self) -> None:
                self.calls: list = []

            def snapshot(self, focus_issue_number, problem_issue_numbers=()):
                self.calls.append((focus_issue_number, tuple(problem_issue_numbers)))
                return BoardSnapshot(
                    generated_at="2026-08-21T00:00:00Z", orchestrator_paused=False
                )

        return _Provider()

    @staticmethod
    def _manifest_downloader():
        class _Downloader:
            def __init__(self) -> None:
                self.calls: list = []

            def download(self, manifest, worktree_path):
                self.calls.append((manifest, worktree_path))
                return manifest

        return _Downloader()

    def _prepare(self, tmp_path: Path, subject, host, store):
        run_dir = tmp_path / "worktree" / ".issue-orchestrator" / "sessions" / "run"
        run_dir.mkdir(parents=True)
        ctx, manifest = _ctx(run_dir)
        prepare_tech_lead_session_data(
            config=self._config(tmp_path),
            repository_host=host,
            manifest_downloader=self._manifest_downloader(),
            tech_lead_authority=store,
            board_snapshot_provider=self._board_provider(),
            issue=subject,
            ctx=ctx,
            tech_lead_scope=TechLeadLaunchScope(
                flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION
            ),
        )
        return run_dir, manifest

    def test_the_bundle_arrives_with_no_human_between_control_and_the_run(
        self, tmp_path: Path
    ) -> None:
        subject = _issue(
            SUBJECT,
            body=f"Governed-by: #{PROCEDURE}\nGoverned-by-optional: #{POLICY}\n",
        )
        host = _Host(
            issues={
                SUBJECT: subject,
                PROCEDURE: _issue(PROCEDURE, body="procedure"),
                POLICY: _issue(POLICY, body="policy"),
            }
        )
        store = InMemoryTechLeadAuthorityStore()

        run_dir, manifest = self._prepare(tmp_path, subject, host, store)

        tech_lead_data = run_dir / "tech-lead-data"
        assert (tech_lead_data / BOARD_SNAPSHOT_FILENAME).is_file()
        descriptor = tech_lead_data / CANONICAL_CONTEXT_FILENAME
        assert descriptor.is_file()
        assert manifest["canonical_context"] == str(descriptor)
        bodies = tech_lead_data / CANONICAL_CONTEXT_BODIES_DIRNAME
        assert (bodies / f"issue-{PROCEDURE}" / "body.md").read_text() == "procedure"
        assert (bodies / f"issue-{POLICY}" / "body.md").read_text() == "policy"
        # The run can name what governed it, straight from the run directory.
        payload = json.loads(descriptor.read_text())
        assert [entry["issue_number"] for entry in payload["sources"]] == [
            SUBJECT,
            PROCEDURE,
            POLICY,
        ]

    def test_staging_changes_no_authority_answer(self, tmp_path: Path) -> None:
        # Direction 4: the same run, with and without declared sources, records
        # exactly the same launch authority - same capability answers, same
        # act-level target set, and no label is touched either way.
        plain = _issue(SUBJECT, body="No declarations.")
        declaring = _issue(
            SUBJECT, body=f"Governed-by: #{PROCEDURE}\nGoverned-by-optional: #{POLICY}\n"
        )
        hosts = {
            "plain": _Host(issues={SUBJECT: plain}),
            "declaring": _Host(
                issues={
                    SUBJECT: declaring,
                    PROCEDURE: _issue(PROCEDURE, body="procedure"),
                    POLICY: _issue(POLICY, body="policy"),
                }
            ),
        }
        recorded = {}
        for name, subject in (("plain", plain), ("declaring", declaring)):
            store = InMemoryTechLeadAuthorityStore()
            self._prepare(tmp_path / name, subject, hosts[name], store)
            recorded[name] = store.load(
                run_id="run-1", session_name=f"issue-{SUBJECT}"
            )

        assert recorded["plain"] == recorded["declaring"]
        authority = recorded["declaring"]
        assert authority is not None
        assert authority.flavor is TechLeadSessionFlavor.PLANNING_INVESTIGATION
        assert authority.allowed_targets() == frozenset({SUBJECT})
        # A planning run owns no act-level target, declarations or not.
        assert authority.allowed_act_level_targets() == frozenset()
        # Staging reached only the two READ owners. The fake implements no
        # label or comment write at all, so any attempt would have raised.
        assert hosts["declaring"].issue_calls[:3] == [SUBJECT, PROCEDURE, POLICY]
        assert hosts["declaring"].comment_calls == [SUBJECT, PROCEDURE, POLICY]
