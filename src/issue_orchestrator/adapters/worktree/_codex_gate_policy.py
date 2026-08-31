"""Establishing a worktree-local Codex refusal of the gate vocabulary.

#289 measured, against codex-cli 0.147.0 on a real linked worktree, that a
``.rules`` file placed in ``<worktree>/.codex/rules/`` is loaded and enforced by
a Codex session running there, that the refusal happens *before process
creation* (``CreateProcess { Rejected("…") }``), that it loads under the
existing #215 trust grant — which names the **common repository root**, so a
linked worktree needs no new trust — and that every ``.rules`` file in the
directory composes rather than replacing the others. That measurement was made
for a ``planning_investigation`` Tech Lead's scratch worktree.

#396 needs the same substrate for a second principal: the review-exchange
reviewer worktree, which is deliberately unprovisioned and must therefore
refuse the same build/test/validation entry points. The two principals differ in
*why* they are refused and in *what they must keep*; they do not differ in how a
Codex worktree policy is written, composed with the shipped safety rules,
verified, and hidden from ``git status``. This module is that shared half, so
neither principal owns a private copy of it and a change to the mechanism cannot
apply to only one of them.

What each principal supplies is a :class:`CodexGatePolicy`: where its file goes,
the prose the refused principal is shown, and the samples whose classification
must be measured before the guard may be called established. What this module
supplies is everything after that:

* **The rendering.** Rules come from :mod:`issue_orchestrator.infra.hooks.
  gate_commands`, the one vocabulary the reviewer's shell dialect reads too.
  Rendering is deterministic — the same vocabulary in, byte-identical file out —
  so a written file can be *compared* with what a launch should have written
  rather than trusted because it exists.
* **The composition.** ``orchestrator.rules`` (the shipped no-bypass/no-merge
  safety policy) is installed beside the principal's policy, because Codex
  resolves the linked worktree as its own project root and would otherwise not
  see the product checkout's copy.
* **The verification.** Every pinned sample — the principal's refusals, the
  principal's allowances, and the safety policy's own refusals — is put to
  ``codex execpolicy check`` through the :class:`ExecPolicyChecker` port before
  this module returns. A sample classified the wrong way, or an answer that
  cannot be classified at all, raises :class:`CodexGatePolicyError`, and every
  caller fails its launch closed on it. Writing a file proves nothing about
  enforcement; this is the difference between a barrier and a decoration.
* **The hiding.** The orchestrator-owned untracked policy paths are added to the
  worktree exclude owner. ``info/`` is a common-dir path in git, so — measured,
  not assumed (:func:`~._worktree_runtime._worktree_exclude_files`) — the entry
  that takes effect is the repository's **shared** ``.git/info/exclude``. That
  is bounded, orchestrator-owned residue: the write is idempotent and names only
  paths the repository does not track.

**What this module does not decide.** It does not choose which providers get a
guard, and it does not decide what an unestablished guard means for a launch.
Those belong to each principal's owner — ``GUARDABLE_PLANNING_PROVIDERS`` and
the ADR-0031 launch owner for planning, the reviewer guard installer and
``create_reviewer_worktree`` for the reviewer — because the consequence of "no
barrier here" is a different decision for each of them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from ...infra.hooks.gate_commands import ArgvPattern, codex_argv_patterns
from ...ports.command_guard import GuardProbe
from ..hooks.codex import CodexAdapter
from ..hooks.codex_execpolicy import (
    ExecPolicyChecker,
    ExecPolicyOutcome,
    ExecPolicyResultError,
)
from ._worktree_runtime import _write_worktree_exclude_entries

logger = logging.getLogger(__name__)

__all__ = [
    "CODEX_SAFETY_RULES",
    "SAFETY_REFUSED_SAMPLES",
    "CodexGatePolicy",
    "CodexGatePolicyError",
    "EstablishedCodexGatePolicy",
]

#: Worktree-relative safety policy the shipped Codex hook template installs.
#: Named here so a principal's installer can put its refusal *beside* it rather
#: than in place of it.
CODEX_SAFETY_RULES = Path(".codex") / "rules" / "orchestrator.rules"

#: What the *composed* safety policy must still refuse once it has been placed
#: beside a principal's policy. #289 acceptance 7 says the shipped no-bypass /
#: no-merge rules compose with the scoped refusal rather than being replaced by
#: it — and this module's own standard is that a written file is not evidence of
#: a barrier. Copying ``orchestrator.rules`` in and returning would hold the
#: safety half to exactly the weaker standard the scoped half is not allowed to
#: use, so it is put to the same checker in the same pass.
SAFETY_REFUSED_SAMPLES: tuple[tuple[str, ...], ...] = (
    ("git", "push", "--no-verify"),
    ("gh", "pr", "merge"),
)


class CodexGatePolicyError(RuntimeError):
    """A Codex worktree policy that should have been established was not.

    Raised for a write failure, a safety-composition failure, a sample the
    enforcing mechanism classified the wrong way, and an answer it could not
    classify at all. Each principal translates it into the failure its own
    caller contract already speaks; none of them may translate it into success.
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


@dataclass(frozen=True)
class EstablishedCodexGatePolicy:
    """The verified policy one launch got, and the answers that verified it."""

    policy_file: Path
    probes: tuple[GuardProbe, ...]


@dataclass(frozen=True)
class CodexGatePolicy:
    """One principal's Codex gate policy: what it writes, and what it proves.

    ``name`` is what failures call this policy, so an operator reading a failed
    launch is told which principal's barrier did not take.

    ``refused_samples`` and ``allowed_samples`` are pinned by the principal
    rather than sampled from the vocabulary they are checking: a probe set
    derived from the same table it is measuring would agree with itself even
    after the table stopped meaning anything. ``allowed_samples`` is not
    optional in spirit — a policy that refuses by refusing everything must fail
    to establish, or enforcement turns its principal into a no-tools role.
    """

    name: str
    rules_path: Path
    header: str
    justification: str
    refused_samples: tuple[tuple[str, ...], ...]
    allowed_samples: tuple[tuple[str, ...], ...]

    def render(self) -> str:
        """Render the whole policy file, deterministically."""
        body = "\n".join(
            _render_rule(pattern, self.justification)
            for pattern in codex_argv_patterns()
        )
        return f"{self.header}\n{body}"

    def establish(
        self, worktree_path: Path, *, execpolicy: ExecPolicyChecker
    ) -> EstablishedCodexGatePolicy:
        """Write, compose, verify and hide this policy in ``worktree_path``.

        Raises:
            CodexGatePolicyError: the policy could not be written, the shipped
                safety rules could not be placed beside it, or the enforcing
                mechanism did not classify a pinned sample the way the policy
                claims to.
        """
        policy_file = Path(worktree_path) / self.rules_path
        safety_file = Path(worktree_path) / CODEX_SAFETY_RULES
        self._write(policy_file)
        self._compose_safety_rules(Path(worktree_path), execpolicy)
        probes = self._verify(policy_file, safety_file, execpolicy)
        _write_worktree_exclude_entries(
            Path(worktree_path), [self.rules_path, CODEX_SAFETY_RULES]
        )
        logger.info(
            "[codex-gate-policy] established: policy=%s worktree=%s refuses=%d "
            "allows=%d",
            self.name,
            worktree_path,
            len([probe for probe in probes if probe.refused]),
            len([probe for probe in probes if not probe.refused]),
        )
        return EstablishedCodexGatePolicy(policy_file=policy_file, probes=probes)

    def _write(self, policy_file: Path) -> None:
        try:
            policy_file.parent.mkdir(parents=True, exist_ok=True)
            policy_file.write_text(self.render(), encoding="utf-8")
        except OSError as exc:
            raise CodexGatePolicyError(
                f"Failed to write the {self.name} at {policy_file}: {exc}"
            ) from exc

    def _compose_safety_rules(
        self, worktree_path: Path, execpolicy: ExecPolicyChecker
    ) -> None:
        """Put the shipped Codex safety policy beside this one.

        Codex resolves this worktree as its own project root, so the product
        checkout's ``orchestrator.rules`` is not in scope for a session running
        here. Codex loads every ``.rules`` file in the directory, so composition
        is a matter of both files being present — and what the copy actually
        refuses is measured by :meth:`_verify`, so a safety file that arrived
        empty, truncated or superseded fails the launch instead of riding along
        unexamined.
        """
        try:
            CodexAdapter(execpolicy=execpolicy).install_hooks(worktree_path)
        except OSError as exc:
            raise CodexGatePolicyError(
                "Failed to install the Codex safety rules alongside the "
                f"{self.name} in {worktree_path}: {exc}"
            ) from exc

    def _verify(
        self, policy_file: Path, safety_file: Path, execpolicy: ExecPolicyChecker
    ) -> tuple[GuardProbe, ...]:
        """Ask the enforcing mechanism how it classifies every pinned sample.

        Both files in ``.codex/rules/`` are measured, each against the samples
        it owns: this principal's policy must refuse the gate vocabulary and
        keep the commands the principal's role needs, and the composed safety
        policy must still refuse the no-bypass / no-merge entry points it exists
        for.
        """
        plan: tuple[tuple[Path, tuple[str, ...], bool], ...] = (
            *((policy_file, sample, True) for sample in self.refused_samples),
            *((policy_file, sample, False) for sample in self.allowed_samples),
            *((safety_file, sample, True) for sample in SAFETY_REFUSED_SAMPLES),
        )
        return tuple(
            self._probe(execpolicy, rules_file, command, expect_refused)
            for rules_file, command, expect_refused in plan
        )

    def _probe(
        self,
        execpolicy: ExecPolicyChecker,
        rules_file: Path,
        command: tuple[str, ...],
        expect_refused: bool,
    ) -> GuardProbe:
        label = " ".join(command)
        try:
            outcome = execpolicy.check(rules_file, command)
        except ExecPolicyResultError as exc:
            raise CodexGatePolicyError(
                f"The {self.name} at {rules_file} gave no classifiable answer "
                f"for {label!r}: {exc}"
            ) from exc
        refused = outcome is ExecPolicyOutcome.FORBIDDEN
        if refused is not expect_refused:
            wanted = "refuse" if expect_refused else "allow"
            raise CodexGatePolicyError(
                f"The {self.name} at {rules_file} does not {wanted} {label!r} "
                f"(execpolicy answered {outcome.value})"
            )
        return GuardProbe(command=tuple(command), refused=refused)
