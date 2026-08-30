"""Named validation profiles and the role → profile binding.

Seam for upstream issue-orchestrator/issue-orchestrator#7059
("Support per-agent / per-workflow validation profiles"), implemented
downstream while the upstream request is open. Everything the feature owns
lives behind :class:`ValidationProfileRegistry` so the seam can be removed in
one piece when upstream ships its own version.

The model deliberately has one shape:

* ``validation.quick`` / ``validation.publish`` at the top level define the
  profile named ``default``. A repository that never mentions profiles gets
  exactly the behavior it had before — the default profile *is* the old global
  configuration, not a fallback for it.
* ``validation.profiles.<name>`` defines additional named profiles.
* ``agents.<label>.validation_profile`` binds a role to one of those names.
  Selection is explicit and typed; nothing is inferred from labels, branch
  names, or working-tree state.

An unknown profile name is a configuration error surfaced at config
validation, never a silent fall back to ``default``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from ..domain.validation_profile import (
    DEFAULT_VALIDATION_PROFILE,
    ValidationGateKind,
)
from .config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
    ValidationProfileConfig,
)
from .dirty_tree_guard import DIRTY_CHECK_MODES

__all__ = [
    "DEFAULT_VALIDATION_PROFILE",
    "UnknownValidationProfileError",
    "ValidationGateContract",
    "ValidationGateKind",
    "ValidationProfile",
    "ValidationProfileRegistry",
]


class UnknownValidationProfileError(ValueError):
    """Raised when a configured profile name has no definition."""

    def __init__(self, name: str, known: Iterable[str]) -> None:
        self.name = name
        self.known = sorted(known)
        super().__init__(
            f"Unknown validation profile {name!r}. "
            f"Known profiles: {', '.join(self.known)}."
        )


@dataclass(frozen=True)
class ValidationGateContract:
    """What one gate run executes, and what it may claim to have executed.

    The single value a gate is constructed from (#25). Command, timeout and
    suite label are all read off it, so a gate cannot be handed the quick
    command while stamping the publish suite onto its record: choosing the
    ``kind`` chooses all three at once.
    """

    kind: ValidationGateKind
    profile: str
    cmd: str | None
    timeout_seconds: int

    @property
    def suite(self) -> str:
        """The suite label records produced by this contract carry."""
        return self.kind.suite

    @property
    def configured(self) -> bool:
        """Whether this profile actually defines a command for this gate."""
        return bool(self.cmd)

    @property
    def is_quick(self) -> bool:
        """Whether this is the profile's quick contract."""
        return self.kind is ValidationGateKind.QUICK

    def result_mismatch(
        self, *, suite: str, command: str, profile: str
    ) -> str | None:
        """Why a stored gate result was *not* produced by this contract.

        The one place this codebase decides whether a result already on disk
        answers for the contract now being asked about. ``None`` means it
        agrees; otherwise the returned word names which half disagreed, so
        callers can say what drifted rather than only that something did.

        Two callers, deliberately one predicate. ``ValidationGate`` asks it of
        a cached :class:`~...ports.session_output.ValidationRecord` before
        reusing it; review admission asks it of the durable
        :class:`~...domain.validation_verdict_receipt.ValidationVerdictReceipt`
        before treating a past pass as authority for a review (#45). A second
        spelling of the comparison could drift from this one, and the drift
        would read as "the gate and the admission disagree about which
        contract ran" — the exact confusion the receipt carries ``command``
        *and* ``profile`` to prevent (#7059).

        ``cmd`` being unset means this profile defines no command for this
        gate, so there is nothing for a recorded command to disagree with —
        the same reading :class:`ValidationGate` has always had.
        """
        if not self.kind.produced(suite):
            return "contract"
        if self.cmd and command != self.cmd:
            return "command"
        if profile != self.profile:
            return "profile"
        return None


@dataclass(frozen=True)
class ValidationProfile:
    """One resolved validation contract, frozen for a run/attempt."""

    name: str
    quick: ValidationCommandConfig
    publish: PublishValidationConfig

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT_VALIDATION_PROFILE

    def contract(self, kind: ValidationGateKind) -> ValidationGateContract:
        """The command/timeout this profile defines for ``kind``.

        The only supported way to get a command out of a profile for a gate
        run. Reaching for ``profile.quick.cmd`` at a gate construction site is
        what let the publish gate execute the quick contract (#25); this
        method makes the requested gate and the resolved command one decision.
        """
        # A total mapping rather than a branch: every kind names exactly one
        # section, and a kind this profile cannot serve raises instead of
        # silently resolving to the other contract.
        sections: dict[
            ValidationGateKind, ValidationCommandConfig | PublishValidationConfig
        ] = {
            ValidationGateKind.QUICK: self.quick,
            ValidationGateKind.PUBLISH: self.publish,
        }
        section = sections[kind]
        return ValidationGateContract(
            kind=kind,
            profile=self.name,
            cmd=section.cmd or None,
            timeout_seconds=section.timeout_seconds,
        )


class ValidationProfileRegistry:
    """Owner of named validation profiles and their role bindings.

    The registry is the only place that answers "which validation commands
    apply here". Callers ask by profile name (the frozen choice recorded in
    durable run state) or by agent label (the launch-time choice); they never
    reach into ``ValidationConfig.profiles`` themselves.
    """

    def __init__(
        self,
        validation: ValidationConfig,
        bindings: Mapping[str, str | None] | None = None,
    ) -> None:
        self._profiles: dict[str, ValidationProfile] = {
            DEFAULT_VALIDATION_PROFILE: ValidationProfile(
                name=DEFAULT_VALIDATION_PROFILE,
                quick=validation.quick,
                publish=validation.publish,
            )
        }
        for name, profile in validation.profiles.items():
            self._profiles[name] = ValidationProfile(
                name=name,
                quick=profile.quick,
                publish=profile.publish,
            )
        self._bindings: dict[str, str] = {
            label: profile_name
            for label, profile_name in (bindings or {}).items()
            if profile_name
        }

    @classmethod
    def single(cls, profile: ValidationProfile) -> "ValidationProfileRegistry":
        """Build a registry holding ``profile`` under its own name.

        Used by callers that already hold one resolved contract (tests, and
        the completion path when it is handed explicit commands) so they still
        route every lookup through the one owner.

        A non-default profile keeps its name: the top-level
        ``ValidationConfig`` pair always means ``default``, so a named profile
        is registered under ``profiles`` instead. Silently renaming it to
        ``default`` would make ``resolve(profile.name)`` raise on the very
        profile the caller handed in.
        """
        if profile.is_default:
            return cls(
                ValidationConfig(quick=profile.quick, publish=profile.publish)
            )
        return cls(
            ValidationConfig(
                profiles={
                    profile.name: ValidationProfileConfig(
                        quick=profile.quick,
                        publish=profile.publish,
                    )
                }
            )
        )

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._profiles)

    def has(self, name: str) -> bool:
        return name in self._profiles

    @property
    def any_quick_command_configured(self) -> bool:
        """Whether *any* profile configures a quick gate.

        Composition asks this before handing the completion path a command
        runner: a repository whose quick gate lives only in a named profile
        still needs one.
        """
        return any(profile.quick.cmd for profile in self._profiles.values())

    @property
    def any_publish_command_configured(self) -> bool:
        """Whether *any* profile configures a publication gate.

        Review admission asks this before demanding a publication receipt
        (#45). A repository that configures no publish command anywhere has no
        publication contract at all: :class:`~..control.publication_gate.
        PublicationGate` treats that as the operator's explicit choice and
        allows publication without running or recording anything. Requiring
        evidence of a gate the configuration does not define would block every
        review in such a repository forever, so the requirement attaches to
        repositories that actually have a publication contract.

        Repository-wide is the right granularity for the *reader* because a
        PR does not say which profile produced it — the receipt is the only
        thing that would. The gate asks the same question on the producing
        side, where the run's own frozen profile is known, and refuses a
        candidate this property would demand a receipt for while its own
        profile could never file one (``PublicationGate.
        _uncertifiable_candidate``). So "any profile has a contract" and "this
        candidate could have been certified" never come apart.
        """
        return any(profile.publish.cmd for profile in self._profiles.values())

    @property
    def any_command_configured(self) -> bool:
        """Whether *any* profile configures either gate."""
        return any(
            profile.quick.cmd or profile.publish.cmd
            for profile in self._profiles.values()
        )

    def resolve(self, name: str | None) -> ValidationProfile:
        """Resolve a profile by name. ``None`` means the default profile.

        Raises:
            UnknownValidationProfileError: when ``name`` is not defined.
        """
        resolved_name = name or DEFAULT_VALIDATION_PROFILE
        profile = self._profiles.get(resolved_name)
        if profile is None:
            raise UnknownValidationProfileError(resolved_name, self._profiles)
        return profile

    def name_for_agent(self, agent_label: str | None) -> str:
        """Profile name bound to ``agent_label`` (``default`` when unbound)."""
        if not agent_label:
            return DEFAULT_VALIDATION_PROFILE
        return self._bindings.get(agent_label, DEFAULT_VALIDATION_PROFILE)

    def for_agent(self, agent_label: str | None) -> ValidationProfile:
        """Resolve the profile bound to ``agent_label``.

        Raises:
            UnknownValidationProfileError: when the binding names an
                undefined profile. Config validation fails closed on this
                first, so reaching it at runtime means the config was
                bypassed.
        """
        return self.resolve(self.name_for_agent(agent_label))

    def freeze_for_run(self, agent_label: str | None) -> ValidationProfile:
        """The contract a run launched for ``agent_label`` is frozen under.

        This is the one launch-time operation. Run creation records
        ``.name`` in the run manifest and the agent session env exports it,
        so "which contract does this run get, and how is it recorded" is
        answered here instead of being reassembled from ``name_for_agent`` +
        a manifest key + an env export at each launch site.

        Unlike :meth:`name_for_agent` this resolves the binding, so a launch
        under a retired profile fails here rather than producing a run that
        claims a contract nothing can execute.

        Raises:
            UnknownValidationProfileError: when the role's binding names an
                undefined profile. Config validation fails closed on this
                first, so reaching it at launch means the config was bypassed.
        """
        return self.for_agent(agent_label)

    def binding_errors(self) -> list[str]:
        """Config-validation errors for role → profile bindings.

        Each message names the offending role and the profile it asked for,
        so a typo fails closed at startup rather than at first validation.
        """
        errors: list[str] = []
        known = ", ".join(sorted(self._profiles))
        for label in sorted(self._bindings):
            profile_name = self._bindings[label]
            if profile_name not in self._profiles:
                errors.append(
                    f"agents.{label}.validation_profile references unknown "
                    f"validation profile '{profile_name}'. "
                    f"Defined profiles: {known}."
                )
        return errors


def profiles_runtime_dict(
    profiles: Mapping[str, ValidationProfileConfig],
) -> dict[str, dict[str, object]]:
    """Fully-populated profile view for the runtime config snapshot."""
    return {
        name: {
            "quick": {
                "cmd": profile.quick.cmd,
                "timeout_seconds": profile.quick.timeout_seconds,
            },
            "publish": {
                "cmd": profile.publish.cmd,
                "timeout_seconds": profile.publish.timeout_seconds,
                "dirty_check": profile.publish.dirty_check,
            },
        }
        for name, profile in profiles.items()
    }


def profiles_yaml_dict(
    profiles: Mapping[str, ValidationProfileConfig],
) -> dict[str, dict[str, object]]:
    """Round-trip named profiles back to YAML shape.

    Mirrors how the top-level quick/publish pair is written: only non-default
    values survive, so a re-saved config stays readable.
    """
    return {name: _profile_yaml_dict(profile) for name, profile in profiles.items()}


def _profile_yaml_dict(profile: ValidationProfileConfig) -> dict[str, object]:
    profile_dict: dict[str, object] = {}
    quick_dict: dict[str, object] = {}
    if profile.quick.cmd:
        quick_dict["cmd"] = profile.quick.cmd
        if profile.quick.timeout_seconds != 300:
            quick_dict["timeout_seconds"] = profile.quick.timeout_seconds
    if quick_dict:
        profile_dict["quick"] = quick_dict
    publish_dict: dict[str, object] = {}
    if profile.publish.cmd:
        publish_dict["cmd"] = profile.publish.cmd
        if profile.publish.timeout_seconds != 1800:
            publish_dict["timeout_seconds"] = profile.publish.timeout_seconds
    if profile.publish.dirty_check != "tracked":
        publish_dict["dirty_check"] = profile.publish.dirty_check
    if publish_dict:
        profile_dict["publish"] = publish_dict
    return profile_dict


def dirty_check_errors(profiles: Mapping[str, ValidationProfileConfig]) -> list[str]:
    """Config-validation errors for per-profile ``publish.dirty_check`` values."""
    return [
        f"validation.profiles.{name}.publish.dirty_check must be "
        f"one of: {', '.join(DIRTY_CHECK_MODES)}"
        for name, profile in sorted(profiles.items())
        if profile.publish.dirty_check not in DIRTY_CHECK_MODES
    ]


def profiles_from_mapping(
    profiles_data: object,
) -> dict[str, ValidationProfileConfig]:
    """Parse the ``validation.profiles`` YAML section.

    ``default`` is reserved: it always names the top-level
    ``validation.quick`` / ``validation.publish`` pair, so redefining it here
    would give one name two meanings.
    """
    if not profiles_data:
        return {}
    if not isinstance(profiles_data, dict):
        raise ValueError("validation.profiles must be a mapping of name -> profile")

    profiles: dict[str, ValidationProfileConfig] = {}
    for name, profile_data in profiles_data.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("validation.profiles keys must be non-empty strings")
        if name == DEFAULT_VALIDATION_PROFILE:
            raise ValueError(
                f"validation.profiles.{DEFAULT_VALIDATION_PROFILE} is reserved; "
                "the default profile is validation.quick / validation.publish"
            )
        if not isinstance(profile_data, dict):
            raise ValueError(f"validation.profiles.{name} must be a mapping")
        unsupported = sorted(set(profile_data) - _SUPPORTED_PROFILE_KEYS)
        if unsupported:
            keys = ", ".join(f"validation.profiles.{name}.{key}" for key in unsupported)
            raise ValueError(
                f"Unsupported validation profile key(s): {keys}. "
                "Supported keys: publish, quick."
            )
        profiles[name] = profile_config_from_mapping(profile_data)
    return profiles


_SUPPORTED_PROFILE_KEYS = frozenset({"publish", "quick"})


def profile_config_from_mapping(data: Mapping[str, object]) -> ValidationProfileConfig:
    """Build one profile from its raw YAML mapping.

    Shared by the full config loader and the lightweight agent-side loader so
    a profile means the same thing on both sides of the session boundary.
    """
    quick_data = data.get("quick") or {}
    publish_data = data.get("publish") or {}
    if not isinstance(quick_data, Mapping) or not isinstance(publish_data, Mapping):
        raise ValueError("validation profile 'quick'/'publish' must be mappings")
    return ValidationProfileConfig(
        quick=ValidationCommandConfig(
            cmd=quick_data.get("cmd"),
            timeout_seconds=quick_data.get("timeout_seconds", 300),
        ),
        publish=PublishValidationConfig(
            cmd=publish_data.get("cmd"),
            timeout_seconds=publish_data.get("timeout_seconds", 1800),
            dirty_check=publish_data.get("dirty_check", "tracked"),
        ),
    )
