#!/usr/bin/env bash
# What `<venv>/.deps-synced` means, and who may write it (#60).
#
# **The marker states that the environment beside it is usable by this
# checkout.** Not that a setup recipe ran to the end — that is the claim it used
# to make, and the claim it could not fail to make. `make venv-fast` wrote it
# with `touch` in a `;`-separated shell block, so a `uv sync` that failed, or
# one that succeeded while installing nothing, still reached the `touch`, and
# the recipe still exited 0 because the last statement had succeeded. A
# provisioning run measured during #53 left `.deps-synced` present next to three
# entries in `site-packages` and no editable `.pth`: an environment that could
# not import the project it claimed to have installed.
#
# So the marker is written here, after the environment answers the only question
# that distinguishes those two runs — does `import issue_orchestrator` resolve
# to *this* checkout's source? — and never by the recipe that ran the sync. A
# marker that cannot fail is worse than no marker, because it invites the
# reliance that makes the false positive load-bearing.
#
# The probe is `python -I`: isolated mode ignores `PYTHONPATH` and the current
# directory, so it reads what the environment itself resolves. Agent sessions
# always export a `PYTHONPATH` pointing at the Control Centre snapshot, and
# without `-I` that ambient value would answer for a different checkout.
#
# `scripts/start_control_center.sh` has asked this question of its own
# environment since before the marker did, and sources this file rather than
# keeping a second copy of the rule: one marker, one meaning, one owner.
#
# Naming: the probe and marker functions below are the API that sourcing
# callers use, so they read as themselves (`installed_project_path`,
# `record_deps_synced`, `deps_marker_guard`). Only the two names a host script
# is likely to have of its own — `usage` and `main` — are prefixed.
#
# The rule is not just *whether* the marker may be written but *when*: withdraw
# the claim, run the sync, re-establish the claim only if the environment proves
# it. That bracket is what `guard` owns, so a writer that runs its sync through
# it cannot do half of it — which is how the Control Centre launcher came to
# `record` without a preceding `clear`. Writers that cannot hand over a single
# command (the `venv*` recipes interleave venv creation and timing logs) still
# bracket by hand with `clear` and `record`.
#
# Usage as a command, for callers that are not bash — the Makefile:
#
#   scripts/deps_marker.sh clear  <venv-path>
#   scripts/deps_marker.sh record <venv-path> <project-root>
#   scripts/deps_marker.sh guard  <venv-path> <project-root> -- <cmd> [args...]

# Path of the marker for a given environment. Callers that *read* the marker
# (the Makefile's `sync-deps` staleness test) name it themselves; this is the
# only path that ever writes it.
deps_marker_path() {
  printf '%s/.deps-synced\n' "$1"
}

# Where this environment resolves `issue_orchestrator` to, or empty when it
# cannot import it at all. Empty is a legitimate answer, not an error, so the
# probe's own failure is absorbed here and judged by the caller. Isolated mode
# ignores PYTHONPATH (including inherited CC snapshots) and the current
# directory while retaining this venv's site-packages: the probe must inspect
# the installed editable, not an import-path override.
installed_project_path() {
  local venv_path="$1"
  "${venv_path}/bin/python" \
    -I \
    -c "from pathlib import Path; import issue_orchestrator; print(Path(issue_orchestrator.__file__).resolve())" \
    2>/dev/null || true
}

project_root_path() {
  (cd "$1" && pwd -P)
}

project_install_is_current() {
  local installed_path="$1"
  local root_path
  [[ -n "${installed_path}" ]] || return 1
  root_path="$(project_root_path "$2")"
  [[ "${installed_path}" == "${root_path}"/* ]]
}

# Fail unless the environment imports the project from this checkout.
verify_project_install() {
  local venv_path="$1"
  local root_dir="$2"
  local installed_path
  installed_path="$(installed_project_path "${venv_path}")"
  if ! project_install_is_current "${installed_path}" "${root_dir}"; then
    echo "ERROR: Dependency sync did not install issue_orchestrator from ${root_dir}: ${installed_path:-not importable}" >&2
    return 1
  fi
}

# Withdraw the claim before doing the work that would re-establish it. Without
# this a sync that aborts halfway leaves yesterday's marker in place, still
# asserting that today's environment is usable.
clear_deps_marker() {
  rm -f "$(deps_marker_path "$1")"
}

# Write the marker, and only after the environment has proved the claim. A
# refused claim also withdraws any older one, so the two ways a sync can end
# badly — it fails before reaching here, or it succeeds having installed
# nothing — leave the same state behind: no marker.
record_deps_synced() {
  local venv_path="$1"
  local root_dir="$2"
  if ! verify_project_install "${venv_path}" "${root_dir}"; then
    clear_deps_marker "${venv_path}"
    return 1
  fi
  touch "$(deps_marker_path "${venv_path}")"
}

# The bracket itself: withdraw, sync, re-establish. The sync's exit status is
# passed through untouched, and a sync that failed never reaches `record`, so
# the failure leaves no marker behind either way.
#
# The command is judged by *its own* exit status. Because a failure being
# captured here suppresses `errexit` inside the command, hand over one command
# or an `&&` chain — a `;`-separated sequence would report only its last
# statement, which is the shape of the defect this file exists to prevent.
deps_marker_guard() {
  local venv_path="$1"
  local root_dir="$2"
  local status=0
  shift 2
  if [[ "${1:-}" == "--" ]]; then
    shift
  fi
  if [[ $# -eq 0 ]]; then
    echo "ERROR: deps_marker guard needs a command to run" >&2
    return 2
  fi

  clear_deps_marker "${venv_path}"
  "$@" || status=$?
  if [[ "${status}" -ne 0 ]]; then
    return "${status}"
  fi
  record_deps_synced "${venv_path}" "${root_dir}"
}

# Namespaced, because this file is sourced into scripts that have a `main` and
# a `usage` of their own.
deps_marker_usage() {
  echo "usage: deps_marker.sh clear <venv-path>" >&2
  echo "       deps_marker.sh record <venv-path> <project-root>" >&2
  echo "       deps_marker.sh guard <venv-path> <project-root> -- <cmd> [args...]" >&2
  exit 2
}

deps_marker_main() {
  # Set here rather than at file scope: sourcing this file must hand the caller
  # its functions, not silently change the shell options it runs under.
  set -euo pipefail

  case "${1:-}" in
    clear)
      [[ $# -eq 2 ]] || deps_marker_usage
      clear_deps_marker "$2"
      ;;
    record)
      [[ $# -eq 3 ]] || deps_marker_usage
      record_deps_synced "$2" "$3"
      ;;
    guard)
      [[ $# -ge 4 ]] || deps_marker_usage
      shift
      deps_marker_guard "$@"
      ;;
    *)
      deps_marker_usage
      ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  deps_marker_main "$@"
fi
