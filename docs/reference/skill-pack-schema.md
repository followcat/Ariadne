# Reference: Skill Pack Schema (v0)

## Directory

```text
<skill_name>/
  SKILL.md
  references/
  templates/
  scripts/
  assets/
  agents/index.yaml
  agents/runtime.yaml
```

## Frontmatter

```yaml
---
name: my_skill
description: Short one-line discovery text
keywords: [optional, list]
requires_tools: [memory, sandbox.exec]
tags: [optional]
version: "0.1"
---
```

## index.yaml (optional)

```yaml
display_name: My Skill
short_description: Short catalog line
```

## runtime.yaml (optional)

```yaml
requires_tools:
  - memory
keywords:
  - example
```

## Validation

- name regex: `^[a-z0-9][a-z0-9._-]{0,63}$`
- frontmatter name == directory name
- description required
- unknown top-level supporting dirs rejected (allowlist only)
