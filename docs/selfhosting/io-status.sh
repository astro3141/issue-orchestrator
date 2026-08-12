#!/bin/sh
# One command that answers "is it running, where is the dashboard, and is the
# agent actually doing anything".
#
# Written because ad-hoc pgrep/lsof invocations got this wrong three times in
# one session:
#   * `pgrep -f "issue-orchestrator start --issue N"` misses the real process,
#     because the command line is `.../issue-orchestrator --config <path> start
#     --issue N` — the words are not adjacent.
#   * `lsof -nP -iTCP -sTCP:LISTEN -p PID` ORs its filters without `-a`, so it
#     prints every listening socket on the machine plus every file the process
#     has open.
#   * A bare `pgrep -fl issue-orchestrator` matches this script and the shell
#     wrapper that launched it, which reads as "the orchestrator is alive" when
#     it is not.
#
# Usage:  sh docs/selfhosting/io-status.sh [issue-number]

set -u
ISSUE="${1:-}"
# Resolve the repository root from git rather than counting "../" hops: this
# script moved from .issue-orchestrator/ to docs/selfhosting/ and the relative
# path silently kept pointing one level too high, so every store lookup below
# found nothing and printed nothing.
REPO_ROOT=$(cd "$(dirname "$0")" && git rev-parse --show-toplevel 2>/dev/null)
if [ -z "${REPO_ROOT}" ]; then
    REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
fi
TOKEN_FILE="$HOME/.issue-orchestrator/api-token"

# --- process -----------------------------------------------------------------
# Match the interpreter running the installed entry point, never a shell.
# Match the installed entry point in the args, and exclude shells so the
# wrapper that launched it is never mistaken for the orchestrator itself.
PID=$(ps -eo pid=,args= \
      | awk '/\/bin\/issue-orchestrator/ && !/\/bin\/(z|ba)?sh / && !/awk/ {print $1; exit}')

if [ -z "${PID}" ]; then
    echo "orchestrator: NOT RUNNING"
else
    ET=$(ps -o etime= -p "${PID}" | tr -d ' ')
    echo "orchestrator: running  pid=${PID}  uptime=${ET}"
fi

# --- ports -------------------------------------------------------------------
# -a ANDs the filters. Without it lsof unions them and the output is unusable.
if [ -n "${PID}" ]; then
    PORTS=$(lsof -nP -a -p "${PID}" -iTCP -sTCP:LISTEN 2>/dev/null \
            | grep -oE '127\.0\.0\.1:[0-9]+' | cut -d: -f2 | sort -un)
    for p in ${PORTS}; do
        TITLE=$(curl -s --max-time 3 "http://127.0.0.1:${p}/" 2>/dev/null \
                | grep -oiE '<title>[^<]*</title>' | head -1 | sed 's/<[^>]*>//g')
        case "${TITLE}" in
            *"Sign in"*) LABEL="login / control center" ;;
            "")          LABEL="?" ;;
            *)           LABEL="dashboard" ;;
        esac
        printf '  http://127.0.0.1:%s  %s\n' "${p}" "${LABEL}"
    done
fi

# --- engine ------------------------------------------------------------------
if [ -n "${PID}" ] && [ -f "${TOKEN_FILE}" ]; then
    SIGNIN=$(echo "${PORTS}" | head -1)
    # No stderr suppression here. An earlier version hid a Python SyntaxError
    # behind 2>/dev/null and simply printed nothing, which read as "no engine"
    # for several checks in a row.
    curl -s --max-time 5 -H "Authorization: Bearer $(cat "${TOKEN_FILE}")" \
        "http://127.0.0.1:${SIGNIN}/api/status" \
    | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
except Exception as exc:
    print('  engine: unreadable status (%s)' % exc)
    raise SystemExit
print('  engine: active=%s pending_reviews=%s pending_reworks=%s paused=%s'
      % (d.get('active_sessions'), d.get('pending_reviews'),
         d.get('pending_reworks'), d.get('paused')))
for s in d.get('sessions') or []:
    print('    %s | %s | %s | %sm | %s'
          % (s.get('session_name'), s.get('agent_type'), s.get('status'),
             s.get('runtime_minutes'), s.get('branch')))
if not (d.get('sessions') or []):
    print('    (engine reports no session — if an agent process is alive below,'
          ' the run already finished or was detached)')
"
fi

# --- recorded state: what the orchestrator believes, process or no process ---
# Live-process checks answer "is something running now". They do not answer
# "what disposition does this issue have", and when the orchestrator is stopped
# they report nothing at all — which reads as "nothing happened" even when the
# dashboard is showing a blocked item from persisted state. These read the
# stores directly, so the answer survives a stopped engine.
if [ -n "${ISSUE}" ]; then
    echo "  recorded state for #${ISSUE}:"

    QC="${REPO_ROOT}/.issue-orchestrator/state/queue_cache.sqlite"
    if [ -f "${QC}" ]; then
        ROW=$(sqlite3 "${QC}" "select labels from queue_issues where number=${ISSUE};" 2>/dev/null)
        WM=$(sqlite3 "${QC}" "select value from meta where key='watermark';" 2>/dev/null)
        if [ -n "${ROW}" ]; then
            echo "    queue    : present  labels=${ROW}"
            case "${ROW}" in
                *proposed-tech-lead*|*blocked*|*needs-human*|*failed*)
                    echo "               ^ carries a blocking-class label — will not be scheduled" ;;
            esac
        else
            echo "    queue    : not in cache (watermark ${WM:-?})"
        fi
    fi

    TL="${REPO_ROOT}/.issue-orchestrator/state/timeline.sqlite"
    if [ -f "${TL}" ]; then
        EV=$(sqlite3 -separator ' ' "${TL}" \
             "select substr(timestamp,12,8), event from timeline_events where issue_number=${ISSUE} order by timestamp desc limit 5;" 2>/dev/null)
        if [ -n "${EV}" ]; then
            echo "    timeline : (most recent first)"
            printf '%s\n' "${EV}" | sed 's/^/               /'
        else
            echo "    timeline : no events — never reached a lifecycle transition"
        fi
    fi
fi
echo ""

# --- is the agent actually working -------------------------------------------
# Claim renewal and active=1 prove liveness, not progress. These two do.
# One glob only: two patterns resolving to the same directory printed every
# worktree twice.
for WT in /Users/astro3141/io-fork-worktrees/*"${ISSUE}"*; do
    [ -d "${WT}" ] || continue
    [ -d "${WT}/.git" ] || [ -f "${WT}/.git" ] || continue
    case "${WT}" in *"-review-"*) ROLE="reviewer" ;; *) ROLE="actor" ;; esac
    CHANGED=$(git -C "${WT}" status --porcelain 2>/dev/null | wc -l | tr -d ' ')
    AHEAD=$(git -C "${WT}" rev-list --count main..HEAD 2>/dev/null || echo "?")
    REC=$(ls -1t "${WT}"/.issue-orchestrator/sessions/*/terminal-recording.jsonl 2>/dev/null | head -1)
    if [ -n "${REC}" ]; then
        SZ=$(wc -c < "${REC}" | tr -d ' ')
        AGE=$(( $(date +%s) - $(stat -f %m "${REC}") ))
        echo "  ${ROLE}: $(basename "${WT}")  changed=${CHANGED} commits=${AHEAD}  recording=${SZ}B  last_write=${AGE}s ago"
        [ "${AGE}" -gt 300 ] && echo "         ^ recording idle >5min — check Coding Recording for a blocked prompt"
    else
        echo "  ${ROLE}: $(basename "${WT}")  changed=${CHANGED} commits=${AHEAD}  (no recording yet)"
    fi
done

exit 0
