"""``claim-retire`` — the one way to retire a pending-work claim (#245).

The operator-facing half of
:mod:`...ports.pending_work_claim_retirement`. It exists because a readable,
unresolved claim whose run has ended had no supported disposition at all:
quarantine parking is reached only for records nothing can read, and the
defer/consume settlements are keyed on a live ``SessionRunAssets`` that a dead
run no longer has. The alternative to this command is hand-editing durable
state, which is not an alternative.

Two subcommands, and the split is the safety property:

* ``record`` takes the decision. It is the only thing in this repository that
  sets the retired bit, it is never called by a scheduler, a startup path or a
  sweep, and it refuses — changing nothing — unless the row it finds is exactly
  the claim the operator described. The one precondition it cannot check for
  itself is the run being over: liveness lives in the orchestrator's control
  state, not in the ledger, so "this run has ended" stays the operator's
  judgement and ``--dry-run`` is how they inspect the row before taking it.
* ``evidence`` reads. It mutates nothing, and it is how the operator confirms
  afterwards that the payload and provenance survived the decision.

Exit codes, following ``trusted-runtime-promote``:

* ``0`` — recorded (or rehearsed with ``--dry-run``, or listed).
* ``1`` — refused. Nothing was written, and the reason is on stderr.
* ``2`` — the request itself was malformed (an unknown work kind, an empty
  authority).

What it deliberately does NOT do:

* touch GitHub. Retirement is a local ledger decision; telling anybody about it
  remotely is a separate act with its own authority, and its failure must not
  be able to undo or retry what committed here;
* decide anything. ``--authority`` is a reference to a decision taken
  elsewhere, by a human, and this command's existence is not evidence that one
  was taken. It records the operator's act; it does not authorize it.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from ...domain.pending_work import PendingWorkKind
from ...execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from ...ports.pending_work_claim_retirement import (
    ClaimRetirementRefused,
    ClaimRetirementRequest,
    ClaimRetirementTarget,
    PendingWorkClaimRetirementStore,
    RetiredClaimRecord,
)

_RECORD = "record"
_EVIDENCE = "evidence"


def _rendered(record: RetiredClaimRecord) -> str:
    """One retirement, in full, including the payload that was preserved."""
    return "\n".join(
        (
            f"  work key    {record.work_key}",
            f"  run key     {record.run_key}",
            f"  started at  {record.started_at}",
            f"  session     {record.session_name}",
            f"  issue       #{record.issue_number}",
            f"  work kind   {record.work_kind.value}",
            f"  flavor      {record.flavor or '(none)'}",
            f"  authority   {record.authority}",
            f"  reason      {record.reason}",
            f"  recorded at {record.recorded_at}",
            f"  payload     {record.payload}",
        )
    )


def record_retirement(
    store: PendingWorkClaimRetirementStore,
    request: ClaimRetirementRequest,
    *,
    dry_run: bool,
) -> int:
    """Retire the described claim, or rehearse the refusals against it.

    ``--dry-run`` is not a second code path: it runs the identical request
    against the identical store and rolls back instead of committing, so a
    rehearsal that passes is evidence about the real thing. Retirement is
    irreversible, and rehearsing it is how an operator finds out that the row
    they are describing is not the row they meant.
    """
    try:
        record = _committed(store, request, dry_run=dry_run)
    except ClaimRetirementRefused as refusal:
        print(f"REFUSED [{refusal.refusal.value}]: {refusal}", file=sys.stderr)
        return 1
    verb = "WOULD RETIRE" if dry_run else "RETIRED"
    print(f"{verb}:\n{_rendered(record)}")
    return 0


def _committed(
    store: PendingWorkClaimRetirementStore,
    request: ClaimRetirementRequest,
    *,
    dry_run: bool,
) -> RetiredClaimRecord:
    """The record the request produces, committed only when it is for real."""
    if dry_run:
        return store.rehearse_claim_retirement(request)
    return store.retire_claim(request)


def show_evidence(store: PendingWorkClaimRetirementStore) -> int:
    """Print every retirement this ledger has recorded. Mutates nothing."""
    records = store.list_retired_claims()
    print(f"{len(records)} recorded retirement(s)")
    for record in records:
        print(_rendered(record))
        print()
    return 0


def _add_record_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--work-key",
        required=True,
        help="Ledger work identity of the claim, e.g. tech_lead:23",
    )
    parser.add_argument(
        "--issue",
        required=True,
        type=int,
        help="Issue number the ledger recorded for that claim",
    )
    parser.add_argument(
        "--work-kind",
        required=True,
        choices=[kind.value for kind in PendingWorkKind],
        help="Pending-work kind the claim must be",
    )
    parser.add_argument(
        "--flavor",
        default=None,
        help=(
            "Variant the claim must be, e.g. planning_investigation. Omit only "
            "when the claim genuinely has no variant - omitting it for a claim "
            "that has one is a mismatch, and the retirement is refused"
        ),
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Why this claim is being abandoned, in the operator's own words",
    )
    parser.add_argument(
        "--authority",
        required=True,
        help=(
            "Attributable reference to the decision that authorized this "
            "retirement, e.g. the issue or record where a human took it"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run every check and report the outcome without writing anything",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claim-retire",
        description=(
            "Retire one pending-work claim whose run has ended, on an explicit "
            "operator authority. Local only - nothing is written to GitHub."
        ),
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Repository whose orchestrator state directory holds the ledger",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    _add_record_arguments(
        subcommands.add_parser(
            _RECORD,
            help="Record the retirement of one described claim",
            description=(
                "Record the retirement of one described claim. Retire only a "
                "claim whose run has ENDED: the ledger cannot see liveness, so "
                "it will not refuse on that ground, and retiring a live run's "
                "claim leaves a decision recorded against work that may still "
                "settle itself. Rehearse with --dry-run first - every check "
                "runs against the real ledger and nothing is written."
            ),
        )
    )
    subcommands.add_parser(
        _EVIDENCE, help="Print the retirements already recorded (read-only)"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    args = build_parser().parse_args(argv)
    try:
        store = SqlitePendingWorkClaimStore.for_repo(args.root)
        if args.command == _EVIDENCE:
            return show_evidence(store)
        return record_retirement(
            store,
            ClaimRetirementRequest(
                target=ClaimRetirementTarget(
                    work_key=args.work_key,
                    issue_number=args.issue,
                    work_kind=PendingWorkKind(args.work_kind),
                    flavor=args.flavor,
                ),
                reason=args.reason,
                authority=args.authority,
                recorded_at=datetime.now(timezone.utc).isoformat(),
            ),
            dry_run=args.dry_run,
        )
    except (TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def safe_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    safe_main()
