#!/bin/sh
# Self-hosting canary verdict for issue #6.
#
# Answers one question with measurements rather than claims:
#
#   candidate Git HEAD == managed worktree filesystem
#                      == source the Reviewer read
#                      == source validation executed
#
# plus two provenance facts: the run used R1, and R0 was not touched.
#
# Usage: sh docs/selfhosting/canary-verify.sh <issue-number>
#
# Three corrections are baked in because each one produced a wrong verdict on
# first use, and each would be easy to reintroduce:
#
#   * `find ... -exec shasum {} +` batches in an order that is not stable
#     between runs. Hashing its output reported an untouched R0 as changed —
#     two consecutive runs over the same tree gave different digests. The file
#     list is sorted first, and git's own view is checked alongside it.
#   * Validation artifacts are not found by guessing at log paths. The record
#     is named for the commit SHA and carries `stdout_path`; follow it, and
#     check its `head_sha` against the candidate while you are there.
#   * Review does not always get its own worktree. The review-exchange path
#     runs in the actor's tree and leaves a session directory, so a missing
#     review worktree is not evidence that review did not happen.

set -u
ISSUE="${1:-8}"
FORK=/Users/astro3141/io-fork/issue-orchestrator
R0=/Users/astro3141/io-tools/issue-orchestrator
R1=/Users/astro3141/io-runtime-r1/issue-orchestrator
BASE=/private/tmp/claude-501/-Users-astro3141-Lab/4be405e7-cc7e-441f-9117-abde5d83243f/scratchpad/canary-baseline.txt
CLI=src/issue_orchestrator/entrypoints/cli_tools

FAIL=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAIL=$((FAIL + 1)); }

# The candidate under test is whatever the agent's worktree has committed.
ACTOR=$(ls -d /Users/astro3141/io-fork-worktrees/*"${ISSUE}" 2>/dev/null | grep -v -- '-review-' | head -1)
REVIEW=$(ls -d /Users/astro3141/io-fork-worktrees/*"${ISSUE}"-review-* 2>/dev/null | head -1)

echo "canary #${ISSUE}"
echo "  actor worktree    : ${ACTOR:-<none>}"
echo "  reviewer worktree : ${REVIEW:-<none>}"
echo ""

[ -n "${ACTOR}" ] || { echo "no actor worktree — cannot judge"; exit 2; }
HEAD_SHA=$(git -C "${ACTOR}" rev-parse HEAD)
echo "  candidate HEAD    : ${HEAD_SHA}"
echo ""

# --- 1. the candidate actually modifies tracked cli_tools source --------------
echo "[1] candidate modifies tracked cli_tools source"
TOUCHED=$(git -C "${ACTOR}" diff --name-only origin/main...HEAD -- "${CLI}" | wc -l | tr -d ' ')
if [ "${TOUCHED}" -gt 0 ]; then
    pass "${TOUCHED} tracked file(s) under ${CLI} changed"
    git -C "${ACTOR}" diff --name-only origin/main...HEAD -- "${CLI}" | sed 's/^/          /'
else
    fail "no tracked cli_tools file changed — the canary would be vacuous"
fi
echo ""

# --- 2. no runtime overlay on repo-owned paths --------------------------------
# This is the #6 fix. Both directions matter: nothing hidden from git, and
# nothing planted over a path the repository owns.
echo "[2] no runtime overlay on repo-owned paths"
for WT in "${ACTOR}" ${REVIEW}; do
    [ -d "${WT}" ] || continue
    NAME=$(basename "${WT}")
    SKIP=$(git -C "${WT}" ls-files -v -- "${CLI}" | grep -c '^S' || true)
    [ "${SKIP}" -eq 0 ] && pass "${NAME}: 0 skip-worktree bits under ${CLI}" \
                        || fail "${NAME}: ${SKIP} skip-worktree bit(s) still set"
    EXC="${WT}/.git/info/exclude"
    [ -f "${WT}/.git" ] && EXC=$(git -C "${WT}" rev-parse --git-path info/exclude)
    if [ -f "${EXC}" ] && grep -q "cli_tools" "${EXC}" 2>/dev/null; then
        fail "${NAME}: stale cli_tools entry in info/exclude"
    else
        pass "${NAME}: no cli_tools exclude entry"
    fi
done
echo ""

# --- 3a. every tree anchored to one reference blob ---------------------------
# C is the single reference: the candidate commit's own blob for the file the
# canary deliberately modified. Comparing each worktree against its *own* HEAD
# is transitively equivalent once [4] holds, but anchoring both to one value
# states the invariant directly and does not depend on that ordering.
echo "[3a] disk content == C (candidate HEAD blob of coding_done.py)"
TARGET="${CLI}/coding_done.py"
C=$(git -C "${ACTOR}" show "HEAD:${TARGET}" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
if [ -z "${C}" ]; then
    fail "cannot read C — ${TARGET} missing at candidate HEAD"
else
    echo "          C = ${C}"
    for WT in "${ACTOR}" ${REVIEW}; do
        [ -d "${WT}" ] || continue
        NAME=$(basename "${WT}")
        DISK=$(shasum -a 256 "${WT}/${TARGET}" 2>/dev/null | cut -d' ' -f1)
        [ "${DISK}" = "${C}" ] && pass "${NAME}: disk SHA == C" \
                               || fail "${NAME}: disk SHA ${DISK:-<missing>} != C"
    done
fi
echo ""

# --- 3b. validation executed the candidate implementation --------------------
# The strongest evidence available: the canary's own test asserts the constant
# that only the candidate's coding_done.py defines. If validation had graded a
# planted copy, this test would fail rather than pass. A green gate is not
# enough on its own — this names the specific test that carries the proof.
echo "[3b] validation loaded the candidate implementation"
# Follow the validation record for this exact commit rather than guessing at
# log locations: the record is named for the SHA and carries stdout_path.
VREC="${ACTOR}/.issue-orchestrator/validation/${HEAD_SHA}.json"
if [ ! -f "${VREC}" ]; then
    fail "no validation record for ${HEAD_SHA} — the gate never ran on this commit"
else
    VP=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('passed'))" "${VREC}")
    VH=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('head_sha',''))" "${VREC}")
    VS=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('stdout_path',''))" "${VREC}")
    [ "${VH}" = "${HEAD_SHA}" ] && pass "validation record head_sha == candidate HEAD" \
                               || fail "validation graded ${VH}, candidate is ${HEAD_SHA}"
    [ "${VP}" = "True" ] && pass "publish_gate passed on that commit" \
                         || fail "publish_gate did not pass (passed=${VP})"

    LOG="${ACTOR}/${VS}"
    if [ -f "${LOG}" ]; then
        HIT=$(grep -iE "source_id.*PASSED" "${LOG}" | head -1)
        MISS=$(grep -iE "source_id.*(FAILED|ERROR)" "${LOG}" | head -1)
        if [ -n "${MISS}" ]; then
            fail "SOURCE_ID test FAILED — a different module was graded"
        elif [ -n "${HIT}" ]; then
            pass "SOURCE_ID test PASSED in the graded run"
            echo "          ${HIT}"
        else
            fail "SOURCE_ID test not found in the graded run's output"
        fi
    else
        fail "stdout_path from the record is missing: ${VS}"
    fi
fi

VAL=$(grep -oE 'CODING_DONE_SOURCE_ID[[:space:]]*[:=][^\n]*' "${ACTOR}/${TARGET}" 2>/dev/null | head -1)
[ -n "${VAL}" ] && pass "constant present in candidate source" \
                || fail "CODING_DONE_SOURCE_ID not found in ${TARGET}"
echo ""

# --- 3. worktree filesystem == candidate HEAD ---------------------------------
# Compare the working file against the blob HEAD records, per file. A dirty
# check alone is not enough: skip-worktree is what used to suppress it.
echo "[3] managed worktree filesystem == candidate HEAD"
for WT in "${ACTOR}" ${REVIEW}; do
    [ -d "${WT}" ] || continue
    NAME=$(basename "${WT}")
    DIFFS=0
    for f in $(git -C "${WT}" ls-files -- "${CLI}"); do
        disk=$(shasum -a 256 "${WT}/${f}" 2>/dev/null | cut -d' ' -f1)
        blob=$(git -C "${WT}" show "HEAD:${f}" 2>/dev/null | shasum -a 256 | cut -d' ' -f1)
        [ "${disk}" = "${blob}" ] || { DIFFS=$((DIFFS + 1)); echo "          differs: ${f}"; }
    done
    [ "${DIFFS}" -eq 0 ] && pass "${NAME}: every tracked cli_tools file matches its HEAD blob" \
                         || fail "${NAME}: ${DIFFS} file(s) diverge from HEAD"
done
echo ""

# --- 4. reviewer read the candidate ------------------------------------------
echo "[4] reviewer source == candidate HEAD"
if [ -n "${REVIEW}" ]; then
    RHEAD=$(git -C "${REVIEW}" rev-parse HEAD)
    [ "${RHEAD}" = "${HEAD_SHA}" ] && pass "reviewer worktree HEAD == candidate HEAD" \
                                   || fail "reviewer at ${RHEAD}, candidate at ${HEAD_SHA}"
else
    # Review does not always get its own worktree. The review-exchange path
    # runs in the actor's tree and leaves a session directory instead, so the
    # absence of a review worktree is not evidence that review did not happen.
    RS=$(ls -1d "${ACTOR}"/.issue-orchestrator/sessions/*review* 2>/dev/null | tail -1)
    if [ -n "${RS}" ]; then
        pass "review ran as review-exchange: $(basename "${RS}")"
        # It read the actor tree, which [3a]/[3] already anchored to C.
        pass "reviewed tree is the actor worktree, already shown == candidate HEAD"
    else
        fail "no reviewer worktree and no review session — review did not run"
    fi
fi
echo ""

# --- 5. provenance ------------------------------------------------------------
echo "[5] runtime provenance"
. "${BASE}" 2>/dev/null || true
R1_NOW=$(git -C "${R1}" rev-parse HEAD)
[ "${R1_NOW}" = "${R1_HEAD:-}" ] && pass "R1 runtime still at ${R1_NOW}" \
                                 || fail "R1 moved: ${R1_NOW} vs recorded ${R1_HEAD:-<none>}"

R0_NOW=$(git -C "${R0}" rev-parse HEAD)
R0_DIRTY_NOW=$(git -C "${R0}" status --porcelain | wc -l | tr -d ' ')
# `find -exec shasum {} +` batches in an order that is not stable between
# runs, so hashing its output compares nothing. Two consecutive runs over an
# untouched tree produced different digests and reported R0 as changed. Sort
# the file list first; git's own view is checked alongside it.
R0_SRC_NOW=$(find "${R0}/src" -name '*.py' -type f | sort | xargs shasum | shasum | cut -d' ' -f1)
R0_TRACKED_DIFF=$(git -C "${R0}" diff --name-only HEAD -- src | wc -l | tr -d ' ')
[ "${R0_TRACKED_DIFF}" = "0" ] && pass "R0 src/ has no tracked modification (git)" \
                              || fail "R0 src/ modified: ${R0_TRACKED_DIFF} file(s)"
[ "${R0_NOW}" = "${R0_HEAD:-}" ] && pass "R0 HEAD unchanged (${R0_NOW})" \
                                 || fail "R0 HEAD moved: ${R0_NOW}"
[ "${R0_DIRTY_NOW}" = "0" ] && pass "R0 working tree clean" || fail "R0 dirty: ${R0_DIRTY_NOW} file(s)"
[ "${R0_SRC_NOW}" = "${R0_SRC_SHA:-}" ] && pass "R0 src/ byte-identical to baseline" \
                                        || fail "R0 src/ changed"
echo ""

if [ "${FAIL}" -eq 0 ]; then
    echo "CANARY: all measured checks PASS"
    echo "  (canonical gate result is separate — read it from the run's publish log)"
    exit 0
fi
echo "CANARY: ${FAIL} check(s) FAILED"
exit 1
