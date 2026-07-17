---
name: shell_project_notes
description: How to inspect a project directory and write a short NOTES.md summary using shell tools.
keywords: [notes, summary, project, shell]
requires_tools: [sandbox_exec]
---

# Shell Project Notes

When the user asks for project notes or a repository summary:

1. Use `sandbox_exec` to list top-level files (`ls -la`).
2. Read key docs if present (`README*`, `pyproject.toml`, `package.json`) with `cat` or `head`.
3. Write `NOTES.md` in the project root with 3-8 bullet points.
4. Show the file with `cat NOTES.md`.
5. Reply with a short confirmation and the bullet list.

Prefer relative paths. Do not invent file contents.
