let dependencyProblems = {};  // issue_number -> problem info

function updateDependencyWarning(issueNumber, problem) {
    const warningIcon = document.getElementById('dep-warning-' + issueNumber);
    if (warningIcon) {
        if (problem) {
            warningIcon.style.display = 'inline';
            warningIcon.title = problem.summary || 'Dependency problem';
            // Store for context menu
            warningIcon.dataset.problemSummary = problem.summary;
        } else {
            warningIcon.style.display = 'none';
            warningIcon.title = '';
        }
    }
}

function loadDependencyProblems() {
    fetch('/api/dependency-problems')
        .then(response => response.json())
        .then(data => {
            if (data.problems) {
                dependencyProblems = data.problems;
                console.log('[deps] Loaded', Object.keys(dependencyProblems).length, 'dependency problems');
                // Update warning icons for all problems
                for (const [issueNum, problem] of Object.entries(dependencyProblems)) {
                    updateDependencyWarning(issueNum, problem);
                }
            }
        })
        .catch(err => console.error('[deps] Failed to load dependency problems:', err));
}

// Stale in-progress tracking
let staleIssues = {};  // issue_number -> stale info

function updateStaleWarning(issueNumber, staleInfo) {
    const warningIcon = document.getElementById('stale-warning-' + issueNumber);
    if (warningIcon) {
        if (staleInfo) {
            warningIcon.style.display = 'inline';
            const ticks = staleInfo.consecutive_ticks || 1;
            const persistent = staleInfo.persistent;
            warningIcon.title = persistent
                ? `Persistent stale: no session for ${ticks} cycles (needs investigation)`
                : `Stale in-progress: no session running (${ticks} cycle${ticks > 1 ? 's' : ''})`;
            // Add/remove persistent class for red color
            if (persistent) {
                warningIcon.classList.add('persistent');
            } else {
                warningIcon.classList.remove('persistent');
            }
        } else {
            warningIcon.style.display = 'none';
            warningIcon.title = '';
            warningIcon.classList.remove('persistent');
        }
    }
}

function loadStaleIssues() {
    fetch('/api/stale-issues')
        .then(response => response.json())
        .then(data => {
            if (data.stale) {
                staleIssues = data.stale;
                console.log('[stale] Loaded', Object.keys(staleIssues).length, 'stale issues');
                // Update warning icons for all stale issues
                for (const [issueNum, staleInfo] of Object.entries(staleIssues)) {
                    updateStaleWarning(issueNum, staleInfo);
                }
            }
        })
        .catch(err => console.error('[stale] Failed to load stale issues:', err));
}

let excludedLoaded = false;

function renderFlowStepper(steps, activeKey, blockedSummary) {
    if (!steps || steps.length === 0) return '';
    const stepHtml = steps.map(step => {
        const active = step.key === activeKey ? 'active' : '';
        return `<span class="flow-step ${active}" tabindex="0">${escapeHtml(step.label)}</span>`;
    }).join('');
    const blockedBadge = blockedSummary
        ? `<span class="blocked-badge" title="${escapeHtml(blockedSummary)}">Blocked</span>`
        : '';
    const blockedClass = blockedSummary ? 'blocked' : '';
    return `<span class="flow-stepper ${blockedClass}">${stepHtml}${blockedBadge}</span>`;
}

function renderExcludedList(items) {
    const list = document.getElementById('excludedList');
    if (!items || items.length === 0) {
        list.innerHTML = '<div class="empty-state">No excluded issues found</div>';
        return;
    }
    list.innerHTML = items.map(item => `
        <div class="excluded-row">
            <div class="excluded-meta">
                <a href="${item.issue_url}" target="_blank">#${item.issue_number}</a>
                <span class="excluded-reason">${escapeHtml(item.excluded_reason || 'not eligible')}</span>
            </div>
            <div class="issue-title">${escapeHtml(item.title)}</div>
            ${renderFlowStepper(item.flow_steps, item.flow_stage, item.blocked_summary)}
        </div>
    `).join('');
}

async function toggleExcluded() {
    const panel = document.getElementById('excludedPanel');
    const toggle = document.getElementById('excludedToggle');
    const opening = panel.style.display === 'none';
    panel.style.display = opening ? 'block' : 'none';
    toggle.classList.toggle('active', opening);

    if (!opening) return;
    if (!excludedLoaded) {
        try {
            const res = await fetch('/api/excluded-issues');
            const data = await res.json();
            const items = data.excluded || [];
            renderExcludedList(items);
            toggle.textContent = `Excluded (${items.length})`;
            excludedLoaded = true;
        } catch (err) {
            console.error('Failed to fetch excluded issues:', err);
            document.getElementById('excludedList').innerHTML =
                '<div class="empty-state">Failed to load excluded issues</div>';
        }
    }
}

// Server-Sent Events for real-time updates
// Always connect - even during startup - so we can receive startup_complete
// IMPORTANT: Connect first, then fetch initial state on open to avoid race conditions
//
// Connection lifecycle — opening, the engine-liveness watchdog, reconnect
// backoff, and the human-visible status — belongs to
// ``live_event_stream.js`` (issue #44). This block only says which events the
// dashboard cares about and what to do when one arrives. Nothing here may
// re-derive "are we live?"; that question has exactly one owner, because the
// previous split answer (a non-null EventSource handle here, /api/info
// reachability there) is what let a dead stream render as a healthy one.
(function() {
    const startupComplete = window.dashboardData.startupComplete;
    const statusElement = document.getElementById('liveStreamStatus');
    const liveStream = window.ioLiveEventStream;

    function wireEventListeners(source) {
        const refreshEvents = [
            'session.started',
            'session.completed',
            'history.reconciled',
            'issue.unblocked',
            'orchestrator.paused',
            'orchestrator.resumed',
            'startup_complete',
            // Provider circuit-breaker outages (issue #5980): refresh the view
            // model so the outage banner + health panel appear/clear live.
            'provider.outage_entered',
            'provider.outage_exited',
            // Issue-scoped provider impact (#5980): a specific issue just gained
            // or lost the provider-blocked state, so its row badge changes.
            'provider.issue_blocked',
            'provider.issue_unblocked',
        ];
        refreshEvents.forEach(eventType => {
            source.addEventListener(eventType, function(e) {
                console.log('[SSE] Received event:', eventType, e.data);
                if (eventType === 'startup_complete') {
                    document.querySelectorAll('.skeleton-card').forEach(el => el.remove());
                }
                setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
            });
        });

        source.addEventListener('tick.completed', function() {
            refreshViewModel({ reloadOnListChange: false });
        });

        source.addEventListener('shutdown_requested', function(e) {
            console.log('[SSE] Shutdown requested:', e.data);
            const badge = document.querySelector('.status-badge');
            if (badge) {
                badge.textContent = 'Stopping...';
                badge.classList.remove('status-running', 'status-starting');
                badge.classList.add('status-paused');
            }
            setTimeout(() => {
                document.body.innerHTML = '<div style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;gap:16px;color:var(--text-muted);"><div style="font-size:48px;">👋</div><h2 style="color:var(--text);">Orchestrator Stopped</h2><p>You can close this tab or wait for it to restart.</p></div>';
            }, 500);
        });

        source.addEventListener('queue.changed', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Queue changed:', data.added.length, 'added,', data.removed.length, 'removed');
                setTimeout(() => refreshViewModel({ reloadOnListChange: true }), 200);
            } catch (err) {
                console.error('[SSE] Failed to parse queue.changed:', err);
            }
        });

        source.addEventListener('dependency.blocked', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Dependency blocked:', data);
                dependencyProblems[data.issue_number] = data;
                updateDependencyWarning(data.issue_number, data);
            } catch (err) {
                console.error('[SSE] Failed to parse dependency.blocked:', err);
            }
        });

        source.addEventListener('dependency.unblocked', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Dependency unblocked:', data);
                delete dependencyProblems[data.issue_number];
                updateDependencyWarning(data.issue_number, null);
            } catch (err) {
                console.error('[SSE] Failed to parse dependency.unblocked:', err);
            }
        });

        source.addEventListener('stale.in_progress_detected', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Stale in-progress detected:', data);
                staleIssues[data.issue_number] = {
                    issue_number: data.issue_number,
                    consecutive_ticks: 1,
                    persistent: false,
                };
                updateStaleWarning(data.issue_number, staleIssues[data.issue_number]);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.in_progress_detected:', err);
            }
        });

        source.addEventListener('stale.in_progress_cleared', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Stale in-progress cleared:', data);
                delete staleIssues[data.issue_number];
                updateStaleWarning(data.issue_number, null);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.in_progress_cleared:', err);
            }
        });

        source.addEventListener('stale.persistent_detected', function(e) {
            try {
                const data = JSON.parse(e.data);
                console.log('[SSE] Persistent stale detected:', data);
                staleIssues[data.issue_number] = {
                    issue_number: data.issue_number,
                    consecutive_ticks: data.consecutive_ticks,
                    persistent: true,
                    threshold: data.threshold,
                };
                updateStaleWarning(data.issue_number, staleIssues[data.issue_number]);
            } catch (err) {
                console.error('[SSE] Failed to parse stale.persistent_detected:', err);
            }
        });

        // E2E lifecycle events — trigger immediate status refresh instead of
        // waiting for the next poll cycle.
        source.addEventListener('e2e.completed', function(event) {
            console.log('[SSE] E2E run completed');
            updateE2EProgress();
        });
        source.addEventListener('e2e.failed', function(event) {
            console.log('[SSE] E2E run failed');
            updateE2EProgress();
        });
        source.addEventListener('e2e.started', function(event) {
            console.log('[SSE] E2E run started');
            updateE2EProgress();
        });
        source.addEventListener('e2e.stopped', function(event) {
            console.log('[SSE] E2E run stopped');
            updateE2EProgress();
        });

    }

    if (!liveStream || typeof liveStream.createLiveEventStream !== 'function') {
        // Fail loudly rather than degrading to a dashboard with no live feed
        // and no way to tell: a silently non-live board is the defect this
        // module exists to prevent.
        console.error('[SSE] live_event_stream.js is not loaded; no live subscription');
        if (statusElement) {
            statusElement.className = 'live-stream-status live-stream-status--lost';
            statusElement.setAttribute('data-live-state', 'lost');
            statusElement.textContent =
                'Live updates unavailable — this view is a cached snapshot, not live.';
        }
        return;
    }

    const stream = liveStream.createLiveEventStream({
        target: window,
        openStream: async () => {
            // Control API requires an authenticated query-string token on
            // /api/events (security #6017), and it is single-use: every
            // reconnect must mint a new one. Fail fast if the shared helper
            // is missing — a raw EventSource would replay an unauthenticated
            // URL forever.
            if (typeof window.openAuthenticatedSseStream !== 'function') {
                throw new Error('authenticated SSE helper is not loaded');
            }
            return window.openAuthenticatedSseStream('/api/events');
        },
        wireEvents: wireEventListeners,
        onOpen: () => {
            console.log('[SSE] Connected to event stream (startup_complete=' + startupComplete + ')');
            loadDependencyProblems();
            loadStaleIssues();
            refreshViewModel({ reloadOnListChange: false });
        },
        onStatus: (status) => {
            liveStream.applyLiveStreamStatus(statusElement, status, document);
        },
    });

    stream.start();
    window.ioDashboardLiveStream = stream;

    window.addEventListener('beforeunload', () => {
        stream.stop();
    });
})();

// Helper to add keyboard support to menu items
// Elements already wired for keyboard activation. The helper synthesises a
// click, so wiring one element twice turns ONE keypress into TWO activations —
// two POSTs, two toasts — while pointer users get one. That is a silent
// behavioural split between input methods, and it cannot be prevented by
// convention across independently loaded chunks, so the helper enforces
// "exactly once" itself (#6994 round 4 F15).
const keyboardActivationWired = new WeakSet();

function addKeyboardSupport(element) {
    if (!element || keyboardActivationWired.has(element)) return;
    keyboardActivationWired.add(element);
    element.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
            // Suppress the browser's own activation as well, so a native
            // <button> is activated exactly once by this handler.
            e.preventDefault();
            element.click();
        }
    });
}

function clampPagePoint(left, top, width, height, margin = 8) {
    const minLeft = window.scrollX + margin;
    const minTop = window.scrollY + margin;
    const maxLeft = Math.max(minLeft, window.scrollX + window.innerWidth - width - margin);
    const maxTop = Math.max(minTop, window.scrollY + window.innerHeight - height - margin);
    return {
        left: Math.max(minLeft, Math.min(left, maxLeft)),
        top: Math.max(minTop, Math.min(top, maxTop)),
    };
}

function clampClientPoint(left, top, width, height, margin = 8) {
    const minLeft = margin;
    const minTop = margin;
    const maxLeft = Math.max(minLeft, window.innerWidth - width - margin);
    const maxTop = Math.max(minTop, window.innerHeight - height - margin);
    return {
        left: Math.max(minLeft, Math.min(left, maxLeft)),
        top: Math.max(minTop, Math.min(top, maxTop)),
    };
}

function normalizeToClientPoint(point) {
    if (!point) return null;
    if (Number.isFinite(point.clientX) && Number.isFinite(point.clientY)) {
        return { x: Number(point.clientX), y: Number(point.clientY) };
    }
    if (Number.isFinite(point.pageX) && Number.isFinite(point.pageY)) {
        return {
            x: Number(point.pageX) - window.scrollX,
            y: Number(point.pageY) - window.scrollY,
        };
    }
    if (Number.isFinite(point.x) && Number.isFinite(point.y)) {
        return {
            x: Number(point.x) - window.scrollX,
            y: Number(point.y) - window.scrollY,
        };
    }
    return null;
}

// Context menu
