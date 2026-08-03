import type { Router } from 'vue-router'

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

  constructor(status: number, message: string, payload: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
  }
}

const normalizeBaseUrl = (value: string): string => value.replace(/\/+$/, '')

export const apiBaseUrl = normalizeBaseUrl(
  String(import.meta.env.VITE_CORE_API_URL || DEFAULT_API_BASE),
)

const parsePayload = async (response: Response): Promise<unknown> => {
  if (response.status === 204) return null
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) return response.json()
  const text = await response.text()
  return text || null
}

const errorMessage = (status: number, payload: unknown): string => {
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
  if (!path.startsWith('/')) throw new TypeError('API path must start with /')
  const method = options.method ?? 'GET'
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
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, payload), payload)
  return payload as TResponse
}

export const installApiErrorHandler = (router: Router): void => {
  router.onError((error) => {
    console.error(error instanceof ApiError ? error.message : '页面加载失败')
  })
}