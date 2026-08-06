import { afterEach, describe, expect, it, vi } from 'vitest'

import { coreApi } from '@/api/core'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Miaoshou shop query API boundary', () => {
  it('uses the protected read-only route and explicit platform/site selectors', async () => {
    const fetchMock = vi.fn(
      async (_input: RequestInfo | URL, _request?: RequestInit): Promise<Response> =>
        new Response(
          JSON.stringify({
            provider: 'miaoshou',
            platform: 'tiktok',
            site: 'US',
            page_no: 1,
            page_size: 100,
            next_page_no: null,
            items: [],
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await coreApi.miaoshouShops('tiktok', 'US')

    const [input, request] = fetchMock.mock.calls[0] ?? []
    expect(String(input)).toContain('/api/miaoshou/shops?')
    expect(String(input)).toContain('platform=tiktok')
    expect(String(input)).toContain('site=US')
    expect(String(input)).toContain('page_no=1')
    expect((request as RequestInit).cache).toBe('no-store')
  })
})