# Design: Sandbox v1 (Redesigned for Ariadne)

Status: **active design proposal** for Ariadne  
Audience: implementers  
Related: [../SANDBOX.md](../SANDBOX.md), [../TOOLCALL.md](../TOOLCALL.md)

## 0. Research summary

AIFlow sandbox stack is powerful but heavy:

```text
OpenSandbox + runtime-agent + egress gateway + mail gateway
  + SMB mounter + browser-runner + confirmation/grant plane
```

What is worth keeping for a personal kernel:

| Keep | Why |
| --- | --- |
| Tool vs CLI natural-form split | Avoid hundreds of fake function tools |
| Turn-scoped session with serial exec reuse | Simple mental model, good cleanup |
| Workspace vs session paths | Persistent vs scratch |
| Observation compression with explicit markers | Huge stdout must not blow the context |
| Heartbeat / lifecycle close in `finally` | Resource hygiene |
| Audit of cmd + exit + truncated outputs | Debuggability |

What Ariadne should **not** require:

| Leave behind | Why |
| --- | --- |
| Egress grant mesh as core | Enterprise; host can add later |
| Mail gateway / SMB / browser microservices | Not kernel identity |
| Business credential injection into sandbox | Out of personal scope |
| Privileged Docker socket mounts | Unsafe default |

---

## 1. Design goals

1. **Callable from tool loop**: `sandbox.exec` is enough for most digital work.
2. **Port-first**: backends are replaceable (`local`, `docker`, later firecracker).
3. **Honest filesystem model**: model knows where files live and what survives.
4. **Safe-enough defaults** for a developer machine without pretending to be multi-tenant isolation theater.
5. **Redesignable**: backend changes must not rewrite skills/memory/tool registry.

Non-goals: company egress adapters, connector confirmation UX, production multi-tenant sandbox fleet.

---

## 2. Conceptual model

```text
TurnApplication
  -> SandboxScope (per turn or active session)
       -> SandboxBackend.start()
       -> repeated SandboxSession.exec()
       -> SandboxSession.close()
```

### 2.1 Filesystem layout (contract)

```text
/workspace   # durable for user/project (backend-defined root)
/session     # scratch for current sandbox scope; deleted on close (default)
/tmp         # OS temp if backend provides it
```

Rules:

- Prefer writing intermediate artifacts to `/session`.
- Prefer durable outputs the user cares about to `/workspace` (or host-mapped project dir).
- Shell process state (`cd`, `export`) does **not** carry across `exec` calls unless the backend offers a persistent shell (v1 does not). Persist needed state in files.
- **`/workspace` is not per chat turn.** Multiple conversation sessions share the same workspace binding (Codex / Grok project model). See [web-workspace.md](web-workspace.md) for the web host matrix (`project` vs `per_user` modes).
- **`/session` is per sandbox scope** (typically user + conversation session under the host data dir), never the durable project tree.

### 2.2 Lifecycle modes

| Mode | Scope | Use |
| --- | --- | --- |
| `per_turn` (default) | one user turn / tool loop | simplest; cleanup easy |
| `active_session` (optional) | multi-turn with idle/max TTL | iterative coding agents |

`active_session` must be an explicit feature, not accidental delayed cleanup.

```text
active_session key: user_id + session_id
idle_ttl: e.g. 10 min
max_ttl: e.g. 60 min
serial exec only in v1
```

---

## 3. Port contracts

```python
@dataclass
class SandboxExecRequest:
    cmd: str
    cwd: str = "/workspace"
    timeout_seconds: float | None = 60
    env: dict[str, str] | None = None
    max_stdout_bytes: int = 256_000
    max_stderr_bytes: int = 64_000

@dataclass
class SandboxExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False
    compressed: bool = False
    duration_ms: int = 0
    cwd: str = "/session"

class SandboxSession(Protocol):
    id: str
    async def exec(self, req: SandboxExecRequest) -> SandboxExecResult: ...
    async def read_file(self, path: str) -> bytes: ...
    async def write_file(self, path: str, data: bytes) -> None: ...
    async def list_dir(self, path: str) -> list[str]: ...
    async def close(self, *, reason: str) -> None: ...

class SandboxBackend(Protocol):
    async def start(
        self,
        *,
        scope_key: str,
    ) -> SandboxSession: ...
```

Kernel tools depend only on these types. `scope_key` encodes the lifecycle
mode (`<session>-<turn>` for `per_turn`, `active-<session>` for
`active_session`); the workspace root is backend configuration, not a
per-start parameter.

---

## 4. Backends

### 4.1 `NullSandbox`
- Any exec → `ARIADNE_SANDBOX_DISABLED`
- Default when user did not configure a backend

### 4.2 `LocalWorkdirSandbox` (personal MVP)
- Host directory tree:
  ```text
  .ariadne/sandbox/<scope>/workspace
  .ariadne/sandbox/<scope>/session
  ```
- `exec` via `asyncio.create_subprocess_shell` or argv list with `cwd`
- env allowlist only (no leaking process secrets by default)
- path confinement: reject `..` escape from sandbox roots

Tradeoff: weak isolation (same user OS permissions). Acceptable for trusted local dev.

### 4.3 `DockerSandbox` (optional)
- One container per scope
- Mount workspace/session
- Drop caps, no privileged, no docker.sock
- Network: default off or user-explicit allow

### 4.4 Future
- Firecracker / gVisor / bubblewrap
- Remote sandbox services behind the same port

---

## 5. Tool surface

### 5.1 Primary tool: `sandbox.exec`

```json
{
  "name": "sandbox.exec",
  "arguments": {
    "cmd": "string",
    "cwd": "string?"
  }
}
```

Description should state:

- use for CLI-native work (python, git, jq, pandoc, ...)
- put durable files under `/workspace`, scratch under `/session`
- large outputs may be truncated/compressed with markers
- do not expect shell state to persist across calls

### 5.2 Optional helpers (thin)

- `sandbox.read_file` / `sandbox.write_file` only if exec-based base64 loops prove too error-prone
- `sandbox.view_image` later if multimodal needed

Avoid exploding into `browser.open`, `csv.filter`, etc. Keep CLI-native tools as CLIs.

### 5.3 Structured kernel tools stay out of shell

Memory, skill load, and other kernel protocols remain function tools. Do not force them through stringly shell.

---

## 6. Observation handling (critical UX)

Pipeline after exec:

```text
raw stdout/stderr
  -> size cap (bytes)
  -> optional compression router (logs vs text vs data)
  -> markers if truncated/compressed
  -> tool result to model
```

Markers must be explicit, e.g.:

```text
[ariadne: output truncated; kept head+tail]
[ariadne: compressed via <strategy>]
```

Rules:

- Never drop nonzero exit reasons silently
- Preserve exit_code always
- If compression backend missing → head/tail fallback, not hard fail (document as intentional observation policy; execution itself still fastfails on real errors)

---

## 7. Integration with turn loop

```text
Turn start:
  optional sandbox prestart (parallel with memory build) when policy says tools may need it

Tool call sandbox.exec:
  ensure session
  exec
  compress
  trace

Turn end (finally):
  close if per_turn
  or touch idle timer if active_session
```

Prestart concurrency should be limited (semaphore) so many parallel agents do not fork-bomb.

---

## 8. Security baseline (personal)

Defaults:

- no network unless configured
- no host home mount unless user opts in
- secret env vars not forwarded
- timeouts always
- output size always capped

Threat model honesty:

- LocalWorkdir is **not** multi-tenant isolation
- Docker improves containment but is not a security boundary against a determined local attacker with host access

---

## 9. Comparison: AIFlow vs Ariadne

| Concern | AIFlow | Ariadne |
| --- | --- | --- |
| Isolation runtime | OpenSandbox | Local / Docker port |
| Network | Egress gateway + grants | Optional host network policy |
| Mail/browser/SMB | first-class services | out of core |
| Session model | turn scope + evolution notes | per_turn + optional active_session |
| Toolbox | curated CLI image | optional profiles later |
| Credentials | gateway injection | not in core |

---

## 10. Phased delivery

### S0
- Port + NullSandbox + errors

### S1
- LocalWorkdirSandbox + sandbox.exec + traces + truncation

### S2
- Observation compression (simple head/tail + optional better compressor)
- prestart + heartbeat optional

### S3
- DockerSandbox backend
- active_session TTL mode

### S4
- Toolbox profiles (docs/data image extras) as optional install docs, not kernel code bloat

---

## 11. Acceptance scenarios

1. Create file in `/session`, read in second exec same turn → success; after turn close → gone (per_turn).
2. Write to `/workspace`, new turn → still present.
3. Command timeout → timed_out=true, clear tool error.
4. Huge yes-output → truncated/compressed with markers, exit_code preserved.
5. Path escape `../../etc/passwd` via read API → rejected.
6. Null backend → structured `ARIADNE_SANDBOX_DISABLED`.

---

## 12. Decision record

| Decision | Choice | Why |
| --- | --- | --- |
| Primary interface | `sandbox.exec` | CLI-native work stays CLI |
| Default lifecycle | per_turn | simple cleanup |
| Default backend | LocalWorkdir | personal DX |
| Isolation ceiling | honest, not multi-tenant | avoid fake security |
| Enterprise gateways | host plugins later | keep kernel thin |
| FS model | /workspace + /session | clear durability |
