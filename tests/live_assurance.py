"""How a live-agent probe says which of the three outcomes it reached (#194).

The live-assurance lane has to tell two failures apart that pytest reports
identically:

* **The boundary was exercised and it did not hold.** A denied write landed, a
  denied secret was read. That is ``SECURITY_FAIL`` and it outranks
  everything.
* **The boundary was never exercised.** The external model chose not to issue
  the required tool call, the provider was unavailable, the attempt timed out.
  That is ``INCONCLUSIVE`` — not a pass, and not a security failure either.
  #109 recorded three of these, and reading them as candidate failures is the
  defect this whole migration exists to remove.

Both arrive at pytest as a failed ``assert``. Distinguishing them by parsing
the failure message would make a security classification depend on prose, so
the channel is a type instead: a probe raises through one of the helpers here
and :mod:`tests.live_assurance_lane` classifies on the exception class.

The assertions themselves are unchanged — same condition, same message. What
changes is which door the failure comes out of.
"""

from __future__ import annotations


class LiveAssuranceFailure(AssertionError):
    """Base for every failure the assurance lane knows how to classify.

    ``AssertionError`` so pytest reports these as ordinary test failures with
    the captured evidence, rather than as infrastructure errors.
    """


class SandboxBreach(LiveAssuranceFailure):
    """A boundary was really exercised and a security assertion about it failed.

    Classified ``SECURITY_FAIL``. Nothing else in the lane may raise this, and
    every "SANDBOX BREACH" assertion must — a breach that came out of a bare
    ``assert`` would be classified ``INCONCLUSIVE`` and read as a provider
    hiccup.
    """


class ProbeDidNotRun(LiveAssuranceFailure):
    """The required operation was never issued, so nothing was proven.

    Classified ``INCONCLUSIVE``. This is the "the model did not run probe 3"
    shape from #109, and the positive controls that detect it: a redirect sink
    that was never created, a tool_use that never appears in the structured
    stream.

    An unusable provider raises it too — an unauthenticated CLI leaves the
    boundary exactly as unexercised as a model that declined to issue the tool
    call, and the lane owes the reader the same answer either way. Saying so
    through this channel rather than through a skip keeps the missing
    prerequisite in the run's failures, where ``AGENTS.md`` requires it to be,
    instead of in a summary line nobody reads.
    """


def assert_no_breach(condition: object, message: str) -> None:
    """Assert a security condition, failing through the breach channel."""
    if not condition:
        raise SandboxBreach(message)


def require_probe_ran(condition: object, message: str) -> None:
    """Assert a probe actually executed, failing through the inconclusive channel.

    Also the readiness gate for a live-agent module: call it from a fixture with
    the provider-availability probe, never from module scope, so blocking
    validation's post-collection deselect does not import a real provider call.
    """
    if not condition:
        raise ProbeDidNotRun(message)


__all__ = [
    "LiveAssuranceFailure",
    "ProbeDidNotRun",
    "SandboxBreach",
    "assert_no_breach",
    "require_probe_ran",
]
