# Repository Guidelines

## Product scope

Ariadne is a **personal open-source agent kernel**.

In scope: callable agent turns, skills, toolcall, memory, sandbox port.

Out of scope: company packs, chat connectors, enterprise business adapters, multi-tenant control planes.

Normative docs live in `docs/`. Read `docs/DESIGN_PRINCIPLES.md` and `docs/NON_GOALS.md` before large changes.

## Working rules

- Prefer small, explicit modules over platform-shaped monogods.
- Fastfail: no silent fallback or compatibility soup.
- One capability registry only.
- Skills teach; tools act; memory persists; sandbox executes.
- Do not import AIFlow company packaging concepts into this repo.

## Docs-first (Phase 0)

Until code lands, update docs in the same change as any design decision.

## Future code toolchain (planned)

- Python 3.13+
- `uv` for environments
- tests with `pytest`
