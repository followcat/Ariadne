# Sandbox

## 1. Stance

Sandbox is important, but **not** Ariadne’s identity. It is a **port** the kernel can call.

Ariadne may redesign sandbox freely. Enterprise topologies (egress gateway, mail gateway, SMB mounts, browser microservices) are **not** required.

## 2. Why a port

Tools like `sandbox.exec` need somewhere to run commands and touch files. Hosts differ:

| Host | Likely backend |
| --- | --- |
| Local dev | subprocess in a workdir |
| Docker-friendly user | one container per session/turn |
| Hardened personal lab | Firecracker / other isolation |
| Tests | fake sandbox recording commands |

The kernel should not hardcode one of these.

## 3. SandboxPort (target)

```python
class SandboxPort(Protocol):
    async def start(self, *, session_id: str, turn_id: str) -> SandboxSession: ...

class SandboxSession(Protocol):
    async def exec(
        self,
        cmd: str,
        *,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        env: dict[str, str] | None = None,
    ) -> SandboxExecResult: ...

    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[str]: ...
    async def close(self, *, reason: str) -> None: ...
```

```python
@dataclass
class SandboxExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
```

## 4. Tool boundary

Recommended model-facing tool:

```text
sandbox.exec(cmd, cwd?)
```

Principles:

1. CLI-native software stays CLI-native inside the sandbox (python, jq, git, etc.).
2. Do not invent dozens of first-class function tools that re-wrap every CLI flag.
3. Structured kernel capabilities (memory, skill load) stay function tools — not forced through shell string protocols.
4. Observation compression may truncate huge outputs with clear markers; never silently drop errors.

## 5. Filesystem contract (model-facing)

```text
/workspace   # durable project (or per-user) tree — shared across chat sessions
/session     # scratch for the active sandbox scope — wiped on close
```

This matches personal agents (Codex / Grok): **open a project, many threads,
one file tree**. Chat session id does not clone `/workspace`.

Web host binding and multi-account modes: [design/web-workspace.md](design/web-workspace.md).

## 6. Lifecycle

Preferred personal v1:

```text
turn start -> ensure session -> execs -> turn end -> close/reuse policy
```

Policies:

- **ephemeral per turn** (simple, safer)
- **reuse per session** (faster iterative file work)

Document which policy a backend uses. Fastfail if `sandbox.exec` is called with `NullSandbox`.

## 7. Security baseline (personal)

Even personal software should:

- avoid mounting sensitive host secrets by default
- avoid privileged containers by default
- bound timeouts and output sizes
- redacted traces for obvious secret patterns

Ariadne does **not** require:

- company egress grant stores
- mandatory human confirmation control planes
- business credential injection gateways

Hosts may add those outside the kernel.

## 8. What we learn from enterprise sandbox designs (and leave behind)

Keep:

- clear Core vs execution environment split
- CLI toolbox idea for digital work
- audit of commands and artifacts
- separation of tool vs CLI natural forms

Leave behind (core non-goals):

- OpenSandbox + egress + mail multi-service mesh as required architecture
- connector-driven confirmation UX as kernel dependency
- business system HTTP adapters as sandbox concerns

## 9. Redesign permission

This document explicitly allows sandbox redesign:

- replace backend
- change lifecycle
- change packaging of toolboxes

without rewriting skills/memory/tool registry — as long as `SandboxPort` and `sandbox.exec` semantics remain honest.

## 10. Implementation phases

1. `NullSandbox` + clear errors
2. `LocalWorkdirSandbox` (subprocess)
3. Optional Docker backend
4. Output compression + file helpers
5. Toolbox profiles (optional image/docs extras)

## Deep design

See [design/sandbox-v1.md](design/sandbox-v1.md) for backend ports, filesystem contract, lifecycle modes, and observation handling.
