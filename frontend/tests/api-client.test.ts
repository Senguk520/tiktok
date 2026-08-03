import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError, apiRequest } from '@/api/client'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Core API error and mutation contract', () => {
  it('reads the stable redacted error envelope', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(
          JSON.stringify({
            error: {
              code: 'COMMERCE_ACCESS_BLOCKED',
              message: 'commerce preconditions are not satisfied',
              request_id: 'request-1',
            },
          }),
          { status: 403, headers: { 'Content-Type': 'application/json' } },
        ),
      ),
    )

    const failure = await apiRequest('/api/example').catch((error: unknown) => error)
    expect(failure).toBeInstanceOf(ApiError)
    expect(failure).toMatchObject({
      status: 403,
      code: 'COMMERCE_ACCESS_BLOCKED',
      requestId: 'request-1',
    })
    expect(String(failure)).not.toContain('token')
  })

  it('sends CSRF and idempotency headers without enabling caches or redirects', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await apiRequest('/api/example', {
      method: 'POST',
      body: { operation: 'safe' },
      csrfToken: 'csrf-in-memory',
      idempotencyKey: 'operation-key-0001',
    })

    const [, request] = fetchMock.mock.calls[0] ?? []
    const options = request as RequestInit
    const headers = options.headers as Headers
    expect(headers.get('X-CSRF-Token')).toBe('csrf-in-memory')
    expect(headers.get('Idempotency-Key')).toBe('operation-key-0001')
    expect(options.credentials).toBe('include')
    expect(options.cache).toBe('no-store')
    expect(options.redirect).toBe('error')
    expect(options.referrerPolicy).toBe('no-referrer')
  })
})