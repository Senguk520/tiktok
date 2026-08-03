<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { coreApi, type ServiceStatus } from '@/api/core'
import { useAdminSession } from '@/state/session'
import { errorText } from '@/ui'

const router = useRouter()
const session = useAdminSession()
const secret = ref('')
const service = ref<ServiceStatus | null>(null)
const serviceError = ref('')

onMounted(async () => {
  try {
    service.value = await coreApi.health()
  } catch (error) {
    serviceError.value = errorText(error)
  }
  if (session.authenticated.value) await router.replace('/overview')
})

const submit = async (): Promise<void> => {
  const suppliedSecret = secret.value
  secret.value = ''
  try {
    await session.login(suppliedSecret)
    ElMessage.success('管理员会话已建立')
    await router.replace('/overview')
  } catch (error) {
    ElMessage.error(errorText(error))
  }
}
</script>

<template>
  <main class="login-shell">
    <section class="login-brand">
      <p class="brand-tag">TIKTOK SHOP · SINGLE STORE</p>
      <h1>把复杂的跨境操作<br />收束成可验证的流程。</h1>
      <p>
        商品、订单、翻译、利润与调度都通过本机 Core API 执行。浏览器不保存 TikTok 密钥、Token 或店铺业务数据。
      </p>
      <div class="service-pill" :class="{ offline: serviceError }">
        <span />
        {{ service ? `Core API · ${service.status}` : serviceError || '正在检查 Core API' }}
      </div>
    </section>

    <section class="login-panel" aria-labelledby="login-title">
      <div class="login-card">
        <p class="page-kicker">LOCAL ADMIN</p>
        <h2 id="login-title">进入管理工作区</h2>
        <p class="muted">使用后端启动时配置的管理员引导密钥。密钥只用于本次受限回环请求，不写入浏览器存储。</p>
        <el-form label-position="top" @submit.prevent="submit">
          <el-form-item label="管理员引导密钥">
            <el-input
              v-model="secret"
              type="password"
              autocomplete="off"
              maxlength="4096"
              show-password
              placeholder="至少 32 个字符"
              @keyup.enter="submit"
            />
          </el-form-item>
          <el-button
            class="login-submit"
            type="primary"
            native-type="submit"
            :loading="session.loginPending.value"
            :disabled="!secret"
          >
            建立安全会话
          </el-button>
        </el-form>
        <el-alert
          v-if="session.lastError.value"
          class="login-error"
          :title="session.lastError.value"
          type="error"
          :closable="false"
          show-icon
        />
        <p class="security-note">HttpOnly Cookie · SameSite Strict · 写操作 CSRF 校验</p>
      </div>
    </section>
  </main>
</template>