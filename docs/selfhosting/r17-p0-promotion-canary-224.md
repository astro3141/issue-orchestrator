# R17 P0 promotion canary — issue #224

**Disposable canary evidence. Not a product or runtime contract.**

This file exists only as the inert payload of canary fixture issue #224, which
is Proof B for controller issue #223: one ordinary self-hosting lifecycle run on
the clean recovery checkout, driven by the fresh provisional built from
`3ab3c504b8710d6e7e44e2f71cf793b9907c7a89`.

## What this file is

- Lifecycle evidence: it proves an ordinary Actor → validation → review → PR
  cycle completed. It proves nothing about product behaviour.
- Documentation only: it changes no source, config, test, schema, workflow, or
  policy, and nothing at runtime reads it.
- Superseding: fixture v3 supersedes the earlier canary fixtures #218 and #221;
  neither of those is reused.

## What this file is not

- Not a specification, runbook, or reference anyone should build on.
- Not a self-hosting readiness claim — see `SELF_HOSTING_READY.md` for the
  actual milestone record.
- Not mergeable into product `main`. The pull request carrying this file is
  canary evidence and must not be merged.

## Disposal

Controller issue #223 owns the PASS/STOP predicates for this run. Once #223
records its verdict, this fixture and its pull request are closed per the
controller's instruction, and this file is discarded with them.
