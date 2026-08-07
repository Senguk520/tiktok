import { computed, readonly, ref } from 'vue'

import { ApiError } from '@/api/client'
import { coreApi } from '@/api/core'

export type SessionPhase = 'checking' | 'anonymous' | 'readonly' | 'authenticated'

const phase = ref<SessionPhase>('checking')
const csrfToken = ref<string | null>(null)
const expiresAt = ref<string | null>(null)
const lastError = ref('')
const loginPending = ref(false)
const needsCheck = ref(true)

const setAnonymous = (): void => {
  phase.value = 'anonymous'
  csrfToken.value = null
  expiresAt.value = null
  needsCheck.value = true
}

const expire = (): void => {
  setAnonymous()
  lastError.value = '管理员会话已失效，请重新认证'
}

const check = async (): Promise<void> => {
  phase.value = 'checking'
  lastError.value = ''
  try {
    const status = await coreApi.sessionStatus()
    expiresAt.value = status.expires_at
    phase.value = status.authenticated && csrfToken.value ? 'authenticated' : 'readonly'
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      setAnonymous()
      return
    }
    setAnonymous()
    lastError.value = error instanceof Error ? error.message : '无法检查管理员会话'
  } finally {
    needsCheck.value = false
  }
}

const login = async (bootstrapSecret: string): Promise<void> => {
  loginPending.value = true
  lastError.value = ''
  try {
    const created = await coreApi.login(bootstrapSecret)
    csrfToken.value = created.csrf_token
    expiresAt.value = created.expires_at
    phase.value = 'authenticated'
    needsCheck.value = true
  } catch (error) {
    setAnonymous()
    lastError.value = error instanceof Error ? error.message : '管理员登录失败'
    throw error
  } finally {
    loginPending.value = false
  }
}

const logout = async (): Promise<void> => {
  if (!csrfToken.value) throw new Error('当前页面没有 CSRF 令牌，请重新认证后再退出')
  await coreApi.logout(csrfToken.value)
  setAnonymous()
}

export const useAdminSession = () => ({
  phase: readonly(phase),
  csrfToken: readonly(csrfToken),
  expiresAt: readonly(expiresAt),
  lastError: readonly(lastError),
  loginPending: readonly(loginPending),
  needsCheck: readonly(needsCheck),
  authenticated: computed(() => phase.value === 'authenticated'),
  canRead: computed(() => phase.value === 'authenticated' || phase.value === 'readonly'),
  canWrite: computed(() => phase.value === 'authenticated' && Boolean(csrfToken.value)),
  check,
  login,
  logout,
  expire,
})
