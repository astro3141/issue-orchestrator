"""Guardrails keeping the three tech_lead prompt variants on the artifact contract.

#6761 finding 10: the no-manifest ("no PRs") path used to instruct a bare
``coding-done`` completion, which guarantees a ``missing_decision`` rejection
under the mandatory-pair rule. Every prompt variant must instead show the
minimal valid empty-audit pair written BEFORE ``coding-done``, and the JSON it
shows must actually validate against the domain contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.tech_lead_issue_policy import (
    protected_tech_lead_label_violations,
)
from issue_orchestrator.domain.tech_lead_artifacts import (
    MAX_ACTION_BODY_CHARS,
    MAX_AREA_CHARS,
    MAX_EVIDENCE_REFS,
    MAX_LABEL_CHARS,
    MAX_LABELS_PER_ACTION,
    MAX_PATTERN_SIGNATURE_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_TECH_LEAD_ACTIONS,
    MAX_TECH_LEAD_FINDINGS,
    MAX_TITLE_CHARS,
    TechLeadDecision,
)
from issue_orchestrator.entrypoints.setup_wizard_prompts import (
    build_tech_lead_review_prompt_text,
)
from issue_orchestrator.infra.config import Config

REPO_ROOT = Path(__file__).resolve().parents[2]

PROMPT_VARIANTS = {
    "setup_wizard": build_tech_lead_review_prompt_text("tech-lead-review", "tech-lead-reviewed"),
    "examples": (REPO_ROOT / "examples" / "prompts" / "tech-lead-review.md").read_text(),
    "repo_specific": (REPO_ROOT / "repo-specific" / "prompts" / "tech-lead.md").read_text(),
}

NO_MANIFEST_MARKER = "**If the manifest is missing or lists no PRs:**"


def _no_manifest_block(text: str) -> str:
    """The no-manifest instructions up to the next section heading."""
    assert NO_MANIFEST_MARKER in text, "no-manifest path missing from prompt"
    start = text.index(NO_MANIFEST_MARKER)
    match = re.search(r"\n#{2,3} ", text[start:])
    end = start + match.start() if match else len(text)
    return text[start:end]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_no_manifest_path_writes_pair_before_coding_done(variant: str) -> None:
    block = _no_manifest_block(PROMPT_VARIANTS[variant])

    assert "tech-lead-decision.json" in block
    assert "tech-lead-report.md" in block
    assert "before completing" in block
    # The pair-writing instructions come BEFORE the completion command.
    assert "coding-done" in block
    assert block.index("tech-lead-decision.json") < block.index("coding-done completed")


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_no_manifest_empty_audit_json_is_contract_valid(variant: str) -> None:
    block = _no_manifest_block(PROMPT_VARIANTS[variant])
    match = re.search(r"<<'JSON'\n(.*?)\nJSON\n", block, re.DOTALL)
    assert match, "empty-audit heredoc JSON missing from no-manifest path"

    decision = TechLeadDecision.from_agent_payload(json.loads(match.group(1)))

    assert decision.findings == ()
    assert decision.proposed_actions == ()
    assert decision.summary


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_compact_decision_example_is_contract_valid(variant: str) -> None:
    """The worked example must parse (canonical ids, evidence, allowed labels)."""
    text = PROMPT_VARIANTS[variant]
    blocks = [
        block
        for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        if "proposed_actions" in block
    ]
    assert blocks, "compact tech-lead-decision.json example missing"

    decision = TechLeadDecision.from_agent_payload(json.loads(blocks[0]))

    assert decision.findings, "example should demonstrate a finding"
    assert all(finding.evidence for finding in decision.findings)
    config = Config()
    labels = LabelManager(config)
    for action in decision.proposed_actions:
        assert (
            protected_tech_lead_label_violations(
                action.labels, config=config, labels=labels
            )
            == []
        ), f"{variant} example proposes protected labels"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_machine_field_bounds_are_documented(variant: str) -> None:
    """Agent-visible limits must not remain a completion-time surprise."""
    text = " ".join(PROMPT_VARIANTS[variant].split())
    expected_phrases = (
        f"`title` values are at most **{MAX_TITLE_CHARS:,} characters**",
        f"`summary` is at most {MAX_SUMMARY_CHARS:,} characters",
        f"`body` is at most {MAX_ACTION_BODY_CHARS:,} characters",
        f"at most {MAX_EVIDENCE_REFS:,} evidence references",
        f"at most {MAX_LABELS_PER_ACTION:,} labels",
        f"label is at most {MAX_LABEL_CHARS:,} characters",
        f"`pattern_signature` is at most {MAX_PATTERN_SIGNATURE_CHARS:,} characters",
        f"`area` is at most {MAX_AREA_CHARS:,} characters",
        f"at most {MAX_TECH_LEAD_FINDINGS:,} findings",
        f"{MAX_TECH_LEAD_ACTIONS:,} proposed actions",
    )
    for phrase in expected_phrases:
        assert phrase in text, f"{variant} does not document bound: {phrase}"
    assert "full explanation" in text


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_investigation_flavor_documents_focus_comment_rule(variant: str) -> None:
    """Finding 2's rule is orchestrator-enforced; the prompts must teach it."""
    text = PROMPT_VARIANTS[variant]
    assert "focus_issue_number" in text
    marker = text.index('"flavor": "failure_investigation"')
    # Scope to the failure-investigation flow (marker -> next level-2 heading)
    # rather than a fixed char window: the flow legitimately grew when the
    # tech-lead investigation rubric + evidence-map guidance were baked in, but
    # the focus post_comment rule must still live somewhere in that flow.
    rest = text[marker:]
    next_section = re.search(r"\n## (?!#)", rest)
    bullet = rest[: next_section.start()] if next_section else rest
    assert "post_comment" in bullet
    assert "target_number" in bullet


# --- Per-flavor flow isolation (#6763 finding 1) ---------------------------
#
# A health or failure-investigation session gets no PR manifest. If the
# manifest-read step or the "Empty batch" artifact-pair fallback lives in a
# section those flavors are told to follow, the session either fails on its
# intentionally absent manifest or publishes an empty-batch result instead of
# walking the board. Each prompt source must therefore isolate every
# batch-only instruction inside the Batch Review Flow. These strings are the
# unambiguous batch-only tells: the `manifest.json` read step and the literal
# "Empty batch" summary the empty-audit fallback writes.
_BATCH_ONLY_TELLS = ("manifest.json", "Empty batch")


def _flow_section(text: str, heading: str) -> str:
    """The named ``## <heading>`` flow, up to the next level-2 heading.

    Level-3 subsections (``### 1. Read the Manifest`` etc.) belong to their
    parent flow, so the section runs until the next ``## `` that is not
    ``### ``.
    """
    marker = f"## {heading}"
    assert marker in text, f"{heading!r} section missing"
    start = text.index(marker)
    rest = text[start + len(marker) :]
    match = re.search(r"\n## (?!#)", rest)
    end = start + len(marker) + (match.start() if match else len(rest))
    return text[start:end]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_all_three_flavor_flows_are_present(variant: str) -> None:
    """The per-flavor structure the guardrails rely on exists in every source."""
    text = PROMPT_VARIANTS[variant]
    for heading in (
        "Batch Review Flow",
        "Failure Investigation Flow",
        "Health Review Flow",
    ):
        assert f"## {heading}" in text, f"{variant} missing '## {heading}'"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_still_carries_the_manifest_and_empty_batch_steps(
    variant: str,
) -> None:
    """Non-vacuity: the tells the other flows must NOT contain live in batch."""
    batch = _flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow")
    for tell in _BATCH_ONLY_TELLS:
        assert tell in batch, f"{variant} batch flow lost the {tell!r} step"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
@pytest.mark.parametrize(
    "heading", ["Health Review Flow", "Failure Investigation Flow"]
)
def test_non_batch_flows_contain_no_batch_only_instructions(
    variant: str, heading: str
) -> None:
    """A no-manifest flavor never inherits a manifest-read or empty-batch step."""
    section = _flow_section(PROMPT_VARIANTS[variant], heading)
    for tell in _BATCH_ONLY_TELLS:
        assert tell not in section, (
            f"{variant} '{heading}' contains batch-only instruction {tell!r}"
        )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_act_level_wiring_state_is_synchronized(variant: str) -> None:
    """#6764/#6778: every variant must document the gated-proposal tier
    (propose = reviewable issue, approval = removing the gate label), the
    wired reset_retry execute authority, and the agent's prohibition on the
    gate label itself."""
    text = PROMPT_VARIANTS[variant]
    assert "`tech_lead.authority.reset_retry: execute`" in text, (
        f"{variant} does not document the wired reset_retry authority"
    )
    assert "`proposed-tech-lead`" in text, (
        f"{variant} does not document the gated-proposal label"
    )
    assert "removing that label" in text, (
        f"{variant} does not document label removal as the approval gesture"
    )
    assert "Never propose or\n  touch the `proposed-tech-lead` label" in text, (
        f"{variant} does not forbid the agent from touching the gate label"
    )
    # The pre-#6778 shadow-only claim must be gone from every variant.
    assert "recorded as would-have-done until its" not in text, (
        f"{variant} still claims kill_hung_session is shadow-only"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_step_back_mandate_synchronized(variant: str) -> None:
    """#6781 amendment: every prompt variant must teach the durable case-file
    contract (flag_pattern needs a stable signature) AND the step-back
    mandate — a recurring same-signature pattern escalates to a deeper
    root-cause design-review issue, not another observation."""
    text = PROMPT_VARIANTS[variant]
    assert "pattern_signature" in text, (
        f"{variant} does not document the required flag_pattern signature"
    )
    assert "case file" in text, (
        f"{variant} does not document the durable case file"
    )
    assert "Step back on recurrence" in text, (
        f"{variant} does not teach the step-back mandate"
    )
    assert "root-cause design review" in text, (
        f"{variant} does not name the root-cause design-review escalation"
    )
    assert "mandate to" in text, (
        f"{variant} does not frame recurrence as a root-cause mandate"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_restart_safe_shipped_fix_evidence_is_synchronized(variant: str) -> None:
    text = PROMPT_VARIANTS[variant]
    assert "recent_shipped_fixes" in text
    assert "issue/PR" in text


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_health_flow_teaches_cohort_scoped_act_level_authority(
    variant: str,
) -> None:
    """#6780: the prompt must match the runtime act-level scope rule.

    The orchestrator records a health review's act-level authority from its
    OWNED cohort and rejects `reset_retry`/`kill_hung_session` proposals for
    anything outside it. The prompt used to tell the agent that act-level
    proposals "may only target THIS tracking issue", which made a storm
    review's whole reason for existing unusable: it could be authorized for
    #41/#42/#43 and still be instructed never to propose for them.
    """
    section = _flow_section(PROMPT_VARIANTS[variant], "Health Review Flow")

    # The superseded rule must be gone, or the agent is told not to use its
    # own authority.
    assert "act-level) may\n  only target THIS tracking issue" not in section, (
        f"{variant} still forbids act-level proposals for the owned cohort"
    )
    # Anchor-scoped proposals stay anchor-scoped.
    assert "`post_comment`/`escalate_to_human` may only target THIS tracking" in (
        section
    ), f"{variant} no longer scopes post_comment/escalate_to_human to the anchor"
    # Act-level scope is the cohort surface, named exactly as the snapshot
    # field the orchestrator writes and validates against.
    assert "problem_cohort" in section, (
        f"{variant} does not name the problem_cohort act-level scope"
    )
    for act_level in ("reset_retry", "kill_hung_session"):
        assert act_level in section, (
            f"{variant} does not name {act_level} in the health flow"
        )
    # An empty cohort grants nothing (the periodic-review case).
    assert "EMPTY `problem_cohort`" in section, (
        f"{variant} does not teach that an empty cohort grants no act-level targets"
    )
    # recent_failures is context, never authority.
    assert "`recent_failures` is CONTEXT, not authority" in section, (
        f"{variant} does not warn that recent_failures is not authority"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_generic_target_scope_rule_matches_the_two_scope_runtime_split(
    variant: str,
) -> None:
    """#6780 round-2 F1: the GENERIC artifact rule must not contradict the flow.

    ``test_health_flow_teaches_cohort_scoped_act_level_authority`` only reads
    the Health Review Flow section, so the later generic rule under Required
    Output Artifacts kept telling every agent that act-level proposals "may
    only target the manifest PRs or your own tracking issue ... or the
    `focus_issue_number`. Any other target is rejected." A storm review could
    therefore be launched holding cohort authority and still be instructed
    that its authorized cohort targets were invalid.

    The rule mirrors the runtime two-scope split
    (``TechLeadLaunchAuthority.allowed_targets`` vs
    ``allowed_act_level_targets``), which is DISJOINT for a health review:
    anchor for post_comment/escalate_to_human, cohort for act-level.
    """
    section = _flow_section(
        PROMPT_VARIANTS[variant], "Required Output Artifacts (MANDATORY)"
    )

    # The superseded single-scope rule must be gone: it lumped the act-level
    # verbs in with the anchor-scoped ones and never named the cohort.
    assert "and `kill_hung_session` may only\n  target the manifest PRs" not in (
        section
    ), f"{variant} generic rule still forbids the health review's cohort targets"
    # The generic rule must name the health-review act-level authority.
    assert "`problem_cohort` (health review)" in section, (
        f"{variant} generic rule does not name problem_cohort act-level authority"
    )
    # Anchor-scoped verbs stay anchor-scoped, including in a health review.
    assert "THIS tracking issue (health review)" in section, (
        f"{variant} generic rule does not keep post_comment anchor-scoped"
    )
    # A batch review's act-level scope is empty at runtime (frozenset()), so
    # the prompt must not invite a manifest-PR/anchor reset (#6764 F1).
    assert "no act-level target at all" in section, (
        f"{variant} generic rule does not teach that batch owns no act-level target"
    )


def test_generic_rule_act_level_verbs_match_the_domain_contract() -> None:
    """The generic rule names the REAL act-level verbs, not a drifted alias.

    Pins the prompt's act-level list to ``ACT_LEVEL_TECH_LEAD_ACTIONS`` so a new
    act-level action type cannot be added to the runtime scope check while the
    generic prompt rule silently keeps teaching the old pair.
    """
    from issue_orchestrator.domain.tech_lead_artifacts import ACT_LEVEL_TECH_LEAD_ACTIONS

    for variant, text in sorted(PROMPT_VARIANTS.items()):
        section = _flow_section(text, "Required Output Artifacts (MANDATORY)")
        act_level_rule = section[section.index("- Act-level ") :]
        for action_type in sorted(ACT_LEVEL_TECH_LEAD_ACTIONS):
            assert f"`{action_type}`" in act_level_rule, (
                f"{variant} generic act-level rule does not name {action_type}"
            )


def _normalized(text: str) -> str:
    """Whitespace-collapsed text, so a re-wrapped prompt line still matches."""
    return " ".join(text.split())


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_generic_rule_teaches_the_role_capability_axis(variant: str) -> None:
    """#133: the agent must be told the axis its decision is rejected on.

    Capability is a SECOND axis, independent of target scope: a kind outside
    the launched flavor's row is rejected before authority translation, taking
    every sibling action in the decision with it. The prompt used to state a
    single flat ``Valid action_type values`` list for all three flavors, which
    would advertise `reset_retry`/`kill_hung_session` to a role that may not
    propose them at all.
    """
    section = _flow_section(
        PROMPT_VARIANTS[variant], "Required Output Artifacts (MANDATORY)"
    )
    normalized = _normalized(section)

    # The superseded flat list must be gone: it told every role the same set.
    assert "Valid `action_type` values: `post_comment`" not in normalized, (
        f"{variant} still states one flat action_type list for every role"
    )
    # The rule keys off the role, named as the assignment field the
    # orchestrator actually reads.
    assert "set by your ROLE" in normalized and "`flavor` in your assignment" in (
        normalized
    ), f"{variant} does not key the valid action kinds off the session's flavor"
    # It is a separate axis from the target scope rule above it.
    assert "separate rule from the target scope" in normalized, (
        f"{variant} does not distinguish capability from target scope"
    )
    # A forbidden kind costs the agent the whole decision, not just the action.
    assert "rejects the WHOLE decision, every sibling action included" in (
        normalized
    ), f"{variant} does not teach that one forbidden kind rejects everything"


def test_capability_rows_match_the_domain_table() -> None:
    """The per-role rows ARE the shipped capability table, not a restatement.

    Pins each prompt variant's rendered row to
    ``TECH_LEAD_ACTION_CAPABILITIES`` the way the act-level verbs are pinned to
    ``ACT_LEVEL_TECH_LEAD_ACTIONS``: narrowing a role's row in the domain owner
    without updating the prompt (or vice versa) fails here, so the agent can
    never be handed a kind the completion contract rejects.
    """
    from issue_orchestrator.domain.tech_lead_capabilities import (
        TECH_LEAD_ACTION_CAPABILITIES,
    )

    table = TECH_LEAD_ACTION_CAPABILITIES.describe_by_flavor()
    assert table, "capability table is empty"

    for variant, text in sorted(PROMPT_VARIANTS.items()):
        section = _flow_section(text, "Required Output Artifacts (MANDATORY)")
        normalized = _normalized(section)
        for flavor, kinds in table:
            row = f"- `{flavor.value}`: " + ", ".join(f"`{kind}`" for kind in kinds)
            assert row in normalized, (
                f"{variant} does not state {flavor.value}'s capability row"
                f" exactly as the domain table declares it: expected {row!r}"
            )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_board_snapshot_fields_document_the_cohort_surface(variant: str) -> None:
    """The snapshot field list must distinguish context from authority."""
    text = PROMPT_VARIANTS[variant]
    assert "`problem_cohort` (the issue" in text, (
        f"{variant} does not document the problem_cohort field"
    )
    assert "`recent_failures` (context)" in text, (
        f"{variant} does not mark recent_failures as context"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_board_snapshot_fields_document_e2e_health(variant: str) -> None:
    """The snapshot field list must surface the aggregate E2E-health signal."""
    assert "`e2e_health`" in PROMPT_VARIANTS[variant], (
        f"{variant} does not document the e2e_health snapshot field"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_health_flow_teaches_e2e_suite_health_assessment(variant: str) -> None:
    """The health review must assess E2E as a SYSTEM (ADR-0031).

    E2E health is easy to neglect — it runs on a slow ungoverned cadence and
    rots unwatched — so every variant's Health Review Flow must teach reading
    `e2e_health` (cadence/streak/chronic) and routing an off-cadence or
    chronically-red suite, and untracked/stale chronic failures, to findings.
    """
    section = _flow_section(PROMPT_VARIANTS[variant], "Health Review Flow")
    for token in (
        "e2e_health",
        "nonpassing_streak",
        "chronic_failures",
        "tracking_issue",
        "e2e suite health",
        "easy to neglect",
    ):
        assert token in section, (
            f"{variant} health flow does not teach the e2e-health token {token!r}"
        )


def test_prompt_e2e_health_rule_matches_the_snapshot_contract() -> None:
    """The prompt names the REAL serialized field, not a drifted alias.

    Pins ``e2e_health`` to the field ``BoardSnapshot.to_dict`` actually writes
    so a rename cannot leave the prompt pointing at a field that never exists.
    """
    from issue_orchestrator.domain.board_snapshot import BoardSnapshot

    snapshot = BoardSnapshot(
        generated_at="2026-07-15T00:00:00",
        orchestrator_paused=False,
    )
    assert "e2e_health" in snapshot.to_dict()


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_board_snapshot_fields_document_hung_evidence(variant: str) -> None:
    """The snapshot field list must surface the per-session hung-evidence."""
    text = PROMPT_VARIANTS[variant]
    for token in ("`idle_minutes`", "`commits_ahead`"):
        assert token in text, (
            f"{variant} does not document the {token} snapshot field"
        )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_health_flow_teaches_evidence_based_hung_judgment(variant: str) -> None:
    """The health review must judge HUNG from EVIDENCE, not age alone (#6823).

    A session hung is judged from idle + no-progress evidence corroborated by
    the run dir/recording, NOT a timer — "take a look, don't kill prematurely".
    A long-running-but-working session (fresh output or landing commits) is not
    hung. The GATED ``kill_hung_session`` follows only from that evidence.
    """
    section = _flow_section(PROMPT_VARIANTS[variant], "Health Review Flow")
    for token in (
        "idle_minutes",
        "commits_ahead",
        "age_minutes` alone",
        "WORKING, not hung",
        "kill_hung_session",
        "prematurely",
    ):
        assert token in section, (
            f"{variant} health flow does not teach the hung-evidence token {token!r}"
        )


def test_prompt_hung_evidence_rule_matches_the_snapshot_contract() -> None:
    """The prompt names the REAL serialized session fields, not drifted aliases.

    Pins ``idle_minutes``/``commits_ahead`` to what ``BoardSnapshot.to_dict``
    writes per active session, so a rename cannot leave the rubric pointing at
    evidence fields the board never carries.
    """
    from issue_orchestrator.domain.board_snapshot import (
        BoardSessionInfo,
        BoardSnapshot,
    )

    snapshot = BoardSnapshot(
        generated_at="2026-07-15T00:00:00",
        orchestrator_paused=False,
        sessions=[
            BoardSessionInfo(
                issue_number=1,
                issue_title="t",
                agent_type="",
                session_type="code",
                status="running",
                started_at="2026-07-15T00:00:00",
                age_minutes=1,
                terminal_id="issue-1",
            )
        ],
    )
    session = snapshot.to_dict()["sessions"][0]
    assert "idle_minutes" in session
    assert "commits_ahead" in session


def test_prompt_cohort_rule_matches_the_snapshot_contract() -> None:
    """The prompt names the REAL serialized field, not a drifted alias.

    A guardrail asserting on a field name the orchestrator never writes would
    pass while the agent looked for something that does not exist.
    """
    from issue_orchestrator.domain.board_snapshot import BoardSnapshot

    snapshot = BoardSnapshot(
        generated_at="2026-07-15T00:00:00",
        orchestrator_paused=False,
        problem_cohort=[41],
    )
    assert "problem_cohort" in snapshot.to_dict()
    assert snapshot.problem_issue_numbers() == frozenset({41})


def _dedup_clause(text: str) -> str:
    """The bounded create_issue-dedup paragraph, lowercased.

    Scoping the semantic assertions to this clause (not the whole multi-flow
    prompt) is the point: words like "verif"/"gated" occur in unrelated sections
    (setup-wizard "Trust but verify", act-level GATED-proposal text), so a
    whole-text check would stay green even if the dedup clause lost its meaning.
    """
    start = text.index("Do not file a duplicate")
    marker = "only valid on `create_issue`"
    end = text.index(marker, start) + len(marker)
    return text[start:end].lower()


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_all_variants_teach_the_duplicate_of_dedup_field(variant: str) -> None:
    """#6878 B4: every prompt variant must teach create_issue dedup with the final
    verify-or-gate semantics — not the old unconditional comment-routing promise
    (production withholds auto-routing until increment 2). Asserted WITHIN the
    bounded dedup clause so unrelated prose can't satisfy it, in both directions."""
    clause = _dedup_clause(PROMPT_VARIANTS[variant])
    assert "duplicate_of" in clause, f"{variant} dedup clause omits duplicate_of"
    assert "untrusted" in clause, f"{variant} does not mark duplicate_of as untrusted intent"
    assert "verif" in clause, f"{variant} dedup clause omits verification semantics"
    assert "gated" in clause, f"{variant} dedup clause omits gating semantics"
    assert "candidate" in clause and "preserv" in clause, (
        f"{variant} dedup clause omits candidate preservation"
    )
    assert "title" in clause and "body" in clause, (
        f"{variant} dedup clause drops the title/body requirement"
    )
    assert (
        "instead of filing a duplicate" not in clause
    ), f"{variant} dedup clause still promises unconditional comment routing"


def _fix_class_clause(text: str) -> str:
    """The bounded flag_pattern fix-classification paragraph, lowercased."""
    start = text.index("Classify every `flag_pattern` with `fix_class`")
    marker = "may carry `fix_class`"
    end = text.index(marker, start) + len(marker)
    return text[start:end].lower()


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_all_variants_teach_the_fix_class_promotion_gate(variant: str) -> None:
    """#6957: promotion only ever acts on findings the tech lead classified
    fix:code, so every prompt variant must teach the field, both values, that
    fix:human is never promoted, and that omitting it is the honest default.
    Asserted within the bounded clause so unrelated prose can't satisfy it."""
    clause = _fix_class_clause(PROMPT_VARIANTS[variant])
    assert "fix_class" in clause
    assert '"code"' in clause and '"human"' in clause
    assert "flag_pattern" in clause, f"{variant} does not scope fix_class to flag_pattern"
    assert "never" in clause, f"{variant} omits the fix:human promotion exclusion"
    assert "omit" in clause, f"{variant} does not teach omitting an unknown classification"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_fix_class_values_match_the_domain_contract(variant: str) -> None:
    """The prompt's vocabulary must be exactly what the artifact parser accepts —
    a value taught here but rejected there is a guaranteed contract violation."""
    from issue_orchestrator.domain.tech_lead_findings import (
        VALID_FINDING_FIX_CLASSES,
    )

    clause = _fix_class_clause(PROMPT_VARIANTS[variant])
    for value in VALID_FINDING_FIX_CLASSES:
        assert f'`"{value}"`' in clause, f"{variant} omits fix_class value {value}"


# --- Planning Investigation Flow (#261) ------------------------------------
#
# These guardrails are scoped to the DEPLOYED prompt on purpose. The planning
# lane runs `repo-specific/prompts/tech-lead.md` — the file this repository's
# `.issue-orchestrator/config/modes/default/main.yaml` hands `agent:tech-lead`
# — and #261's contract is about what that shipped prompt tells the already
# authorized `planning_investigation` role to DO. Nothing here widens runtime
# authority: the capability row, the target scope, and the label rule are all
# pinned below to the domain owners that already enforce them.
#
# Before #261 the shipped prompt told the planning role it "has no flow of its
# own yet" and to use `escalate_to_human` to hand the preparation question to a
# person — the exact Human relay the bounded planning lane exists to remove. An
# agent obeying that prompt could correctly refuse to prepare anything.

DEPLOYED_PROMPT = PROMPT_VARIANTS["repo_specific"]

PLANNING_ASSIGNMENT_MARKER = "- **`planning_investigation`**"


def _planning_flow() -> str:
    """The deployed `## Planning Investigation Flow` section."""
    return _flow_section(DEPLOYED_PROMPT, "Planning Investigation Flow")


def _planning_assignment_entry() -> str:
    """The `planning_investigation` bullet of the Your Assignment list.

    Bounded to the bullet (it ends at the next unindented line) so an
    assertion about what the ASSIGNMENT says cannot be satisfied by prose
    from the flow section far below it.
    """
    assert PLANNING_ASSIGNMENT_MARKER in DEPLOYED_PROMPT, (
        "planning_investigation assignment entry missing from the deployed prompt"
    )
    start = DEPLOYED_PROMPT.index(PLANNING_ASSIGNMENT_MARKER)
    rest = DEPLOYED_PROMPT[start + len(PLANNING_ASSIGNMENT_MARKER) :]
    match = re.search(r"\n(?=\S)", rest)
    end = start + len(PLANNING_ASSIGNMENT_MARKER) + (
        match.start() if match else len(rest)
    )
    return DEPLOYED_PROMPT[start:end]


def test_planning_flow_exists_and_the_assignment_points_at_it() -> None:
    """Acceptance 1: the flow is reachable from the assignment entry."""
    assert "## Planning Investigation Flow" in DEPLOYED_PROMPT, (
        "deployed prompt has no dedicated Planning Investigation Flow"
    )
    entry = _planning_assignment_entry()
    assert "Planning Investigation Flow" in entry, (
        "the planning_investigation assignment entry does not point at its flow"
    )
    assert "no flow of its own" not in DEPLOYED_PROMPT, (
        "deployed prompt still tells planning_investigation it has no flow"
    )


def test_planning_assignment_no_longer_defaults_to_a_human_relay() -> None:
    """Acceptance 3: routine preparation is not handed to a person."""
    entry = _normalized(_planning_assignment_entry())
    assert "escalate_to_human" not in entry, (
        "the planning assignment entry still routes preparation to a human"
    )
    assert "hand the preparation question to a person" not in _normalized(
        DEPLOYED_PROMPT
    ), "the superseded default-human-relay instruction is still shipped"


def test_planning_flow_names_create_issue_as_the_normal_bounded_output() -> None:
    """Acceptance 2 and 5: one bounded, self-contained leaf is the result."""
    flow = _normalized(_planning_flow())
    assert "The normal successful output is exactly ONE bounded `create_issue`" in (
        flow
    ), "the planning flow does not name create_issue as its normal output"
    assert "self-contained" in flow, (
        "the planning flow does not require a self-contained issue body"
    )
    for token in (
        "governing provenance",
        "acceptance criteria",
        "non-goals",
        "failure direction",
        "measured seam",
    ):
        assert token in flow, (
            f"the planning flow does not require {token!r} in the proposed leaf"
        )


def test_planning_flow_requires_an_unscheduled_leaf() -> None:
    """Acceptance 6: a successful run must leave the leaf unscheduled."""
    flow = _normalized(_planning_flow())
    assert "UNSCHEDULED" in flow, (
        "the planning flow does not require the proposed leaf to stay unscheduled"
    )
    assert "`agent:*`" in flow, (
        "the planning flow does not forbid the agent:* scheduling label"
    )
    assert "workflow-control label" in flow, (
        "the planning flow does not forbid workflow-control labels generally"
    )


def test_planning_scheduling_label_ban_matches_the_runtime_label_rule() -> None:
    """The prompt forbids what the completion contract actually rejects.

    Pins the `agent:*` ban the planning flow teaches to
    ``protected_tech_lead_label_violations`` — the check a `create_issue`
    proposal is judged by — so the prompt cannot end up forbidding a label the
    runtime accepts, or (worse) permitting one it rejects.
    """
    config = Config()
    labels = LabelManager(config)
    assert protected_tech_lead_label_violations(
        ("agent:backend",), config=config, labels=labels
    ), "runtime no longer rejects agent:* labels the planning flow forbids"
    assert (
        protected_tech_lead_label_violations(
            ("enhancement",), config=config, labels=labels
        )
        == []
    ), "runtime rejects the plain descriptive labels the planning flow asks for"


def test_planning_flow_reserves_escalation_for_an_authority_boundary() -> None:
    """Acceptance 3: escalation is the exception, not the default path."""
    flow = _normalized(_planning_flow())
    assert "`escalate_to_human` is reserved for a real authority boundary" in flow, (
        "the planning flow does not reserve escalation for an authority boundary"
    )
    assert "genuinely NEW strategy, policy or authority decision" in flow, (
        "the planning flow does not say what makes a question a human one"
    )
    assert "are NOT human questions" in flow, (
        "the planning flow does not exclude routine decomposition from escalation"
    )


def test_planning_flow_excludes_the_failure_investigation_procedure() -> None:
    """Acceptance 4: the failure rubric must not leak into planning."""
    flow = _normalized(_planning_flow())
    assert "Do NOT borrow the Failure Investigation Flow" in flow, (
        "the planning flow does not exclude the failure-investigation procedure"
    )
    assert "do NOT key your result on `validation.passed`" in flow, (
        "the planning flow does not exclude the validation.passed outcome rubric"
    )
    assert "code-candidate publication/validation gate" in flow, (
        "the planning flow does not exclude the code-candidate validation gate"
    )
    assert "healthy OPEN planning subject as a failed implementation" in flow, (
        "the planning flow does not warn against reading its subject as failed"
    )
    # Non-vacuity: the excluded rubric really does live in the failure flow, so
    # this guardrail keeps testing something after a rewrite of either section.
    failure = _normalized(_flow_section(DEPLOYED_PROMPT, "Failure Investigation Flow"))
    assert "`validation.passed`" in failure, (
        "the failure flow no longer keys on validation.passed; re-anchor the"
        " planning exclusion"
    )


def test_planning_flow_preserves_bounded_read_only_seam_measurement() -> None:
    """Acceptance 4: excluding the failure rubric must not blind the role."""
    flow = _normalized(_planning_flow())
    assert "READ-ONLY inspection is allowed" in flow, (
        "the planning flow no longer permits bounded read-only seam measurement"
    )
    assert "Do NOT edit product code, config or policy" in flow, (
        "the planning flow does not keep preparation separate from implementation"
    )


def test_planning_flow_requires_canonical_context_provenance() -> None:
    """Acceptance 5: provenance first, and the missing/clipped fail-safe."""
    flow = _normalized(_planning_flow())
    for token in (
        "canonical-context.json",
        "`issue_number`",
        "`updated_at`",
        "`body_sha256`",
        '`"staged": false`',
        "`comment_count`",
        "CLIPPED",
    ):
        assert token in flow, (
            f"the planning flow does not teach canonical provenance token {token!r}"
        )
    assert "If load-bearing evidence is missing or truncated, do NOT invent it" in (
        flow
    ), "the planning flow lost the missing/truncated-source fail-safe"
    assert "instead of converting it into a generic governance escalation" in flow, (
        "the planning flow does not keep an evidence blocker out of escalation"
    )


def test_planning_provenance_fields_match_the_staged_descriptor() -> None:
    """The prompt names the REAL descriptor fields, not drifted aliases.

    A guardrail asserting on field names the orchestrator never writes would
    pass while the agent looked for provenance that does not exist.
    """
    from issue_orchestrator.domain.canonical_context import (
        CANONICAL_CONTEXT_FILENAME,
        CanonicalContextSnapshot,
        CanonicalSource,
        CanonicalSourceKind,
    )

    snapshot = CanonicalContextSnapshot(
        subject_issue_number=7,
        sources=(
            CanonicalSource(
                kind=CanonicalSourceKind.SUBJECT,
                issue_number=7,
                required=True,
                fetched_at="2026-08-25T00:00:00",
                staged=True,
                updated_at="2026-08-24T00:00:00",
                body_sha256="a" * 64,
                comment_count=2,
            ),
        ),
    )
    source = snapshot.to_dict()["sources"][0]
    for field in ("issue_number", "updated_at", "body_sha256", "staged", "comment_count"):
        assert field in source, f"staged descriptor no longer carries {field!r}"
    assert CANONICAL_CONTEXT_FILENAME == "canonical-context.json"


def test_planning_flow_forbids_post_comment_as_the_leaf_substitute() -> None:
    """Acceptance 5/6: the plan may not be relayed through a comment."""
    flow = _normalized(_planning_flow())
    assert "`post_comment` is not a substitute for the leaf" in flow, (
        "the planning flow permits dumping the plan onto the subject instead"
    )
    assert "reconstruct into an issue" in flow, (
        "the planning flow does not forbid human reconstruction of the plan"
    )


def test_planning_flow_contains_no_batch_only_instructions() -> None:
    """A planning session gets no PR manifest; batch steps must not reach it."""
    flow = _planning_flow()
    for tell in _BATCH_ONLY_TELLS:
        assert tell not in flow, (
            f"planning flow contains batch-only instruction {tell!r}"
        )


def test_planning_flow_target_scope_matches_the_launch_authority() -> None:
    """Acceptance 7: the flow restates the runtime scope, it does not widen it."""
    from issue_orchestrator.domain.tech_lead_session import (
        TechLeadLaunchAuthority,
        TechLeadSessionFlavor,
    )

    authority = TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.PLANNING_INVESTIGATION,
        anchor_issue_number=99,
        focus_issue_number=42,
    )
    assert authority.allowed_targets() == frozenset({42})
    assert authority.allowed_act_level_targets() == frozenset()

    flow = _normalized(_planning_flow())
    assert (
        "`post_comment` and `escalate_to_human` may only target your"
        " `focus_issue_number`" in flow
    ), "the planning flow does not scope its comment/escalation targets"
    assert "You own no act-level target and no recovery kind" in flow, (
        "the planning flow does not state that it owns no act-level target"
    )


def test_planning_flow_recovery_exclusion_matches_the_capability_table() -> None:
    """Acceptance 7: recovery stays structurally unreachable for planning."""
    from issue_orchestrator.domain.tech_lead_capabilities import (
        TECH_LEAD_ACTION_CAPABILITIES,
    )
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionFlavor

    flavor = TechLeadSessionFlavor.PLANNING_INVESTIGATION
    assert not TECH_LEAD_ACTION_CAPABILITIES.permits_recovery(flavor)
    kinds = TECH_LEAD_ACTION_CAPABILITIES.allowed_kinds(flavor)
    assert "create_issue" in kinds, (
        "planning may no longer propose the create_issue leaf the flow teaches"
    )

    flow = _normalized(_planning_flow())
    for forbidden in sorted({"reset_retry", "kill_hung_session"} - set(kinds)):
        assert f"`{forbidden}`" in flow, (
            f"the planning flow does not name {forbidden} as outside its row"
        )


def test_planning_flow_routes_completion_through_the_decision_artifact() -> None:
    """Acceptance 5/9: the artifact asks for the issue, not report prose."""
    flow = _normalized(_planning_flow())
    assert "Write both required artifacts (below), then complete with" in flow, (
        "the planning flow does not require the mandatory artifact pair"
    )
    assert "`coding-done`" in flow, (
        "the planning flow does not complete through the coding-done path"
    )
    assert (
        "The decision artifact is what asks the orchestrator to create the issue"
        in flow
    ), "the planning flow does not name the decision artifact as the effect channel"


def test_generic_target_rule_names_the_planning_focus_scope() -> None:
    """The generic rule must not omit the planning role's only target.

    ``allowed_targets`` is the focus issue for BOTH issue-focused flavors, so a
    generic rule naming only the failure investigation would tell a planning
    agent its one legal comment target was out of scope.
    """
    section = _flow_section(
        DEPLOYED_PROMPT, "Required Output Artifacts (MANDATORY)"
    )
    normalized = _normalized(section)
    assert "planning investigation" in normalized, (
        "the generic target-scope rule does not name the planning focus scope"
    )


# --- Exact-candidate verdict axis (#345) -----------------------------------
#
# The orchestrator projects merge-facing authority from `candidate_verdicts`
# alone. A prompt variant that does not teach the axis produces sessions whose
# every candidate silently receives no disposition, which looks exactly like a
# clean audit — so each variant is held to teaching it.


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_teaches_the_per_candidate_verdict(variant: str) -> None:
    batch = _flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow")

    assert "candidate_verdicts" in batch
    for disposition in ("`pass`", "`rework`", "`human_a`"):
        assert disposition in batch, f"{variant} batch flow omits {disposition}"


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_names_the_staged_reviewer_evidence(variant: str) -> None:
    """The prerequisite is staged; the agent may not go and fetch it."""
    batch = _flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow")

    assert "candidate-evidence.json" in batch
    assert "gap" in batch
    assert "head_sha" in batch


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_the_decision_example_renders_a_candidate_verdict(variant: str) -> None:
    text = PROMPT_VARIANTS[variant]
    blocks = [
        block
        for block in re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
        if "proposed_actions" in block
    ]
    decision = TechLeadDecision.from_agent_payload(json.loads(blocks[0]))

    assert decision.candidate_verdicts, "example renders no candidate verdict"
    verdict = decision.candidate_verdicts[0]
    assert verdict.candidate.is_bound
    assert verdict.rationale


# --- Staged executable-leaf contract (#345 direction C/H) -------------------
#
# The runtime stages, per candidate, the executable issue the pull request
# implements and the governing sources that issue declares. A prompt variant
# that does not name that file and does not tell the run to judge conformance
# against it leaves the "governing contract" of the verdict undefined — which
# is the audit-only wording this leaf replaces.


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_names_the_staged_leaf_contract(variant: str) -> None:
    batch = _flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow")

    assert "candidate-contracts.json" in batch, (
        f"{variant} batch flow does not name the staged leaf contract"
    )
    assert "candidate-contracts/" in batch, (
        f"{variant} batch flow does not point at the staged contract bodies"
    )
    assert "Governed-by" in batch, (
        f"{variant} batch flow does not say which sources the leaf declares"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_batch_flow_teaches_that_a_leaf_only_constraint_governs(
    variant: str,
) -> None:
    """The whole reason repository Spec/TD is not enough (#345 F)."""
    batch = _normalized(_flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow"))

    assert "narrow the work below the repository's Spec/TD" in batch, (
        f"{variant} does not teach that a leaf may narrow Spec/TD"
    )
    assert "exists ONLY in the leaf" in batch, (
        f"{variant} does not teach that a leaf-only constraint governs"
    )
    assert "do NOT reconstruct the contract from the PR description" in batch, (
        f"{variant} permits inferring the contract from PR prose"
    )
    assert "anything a previous session knew" in batch, (
        f"{variant} does not forbid reasoning from prior-session memory"
    )


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_pass_requires_both_staged_prerequisites(variant: str) -> None:
    """The mutation direction: the audit-only `pass` rule must be gone.

    The superseded bullet made `pass` rest on the reviewer approval alone and
    asked for patterns rather than conformance. If a variant reverts to it,
    this fails — which is what makes the prompt part of the contract rather
    than commentary.
    """
    batch = _normalized(_flow_section(PROMPT_VARIANTS[variant], "Batch Review Flow"))

    assert "Requires BOTH staged prerequisites for this candidate" in batch, (
        f"{variant} does not require both staged prerequisites for a pass"
    )
    assert "a resolved leaf contract in `candidate-contracts.json` with an empty" in (
        batch
    ), f"{variant} does not require a resolved leaf contract for a pass"
    assert "acceptance criteria" in batch and "STOP conditions" in batch, (
        f"{variant} does not ask for contract conformance, only patterns"
    )
    assert "rather than listing patterns" in batch, (
        f"{variant} still frames the batch flow as a pattern audit"
    )
    # The superseded single-prerequisite sentence must not survive anywhere.
    assert "a `pass` on a candidate it never established an exact-commit" not in (
        batch
    ), f"{variant} still states the reviewer-only pass prerequisite"


def _completion_section(text: str) -> str:
    """The ``## Completion …`` section, whatever the variant titles it.

    The three variants spell the heading differently ("(MANDATORY)",
    "(Labels Are Automatic)", "(Labels are Automatic)"), which is exactly how
    a rule stated in one of them drifted out of another unnoticed.
    """
    match = re.search(r"^## Completion\b.*$", text, re.MULTILINE)
    assert match is not None, "completion section missing"
    rest = text[match.end() :]
    end = re.search(r"\n## (?!#)", rest)
    return text[match.start() : match.end() + (end.start() if end else len(rest))]


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_completion_effects_name_both_pass_prerequisites(variant: str) -> None:
    """What the orchestrator does AFTER the verdict is stated the same way.

    ``test_pass_requires_both_staged_prerequisites`` pins the agent-facing rule
    in the batch flow; this pins the narrative of the orchestrator's own
    completion effects, which is a different sentence in a different section
    and drifted independently — one variant kept describing the refused `pass`
    as resting on the reviewer prerequisite alone while its `pass` bullet
    already required both.
    """
    section = _normalized(_completion_section(PROMPT_VARIANTS[variant]))

    assert "EITHER staged prerequisite" in section, (
        f"{variant} does not say a refused `pass` may want either prerequisite"
    )
    assert "exact-candidate reviewer approval" in section, (
        f"{variant} does not name the reviewer prerequisite in its effects"
    )
    assert "resolved leaf contract" in section, (
        f"{variant} does not name the leaf-contract prerequisite in its effects"
    )
    # The superseded half-stated sentence, in the shape all three carried it.
    assert "want of an exact-candidate reviewer approval" not in section, (
        f"{variant} still describes the refusal as reviewer-only"
    )


def test_prompt_leaf_contract_names_match_the_staged_artifact() -> None:
    """The prompt names the REAL descriptor, not a drifted alias.

    Pins the filename and directory the batch flow tells the agent to read to
    the constants the staging owner actually writes, so a rename cannot leave
    every variant pointing at a file that never exists.
    """
    from issue_orchestrator.domain.tech_lead_candidate_contract import (
        TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME,
        TECH_LEAD_CANDIDATE_CONTRACT_FILENAME,
        TechLeadCandidateContract,
        candidate_sources_dirname,
    )
    from issue_orchestrator.domain.tech_lead_candidate import TechLeadCandidate

    assert TECH_LEAD_CANDIDATE_CONTRACT_FILENAME == "candidate-contracts.json"
    assert TECH_LEAD_CANDIDATE_CONTRACT_DIRNAME == "candidate-contracts"
    # The per-candidate directory shape the prompt shows.
    candidate = TechLeadCandidate(123, "4f2a9c1b8e77" + "0" * 28)
    assert candidate_sources_dirname(candidate) == "pr-123-4f2a9c1b8e77"
    # And the field the prompt tells the agent to read the refusal off.
    unresolved = TechLeadCandidateContract(candidate=candidate, gap="unreadable")
    assert unresolved.to_payload()["gap"] == "unreadable"
    assert unresolved.establishes_leaf_contract is False


@pytest.mark.parametrize("variant", sorted(PROMPT_VARIANTS))
def test_non_batch_flows_do_not_inherit_the_leaf_contract_step(
    variant: str,
) -> None:
    """Only a batch review is staged one; the other flavors get no manifest."""
    for heading in ("Health Review Flow", "Failure Investigation Flow"):
        section = _flow_section(PROMPT_VARIANTS[variant], heading)
        assert "candidate-contracts.json" not in section, (
            f"{variant} '{heading}' contains the batch-only leaf-contract step"
        )
