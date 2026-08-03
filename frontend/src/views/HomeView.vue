<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { apiRequest } from '@/api/client'

interface ServiceStatus {
  service: string
  status: string
  database: string
}

const service = ref<ServiceStatus | null>(null)
const error = ref('')

onMounted(async () => {
  try {
    service.value = await apiRequest<ServiceStatus>('/healthz')
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : 'Core API 未连接'
  }
})
</script>

<template>
  <main class="shell">
    <section class="status-card" aria-labelledby="system-title">
      <p class="eyebrow">TikTok Shop</p>
      <h1 id="system-title">单店管理系统</h1>
      <p v-if="service" class="healthy">Core API 已连接 · {{ service.status }}</p>
      <el-alert
        v-else-if="error"
        title="Core API 尚未就绪"
        :description="error"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-skeleton v-else :rows="2" animated />
      <p class="note">系统不会在浏览器中持久化店铺业务数据。</p>
    </section>
  </main>
</template>