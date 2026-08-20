"""The one membership rule seven control-layer call sites now share.

``is_final_review_exchange_mode`` used to be spelled inline at each of those
sites as ``exchange_mode in {"via-mcp", "via-local-loop"}``. Consolidating them
means one place can now change what "review completes before publish" means for
publish-pipeline selection, draft-PR choice, exchange preparation, and the
``CompletedReviewExchange`` derivation at once — so the set is worth pinning
against the modes the configuration actually accepts.
"""

from __future__ import annotations

from typing import get_args

import pytest

from issue_orchestrator.control.review_exchange_modes import (
    is_final_review_exchange_mode,
)
from issue_orchestrator.infra.settings_schema import ReviewSettings


@pytest.mark.parametrize("mode", ["via-mcp", "via-local-loop"])
def test_a_mode_that_reviews_before_publish_is_final(mode: str) -> None:
    assert is_final_review_exchange_mode(mode)


@pytest.mark.parametrize("mode", ["via-draft-pr", "auto", None, ""])
def test_every_other_mode_is_not(mode: str | None) -> None:
    """``auto`` included: it is resolved to a concrete mode before this is asked.

    ``None`` is the absent mode, and answering ``True`` for it would let a
    completion that configured no exchange be treated as one that concluded.
    """
    assert not is_final_review_exchange_mode(mode)


def test_every_configured_mode_gets_an_answer_from_this_one_rule() -> None:
    """No accepted mode is unclassified — a new one must be decided here.

    Reading the accepted set off the settings schema rather than restating it:
    adding a mode there without deciding whether it reviews before publish
    fails this test instead of silently defaulting to draft-PR at seven sites.
    """
    accepted = set(get_args(ReviewSettings.model_fields["exchange_mode"].annotation))

    assert accepted == {"via-draft-pr", "via-mcp", "via-local-loop", "auto"}
    assert {mode for mode in accepted if is_final_review_exchange_mode(mode)} == {
        "via-mcp",
        "via-local-loop",
    }
