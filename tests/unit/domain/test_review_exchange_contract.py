"""The admitted leaf contract value objects (#399).

These pin the two properties the exchange leans on: a digest that
depends on the contract bytes and nothing else, and a handle that either
describes real, attributable bytes or refuses to exist.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.review_exchange_contract import (
    LEAF_CONTRACT_MANIFEST_SCHEMA,
    AdmittedLeafContract,
    LeafContractUnavailable,
    StagedLeafContract,
    leaf_contract_digest,
    staged_leaf_contract_from_manifest_payload,
)

CONTRACT_PATH = Path("/wt/run/review-exchange/issue-contract.md")
BODY = "## Admitted mutation\n\nChange exactly `src/only_this_file.py`.\n"


def _contract(**overrides: object) -> AdmittedLeafContract:
    kwargs: dict[str, object] = {
        "issue_number": 399,
        "issue_title": "Carry the contract",
        "body": BODY,
    }
    kwargs.update(overrides)
    return AdmittedLeafContract(**kwargs)  # type: ignore[arg-type]


class TestTheDigestIsOverTheContractBytesAlone:
    def test_the_same_body_digests_the_same_whoever_staged_it(self) -> None:
        # F2 rests on this: two roles' packets are comparable only because
        # the digest does not carry run-scoped or role-scoped input.
        assert _contract().digest == _contract(
            issue_number=1,
            issue_title="Something else entirely",
        ).digest

    def test_a_changed_body_changes_the_digest(self) -> None:
        assert _contract().digest != _contract(body=BODY + "and file B.\n").digest

    def test_the_digest_names_its_algorithm(self) -> None:
        assert _contract().digest.startswith("sha256:")
        assert _contract().digest == leaf_contract_digest(BODY)


class TestAContractWithNothingToReviewAgainstIsRefused:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("body", ""),
            ("body", "   \n"),
            ("issue_title", ""),
            ("issue_number", 0),
            ("issue_number", -1),
        ],
    )
    def test_it_cannot_be_constructed(self, field: str, value: object) -> None:
        # The title is deliberately not a usable stand-in for a missing
        # body: reviewing against "what the work is called" is the
        # approximation this artifact exists to refuse.
        with pytest.raises(ValueError):
            _contract(**{field: value})


class TestTheStagedHandle:
    def test_it_round_trips_through_its_manifest_fields(self) -> None:
        staged = _contract().staged_at(CONTRACT_PATH)

        assert StagedLeafContract.from_manifest(staged.to_manifest_fields()) == staged

    def test_it_recognises_the_bytes_it_attests_to(self) -> None:
        staged = _contract().staged_at(CONTRACT_PATH)

        assert staged.matches(BODY)
        assert not staged.matches(BODY + "\n")

    def test_a_relative_path_is_refused(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            StagedLeafContract(
                path=Path("review-exchange/issue-contract.md"),
                issue_number=399,
                issue_title="t",
                digest=leaf_contract_digest(BODY),
            )

    def test_a_digest_that_does_not_name_its_algorithm_is_refused(self) -> None:
        with pytest.raises(ValueError, match="sha256:"):
            StagedLeafContract(
                path=CONTRACT_PATH,
                issue_number=399,
                issue_title="t",
                digest="deadbeef",
            )

    @pytest.mark.parametrize(
        "manifest",
        [
            "not-a-mapping",
            {"issue_number": 399, "issue_title": "t", "digest": "sha256:x"},
            {"path": "", "issue_number": 399, "issue_title": "t", "digest": "sha256:x"},
            {"path": "/a.md", "issue_number": "399", "issue_title": "t", "digest": "sha256:x"},
            {"path": "/a.md", "issue_number": True, "issue_title": "t", "digest": "sha256:x"},
            {"path": "/a.md", "issue_number": 399, "issue_title": "t", "digest": "nope"},
            {"path": "a.md", "issue_number": 399, "issue_title": "t", "digest": "sha256:x"},
        ],
    )
    def test_an_unusable_manifest_yields_no_handle(self, manifest: object) -> None:
        # ``None`` means "unusable", which the packet reader turns into a
        # rejected packet — never into a silently unset dependency.
        assert StagedLeafContract.from_manifest(manifest) is None


class TestTheAttributionSidecar:
    def test_it_describes_the_bytes_staged_beside_it(self) -> None:
        payload = _contract().manifest_payload(contract_path=CONTRACT_PATH)

        assert payload["schema"] == LEAF_CONTRACT_MANIFEST_SCHEMA
        assert payload["issue_number"] == 399
        assert payload["digest"] == leaf_contract_digest(BODY)
        assert payload["contract_file"] == "issue-contract.md"

    def test_it_reads_back_as_the_handle_that_wrote_it(self) -> None:
        contract = _contract()

        recovered = staged_leaf_contract_from_manifest_payload(
            contract.manifest_payload(contract_path=CONTRACT_PATH),
            contract_path=CONTRACT_PATH,
        )

        assert recovered == contract.staged_at(CONTRACT_PATH)

    @pytest.mark.parametrize(
        ("payload", "match"),
        [
            ("not-an-object", "not a JSON object"),
            ({"schema": "something.v9"}, "schema"),
            (
                {
                    "schema": LEAF_CONTRACT_MANIFEST_SCHEMA,
                    "issue_title": "t",
                    "digest": "sha256:x",
                },
                "issue_number",
            ),
            (
                {
                    "schema": LEAF_CONTRACT_MANIFEST_SCHEMA,
                    "issue_number": 399,
                    "digest": "sha256:x",
                },
                "issue_title",
            ),
            (
                {
                    "schema": LEAF_CONTRACT_MANIFEST_SCHEMA,
                    "issue_number": 399,
                    "issue_title": "t",
                },
                "digest",
            ),
            (
                {
                    "schema": LEAF_CONTRACT_MANIFEST_SCHEMA,
                    "issue_number": 399,
                    "issue_title": "t",
                    "digest": "not-a-sha",
                },
                "invalid",
            ),
        ],
    )
    def test_a_malformed_sidecar_refuses_rather_than_guesses(
        self, payload: object, match: str
    ) -> None:
        with pytest.raises(LeafContractUnavailable, match=match):
            staged_leaf_contract_from_manifest_payload(
                payload,
                contract_path=CONTRACT_PATH,
            )
