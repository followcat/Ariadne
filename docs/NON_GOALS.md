# Non-Goals

This list protects Ariadne from becoming an accidental enterprise platform rewrite.

## Hard non-goals (core)

1. **Company Pack / multi-company extension model**
2. **First-class WeCom / Feishu / Telegram / Slack connectors**
3. **Business system adapters** (Odoo, GitLab, Redmine, internal ERP) inside core
4. **Mandatory multi-tenant SaaS control plane**
5. **Enterprise egress/mail gateway mesh as required architecture**
6. **Second tool registry** for demos, benchmarks, or plugins
7. **Silent compatibility fallbacks** for undefined behavior
8. **In-process arbitrary plugin code execution** from skill packs
9. **Secret material in prompts, skills, or tool descriptions**
10. **Forking AIFlow company deployment topology “as the product”**

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
