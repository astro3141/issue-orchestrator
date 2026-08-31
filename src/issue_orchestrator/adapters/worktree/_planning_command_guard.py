"""Establishes the launch-scoped Codex gate-command refusal for planning (#289).

A ``planning_investigation`` Tech Lead is told in its prompt that the
repository's build/test/validation gate is not a planning verdict. R22 Pilot 4
ran one anyway, for seventeen minutes, inside a sandbox that could not satisfy
it, and returned BLOCKED without the bounded ``create_issue`` it was launched
to produce. This module is the barrier that makes the instruction hold.

**Where the policy is registered, and why there.** A focused Tech Lead run
already executes in a *disposable scratch worktree* created for that run alone
(``control/tech_lead_session_policy.focused_tech_lead_scratch_identity``).
Codex detects a project root by walking up for a ``.git`` marker, and a linked
worktree's ``.git`` is a file — so the project root Codex resolves for a
planning launch is the scratch worktree itself, not the product checkout. Its
``.codex/rules/`` directory is therefore a *per-launch* policy surface that no
other principal reads and that the ordinary worktree owner deletes with the
run. The product checkout's ``.codex/rules`` is never touched, so an ordinary
Codex Actor validating a candidate in its own worktree is unaffected, and
nothing in the operator's ``~/.codex`` is written.

**What this DOES leave outside the worktree, stated exactly.** Registering the
policy also keeps it out of ``git status`` in the scratch checkout, and the
only mechanism git offers for that is an exclude entry. ``info/`` is a
common-dir path, so — measured, not assumed — the per-worktree
``.git/worktrees/<name>/info/exclude`` is inert and the entry that takes effect
is the repository's **shared** ``.git/info/exclude``
(:func:`~._worktree_runtime._worktree_exclude_files` records the measurement).
So a Codex planning launch adds two lines to the product checkout's shared
exclude file:

.. code-block:: text

   .codex/rules/planning-gate.rules
   .codex/rules/orchestrator.rules

That is deliberate, and it is *bounded*: the write is idempotent (an entry
already present is not re-appended), so the residue is those two lines once,
not two per launch, and it names only paths the orchestrator owns and the
repository does not track. ``orchestrator.rules`` is the file ``io hooks
install`` already writes untracked at the product root, so hiding it there is
the same statement the reviewer guard makes about
``.claude/settings.local.json``; ``planning-gate.rules`` never exists at the
product root at all.

The entries are deliberately **not** removed when the scratch worktree is
deleted. The exclude file is shared, so a teardown-time drop would unhide a
*concurrently live* planning launch's policy files mid-run, and would still
leave the lines behind whenever a run dies without reaching teardown — a
cleanup that is neither complete nor safe. Two stable, orchestrator-owned lines
are the honest cost. ``docs/architecture/validation.md`` states it for
operators.

**Measured, not assumed** (codex-cli 0.147.0, macOS, live ``codex exec`` in a
real linked worktree):

* a rule file in ``<linked worktree>/.codex/rules/`` is loaded and enforced,
  and the command is refused *before process creation* — Codex answers
  ``CreateProcess { Rejected("...") }`` and no shell ever runs;
* it loads under the existing #215 trust grant, which names the **common
  repository root** (the product checkout). No new root is trusted, and no
  trust, sandbox, approval or credential state changes for this guard;
* every ``.rules`` file in that directory is loaded, so the shipped safety
  rules (``git push --no-verify``, ``git commit --no-verify``,
  ``gh pr merge``, ``gh api``) and the planning refusal compose rather than
  replace one another. Both were observed refusing in the same session;
* a project-local ``.codex/hooks.json`` was measured and rejected as the
  mechanism: an unattended launch parked on it, which is the #204 failure
  class this repository already refuses to re-open.

**The guard is data, not an assumption.** Writing a file proves nothing about
enforcement, so before returning, every pinned sample is put to the same
authority the shipped hook gate uses — ``codex execpolicy check``, through the
:class:`~issue_orchestrator.adapters.hooks.codex_execpolicy.ExecPolicyChecker`
port (#288 validated that checker against this CLI). A sample that is
classified the wrong way, or that the checker cannot classify at all, raises
:class:`PlanningCommandGuardError` and the caller fails the launch closed. What
this verifies is that the policy *content* refuses; that a running session
loads it is the provider behaviour measured above and re-measured by
``tests/integration/test_codex_planning_guard_live.py``.

**One classifier.** The refused entry points come from
:mod:`issue_orchestrator.infra.hooks.gate_commands`, the same vocabulary the
reviewer worktree's guard reads. This module owns the planning-specific refusal
prose and the samples a planning run must keep; how a Codex worktree policy is
rendered, composed with the shipped safety rules, verified and hidden belongs to
:mod:`._codex_gate_policy`, which the reviewer's Codex registration (#396) uses
too. This module owns no part of the vocabulary and no part of the mechanism.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ...domain.artifact_contracts import AgentProvider
from ...ports.planning_command_guard import (
    GUARDABLE_PLANNING_PROVIDERS,
    PlanningCommandGuard,
    PlanningCommandGuardError,
)
from ..hooks.codex_execpolicy import CodexCliExecPolicy, ExecPolicyChecker
from ._codex_gate_policy import (
    CODEX_SAFETY_RULES,
    SAFETY_REFUSED_SAMPLES,
    CodexGatePolicy,
    CodexGatePolicyError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CODEX_SAFETY_RULES",
    "PLANNING_ALLOWED_SAMPLES",
    "PLANNING_GATE_POLICY",
    "PLANNING_GUARD_RULES",
    "PLANNING_REFUSAL_JUSTIFICATION",
    "PLANNING_REFUSED_SAMPLES",
    "SAFETY_REFUSED_SAMPLES",
    "CodexPlanningCommandGuardInstaller",
    "render_planning_rules",
]

#: Worktree-relative policy file this installer owns outright.
PLANNING_GUARD_RULES = Path(".codex") / "rules" / "planning-gate.rules"

#: What the refused principal is told. Deliberately *not* the reviewer's prose:
#: a planning worktree is a fully provisioned checkout, so "this worktree has
#: no runtime prerequisites" would be false here. The true reason is what the
#: principal is for.
PLANNING_REFUSAL_JUSTIFICATION = (
    "This planning_investigation session prepares a bounded issue; it does "
    "not validate a code candidate. The repository's build/test/validation "
    "gate produces no planning verdict, and running it here spends the "
    "round's budget on a gate this session's sandbox cannot satisfy. Read "
    "the source and the staged governing evidence instead, then record the "
    "work through the bounded create_issue effect."
)

#: Gate commands whose refusal is verified before the session may start. Pinned
#: rather than sampled from the vocabulary: #289 names ``make validate-pr-raw``
#: and a pytest-shaped command as the acceptance direction, and a probe set
#: derived from the same table it is checking would agree with itself even
#: after the table stopped meaning anything.
PLANNING_REFUSED_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("make", "validate-pr-raw"),
    ("pytest", "-q", "tests/unit"),
    ("python", "-m", "pytest"),
)

#: What a planning principal must keep. Verified in the same pass, so a policy
#: that refuses by refusing everything fails to establish. The last entry is
#: the run's own way out: #261 gave planning a bounded ``create_issue`` flow
#: that it reaches through ``coding-done``, and enforcement that turned
#: planning into a no-tools role would be a worse outcome than the gate it
#: prevents.
PLANNING_ALLOWED_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("git", "log", "--oneline", "-20"),
    ("rg", "-n", "planning_investigation", "src"),
    ("cat", "AGENTS.md"),
    ("coding-done", "completed", "--implementation", "prepared the leaf"),
)

_RULES_HEADER = """\
# Launch-scoped planning guard for one issue-orchestrator Tech Lead run.
#
# Generated by adapters/worktree/_planning_command_guard.py from the single
# gate-command vocabulary in infra/hooks/gate_commands.py. This file lives in
# the run's disposable scratch worktree only: the product checkout's
# .codex/rules and the operator's ~/.codex are untouched, and the file goes
# away with the worktree. The one thing that outlives it is the repository's
# shared .git/info/exclude, which gains a line naming this path so the policy
# stays out of git status; it is written once and not removed. Editing this
# file by hand has no effect on the next run.
"""

#: The planning principal's half of the shared Codex policy mechanism.
PLANNING_GATE_POLICY = CodexGatePolicy(
    name="planning command guard",
    rules_path=PLANNING_GUARD_RULES,
    header=_RULES_HEADER,
    justification=PLANNING_REFUSAL_JUSTIFICATION,
    refused_samples=PLANNING_REFUSED_SAMPLES,
    allowed_samples=PLANNING_ALLOWED_SAMPLES,
)


def render_planning_rules() -> str:
    """Render the whole planning policy file, deterministically.

    Same vocabulary in, byte-identical file out, so a rendered file can be
    compared with what a launch should have written instead of being trusted
    because it exists.
    """
    return PLANNING_GATE_POLICY.render()


class CodexPlanningCommandGuardInstaller:
    """Writes and verifies the planning policy for one Codex launch.

    Takes the checker that answers execpolicy questions so the verification is
    injectable at the port boundary the hook gate already uses; production
    passes the installed Codex CLI, which is the only authority on its own
    rules.
    """

    def __init__(self, execpolicy: ExecPolicyChecker | None = None) -> None:
        self._execpolicy: ExecPolicyChecker = (
            CodexCliExecPolicy() if execpolicy is None else execpolicy
        )

    def establish(
        self, worktree_path: Path, *, provider: AgentProvider
    ) -> PlanningCommandGuard:
        """Register the planning refusal in ``worktree_path`` if it can be."""
        if provider.value not in GUARDABLE_PLANNING_PROVIDERS:
            logger.warning(
                "Planning worktree is UNGUARDED: no launch-scoped gate-command "
                "guard mechanism is implemented for provider %s (guardable: "
                "%s). Nothing but the planning prompt stops a build/test/"
                "validation command in %s.",
                provider.value,
                ", ".join(sorted(GUARDABLE_PLANNING_PROVIDERS)),
                worktree_path,
            )
            return PlanningCommandGuard(provider=provider)

        try:
            established = PLANNING_GATE_POLICY.establish(
                worktree_path, execpolicy=self._execpolicy
            )
        except CodexGatePolicyError as exc:
            raise PlanningCommandGuardError(str(exc)) from exc
        return PlanningCommandGuard(
            provider=provider,
            policy_file=established.policy_file,
            probes=established.probes,
        )
