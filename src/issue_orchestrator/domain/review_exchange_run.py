"""Typed run ownership contracts for review exchange artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .path_guards import require_absolute_path, require_path_under
from .review_exchange_contract import (
    LEAF_CONTRACT_FILENAME,
    LEAF_CONTRACT_MANIFEST_FILENAME,
)
from .review_exchange_turn_artifacts import review_exchange_dir


@dataclass(frozen=True, slots=True)
class ReviewExchangeRunAssets:
    """Canonical artifact locations for one review-exchange run."""

    run_dir: Path
    exchange_dir: Path
    summary_path: Path
    validation_record_path: Path
    leaf_contract_path: Path
    """The exact admitted leaf contract bytes both roles consume (#399).

    One path, owned here rather than derived at each reader, because the
    whole point of the artifact is that the Coder and the Reviewer of one
    exchange read the *same* bytes. A second derivation is a second place
    they could stop being the same file.
    """
    leaf_contract_manifest_path: Path
    """Attribution for the bytes above: issue identity and digest."""

    def __post_init__(self) -> None:
        _require_absolute(self.run_dir, "run_dir")
        _require_absolute(self.exchange_dir, "exchange_dir")
        _require_absolute(self.summary_path, "summary_path")
        _require_absolute(self.validation_record_path, "validation_record_path")
        _require_absolute(self.leaf_contract_path, "leaf_contract_path")
        _require_absolute(
            self.leaf_contract_manifest_path,
            "leaf_contract_manifest_path",
        )
        expected_exchange_dir = review_exchange_dir(self.run_dir)
        if self.exchange_dir.resolve() != expected_exchange_dir.resolve():
            raise ValueError(
                "review exchange assets must use the canonical exchange_dir"
            )
        _require_under(self.summary_path, self.exchange_dir, "summary_path")
        _require_under(
            self.validation_record_path,
            self.run_dir,
            "validation_record_path",
        )
        _require_under(
            self.leaf_contract_path,
            self.exchange_dir,
            "leaf_contract_path",
        )
        _require_under(
            self.leaf_contract_manifest_path,
            self.exchange_dir,
            "leaf_contract_manifest_path",
        )

    @classmethod
    def from_run_dir(cls, run_dir: Path) -> "ReviewExchangeRunAssets":
        exchange_dir = review_exchange_dir(run_dir)
        return cls(
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            summary_path=exchange_dir / "summary.json",
            validation_record_path=run_dir / "validation-record.json",
            leaf_contract_path=exchange_dir / LEAF_CONTRACT_FILENAME,
            leaf_contract_manifest_path=(
                exchange_dir / LEAF_CONTRACT_MANIFEST_FILENAME
            ),
        )

    @classmethod
    def from_exchange_dir(cls, exchange_dir: Path) -> "ReviewExchangeRunAssets":
        return cls.from_run_dir(exchange_dir.parent)


@dataclass(frozen=True, slots=True)
class ReviewExchangeRun:
    """A concrete review-exchange session run allocated by the run owner."""

    session_name: str
    run_id: str
    parent_session_name: str
    assets: ReviewExchangeRunAssets
    validation_profile: str
    """Named validation contract frozen for this exchange (#7059).

    Required, not optional-with-a-default: the in-exchange coder's
    ``coding-done`` writes this repo's primary validation evidence, so a
    launch path that forgot to state the contract used to produce a record
    stamped ``default`` regardless of which gate actually ran. Carrying it on
    the run means the round env exports the frozen value rather than
    re-resolving it at spawn time.
    """

    def __post_init__(self) -> None:
        if not self.session_name:
            raise ValueError("review exchange run requires session_name")
        if not self.run_id:
            raise ValueError("review exchange run requires run_id")
        if not self.parent_session_name:
            raise ValueError("review exchange run requires parent_session_name")
        if not self.validation_profile:
            raise ValueError("review exchange run requires validation_profile")


def _require_absolute(path: object, field_name: str) -> None:
    require_absolute_path(path, field_name)


def _require_under(path: Path, root: Path, field_name: str) -> None:
    require_path_under(path, root, field_name)
