export type SessionRow = {
  session_id: string
  turns: number
  mtime: number
  preview: string
  title?: string
}

export type Me = {
  username: string
  provider_configured: boolean
  model?: string
  base_url?: string
  /** Host absolute path of the active /workspace root for this account. */
  workspace?: string
  /** project (shared serve cwd) | per_user (account tree). */
  workspace_mode?: 'project' | 'per_user' | string
  /** Serve-process project folder (always the open-folder root). */
  project_root?: string
}

function authHeaders(token: string): HeadersInit {
  return { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }
}

export async function api(
  path: string,
  token: string,
  opts: RequestInit = {},
): Promise<Response> {
  const headers = {
    ...authHeaders(token),
    ...(opts.headers || {}),
  }
  const r = await fetch(path, { ...opts, headers })
  if (r.status === 401) {
    const err = new Error('unauthorized') as Error & { status: number }
    err.status = 401
    throw err
  }
  return r
}

export async function login(username: string, password: string) {
  return fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
}

export async function register(username: string, password: string) {
  return fetch('/api/auth/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username, password }),
  })
}

export type StreamEvent = {
  kind: string
  data?: Record<string, unknown>
  result?: Record<string, unknown> | null
  error?: { code?: string; message?: string; details?: unknown }
}

/** Parse SSE body chunks into discrete events. */
export function parseSseBuffer(buf: string): { events: StreamEvent[]; rest: string } {
  const parts = buf.split('\n\n')
  const rest = parts.pop() || ''
  const events: StreamEvent[] = []
  for (const part of parts) {
    const line = part.trim()
    if (!line.startsWith('data:')) continue
    try {
      events.push(JSON.parse(line.replace(/^data:\s?/, '')))
    } catch {
      /* skip bad frames */
    }
  }
  return { events, rest }
}
