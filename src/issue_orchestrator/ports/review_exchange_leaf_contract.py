"""Port for staging the admitted executable leaf contract of one exchange.

The review exchange must hand both roles the same admitted contract bytes
(#399). Producing them is two things at once — reading the canonical
issue and writing an exact snapshot into the run's evidence directory —
and the exchange runner should ask for the *outcome*, not assemble it: a
runner that fetched an issue and then wrote a file would be a second
place that decides what "the admitted contract" is.

So this port is behavior-level. One call, one answer: the handle proving
which bytes the exchange will consume, or
:class:`~..domain.review_exchange_contract.LeafContractUnavailable`.
There is no "returns None" spelling, because the caller's only correct
response to a missing contract is to refuse the exchange, and an
optional return invites a caller to continue without one.
"""

from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

from ..domain.review_exchange_contract import LeafContractUnavailable

if TYPE_CHECKING:
    from ..domain.review_exchange_contract import StagedLeafContract
    from ..domain.review_exchange_run import ReviewExchangeRunAssets


class AdmittedLeafContractStaging(Protocol):
    """Stage one exchange's admitted leaf contract into its run evidence."""

    def stage(
        self,
        *,
        issue_number: int,
        assets: "ReviewExchangeRunAssets",
    ) -> "StagedLeafContract":
        """Write the exact admitted contract into ``assets`` and describe it.

        Raises:
            LeafContractUnavailable: when the canonical contract cannot be
                read exactly. Callers must fail the exchange closed; there
                is no approximate substitute (issue title, PR prose,
                labels) that would preserve the authority the contract
                carries.
        """
        ...


class UnstageableAdmittedLeafContract:
    """The default staging owner: refuses every exchange.

    A deployment that forgot to wire the real one loses the exchange
    rather than running a Reviewer with no admitted scope, which is the
    failure #399 exists to close. Same stance as the unwired Tech Lead
    completion validator: the default is the strict one.
    """

    def stage(
        self,
        *,
        issue_number: int,
        assets: "ReviewExchangeRunAssets",
    ) -> "StagedLeafContract":
        _ = assets
        raise LeafContractUnavailable(
            "no admitted leaf contract staging is wired — the review exchange "
            f"cannot prove the contract admitted for issue #{issue_number}"
        )


UNSTAGEABLE_ADMITTED_LEAF_CONTRACT: AdmittedLeafContractStaging = (
    UnstageableAdmittedLeafContract()
)
