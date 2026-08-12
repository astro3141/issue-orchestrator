"""Registration that has to reach every test root, not just ``tests/``.

``pyproject.toml`` declares two testpaths - ``tests`` and
``packages/agent_runner/tests`` - and ``conftest.py`` is directory-scoped, so
anything registered in ``tests/conftest.py`` stops at that tree.  Codex-home
isolation is an invariant of the whole run ("a newly added live test cannot
leak by omission"), which only holds if the fixtures are registered above every
root.  This directory is the one ancestor both testpaths share, so they live
here rather than in either tree.

Keep this file to registration.  Fixtures with a single tree's worth of meaning
belong in that tree's ``conftest.py``.
"""

from tests.codex_home import (  # noqa: F401  (imported to register the fixtures)
    codex_home_guard,
    codex_home_session,
    isolated_codex_home,
)
