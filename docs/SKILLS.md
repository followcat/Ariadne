# Skills

## 1. Purpose

Skills are **procedural guidance packages** for the model: when to act, how to sequence tools, domain notes, templates, and references.

Skills are **not**:

- executable plugins that mutate the kernel
- a second tool registry
- a place to store secrets
- company deployment packs

## 2. Three-layer skill runtime

Ariadne adopts a three-layer model proven useful in production agent cores:

```text
1) Skill index / selection plan   — short discovery text
2) search_skills / load_skill     — on-demand retrieval of full guidance
3) callable tools                 — real side-effecting actions
```

Global system policy explains *that* skills exist and the selection discipline.  
It does **not** dump every `SKILL.md` into every turn.

## 3. Skill pack layout

```text
skills/
  my_skill/
    SKILL.md
    references/           # optional long-form docs
    templates/            # optional
    scripts/              # optional examples (not auto-executed by kernel)
    assets/               # optional
    agents/
      index.yaml          # short catalog fields
      runtime.yaml        # optional metadata: requires_tools, keywords, ...
```

### 3.1 `SKILL.md`

- YAML frontmatter required
- Required fields: `name`, `description`
- Optional: `keywords`, `requires_tools`, `tags`, `version`
- Body is the primary guidance loaded by the model

Validation rules (v1):

- name matches `^[a-z0-9][a-z0-9._-]{0,63}$`
- frontmatter `name` equals directory name
- description length bounded
- total body size bounded (exact budgets configurable; fail when exceeded)

### 3.2 Index fields

Short fields for discovery only, e.g.:

```yaml
display_name: Scheduled Task Runbook
short_description: How to create reliable recurring work
```

Do not put long calling policies in the index.

## 4. Runtime tools

These are normal tools in the **one** capability registry:

| Tool | Role |
| --- | --- |
| `search_skills` | semantic or lexical search over installed skills |
| `load_skill` / `load_body` | load full skill body + selected references |
| `skill_manage` (optional later) | create/update **user** skills with versioning |

Builtin catalog skills are read-only in personal v1 unless explicitly configured otherwise.

### 4.1 Load semantics (design preference)

Prefer **turn-scoped** load:

- `load_skill` returns body as tool result for this loop
- avoids permanent system-prompt growth after load
- next turn re-selects/reloads as needed

Legacy “inject into system forever this session” mode is discouraged.

## 5. Skill selection plan

On each turn, `SkillSelector.plan(query, session)` may produce:

```text
auto_load:      skills with very high confidence (small N)
recommended:    likely relevant short entries
other:          compact remainder or “use search_skills”
```

Design lessons to preserve:

1. Linear full indexes collapse when skill count grows (~20 already painful).
2. Keyword-only search is a weak ceiling; vector/hybrid search is the target.
3. Selection results should sit in a **strong attention** region of the prompt (near user input), not buried mid-prompt.
4. Ranking should be explainable (scores in traces), even if weights start hand-tuned.

### 5.1 Selection discipline (policy text)

The core policy should include rules equivalent to:

```text
- Prefer recommended skills when they match the user goal.
- If none match, call search_skills with the user intent.
- Load a skill before inventing multi-step workflows for that domain.
- Skills guide tool use; they do not replace tools.
- Do not paste entire references unless needed; load targeted sections when supported.
```

## 6. Skills vs tools vs memory

| Need | Use |
| --- | --- |
| “How do I do X well?” | Skill |
| “Do X now” | Tool |
| “What did we decide about X?” | Memory |
| “Run a command / edit files” | Sandbox tool |

Anti-pattern: encoding durable user facts only inside a skill body.

## 7. Authoring checklist

New skill:

1. Create folder + `SKILL.md` frontmatter
2. Write short description for index
3. Put long rules in `references/`
4. Declare `requires_tools` if the workflow depends on specific tools
5. Validate with `ariadne skills validate`
6. Add at least one example trigger in docs or evals

## 8. Personal vs builtin namespaces

Personal v1:

```text
builtin/...     # shipped examples
user/...        # local user skills
```

No company namespace system, no multi-tenant pack install protocol.

## 9. Non-goals for skills subsystem

- Hot-loading arbitrary Python into the kernel process from skill packs
- Connector-owned skills that execute platform side effects without tools
- Background company skill learning pipelines as a required component

Background “skill learning” may appear later as an optional module; it must still write versioned skill data, not mutate kernel code.

## 10. Implementation phases

See [ROADMAP.md](ROADMAP.md). Skills MVP order:

1. filesystem store + validate
2. short index injection
3. `search_skills` (lexical) + `load_skill`
4. hybrid/vector search
5. richer selection plan + budgets
