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
reviewer worktree's guard reads. This module owns the Codex dialect — how an
entry point becomes a ``prefix_rule`` — and the planning-specific refusal
prose. It owns no part of the vocabulary, so a gate command added there is
refused for both principals and one removed there breaks both.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ...domain.artifact_contracts import AgentProvider
from ...infra.hooks.gate_commands import ArgvPattern, codex_argv_patterns
from ...ports.planning_command_guard import (
    GUARDABLE_PLANNING_PROVIDERS,
    GuardProbe,
    PlanningCommandGuard,
    PlanningCommandGuardError,
)
from ..hooks.codex import CodexAdapter
from ..hooks.codex_execpolicy import (
    CodexCliExecPolicy,
    ExecPolicyChecker,
    ExecPolicyOutcome,
    ExecPolicyResultError,
)
from ._worktree_runtime import _write_worktree_exclude_entries

logger = logging.getLogger(__name__)

__all__ = [
    "PLANNING_ALLOWED_SAMPLES",
    "PLANNING_GUARD_RULES",
    "PLANNING_REFUSAL_JUSTIFICATION",
    "PLANNING_REFUSED_SAMPLES",
    "SAFETY_REFUSED_SAMPLES",
    "CodexPlanningCommandGuardInstaller",
    "render_planning_rules",
]

#: Worktree-relative policy file this installer owns outright.
PLANNING_GUARD_RULES = Path(".codex") / "rules" / "planning-gate.rules"

#: Worktree-relative safety policy the shipped Codex hook template installs.
#: Named here so this installer can put the planning refusal *beside* it rather
#: than in place of it.
CODEX_SAFETY_RULES = Path(".codex") / "rules" / "orchestrator.rules"

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

#: What the *composed* safety policy must still refuse once it has been placed
#: beside the planning one. #289 acceptance 7 says the shipped no-bypass /
#: no-merge rules compose with the planning refusal rather than being replaced
#: by it — and this module's own standard is that a written file is not
#: evidence of a barrier. Copying ``orchestrator.rules`` in and returning would
#: hold the safety half to exactly the weaker standard the planning half is not
#: allowed to use, so it is put to the same checker in the same pass.
SAFETY_REFUSED_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("git", "push", "--no-verify"),
    ("gh", "pr", "merge"),
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


def _render_token(token: str | tuple[str, ...]) -> str:
    if isinstance(token, tuple):
        inner = ", ".join(json.dumps(value) for value in token)
        return f"[{inner}]"
    return json.dumps(token)


def _render_rule(pattern: ArgvPattern, justification: str) -> str:
    tokens = ", ".join(_render_token(token) for token in pattern)
    return (
        "prefix_rule(\n"
        f"    pattern = [{tokens}],\n"
        '    decision = "forbidden",\n'
        f"    justification = {json.dumps(justification)},\n"
        ")\n"
    )


def render_planning_rules() -> str:
    """Render the whole planning policy file, deterministically.

    Same vocabulary in, byte-identical file out, so a rendered file can be
    compared with what a launch should have written instead of being trusted
    because it exists.
    """
    body = "\n".join(
        _render_rule(pattern, PLANNING_REFUSAL_JUSTIFICATION)
        for pattern in codex_argv_patterns()
    )
    return f"{_RULES_HEADER}\n{body}"


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

        policy_file = Path(worktree_path) / PLANNING_GUARD_RULES
        safety_file = Path(worktree_path) / CODEX_SAFETY_RULES
        self._write_policy(policy_file)
        self._install_safety_rules(worktree_path)
        probes = self._verify(policy_file, safety_file)
        _write_worktree_exclude_entries(
            Path(worktree_path), [PLANNING_GUARD_RULES, CODEX_SAFETY_RULES]
        )
        logger.info(
            "[planning-guard] established: worktree=%s policy=%s refuses=%s "
            "allows=%s",
            worktree_path,
            policy_file,
            len([probe for probe in probes if probe.refused]),
            len([probe for probe in probes if not probe.refused]),
        )
        return PlanningCommandGuard(
            provider=provider, policy_file=policy_file, probes=probes
        )

    def _write_policy(self, policy_file: Path) -> None:
        try:
            policy_file.parent.mkdir(parents=True, exist_ok=True)
            policy_file.write_text(render_planning_rules(), encoding="utf-8")
        except OSError as exc:
            raise PlanningCommandGuardError(
                f"Failed to write the planning command guard at {policy_file}: {exc}"
            ) from exc

    def _install_safety_rules(self, worktree_path: Path) -> None:
        """Put the shipped Codex safety policy beside the planning one.

        Codex resolves this run's project root to the scratch worktree, so the
        product checkout's ``orchestrator.rules`` is not in scope for it. #289
        requires the guarded planning launch to still carry the existing
        no-bypass / no-merge rules, and Codex loads every ``.rules`` file in
        the directory — so composition is a matter of both files being here.

        Copying is only half of it. What the copy actually refuses is measured
        by :meth:`_verify` against :data:`SAFETY_REFUSED_SAMPLES`, so a safety
        file that arrived empty, truncated or superseded fails the launch
        instead of riding along unexamined.
        """
        try:
            CodexAdapter(execpolicy=self._execpolicy).install_hooks(worktree_path)
        except OSError as exc:
            raise PlanningCommandGuardError(
                "Failed to install the Codex safety rules alongside the "
                f"planning command guard in {worktree_path}: {exc}"
            ) from exc

    def _verify(
        self, policy_file: Path, safety_file: Path
    ) -> tuple[GuardProbe, ...]:
        """Ask the enforcing mechanism how it classifies every pinned sample.

        Both files this launch put in ``.codex/rules/`` are measured, each
        against the samples it owns: the planning policy must refuse the gate
        vocabulary and keep the reading and ``coding-done`` samples, and the
        composed safety policy must still refuse the no-bypass / no-merge
        entry points it exists for.

        A sample classified the wrong way, or an answer that cannot be
        classified at all, fails the guard: an unreadable verdict is not
        evidence of a barrier.
        """
        plan: tuple[tuple[Path, tuple[str, ...], bool], ...] = (
            *((policy_file, s, True) for s in PLANNING_REFUSED_SAMPLES),
            *((policy_file, s, False) for s in PLANNING_ALLOWED_SAMPLES),
            *((safety_file, s, True) for s in SAFETY_REFUSED_SAMPLES),
        )
        return tuple(
            self._probe(rules_file, command, expect_refused)
            for rules_file, command, expect_refused in plan
        )

    def _probe(
        self, rules_file: Path, command: tuple[str, ...], expect_refused: bool
    ) -> GuardProbe:
        label = " ".join(command)
        try:
            outcome = self._execpolicy.check(rules_file, command)
        except ExecPolicyResultError as exc:
            raise PlanningCommandGuardError(
                f"The planning command guard at {rules_file} gave no "
                f"classifiable answer for {label!r}: {exc}"
            ) from exc
        refused = outcome is ExecPolicyOutcome.FORBIDDEN
        if refused is not expect_refused:
            wanted = "refuse" if expect_refused else "allow"
            raise PlanningCommandGuardError(
                f"The planning command guard at {rules_file} does not "
                f"{wanted} {label!r} (execpolicy answered {outcome.value})"
            )
        return GuardProbe(command=tuple(command), refused=refused)
