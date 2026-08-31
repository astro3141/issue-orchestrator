"""Staging and reading the exchange's admitted leaf contract (#399).

Every way the evidence can go wrong is a way the exchange must refuse to
proceed, so the negative cases are the point of this file. They are
driven through the same reader the round loop uses — a stub that could
not fail the way the production reader fails would prove nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.review_exchange_contract import (
    AdmittedLeafContract,
    LeafContractUnavailable,
)
from issue_orchestrator.domain.review_exchange_run import ReviewExchangeRunAssets
from issue_orchestrator.execution.review_exchange_leaf_contract import (
    IssueTrackerLeafContractStaging,
    load_staged_leaf_contract,
    stage_leaf_contract,
    verify_staged_leaf_contract,
)
from issue_orchestrator.ports.review_exchange_leaf_contract import (
    UNSTAGEABLE_ADMITTED_LEAF_CONTRACT,
    UnstageableAdmittedLeafContract,
)

BODY = "## Admitted mutation\n\nChange exactly `src/only_this_file.py`.\n"


class _Issues:
    """The one tracker method contract staging asks for."""

    def __init__(self, issue: Issue | None = None, *, raises: bool = False) -> None:
        self._issue = issue
        self._raises = raises
        self.calls: list[int] = []

    def get_issue(self, issue_number: int) -> Issue | None:
        self.calls.append(issue_number)
        if self._raises:
            raise RuntimeError("repository host is unreachable")
        return self._issue


def _assets(tmp_path: Path) -> ReviewExchangeRunAssets:
    return ReviewExchangeRunAssets.from_run_dir(tmp_path / "run")


def _issue(body: str | None = BODY) -> Issue:
    return Issue(number=399, title="Carry the contract", labels=[], body=body)


class TestStagingFromTheIssueTracker:
    def test_it_writes_the_exact_issue_body_and_its_attribution(
        self, tmp_path: Path
    ) -> None:
        assets = _assets(tmp_path)
        issues = _Issues(_issue())

        staged = IssueTrackerLeafContractStaging(issues).stage(  # type: ignore[arg-type]
            issue_number=399,
            assets=assets,
        )

        assert assets.leaf_contract_path.read_text(encoding="utf-8") == BODY
        manifest = json.loads(assets.leaf_contract_manifest_path.read_text())
        assert manifest["digest"] == staged.digest
        assert manifest["issue_number"] == 399
        assert staged.path == assets.leaf_contract_path

    def test_it_reads_the_issue_once_per_exchange(self, tmp_path: Path) -> None:
        # GitHub calls are a limited resource, and the artifact exists so
        # that neither role has to refetch: one read, frozen as bytes.
        issues = _Issues(_issue())

        IssueTrackerLeafContractStaging(issues).stage(  # type: ignore[arg-type]
            issue_number=399,
            assets=_assets(tmp_path),
        )

        assert issues.calls == [399]

    def test_a_missing_issue_refuses_rather_than_stages_a_placeholder(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(LeafContractUnavailable, match="not found"):
            IssueTrackerLeafContractStaging(_Issues(None)).stage(  # type: ignore[arg-type]
                issue_number=399,
                assets=_assets(tmp_path),
            )

    def test_an_empty_body_is_not_replaced_by_the_title(self, tmp_path: Path) -> None:
        assets = _assets(tmp_path)

        with pytest.raises(LeafContractUnavailable, match="no usable admitted"):
            IssueTrackerLeafContractStaging(_Issues(_issue(body=None))).stage(  # type: ignore[arg-type]
                issue_number=399,
                assets=assets,
            )

        assert not assets.leaf_contract_path.exists()

    def test_an_unreachable_tracker_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(LeafContractUnavailable, match="could not read issue"):
            IssueTrackerLeafContractStaging(_Issues(raises=True)).stage(  # type: ignore[arg-type]
                issue_number=399,
                assets=_assets(tmp_path),
            )


class TestLoadingWhatWasStaged:
    def test_a_staged_contract_loads_back_identically(self, tmp_path: Path) -> None:
        assets = _assets(tmp_path)
        staged = stage_leaf_contract(
            AdmittedLeafContract(issue_number=399, issue_title="t", body=BODY),
            assets=assets,
        )

        assert load_staged_leaf_contract(assets) == staged
        assert verify_staged_leaf_contract(staged) == BODY

    def test_a_missing_contract_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(LeafContractUnavailable, match="unreadable"):
            load_staged_leaf_contract(_assets(tmp_path))

    def test_a_manifest_that_is_not_json_refuses(self, tmp_path: Path) -> None:
        assets = _assets(tmp_path)
        assets.exchange_dir.mkdir(parents=True, exist_ok=True)
        assets.leaf_contract_manifest_path.write_text("{oops", encoding="utf-8")

        with pytest.raises(LeafContractUnavailable, match="not valid JSON"):
            load_staged_leaf_contract(assets)

    def test_contract_bytes_edited_after_staging_refuse(self, tmp_path: Path) -> None:
        # The freshness half of R4: an exchange that read one scope and
        # then had another substituted under it must not keep going.
        assets = _assets(tmp_path)
        staged = stage_leaf_contract(
            AdmittedLeafContract(issue_number=399, issue_title="t", body=BODY),
            assets=assets,
        )
        assets.leaf_contract_path.write_text(BODY + "…and file B.\n", encoding="utf-8")

        with pytest.raises(LeafContractUnavailable, match="recorded digest"):
            verify_staged_leaf_contract(staged)
        with pytest.raises(LeafContractUnavailable, match="recorded digest"):
            load_staged_leaf_contract(assets)

    def test_a_deleted_contract_file_refuses_even_with_its_manifest(
        self, tmp_path: Path
    ) -> None:
        assets = _assets(tmp_path)
        stage_leaf_contract(
            AdmittedLeafContract(issue_number=399, issue_title="t", body=BODY),
            assets=assets,
        )
        assets.leaf_contract_path.unlink()

        with pytest.raises(LeafContractUnavailable, match="unreadable"):
            load_staged_leaf_contract(assets)


class TestTheRunAssetsOwnOneStablePath:
    def test_both_contract_artifacts_live_under_the_exchange_evidence_dir(
        self, tmp_path: Path
    ) -> None:
        assets = _assets(tmp_path)

        assert assets.leaf_contract_path.parent == assets.exchange_dir
        assert assets.leaf_contract_manifest_path.parent == assets.exchange_dir
        assert assets.leaf_contract_path.name == "issue-contract.md"


class TestTheUnwiredDefaultRefuses:
    """The port's default owner, which every unwired deployment gets.

    ``UNSTAGEABLE_ADMITTED_LEAF_CONTRACT`` is what makes the staging
    argument safe to default: a root that forgot to wire the real one
    loses the exchange instead of running a Reviewer with no admitted
    scope. That is a code path like any other, and an untested
    fail-closed guarantee is an unproven one.
    """

    def test_it_stages_nothing_and_names_the_issue_it_could_not_prove(
        self, tmp_path: Path
    ) -> None:
        assets = _assets(tmp_path)

        with pytest.raises(LeafContractUnavailable, match="#399") as excinfo:
            UNSTAGEABLE_ADMITTED_LEAF_CONTRACT.stage(
                issue_number=399,
                assets=assets,
            )

        assert "no admitted leaf contract staging is wired" in excinfo.value.reason
        assert not assets.leaf_contract_path.exists()
        assert not assets.leaf_contract_manifest_path.exists()

    def test_the_module_default_is_the_refusing_owner(self) -> None:
        # Not merely *a* refusing object: the exported singleton the
        # runner defaults to has to be this class, or the default the
        # composition roots rely on is something else.
        assert isinstance(
            UNSTAGEABLE_ADMITTED_LEAF_CONTRACT,
            UnstageableAdmittedLeafContract,
        )
