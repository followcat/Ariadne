# Non-Goals

This list protects Ariadne from becoming an accidental enterprise platform rewrite.

## Hard non-goals (core)

1. **Company Pack / multi-company extension model** — replaced by **official
   optional plugins** (`src/ariadne/plugins/`): odoo / gitlab / redmine are
   supported integrations enabled with a URL + token/api key. Config is a
   **user attribute** (CLI: `~/.ariadne/plugins.json` by default, optional
   workspace override; Web: per registered account via `/api/me/plugins`).
   Plugin tools register into the one capability registry at compose time.
   The kernel core never depends on them, and there is no multi-company pack
   manifest/namespace system.
2. **First-class WeCom / Feishu / Telegram / Slack connectors**
3. **Business system adapters inside core** — they live in the official
   plugin modules, configured per host, never as required kernel code
4. **Mandatory multi-tenant SaaS control plane**
5. **Enterprise egress/mail gateway mesh as required architecture**
6. **Second tool registry** for demos, benchmarks, or plugins
7. **Silent compatibility fallbacks** for undefined behavior
8. **In-process arbitrary plugin code execution** from skill packs
9. **Secret material in prompts, skills, or tool descriptions**
10. **Forking AIFlow company deployment topology "as the product"**

## Soft non-goals (not now)

- Full HTTP product API parity with AIFlow Responses
- Background skill learning workers
- Perfect conversation-state projection on day one
- Browser microservice
- Mobile clients

## What “done” is not

Ariadne is not done when it has more services.  
Ariadne is done when a developer can **call an agent** with skills, tools, and memory—and understand every failure.

## Allowed future plugins (outside core)

External repos may implement:

- connectors that call Ariadne
- company-specific tool handlers registered by the host
- hardened sandbox backends

They must depend **on** Ariadne contracts, not reverse the dependency.
