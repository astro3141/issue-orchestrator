"""Framework resources - canonical docs injected into agent sessions."""

from importlib import resources
from functools import lru_cache

from ..domain.session_key import TaskKind


@lru_cache(maxsize=1)
def get_coding_done_instructions() -> str:
    """Load the canonical coding-done instructions.

    Injected into coding/rework agent sessions so they know
    how to signal completion with dirty-file checks and validation.

    Cached since the content never changes during runtime.
    """
    return resources.files(__package__).joinpath("coding_done.md").read_text()


@lru_cache(maxsize=1)
def get_tech_lead_done_instructions() -> str:
    """Load the canonical tech_lead completion instructions (#385).

    A Tech Lead completes with ``coding-done``, like a coder, but its
    completion protocol is NOT the coder's: the coder document makes
    ``prepush-check --dirty-only -v`` mandatory, and that command records
    timings under the repository's shared git common dir — a write outside a
    bounded Tech Lead's sandbox write roots. A live bounded run therefore could
    not complete at all.

    This document is the same protocol with that one obligation moved to its
    trusted owner: the orchestrator executes the mandatory completion
    validation outside the session and gates the completion on the result, so
    nothing is waived and the model is asked for nothing it may not do.

    Cached since the content never changes during runtime.
    """
    return resources.files(__package__).joinpath("tech_lead_done.md").read_text()


@lru_cache(maxsize=1)
def get_reviewer_done_instructions() -> str:
    """Load the canonical reviewer-done instructions.

    Injected into review/tech lead agent sessions so they know
    how to signal their verdict.

    Cached since the content never changes during runtime.
    """
    return resources.files(__package__).joinpath("reviewer_done.md").read_text()


@lru_cache(maxsize=1)
def get_review_exchange_coder_instructions() -> str:
    """Load review exchange coder instructions.

    Used in via-mcp review exchange rounds so the coder knows to
    call coding-done AND write JSON to the response file.
    """
    return resources.files(__package__).joinpath("review_exchange_coder.md").read_text()


@lru_cache(maxsize=1)
def get_review_exchange_reviewer_instructions() -> str:
    """Load review exchange reviewer instructions.

    Used in via-mcp review exchange rounds so the reviewer knows to
    write JSON to the response file (NOT call reviewer-done).
    """
    return resources.files(__package__).joinpath("review_exchange_reviewer.md").read_text()


def get_completion_instructions(task_kind: str) -> str:
    """Load task-specific completion instructions.

    The task kind names the ROLE whose completion protocol applies, which is
    why ``tech-lead`` has an entry of its own: a Tech Lead completes with
    ``coding-done`` like a coder, but it may not execute the coder protocol's
    host/shared-repository pre-push step (#385).

    Args:
        task_kind: The task kind value (e.g., "code", "rework", "review",
                  "tech-lead", "review_exchange_coder",
                  "review_exchange_reviewer").

    Returns:
        Markdown instructions for the appropriate completion command.
    """
    if task_kind in ("code", "rework"):
        return get_coding_done_instructions()
    if task_kind == TaskKind.TECH_LEAD.value:
        return get_tech_lead_done_instructions()
    if task_kind == "review_exchange_coder":
        return get_review_exchange_coder_instructions()
    if task_kind == "review_exchange_reviewer":
        return get_review_exchange_reviewer_instructions()
    return get_reviewer_done_instructions()
