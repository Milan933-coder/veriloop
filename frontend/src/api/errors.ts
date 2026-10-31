import type { ProblemDetails } from './types'

/** Error from the VeriLoop API (RFC 7807 problem+json when the server provides it). */
export class ApiError extends Error {
  readonly status: number
  readonly problem: ProblemDetails | null
  readonly requestId: string | null

  constructor(status: number, message: string, problem: ProblemDetails | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
    this.requestId = problem?.request_id ?? null
  }

  static fromResponse(status: number, body: unknown, fallbackText = ''): ApiError {
    const p = body && typeof body === 'object' ? (body as ProblemDetails & { detail?: unknown }) : null
    // FastAPI validation errors use `detail: [...]`; problem+json uses `title` + `detail` strings.
    const detail = typeof p?.detail === 'string' ? p.detail : Array.isArray(p?.detail) ? 'The request was not valid.' : ''
    const title = typeof p?.title === 'string' ? p.title : ''
    let message = [title, detail].filter(Boolean).join(': ') || fallbackText.slice(0, 200) || `HTTP ${status}`
    if (status === 429) message = `Too many requests: ${detail || 'please wait a minute and try again.'}`
    const problem: ProblemDetails | null = p
      ? { ...p, detail: typeof p.detail === 'string' ? p.detail : undefined, status }
      : null
    return new ApiError(status, message, problem)
  }
}

export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.requestId ? `${e.message} (request ${e.requestId})` : e.message
  if (e instanceof Error) return e.message
  return String(e)
}
