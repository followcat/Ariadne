# Design: Memory Scopes (Personal / 2C)

Status: **active design** for personal Ariadne  
Audience: implementers  
Related: [../MEMORY.md](../MEMORY.md), [memory-v1.md](memory-v1.md),
[memory-search.md](memory-search.md), [atelier.md](atelier.md),
[web-workspace.md](web-workspace.md)

## 0. Purpose

Memory is not one flat bag. Personal Ariadne isolates durable facts, project
facts, and conversation state by **scope**. Scopes answer: *where does this
entry live, and who may read it on the next turn?*

This doc is **2C / single-operator** only. It is not a multi-tenant control
plane. See [../NON_GOALS.md](../NON_GOALS.md).

---

## 1. Scope taxonomy (normative)

| Scope | Question | Lifetime | Typical content |
| --- | --- | --- | --- |
| **user** | What is true about *this person* across projects? | Survives workspaces and sessions | Preferences, standing instructions, identity facts the user cares about |
| **workspace** | What is true about *this project / atelier*? | Survives sessions inside the workspace | Project facts, path conventions (as memory, not ops handbook), long-lived decisions |
| **session** | What is true *in this conversation thread*? | Session lifetime (or until projected/archived) | Transcript (L0), L1 summaries, L2 conversation state, ephemeral bindings |

Hard rules:

1. **Read path always resolves a concrete scope.** No silent “search everything
   and hope.”
2. **Writes name their scope.** Curated `add`/`replace`/`forget` and
   `memory_search` take an explicit scope (or a host default that is still
   recorded in traces).
3. **Session never upgrades to user** without an explicit curated write (or a
   later consolidation apply the user accepts).

---

## 2. Host layout

CLI and Web are different **hosts**. They do not share a single product “user”
object. See [web-workspace.md](web-workspace.md).

### 2.1 CLI host

| Scope | Default location | Notes |
| --- | --- | --- |
| **user** | `~/.ariadne/memory/` (or host `user_data` equivalent) | Cross-workspace durable curated (L3 user) |
| **workspace** | `{workspace}/.ariadne/memory/` | Project-local; atelier main uses atelier `.ariadne/` |
| **session** | under workspace (or atelier) data dir, keyed by `session_id` | Transcript, L1/L2, semantic index for that thread |

```text
CLI (open folder)
  ~/.ariadne/                  # user-level host data
    memory/                    # user scope (L3 curated default)
  <project>/
    .ariadne/                  # workspace data_dir
      memory/                  # workspace scope
      sessions/                # session transcripts
      ...

CLI (atelier)
  ~/.ariadne/ateliers/<slug>/  # or ARIADNE_ATELIER_ROOT
    KNOWLEDGE.md               # ops handbook — NOT memory scope
    .ariadne/
      memory/                  # main workspace memory
      sessions/
      scopes/branch-<slug>/    # branch-isolated memory + sandbox data
```

### 2.2 Web host

| Scope | Default location | Notes |
| --- | --- | --- |
| **user** | `{serve}/web/users/<account>/memory/` | Per registered account |
| **workspace** | atelier data under that account (`…/ateliers/<slug>/.ariadne/…`) | Ordinary non-atelier chat may use serve open-folder `.ariadne` — document which |
| **session** | under account `sessions/` + memory keyed by agent session id | Branch: `scopes/branch-<slug>/` |

```text
Web
  {data}/web/users/<account>/
    memory/                    # user scope
    sessions/
    ateliers/<slug>/
      KNOWLEDGE.md
      .ariadne/
        memory/                # atelier main workspace scope
        scopes/branch-<x>/     # branch isolation
```

Identity:

| Host | Identity for memory keys |
| --- | --- |
| CLI | OS user + paths; `user_id` may be omitted only when the host is single-user and the default is explicit in Settings |
| Web | Registered account name; maps to `user_id` for memory paths |

---

## 3. What belongs where

| Kind of fact | Scope | Layer (when applicable) |
| --- | --- | --- |
| “Prefer tables over prose” | **user** | L3 curated |
| “I am allergic to shellfish” (user-stated durable) | **user** | L3 curated |
| “This repo’s test command is `uv run pytest`” as a *recallable project fact* | **workspace** | L3 curated (or L2 if session-only) |
| “Todo: fix login redirect” | **session** | L2 (not L3 when L2 enabled) |
| “Yesterday we chose Postgres over SQLite” in this chat | **session** | L1 / L4 / search hits |
| “We just agreed on the API name” (last few turns) | **session** | L0 recent raw |

### Do / don’t

| Do | Don’t |
| --- | --- |
| Put standing prefs in **user** | Dump full transcripts into **user** |
| Put project-long decisions in **workspace** when they should outlive one chat | Dual-write the same fact into user + workspace without reason |
| Keep todos / entity fields in **session** L2 | Store same-session todos in L3 curated when L2 is on ([memory-v1.md](memory-v1.md) §1) |
| Isolate atelier **branch** memory under branch scope | Let branch writes pollute main workspace memory |

---

## 4. Boundary: `KNOWLEDGE.md` vs Memory

Atelier **小本本** (`KNOWLEDGE.md`) is an **ops handbook**, not a memory layer.

| | **KNOWLEDGE.md** | **Memory (L0–L4 + search)** |
| --- | --- | --- |
| Owner | User (main post-turn may append small 约定) | System + explicit `memory` tool / projection |
| Content | How this workshop runs: paths, process, cautions | Facts, prefs, episodic recall, current state |
| Injection | Always (budgeted, always-on system inject) | Layered `build_context` + on-demand `memory_search` |
| Branch | Read-only reference; merge does not write main | Branch has own memory data_dir |
| Authority for “what is true now” | Not a state reducer | L2 when enabled; else recent raw + curated |

```text
KNOWLEDGE.md  →  “how we work here”
Memory        →  “what we know / what happened / what is true now”
```

Rules:

1. Do **not** treat Memory as a second KNOWLEDGE.md (no free-form ops dump into L3).
2. Do **not** treat KNOWLEDGE.md as episodic recall (no full chat history).
3. Paths and standing workshop rules → KNOWLEDGE; multi-hop “what did we say
   last week” → Memory search ([memory-search.md](memory-search.md)).

See [atelier.md](atelier.md) §7.

---

## 5. `user_id` mapping (personal, not SaaS)

### 5.1 Intent

`user_id` exists so durable **user** scope and isolation checks are honest —
even in personal software with multi-session or multi-account Web.

It is **not** an org/tenant control plane.

### 5.2 Normative behavior

| Situation | Required behavior |
| --- | --- |
| Web registered account | Host sets `user_id` (or equivalent stable key) from the account; user-scope paths under that account |
| CLI single-operator | Host may use a fixed default (e.g. `"local"`) **or** omit only if Settings document “single-user mode” and all stores use the same data root |
| Call supplies `user_id` that does not match host identity | **Fastfail** — do not silently remap or ignore |
| Call omits `user_id` where host requires multi-account isolation (Web) | **Fastfail** — do not fall back to another account’s memory |
| Session belongs to user A; request claims user B | **Fastfail** isolation error |

Anti-patterns:

- Ignoring `user_id` “because personal MVP”
- Merging all users into one SQLite table without a column and filtering only
  “when convenient”
- Cross-account semantic search on Web

Personal defaults may use one physical DB file **with** a `user_id` dimension,
or separate directories per account. Either way: **no silent ignore**.

### 5.3 Scope resolution algorithm (conceptual)

```text
resolve_memory_roots(host, user_id, workspace_binding, session_id)
  user_root      ← host.user_data(user_id) / memory
  workspace_root ← active workspace or atelier .ariadne (branch scope if branch)
  session_key    ← session_id (+ atelier session suffix when applicable)

build_context / memory_search
  must use roots for the requested scope only
  cross-scope reads require explicit multi-scope API (v2+) — not silent union
```

For v1 graded search, `memory_search(scope=…)` is **one scope per call**.
The model may call twice if it needs user + workspace.

---

## 6. Interaction with layers

| Layer | Default scope affinity |
| --- | --- |
| L0 recent raw | **session** |
| L1 turn summaries | **session** |
| L2 conversation state | **session** |
| L3 curated durable | **user** and/or **workspace** (explicit) |
| L4 semantic index | **workspace data_dir** (all sessions in that index); **user episodic** under `user_memory_dir/episodic/` (cross-workspace, same operator) |
| `memory_search` | caller-chosen: `session` \| `workspace` \| `user` |

`build_context` each turn loads high-signal layers for the **active** session
and applicable curated scopes (user + workspace), within budgets. Deep episodic
recall is **not** dumped every turn — see [memory-search.md](memory-search.md).

### 6.1 User episodic layout (implemented)

```text
CLI:  ~/.ariadne/memory/
        curated.json              # user-scope L3
        episodic/semantic.json    # user-wide L4 chunks (dual-written on turn)

Web:  {serve}/web/users/<account>/memory/
        curated.json
        episodic/semantic.json
```

On turn complete the host indexes the same turn into the **workspace** semantic
index and, when configured, the **user episodic** index. Branch atelier
sessions still share the account user root (same person); they must not write
another account’s tree.

Shared curated and semantic JSON files use **fcntl-locked** read-modify-write.

---

## 7. Fastfail checklist

| Condition | Outcome |
| --- | --- |
| Unknown scope name | error, no default-to-session |
| Curated capacity exceeded | structured capacity error |
| user_id mismatch / missing when required | isolation / validation error |
| Branch session writing main memory path | must not happen; path resolution bug = fail |
| Store error on a configured layer | `LayerReport.failed` or tool error — not empty success |

---

## 8. Non-goals

- Multi-tenant org / team shared memory meshes
- Company knowledge packs as a scope
- Silent cross-user or cross-account recall
- Replacing KNOWLEDGE.md with Memory or the reverse
- Automatic promotion of every session fact to user scope

---

## 9. Related

- [memory-v1.md](memory-v1.md) — L0–L5 taxonomy and authority
- [memory-search.md](memory-search.md) — graded retrieval / `memory_search`
- [atelier.md](atelier.md) — branch isolation + KNOWLEDGE.md
- [web-workspace.md](web-workspace.md) — CLI vs Web identity
- [../MEMORY.md](../MEMORY.md) — product-level memory contract
