"""What a worktree's ``.venv`` has to prove before it may be reused (#53, #61).

**A worktree's ``.venv`` is either this worktree's own healthy environment, or
it is absent.** There is no third state, and "it is a directory" is not evidence
for the first one.

That was the whole of the second half of #53. Setup asked one question — is this
a symlink out of the worktree? — and a ``.venv`` that was a real directory passed
untouched on the strength of being a directory. Provisioning then runs the
repository's own recipe in the worktree, and the recipe's own reuse test was
``[ -d .venv ]``, so a directory that merely *looked* like an environment was
kept and handed to ``uv sync``. What the incident left behind was exactly that:
``_virtualenv.pth``, ``__pycache__`` and a ``bin/python`` symlinked to the system
interpreter, with no ``pyvenv.cfg`` — enough to satisfy ``[ -d .venv ]``, not
enough to be an environment. ``uv`` resolved the project as *installed but
mismatched* against an install record that lived in **another checkout**, and
reconciled it by reinstalling editable there, which moved that checkout's
``.pth``. The other checkout could then no longer import its own package.

So this module asks for two things instead, and removes what cannot show both:

* **Provenance** — every install record inside the environment names a path
  inside this worktree. An editable install is a recorded path, and a record
  naming another checkout is precisely the evidence of the incident.
* **Health** — the directory is an environment at all: ``pyvenv.cfg`` plus an
  interpreter that resolves. ``pyvenv.cfg`` is not a formality; its absence is
  *why* the interpreter resolved into a different environment.

The provenance question is not a new one in this repository. The Control Centre
launcher already asks it of its own environment — ``verify_project_install`` in
``scripts/start_control_center.sh`` fails when the installed editable resolves
outside the checkout it just synced — and answers it by *running* the
interpreter. This module answers the same question from the records instead,
because it runs before there is an environment worth running: a partial one
answers such a probe from whatever prefix it happens to resolve to, which is the
failure rather than a reading of it.

The base interpreter is deliberately not treated as an install record. Every
virtualenv points at one (``pyvenv.cfg``'s ``home``, and ``bin/python`` itself),
it lives outside every worktree by construction, and it is read shared rather
than rewritten — which is the property the ``.venv`` link did not have.

What removal costs, and why it is the right direction: a rejected ``.venv`` is
rebuilt by ``worktrees.setup``, at the price of one sync against the shared
``uv`` cache (measured in ``docs/architecture/validation.md``). A wrongly kept
one silently rewrites another checkout's environment and is discovered hours
later by whatever next needs it. A repository whose environment legitimately
records paths outside its own checkout — a sibling package installed editable
from a monorepo — pays that rebuild every session; that is a decided trade-off,
not an oversight, and it is the only shape of environment this rule rejects
without the incident's damage being possible.

Removal never follows a link out of the worktree: a symlinked ``.venv`` is
unlinked, never emptied. Deleting the contents of another checkout's environment
would be a worse version of the defect this module exists to end.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from urllib.parse import unquote, urlparse

from ._worktree_errors import WorktreeError

logger = logging.getLogger(__name__)

__all__ = [
    "VENV_DIR_NAME",
    "VenvJudgement",
    "VenvTrust",
    "ensure_worktree_owns_its_venv",
    "judge_worktree_venv",
]

VENV_DIR_NAME = ".venv"

# The file that makes a directory a virtual environment rather than a directory
# with an interpreter in it. `python -m venv`, `virtualenv` and `uv venv` all
# write it; without it an interpreter under `bin/` resolves against some other
# prefix entirely, which is how one run reached another checkout's site-packages.
_ENVIRONMENT_MARKER = "pyvenv.cfg"

# Interpreters, in the layouts the tools above produce. `exists()` follows the
# symlink, so a link to an interpreter that has since been uninstalled reads as
# what it is: an environment that cannot run.
_INTERPRETERS: tuple[Path, ...] = (
    Path("bin") / "python",
    Path("bin") / "python3",
    Path("Scripts") / "python.exe",
)

_SITE_PACKAGES_PATTERNS: tuple[str, ...] = (
    "lib/*/site-packages",
    "lib64/*/site-packages",
    "Lib/site-packages",
)

# Records that name a source tree by writing its path down. `.pth` entries are
# what `uv` and modern pip write for an editable install; `.egg-link` is
# setuptools' older form of the same statement.
_PATH_LINE_RECORDS: tuple[str, ...] = ("*.pth", "*.egg-link")

# The installer's own record of where a distribution came from, editable or not.
_DIRECT_URL_RECORDS = "*.dist-info/direct_url.json"


class VenvTrust(Enum):
    """Why a worktree's ``.venv`` may or may not be handed on as it stands.

    Attributes:
        ABSENT: There is nothing there. ``worktrees.setup`` builds it.
        OWN_ENVIRONMENT: A usable environment whose records all name this
            worktree. The only state that is reused.
        ESCAPES_WORKTREE: A link resolving outside the worktree, so a write to
            it lands in another checkout's environment.
        NOT_AN_ENVIRONMENT: A directory that cannot be used as an environment —
            the shape the incident left behind.
        FOREIGN_INSTALL_RECORD: A usable environment holding a record that names
            a path outside this worktree.
        UNREADABLE_INSTALL_RECORD: A record that could not be read, so
            provenance cannot be established either way.
    """

    ABSENT = auto()
    OWN_ENVIRONMENT = auto()
    ESCAPES_WORKTREE = auto()
    NOT_AN_ENVIRONMENT = auto()
    FOREIGN_INSTALL_RECORD = auto()
    UNREADABLE_INSTALL_RECORD = auto()


@dataclass(frozen=True)
class VenvJudgement:
    """A verdict on one worktree's ``.venv``, and the evidence behind it.

    The evidence is carried rather than re-derived because both things done with
    a verdict — logging a removal, and failing when the removal does not work —
    have to name what was found. A verdict without its evidence would report
    that something was removed without saying what it was.

    Args:
        trust: What the environment proved about itself.
        evidence: The environment described as the reason names it, ready to
            drop into a sentence: "Removed <evidence>".
    """

    trust: VenvTrust
    evidence: str

    @property
    def reusable(self) -> bool:
        """Whether provisioning may be handed this ``.venv`` as it stands."""
        return self.trust in {VenvTrust.ABSENT, VenvTrust.OWN_ENVIRONMENT}


def _site_packages_dirs(venv: Path) -> list[Path]:
    """Every ``site-packages`` the environment could hold records in.

    De-duplicated by resolved path because ``lib`` and ``Lib`` are the same
    directory on a case-insensitive filesystem, and reading the same record
    twice would report the same finding twice.
    """
    found: dict[Path, Path] = {}
    for pattern in _SITE_PACKAGES_PATTERNS:
        for candidate in sorted(venv.glob(pattern)):
            if candidate.is_dir():
                found.setdefault(candidate.resolve(), candidate)
    return list(found.values())


def _path_lines(record: Path) -> list[str]:
    """Return the path entries of a ``.pth``/``.egg-link`` record.

    A ``.pth`` line beginning with ``import`` is code the interpreter executes,
    not a path — ``_virtualenv.pth`` is exactly that — and a blank or commented
    line names nothing.

    Raises:
        OSError: If the record cannot be read. Unreadable is not evidence of
            provenance, so the caller must not mistake it for a clean record.
        UnicodeDecodeError: Same, for bytes that are not text.
    """
    lines = record.read_text(encoding="utf-8").splitlines()
    return [
        stripped
        for line in lines
        if (stripped := line.strip())
        and not stripped.startswith("#")
        and not stripped.startswith("import ")
        and stripped != "import"
    ]


def _direct_url_path(record: Path) -> str | None:
    """Return the local path a ``direct_url.json`` names, if it names one.

    A distribution installed from an index records a URL with no local path,
    which is nothing to judge. Only ``file://`` says "this came from a directory
    on this machine".

    Raises:
        OSError: If the record cannot be read.
        UnicodeDecodeError: If it is not text.
        json.JSONDecodeError: If it is not JSON. A record this module cannot
            parse is one whose provenance it cannot vouch for.
    """
    document = json.loads(record.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return None
    url = document.get("url")
    if not isinstance(url, str) or not url.startswith("file:"):
        return None
    return unquote(urlparse(url).path)


def _recorded_paths(venv: Path) -> Iterator[tuple[Path, str]]:
    """Yield every ``(record, path it names)`` pair the environment holds.

    Raises:
        OSError, UnicodeDecodeError, json.JSONDecodeError: Propagated from the
            individual record readers; see :func:`_provenance_of`.
    """
    for site_packages in _site_packages_dirs(venv):
        for pattern in _PATH_LINE_RECORDS:
            for record in sorted(site_packages.glob(pattern)):
                for line in _path_lines(record):
                    yield record, line
        for record in sorted(site_packages.glob(_DIRECT_URL_RECORDS)):
            named = _direct_url_path(record)
            if named is not None:
                yield record, named


def _provenance_of(venv: Path, worktree: Path) -> VenvJudgement:
    """Judge the environment by the paths its install records name.

    A relative entry is resolved by the interpreter against ``site-packages``,
    so it cannot leave the environment and is not evidence of anything.

    An unreadable or unparseable record is a verdict of its own rather than a
    silent pass: provenance was not established, and the environment is rebuilt
    instead of trusted. That direction costs one sync; the other direction is
    the incident.
    """
    try:
        for record, named in _recorded_paths(venv):
            if not os.path.isabs(named):
                continue
            if not Path(named).resolve().is_relative_to(worktree):
                return VenvJudgement(
                    VenvTrust.FOREIGN_INSTALL_RECORD,
                    f"venv {venv} whose install record {record} names {named}, "
                    "which is outside this worktree",
                )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return VenvJudgement(
            VenvTrust.UNREADABLE_INSTALL_RECORD,
            f"venv {venv} holding an install record that could not be read "
            f"({exc}), so it cannot be shown to belong to this worktree",
        )
    return VenvJudgement(
        VenvTrust.OWN_ENVIRONMENT, f"venv {venv}, recording only this worktree"
    )


def judge_worktree_venv(worktree_path: Path) -> VenvJudgement:
    """Decide whether ``worktree_path``'s ``.venv`` is its own healthy environment.

    Inspection only — nothing is written or removed here, so the verdict can be
    asserted on directly and :func:`ensure_worktree_owns_its_venv` has exactly
    one rule to enforce.

    The order of the questions is the order of the damage. A link out of the
    worktree is judged first and by where it points, because that one is
    dangerous whatever it points at: ``resolve()`` is non-strict, so a dangling
    link — the state the incident left behind — still names its target and is
    judged like any other.
    """
    worktree = Path(worktree_path).resolve()
    venv = Path(worktree_path) / VENV_DIR_NAME

    if venv.is_symlink():
        target = venv.resolve()
        if not target.is_relative_to(worktree):
            return VenvJudgement(
                VenvTrust.ESCAPES_WORKTREE,
                f"shared venv link {venv} -> {target}",
            )
    elif not venv.exists():
        return VenvJudgement(VenvTrust.ABSENT, f"no venv at {venv}")

    if not (venv / _ENVIRONMENT_MARKER).is_file():
        return VenvJudgement(
            VenvTrust.NOT_AN_ENVIRONMENT,
            f"venv {venv} with no {_ENVIRONMENT_MARKER}, which is not a usable "
            "environment however much it looks like one",
        )
    if not any((venv / interpreter).exists() for interpreter in _INTERPRETERS):
        return VenvJudgement(
            VenvTrust.NOT_AN_ENVIRONMENT,
            f"venv {venv} with no interpreter that resolves",
        )
    return _provenance_of(venv, worktree)


def _remove_venv(venv: Path, judgement: VenvJudgement) -> None:
    """Remove a ``.venv`` that failed the check, without following it out.

    A symlink is unlinked and anything else is deleted whole. The distinction is
    the point: emptying a link would delete the contents of the environment
    another checkout is using, which is the defect with a bigger blast radius
    rather than a fix for it.

    Raises:
        WorktreeError: If the removal fails. Handing provisioning an environment
            that was found untrustworthy is the defect, so setup fails instead.
    """
    try:
        if venv.is_symlink() or not venv.is_dir():
            venv.unlink()
        else:
            shutil.rmtree(venv)
    except OSError as exc:
        raise WorktreeError(f"Failed to remove {judgement.evidence}: {exc}") from exc


def ensure_worktree_owns_its_venv(worktree_path: Path) -> None:
    """Leave ``worktree_path`` holding its own healthy ``.venv``, or none at all.

    Nothing is installed in place of what is removed. Building the worktree's
    environment is ``worktrees.setup``'s job — the same division #48 settled,
    where the manager supplies the checkout and the provisioner supplies what
    makes it runnable. A repository that declares no setup commands gets a
    worktree with no virtualenv, which is what ``docs/architecture/validation.md``
    already says such a repository gets.

    This is also what makes the recipe's own reuse test sound. ``venv-fast``
    can see whether a directory is structurally an environment, but not whether
    it is *this* checkout's; provenance is answered here, before the recipe
    runs, so what the recipe finds is a directory setup vouched for.

    Removing the sharing is what makes concurrent provisioning safe too: there
    is no longer one environment for two runs to race over, so no lock has to be
    trusted to hold.

    Raises:
        WorktreeError: If a ``.venv`` that failed the check cannot be removed.
    """
    judgement = judge_worktree_venv(worktree_path)
    if judgement.reusable:
        logger.debug(
            "Worktree venv accepted (%s): %s",
            judgement.trust.name,
            judgement.evidence,
        )
        return

    _remove_venv(Path(worktree_path) / VENV_DIR_NAME, judgement)
    logger.warning(
        "Removed %s; this worktree's environment is worktrees.setup's to build "
        "(#53, #61)",
        judgement.evidence,
    )
