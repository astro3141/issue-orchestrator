"""Lightweight validation config loading for hooks and completion commands."""

from dataclasses import asdict
import os
from pathlib import Path

import yaml

from ..domain.repository_launch_selection import (
    ConfigurationModeName,
    RepositoryLaunchSelection,
)
from .config_paths import (
    DEFAULT_CONFIG_NAME,
    find_config_file,
    selection_from_config_path,
)
from .config_models import (
    PublishValidationConfig,
    ValidationCommandConfig,
    ValidationConfig,
)
from .env import get_env
from .validation_junit_paths import configured_validation_junit_xml_paths_from_mapping
from .validation_profiles import (
    DEFAULT_VALIDATION_PROFILE,
    ValidationProfile,
    ValidationProfileRegistry,
    profiles_from_mapping,
)


def default_validation_config() -> dict:
    """Return the validation defaults without loading the full config model."""
    defaults = asdict(ValidationConfig())
    # ``profiles`` is a config-authoring concept; this mapping is the
    # *resolved* contract, so it carries the selected name instead.
    defaults.pop("profiles", None)
    defaults["profile"] = DEFAULT_VALIDATION_PROFILE
    return defaults


def _selected_profile(validation: dict, profile: str | None) -> ValidationProfile:
    """Resolve the selected profile straight from parsed YAML.

    Routed through :class:`ValidationProfileRegistry` — the same owner the
    orchestrator side uses — so "which gates does this name mean" is answered
    once, not once per side of the session boundary. A named profile must
    exist: the agent side fails closed rather than silently validating with
    somebody else's contract (#7059).
    """
    quick = validation.get("quick", {}) or {}
    publish = validation.get("publish", {}) or {}
    registry = ValidationProfileRegistry(
        ValidationConfig(
            quick=ValidationCommandConfig(
                cmd=quick.get("cmd"),
                timeout_seconds=quick.get("timeout_seconds", 300),
            ),
            publish=PublishValidationConfig(
                cmd=publish.get("cmd"),
                timeout_seconds=publish.get("timeout_seconds", 1800),
                dirty_check=publish.get("dirty_check", "tracked"),
            ),
            profiles=profiles_from_mapping(validation.get("profiles")),
        )
    )
    return registry.resolve(profile)


def extract_validation_config(config: dict, profile: str | None = None) -> dict:
    """Extract the validation section from parsed YAML data.

    ``profile`` selects a named validation profile; the returned mapping
    always exposes the flat ``quick``/``publish`` shape its callers already
    consume, plus the resolved ``profile`` name so artifacts can record which
    contract ran.
    """
    defaults = default_validation_config()
    guardrail_defaults = defaults["coverage_guardrail"]
    validation = config.get("validation", {}) or {}
    selected = _selected_profile(validation, profile)
    guardrail = validation.get("coverage_guardrail", {}) or {}
    return {
        "profile": selected.name,
        "quick": {
            "cmd": selected.quick.cmd,
            "timeout_seconds": selected.quick.timeout_seconds,
        },
        "publish": {
            "cmd": selected.publish.cmd,
            "timeout_seconds": selected.publish.timeout_seconds,
            "dirty_check": selected.publish.dirty_check,
        },
        "junit_xml_paths": configured_validation_junit_xml_paths_from_mapping(config),
        "coverage_guardrail": {
            "enabled": guardrail.get("enabled", guardrail_defaults["enabled"]),
            "min_percent": guardrail.get("min_percent", guardrail_defaults["min_percent"]),
            "apply_to": guardrail.get("apply_to", guardrail_defaults["apply_to"]),
            "scope": guardrail.get("scope", guardrail_defaults["scope"]) or [],
            "coverage_type": guardrail.get("coverage_type", guardrail_defaults["coverage_type"]),
            "exclude": guardrail.get("exclude", guardrail_defaults["exclude"]) or [],
        },
    }


def load_validation_config(
    start_path: Path | None = None,
    config_name: str | None = None,
    mode: str | ConfigurationModeName = "default",
    profile: str | None = None,
) -> dict:
    """Load validation configuration from the config file.

    This is for validation hooks that need only the validation config, not the
    full Config object.
    """
    selected_config_name = config_name or DEFAULT_CONFIG_NAME
    if selected_config_name and not selected_config_name.endswith(".yaml"):
        selected_config_name = f"{selected_config_name}.yaml"

    selected_mode = (
        mode if isinstance(mode, ConfigurationModeName) else ConfigurationModeName(mode)
    )
    config_path = find_config_file(
        start_path,
        selected_config_name,
        selected_mode,
    )
    if not config_path:
        if config_name:
            start_from = start_path or Path.cwd()
            raise FileNotFoundError(
                f"Configured file '{selected_config_name}' not found under "
                f"{start_from}/.issue-orchestrator/config/modes/{selected_mode.value}"
            )
        return default_validation_config()

    try:
        with config_path.open() as file:
            config = yaml.safe_load(file) or {}
    except Exception:
        return default_validation_config()
    # Deliberately outside the tolerant read: an unreadable config file may
    # degrade to defaults, but a *selected profile that does not exist* must
    # not silently become somebody else's validation contract (#7059).
    return extract_validation_config(config, profile)


def load_validation_config_from_file(
    config_path: Path,
    profile: str | None = None,
) -> dict:
    """Load only the validation section from an explicit config file path.

    Raises:
        FileNotFoundError: when config_path does not exist.
        UnknownValidationProfileError: when ``profile`` is not defined.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configured file not found: {config_path}")

    with config_path.open() as file:
        config = yaml.safe_load(file) or {}
    return extract_validation_config(config, profile)


def runtime_validation_profile() -> str | None:
    """The validation profile the orchestrator froze for this session.

    Exported by the session-environment owner at launch, so an agent-side
    tool consumes the contract that was *selected for the run* rather than
    re-deriving one from its own surroundings.
    """
    return get_env("VALIDATION_PROFILE") or None


def load_runtime_validation_config(
    start_path: Path | None = None,
) -> dict:
    """Load validation config honoring explicit runtime config selection.

    Precedence (two orthogonal axes; see #27):

    1. explicit config path — ``ISSUE_ORCHESTRATOR_CONFIG_PATH`` /
       ``ORCHESTRATOR_CONFIG_PATH``, cross-checked against ``MODE`` and
       ``CONFIG_NAME`` when those are also set
    2. mode + config name — ``ISSUE_ORCHESTRATOR_MODE`` selects the
       configuration namespace, ``ISSUE_ORCHESTRATOR_CONFIG_NAME`` the file
    3. repo-local ``default.yaml`` search

    Those choose *which configuration*. The validation profile then chooses
    *which validation contract inside it*, and is applied on every branch
    above — a selected profile that does not exist raises rather than falling
    back to somebody else's contract.

    The gate commands come from ``ISSUE_ORCHESTRATOR_VALIDATION_PROFILE``
    when the orchestrator selected one, and from the top-level
    ``validation.quick`` / ``validation.publish`` pair otherwise.
    """
    profile = runtime_validation_profile()
    config_path_env = get_env("CONFIG_PATH") or os.environ.get("ORCHESTRATOR_CONFIG_PATH")
    config_name = get_env("CONFIG_NAME") or os.environ.get("ORCHESTRATOR_CONFIG_NAME")
    mode_raw = get_env("MODE") or os.environ.get("ORCHESTRATOR_MODE")
    runtime_selection = RepositoryLaunchSelection.parse(
        mode=mode_raw,
        config_name=config_name,
    )
    if config_path_env:
        config_path = Path(config_path_env)
        path_selection = selection_from_config_path(config_path)
        if mode_raw is not None and path_selection.mode != runtime_selection.mode:
            raise ValueError(
                "Runtime config path mode does not match ISSUE_ORCHESTRATOR_MODE"
            )
        if config_name is not None and path_selection.config != runtime_selection.config:
            raise ValueError(
                "Runtime config path name does not match ISSUE_ORCHESTRATOR_CONFIG_NAME"
            )
        return load_validation_config_from_file(config_path, profile)

    return load_validation_config(
        start_path,
        config_name=config_name,
        mode=runtime_selection.mode,
        profile=profile,
    )
