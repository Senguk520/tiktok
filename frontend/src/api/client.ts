const DEFAULT_API_BASE = 'http://127.0.0.1:8000'

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'

export interface RequestOptions<TBody = unknown> {
  method?: HttpMethod
  body?: TBody
  headers?: Readonly<Record<string, string>>
  signal?: AbortSignal
  idempotencyKey?: string
  csrfToken?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly payload: unknown
  readonly code: string | null
  readonly requestId: string | null

  constructor(status: number, message: string, payload: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
    const details = coreErrorDetails(payload)
    this.code = details?.code ?? null
    this.requestId = details?.request_id ?? null
  }
}

interface CoreErrorDetails {
  code: string
  message: string
  request_id: string
}

const coreErrorDetails = (payload: unknown): CoreErrorDetails | null => {
  if (!payload || typeof payload !== 'object' || !('error' in payload)) return null
  const error = (payload as { error?: unknown }).error
  if (!error || typeof error !== 'object') return null
  const candidate = error as Partial<CoreErrorDetails>
  return typeof candidate.code === 'string' &&
    typeof candidate.message === 'string' &&
    typeof candidate.request_id === 'string'
    ? (candidate as CoreErrorDetails)
    : null
}

const trustedBaseUrl = (value: string): string => {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    throw new TypeError('Core API 地址必须是有效的受信任回环 URL')
  }
  const loopbackHosts = new Set(['127.0.0.1', 'localhost', '[::1]'])
  if (
    !['http:', 'https:'].includes(parsed.protocol) ||
    !loopbackHosts.has(parsed.hostname) ||
    parsed.port !== '8000' ||
    parsed.username ||
    parsed.password ||
    (parsed.pathname !== '/' && parsed.pathname !== '') ||
    parsed.search ||
    parsed.hash
  ) {
    throw new TypeError('Core API 地址仅允许 127.0.0.1、localhost 或 ::1 的 8000 端口')
  }
  return parsed.origin
}

export const apiBaseUrl = trustedBaseUrl(
  String(import.meta.env.VITE_CORE_API_URL || DEFAULT_API_BASE),
)

let unauthorizedHandler: (() => void) | null = null

export const onUnauthorized = (handler: () => void): void => {
  unauthorizedHandler = handler
}

const parsePayload = async (response: Response): Promise<unknown> => {
  if (response.status === 204) return null
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) return response.json()
  const text = await response.text()
  return text || null
}

const errorMessage = (status: number, payload: unknown): string => {
  const coreError = coreErrorDetails(payload)
  if (coreError) return `${coreError.message}（${coreError.code}）`
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail
    if (typeof detail === 'string' && detail) return detail
  }
  return `请求失败（HTTP ${status}）`
}

export const apiRequest = async <TResponse, TBody = unknown>(
  path: string,
  options: RequestOptions<TBody> = {},
): Promise<TResponse> => {
  if (!path.startsWith('/') || path.startsWith('//') || path.includes('#')) {
    throw new TypeError('API path must be an absolute same-origin path without a fragment')
  }
  const method = options.method ?? 'GET'
  const isLogin = path === '/api/session' && method === 'POST'
  const isMutation = method !== 'GET'
  if (isMutation && !isLogin && !options.csrfToken) {
    throw new TypeError('Core API 写请求必须携带内存中的 CSRF 令牌')
  }
  if (method === 'GET' && options.body !== undefined) {
    throw new TypeError('Core API GET 请求不能携带正文')
  }
  const headers = new Headers(options.headers)
  headers.set('Accept', 'application/json')
  if (options.idempotencyKey) headers.set('Idempotency-Key', options.idempotencyKey)
  if (options.csrfToken) headers.set('X-CSRF-Token', options.csrfToken)

  let body: BodyInit | undefined
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json')
    body = JSON.stringify(options.body)
  }

  const response = await fetch(`${apiBaseUrl}${path}`, {
    method,
    headers,
    body,
    signal: options.signal,
    credentials: 'include',
    cache: 'no-store',
    redirect: 'error',
    referrerPolicy: 'no-referrer',
  })
  const payload = await parsePayload(response)
  if (!response.ok) {
    if (response.status === 401) unauthorizedHandler?.()
    throw new ApiError(response.status, errorMessage(response.status, payload), payload)
  }
  return payload as TResponse
}
