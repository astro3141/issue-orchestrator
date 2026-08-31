"""Staging and reading the exchange's admitted leaf contract artifact.

The I/O half of ``domain/review_exchange_contract.py``. Three jobs:

* :class:`IssueTrackerLeafContractStaging` implements the
  :class:`~..ports.review_exchange_leaf_contract.AdmittedLeafContractStaging`
  port over the issue tracker the orchestrator already uses. It reads the
  canonical issue once, before the exchange starts, and writes an exact
  snapshot into the run's evidence directory. Neither role refetches:
  the Reviewer reads the staged bytes, the Coder reads the staged bytes,
  and a reader of the persisted turn packets can prove they were the same
  ones.
* :func:`load_staged_leaf_contract` recovers the handle from that
  directory and refuses anything it cannot prove — file gone, sidecar
  malformed, digest disagreeing with the bytes beside it.
* :func:`verify_staged_leaf_contract` re-reads the bytes mid-exchange, so
  a contract that changes under a running exchange fails the round rather
  than silently reviewing round 3 against different scope than round 1.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..domain.review_exchange_contract import (
    AdmittedLeafContract,
    LeafContractUnavailable,
    StagedLeafContract,
    staged_leaf_contract_from_manifest_payload,
)
from ..domain.review_exchange_run import ReviewExchangeRunAssets
from ..ports.issue_tracker import IssueTracker


class IssueTrackerLeafContractStaging:
    """Stage the admitted leaf contract from the canonical issue tracker.

    The tracker is the same source the ordinary execution lane's contract
    comes from, so this introduces no second source of truth — it moves
    the read to the orchestrator, once, and freezes the answer as bytes.
    """

    def __init__(self, issues: IssueTracker) -> None:
        self._issues = issues

    def stage(
        self,
        *,
        issue_number: int,
        assets: ReviewExchangeRunAssets,
    ) -> StagedLeafContract:
        contract = self._read_contract(issue_number)
        return stage_leaf_contract(contract, assets=assets)

    def _read_contract(self, issue_number: int) -> AdmittedLeafContract:
        try:
            issue = self._issues.get_issue(issue_number)
        except Exception as exc:
            raise LeafContractUnavailable(
                f"could not read issue #{issue_number} for its admitted "
                f"leaf contract: {exc}"
            ) from exc
        if issue is None:
            raise LeafContractUnavailable(
                f"issue #{issue_number} was not found, so the exchange has no "
                "admitted leaf contract to review against"
            )
        body = issue.body or ""
        try:
            return AdmittedLeafContract(
                issue_number=issue_number,
                issue_title=issue.title,
                body=body,
            )
        except ValueError as exc:
            # Deliberately not falling back to the title: a contract that
            # says only what the work is called cannot say what mutation
            # was admitted, and reviewing against it is the widening this
            # artifact exists to prevent.
            raise LeafContractUnavailable(
                f"issue #{issue_number} has no usable admitted leaf "
                f"contract: {exc}"
            ) from exc


def stage_leaf_contract(
    contract: AdmittedLeafContract,
    *,
    assets: ReviewExchangeRunAssets,
) -> StagedLeafContract:
    """Write ``contract`` into the run's evidence directory."""
    assets.exchange_dir.mkdir(parents=True, exist_ok=True)
    try:
        assets.leaf_contract_path.write_text(contract.body, encoding="utf-8")
        assets.leaf_contract_manifest_path.write_text(
            json.dumps(
                contract.manifest_payload(contract_path=assets.leaf_contract_path),
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        raise LeafContractUnavailable(
            f"could not stage the admitted leaf contract for issue "
            f"#{contract.issue_number}: {exc}"
        ) from exc
    return contract.staged_at(assets.leaf_contract_path)


def load_staged_leaf_contract(
    assets: ReviewExchangeRunAssets,
) -> StagedLeafContract:
    """Recover the staged contract handle, or refuse the exchange."""
    manifest_payload = _read_json(assets.leaf_contract_manifest_path)
    staged = staged_leaf_contract_from_manifest_payload(
        manifest_payload,
        contract_path=assets.leaf_contract_path,
    )
    verify_staged_leaf_contract(staged)
    return staged


def verify_staged_leaf_contract(staged: StagedLeafContract) -> str:
    """Return the staged bytes, proving they are the attested ones."""
    try:
        body = staged.path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LeafContractUnavailable(
            f"staged leaf contract {staged.path} is unreadable: {exc}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise LeafContractUnavailable(
            f"staged leaf contract {staged.path} is not valid UTF-8: {exc}"
        ) from exc
    if not staged.matches(body):
        raise LeafContractUnavailable(
            f"staged leaf contract {staged.path} no longer matches its "
            f"recorded digest {staged.digest}"
        )
    return body


def _read_json(path: Path) -> object:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LeafContractUnavailable(
            f"staged leaf contract manifest {path} is unreadable: {exc}"
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LeafContractUnavailable(
            f"staged leaf contract manifest {path} is not valid JSON: {exc}"
        ) from exc
