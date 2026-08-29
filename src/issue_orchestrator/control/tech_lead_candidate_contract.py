"""Owner for staging each audited candidate's EXECUTABLE LEAF contract (#345).

The batch-review inputs already answered "what changed" (the candidate's diff)
and "who approved it" (``candidate-evidence.json``). They did not answer "what
was this candidate supposed to do", and the prompt then asked for a verdict
against a governing contract that appeared in none of the staged files. A
stateless Tech Lead run could only infer the leaf contract from PR prose,
repository context, or a previous session's memory — none of which is
authority.

This module closes that, per candidate and no wider:

1. **Which issue** the pull request implements is read from the branch, the way
   every other PR-to-issue association in this orchestrator is read — the same
   relationship :mod:`.tech_lead_candidate_evidence` uses to find the durable
   candidate record. Never from a label, the PR body, or model prose.
2. **What that issue says**, staged as bytes with a digest, so the run reads the
   current revision rather than remembering an old one.
3. **What that issue declares as governing** (``Governed-by:`` /
   ``Governed-by-optional:``) — and nothing else. This is deliberately not a
   project-wide document crawler: the leaf's own pointers are the boundary.

What it writes, beside ``manifest.json`` and ``candidate-evidence.json``::

    candidate-contracts.json                                 the descriptor
    candidate-contracts/pr-<n>-<sha>/issue-<m>/body.md       one body per source
    candidate-contracts/pr-<n>-<sha>/issue-<m>/comment-<id>.md

The failure direction is per CANDIDATE, and that is the one deliberate
difference from :mod:`.tech_lead_canonical_context`, which raises and kills the
launch. A batch is threshold-sized and shared: one pull request whose issue was
deleted must not destroy the audit of its siblings. So an unresolvable leaf is
recorded as a
:attr:`~..domain.tech_lead_candidate_contract.TechLeadCandidateContract.gap`,
the review still runs, and that candidate simply cannot be passed — the same
stance staged reviewer evidence takes, enforced at the same seam
(:class:`~..domain.tech_lead_candidate.CandidatePassPrerequisite`). Incomplete
context is not permissive context.

The fetch/write/digest mechanics are shared with the planning owner through
:mod:`.canonical_source_staging`, so "what a staged source is" cannot mean two
things on two paths.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from ..domain.branch_naming import extract_issue_number_from_branch
from ..domain.canonical_context import CanonicalSourceKind, parse_governing_sources
from ..domain.tech_lead_candidate_contract import (
    TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME,
    TECH_LEAD_CANDIDATE_CONTRACT_FILENAME,
    TechLeadCandidateContract,
    TechLeadCandidateContractSet,
    candidate_sources_dirname,
)
from .canonical_source_staging import stage_governing_sources, stage_issue_source

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..domain.tech_lead_manifest import PRToReview
    from ..ports import RepositoryHost

logger = logging.getLogger(__name__)


def _contract_for(
    entry: "PRToReview", *, repository_host: "RepositoryHost", bodies_root: Path
) -> TechLeadCandidateContract:
    """Stage ONE candidate's leaf contract, or say why it has none.

    Never raises: every failure becomes a gap on this candidate. The three
    directions are kept apart in the text because they are different facts —
    a pull request nobody can associate with an issue, an issue that cannot be
    read, and a leaf whose own declared governing source cannot be read are
    fixed by different people.
    """
    candidate = entry.candidate()
    issue_number = extract_issue_number_from_branch(entry.branch)
    if issue_number is None:
        return TechLeadCandidateContract(
            candidate=candidate,
            gap=(
                f"branch {entry.branch!r} names no issue, so the executable"
                " contract this pull request implements cannot be identified"
            ),
        )
    sources_dir = candidate_sources_dirname(candidate)
    bodies_dir = bodies_root / sources_dir

    def unresolved(gap: str) -> TechLeadCandidateContract:
        """Record the gap and drop the partial bundle it half-wrote.

        A staging run can fail after some bytes have landed, and a directory
        the descriptor attributes to nobody is exactly the text a run must not
        reason from. What is on disk stays exactly what the descriptor names.
        """
        shutil.rmtree(bodies_dir, ignore_errors=True)
        return TechLeadCandidateContract(candidate=candidate, gap=gap)

    try:
        leaf = stage_issue_source(
            repository_host=repository_host,
            bodies_dir=bodies_dir,
            issue_number=issue_number,
            kind=CanonicalSourceKind.SUBJECT,
            required=True,
        )
    except ValueError as exc:
        logger.warning(
            "[tech_lead] No leaf contract staged for PR #%d @ %s: %s",
            candidate.pr_number,
            candidate.short_sha,
            exc,
        )
        return unresolved(
            f"the executable issue #{issue_number} this candidate implements"
            f" could not be staged: {exc}"
        )
    # The declaration is read from the body just STAGED, never from a snapshot
    # in hand: what the descriptor attributes and what the pointers were read
    # from must be the same revision of the issue.
    try:
        declarations = parse_governing_sources(
            leaf.body, subject_issue_number=issue_number
        )
        governing = stage_governing_sources(
            repository_host=repository_host,
            bodies_dir=bodies_dir,
            declarations=declarations,
        )
    except ValueError as exc:
        logger.warning(
            "[tech_lead] Leaf contract for PR #%d @ %s declares a governing"
            " source that could not be honoured: %s",
            candidate.pr_number,
            candidate.short_sha,
            exc,
        )
        return unresolved(
            f"issue #{issue_number} declares a governing source this review"
            f" could not resolve: {exc}"
        )
    return TechLeadCandidateContract(
        candidate=candidate,
        issue_number=issue_number,
        sources_dir=f"{TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME}/{sources_dir}",
        sources=(leaf.source, *governing),
    )


def build_candidate_contracts(
    entries: Sequence["PRToReview"],
    *,
    repository_host: "RepositoryHost",
    data_path: Path,
) -> TechLeadCandidateContractSet:
    """Stage every audited candidate's leaf contract under ``data_path``."""
    bodies_root = data_path / TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME
    return TechLeadCandidateContractSet(
        entries=tuple(
            _contract_for(
                entry, repository_host=repository_host, bodies_root=bodies_root
            )
            for entry in entries
        )
    )


def write_candidate_contracts(
    data_path: Path, contracts: TechLeadCandidateContractSet
) -> Path:
    """Write the descriptor beside the manifest, and return its path.

    Fail-fast like the board snapshot: a launch that cannot write the file
    would spawn a session with no contract to judge against and no way to see
    that, so it must fail rather than produce an audit of nothing.
    """
    path = data_path / TECH_LEAD_CANDIDATE_CONTRACT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contracts.to_payload(), indent=2) + "\n")
    return path


def stage_candidate_contracts(
    entries: Sequence["PRToReview"],
    *,
    repository_host: "RepositoryHost",
    data_path: Path,
) -> TechLeadCandidateContractSet:
    """Stage and record every candidate's leaf contract, and mark the manifest.

    ``PRToReview.contract_established`` is the orchestrator's own answer copied
    onto the manifest entries, exactly as ``review_established`` is, so the
    launch authority can carry it out of the agent's reach and completion can
    refuse a ``pass`` on a candidate whose contract was never resolved. The
    contract's ``gap`` rides along with it for the same reason the reviewer
    evidence's does: the descriptor written above is disposed of with this
    session's worktree, and the refusal receipt that has to explain itself is
    published after that.
    """
    contracts = build_candidate_contracts(
        entries, repository_host=repository_host, data_path=data_path
    )
    path = write_candidate_contracts(data_path, contracts)
    resolved = contracts.contracted_pr_numbers()
    # One-for-one and in order, like the evidence half: ``build_candidate_
    # contracts`` stages exactly the entries it was handed.
    for entry, contract in zip(entries, contracts.entries, strict=True):
        entry.contract_established = contract.establishes_leaf_contract
        entry.contract_gap = contract.gap
    logger.info(
        "[tech_lead] Staged leaf contracts for %d of %d candidate(s): %s",
        len(resolved),
        len(contracts.entries),
        path,
    )
    return contracts


__all__ = [
    "build_candidate_contracts",
    "stage_candidate_contracts",
    "write_candidate_contracts",
]
