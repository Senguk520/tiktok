import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '@/api/client'
import { coreApi } from '@/api/core'
import { router } from '@/router'
import { useAdminSession } from '@/state/session'

const EXPIRES_AT = '2026-08-07T12:00:00.000Z'

const loginResponse = (csrfToken: string) => ({
  authenticated: true,
  csrf_token: csrfToken,
  expires_at: EXPIRES_AT,
})

afterEach(() => {
  vi.restoreAllMocks()
  useAdminSession().expire()
})

describe('管理员 Session 状态边界', () => {
  it('logout 成功后清理 CSRF token 和过期时间', async () => {
    const session = useAdminSession()
    vi.spyOn(coreApi, 'login').mockResolvedValue(loginResponse('csrf-before-logout'))
    vi.spyOn(coreApi, 'logout').mockResolvedValue({ authenticated: false })

    await session.login('bootstrap-secret')
    expect(session.csrfToken.value).toBe('csrf-before-logout')
    expect(session.expiresAt.value).toBe(EXPIRES_AT)

    await session.logout()

    expect(session.csrfToken.value).toBeNull()
    expect(session.expiresAt.value).toBeNull()
    expect(session.canWrite.value).toBe(false)
  })

  it('401 和 expire 都会清理内存中的 token', async () => {
    const session = useAdminSession()
    const loginMock = vi.spyOn(coreApi, 'login')
    loginMock
      .mockResolvedValueOnce(loginResponse('csrf-before-expire'))
      .mockResolvedValueOnce(loginResponse('csrf-before-401'))
    const statusMock = vi.spyOn(coreApi, 'sessionStatus')
      .mockRejectedValue(new ApiError(401, 'unauthorized', null))

    await session.login('bootstrap-secret')
    session.expire()
    expect(session.csrfToken.value).toBeNull()
    expect(session.expiresAt.value).toBeNull()

    await session.login('bootstrap-secret')
    await session.check()

    expect(statusMock).toHaveBeenCalledTimes(1)
    expect(session.csrfToken.value).toBeNull()
    expect(session.expiresAt.value).toBeNull()
    expect(session.canWrite.value).toBe(false)
  })

  it('readonly 即使仍有 token 也不授予写权限', async () => {
    const session = useAdminSession()
    vi.spyOn(coreApi, 'login').mockResolvedValue(loginResponse('csrf-readonly'))
    vi.spyOn(coreApi, 'sessionStatus').mockResolvedValue({
      authenticated: false,
      expires_at: EXPIRES_AT,
    })

    await session.login('bootstrap-secret')
    await session.check()

    expect(session.phase.value).toBe('readonly')
    expect(session.csrfToken.value).toBe('csrf-readonly')
    expect(session.canRead.value).toBe(true)
    expect(session.canWrite.value).toBe(false)
  })

  it('登出后重新登录的导航会再次执行 session check', async () => {
    const session = useAdminSession()
    const statusMock = vi.spyOn(coreApi, 'sessionStatus').mockResolvedValue({
      authenticated: true,
      expires_at: EXPIRES_AT,
    })
    vi.spyOn(coreApi, 'login')
      .mockResolvedValueOnce(loginResponse('csrf-first-login'))
      .mockResolvedValueOnce(loginResponse('csrf-second-login'))
    vi.spyOn(coreApi, 'logout').mockResolvedValue({ authenticated: false })

    await router.replace({ name: 'login', query: { cycle: 'initial' } })
    expect(statusMock).toHaveBeenCalledTimes(1)

    await session.login('bootstrap-secret')
    await router.replace({ name: 'overview', query: { cycle: 'first' } })
    expect(statusMock).toHaveBeenCalledTimes(2)

    await session.logout()
    await router.replace({ name: 'login', query: { cycle: 'after-logout' } })
    expect(statusMock).toHaveBeenCalledTimes(3)

    await session.login('bootstrap-secret')
    await router.replace({ name: 'overview', query: { cycle: 'second' } })
    expect(statusMock).toHaveBeenCalledTimes(4)
  })
})