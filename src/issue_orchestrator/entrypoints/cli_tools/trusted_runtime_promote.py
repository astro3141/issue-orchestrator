"""``trusted-runtime-promote`` — the promotion gate as a command (#194).

The operator-facing half of :mod:`...control.trusted_runtime_promotion`. It
answers one question about one artifact and says so in its exit code:

* ``0`` — the live-assurance lane recorded ``PASS`` for this exact artifact;
  the promotion procedure may continue.
* ``1`` — refused, with the reason on stderr. No record, ``INCONCLUSIVE``, or
  ``SECURITY_FAIL``.
* ``2`` — the request itself was malformed (not a full SHA, unreadable
  record).

It deliberately does **not** move any pin. The pin and the promotion procedure
live in issue #18, and inventing a second home for them here would make two
places authoritative about which runtime is trusted. What this command adds is
the check the prose could not enforce: a gate that refuses, in a shell, before
anything is rebuilt.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ...control.trusted_runtime_promotion import (
    TrustedRuntimePromotion,
    TrustedRuntimePromotionRefused,
)
from ...execution.live_assurance_provider import live_assurance_store_for


def run_promotion_gate(*, head_sha: str, root: Path) -> int:
    """Ask the gate about ``head_sha`` and render its answer as an exit code."""
    gate = TrustedRuntimePromotion(live_assurance_store_for(root))
    try:
        gate.admit(head_sha)
    except TrustedRuntimePromotionRefused as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return 1
    print(f"ADMITTED: live-assurance PASS recorded for artifact {head_sha}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="trusted-runtime-promote",
        description=(
            "Refuse a trusted-runtime promotion unless the live-assurance "
            "lane recorded PASS for that exact artifact."
        ),
    )
    parser.add_argument(
        "--head-sha",
        required=True,
        help="Full 40-character commit SHA of the runtime artifact being promoted",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="Checkout holding .issue-orchestrator/live-assurance (default: cwd)",
    )
    args = parser.parse_args(argv)

    try:
        return run_promotion_gate(head_sha=args.head_sha, root=args.root)
    except (TypeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def safe_main() -> None:
    sys.exit(main())


if __name__ == "__main__":
    safe_main()
