# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Layout

This repo uses a single-context layout:

- `CONTEXT.md` at the repo root for shared domain language.
- `docs/adr/` for architectural decision records.

## Before exploring, read these

- `CONTEXT.md` at the repo root if it exists.
- `docs/adr/` ADRs that touch the area being changed.

If these files do not exist yet, proceed silently. They can be created later when the domain language or architectural decisions need to be documented.

## Use the glossary's vocabulary

When naming a domain concept in an issue title, refactor proposal, hypothesis, or test name, use the term as defined in `CONTEXT.md`.

If the concept is missing from the glossary, either avoid inventing new language or note it as something to clarify later.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly instead of silently overriding it.
