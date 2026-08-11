"""Lightweight validation config loading for hooks and completion commands."""

from dataclasses import asdict
import os
from pathlib import Path

import yaml

from .config_paths import DEFAULT_CONFIG_NAME, find_config_file
from .config_models import ValidationConfig
from .env import get_env
from .validation_junit_paths import configured_validation_junit_xml_paths_from_mapping
from .validation_profiles import (
    DEFAULT_VALIDATION_PROFILE,
    UnknownValidationProfileError,
)


def default_validation_config() -> dict:
    """Return the validation defaults without loading the full config model."""
    defaults = asdict(ValidationConfig())
    # ``profiles`` is a config-authoring concept; this mapping is the
    # *resolved* contract, so it carries the selected name instead.
    defaults.pop("profiles", None)
    defaults["profile"] = DEFAULT_VALIDATION_PROFILE
    return defaults


def _profile_gates(validation: dict, profile: str | None) -> tuple[dict, dict]:
    """Return the ``(quick, publish)`` mappings for the selected profile.

    ``None`` or ``default`` means the top-level pair, which is why an
    existing config that never mentions profiles is byte-for-byte unchanged.
    A named profile must exist: the agent side fails closed rather than
    silently validating with somebody else's contract (#7059).
    """
    if not profile or profile == DEFAULT_VALIDATION_PROFILE:
        return validation.get("quick", {}) or {}, validation.get("publish", {}) or {}

    profiles = validation.get("profiles", {}) or {}
    selected = profiles.get(profile)
    if not isinstance(selected, dict):
        raise UnknownValidationProfileError(
            profile,
            [DEFAULT_VALIDATION_PROFILE, *profiles],
        )
    return selected.get("quick", {}) or {}, selected.get("publish", {}) or {}


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
    quick, publish = _profile_gates(validation, profile)
    guardrail = validation.get("coverage_guardrail", {}) or {}
    return {
        "profile": profile or DEFAULT_VALIDATION_PROFILE,
        "quick": {
            "cmd": quick.get("cmd"),
            "timeout_seconds": quick.get(
                "timeout_seconds",
                defaults["quick"]["timeout_seconds"],
            ),
        },
        "publish": {
            "cmd": publish.get("cmd"),
            "timeout_seconds": publish.get(
                "timeout_seconds",
                defaults["publish"]["timeout_seconds"],
            ),
            "dirty_check": publish.get(
                "dirty_check",
                defaults["publish"]["dirty_check"],
            ),
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
    profile: str | None = None,
) -> dict:
    """Load validation configuration from the config file.

    This is for validation hooks that need only the validation config, not the
    full Config object.
    """
    selected_config_name = config_name or DEFAULT_CONFIG_NAME
    if selected_config_name and not selected_config_name.endswith(".yaml"):
        selected_config_name = f"{selected_config_name}.yaml"

    config_path = find_config_file(start_path, selected_config_name)
    if not config_path:
        if config_name:
            start_from = start_path or Path.cwd()
            raise FileNotFoundError(
                f"Configured file '{selected_config_name}' not found under "
                f"{start_from}/.issue-orchestrator/config"
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

    Precedence:
    1. ``ISSUE_ORCHESTRATOR_CONFIG_PATH`` / ``ORCHESTRATOR_CONFIG_PATH``
    2. ``ISSUE_ORCHESTRATOR_CONFIG_NAME`` / ``ORCHESTRATOR_CONFIG_NAME``
    3. repo-local ``default.yaml`` search

    The gate commands come from ``ISSUE_ORCHESTRATOR_VALIDATION_PROFILE``
    when the orchestrator selected one, and from the top-level
    ``validation.quick`` / ``validation.publish`` pair otherwise.
    """
    profile = runtime_validation_profile()
    config_path_env = get_env("CONFIG_PATH") or os.environ.get("ORCHESTRATOR_CONFIG_PATH")
    if config_path_env:
        return load_validation_config_from_file(Path(config_path_env), profile)

    config_name = get_env("CONFIG_NAME") or os.environ.get("ORCHESTRATOR_CONFIG_NAME")
    return load_validation_config(start_path, config_name=config_name, profile=profile)
