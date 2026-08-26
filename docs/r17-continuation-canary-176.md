# R17 post-#173 continuation canary target

Inert companion content for the disposable canary fixture in
`tests/unit/test_r17_continuation_live_canary_176.py`.

- Controller: issue #175. Fixture: issue #176.
- Product baseline: `de1dbd5c841126c07ef8aca03e6319e8f3277477`.
- Purpose: give the candidate ordinary content to carry through review while the
  canary makes the first canonical full publication gate fail exactly once and
  pass unchanged on the same SHA after restart.

This file and the canary module are disposable. Neither is product
functionality, and this branch is not for product `main`.
