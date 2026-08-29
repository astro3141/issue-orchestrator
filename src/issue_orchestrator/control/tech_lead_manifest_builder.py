"""Tech Lead manifest builder - creates manifests for tech_lead sessions.

Queries GitHub to find PRs that need tech_lead (carry the tech_lead watch label
but not tech-lead-reviewed or tech-lead-failed labels). Both halves of that
question — which label selects a pull request, and whether a selected pull
request is still a candidate — come from :class:`~.tech_lead_candidate_policy.
TechLeadCandidatePolicy`, the single owner shared with threshold fact gathering
and with the completion-time disposition planner, so the PR set that trips the
threshold is exactly the set the session audits and exactly the set the review
settles.
"""

import logging
import time

from ..domain.tech_lead_manifest import TechLeadManifest, PRToReview, PRFiles
from ..ports import RepositoryHost
from .tech_lead_candidate_policy import TechLeadCandidatePolicy

logger = logging.getLogger(__name__)


class TechLeadManifestBuilder:
    """Builds tech_lead manifests by querying for PRs that need review."""

    def __init__(
        self,
        repository_host: RepositoryHost,
        *,
        candidate_policy: TechLeadCandidatePolicy,
    ):
        self._host = repository_host
        # The selecting label is the POLICY's, not a second parameter beside
        # it: a builder that could be handed a watch label the policy does not
        # know about is a builder that can audit a set nobody counted.
        self._watch_label = candidate_policy.watch_label
        self._policy = candidate_policy

    def build(self, data_dir: str) -> TechLeadManifest:
        """Build a tech_lead manifest with PRs that need review.

        Args:
            data_dir: Relative path from worktree root where data files will go

        Returns:
            TechLeadManifest with PRs to review (data not yet downloaded)
        """
        prs = self._host.get_prs_with_label(self._watch_label, state="all")
        logger.info(
            "[tech_lead] Found %d PRs with '%s' label",
            len(prs), self._watch_label
        )

        # Filter through the shared candidate owner (already-triaged PRs and,
        # on filtered runs, PRs outside the filter label scope drop out).
        prs_to_review = [
            PRToReview(
                number=pr.number,
                title=pr.title,
                url=pr.url,
                branch=pr.branch,
                # The candidate identity, read from the SAME observation that
                # selected the PR (#345). An unreadable head stays "" — the PR
                # is still audited, it simply cannot receive a merge-facing
                # disposition later — rather than dropping out of the manifest
                # and re-opening the threshold/manifest divergence #6768 closed.
                head_sha=pr.head_sha or "",
                files=PRFiles(),
            )
            for pr in prs
            if self._policy.is_candidate(pr.labels)
        ]

        logger.info(
            "[tech_lead] %d PRs need tech_lead review (filtered from %d)",
            len(prs_to_review), len(prs)
        )

        return TechLeadManifest(
            session_type="tech_lead",
            generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            data_dir=data_dir,
            prs=prs_to_review,
        )
