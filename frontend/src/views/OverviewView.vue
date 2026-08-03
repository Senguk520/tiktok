<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  coreApi,
  type ProductCapabilities,
  type ScheduleCapabilities,
  type ServiceStatus,
  type ToolsCapabilities,
  type WebhookCapabilities,
} from '@/api/core'
import CapabilityPanel from '@/components/CapabilityPanel.vue'
import PageHeader from '@/components/PageHeader.vue'
import ShopGate from '@/components/ShopGate.vue'
import { useShopContext } from '@/state/shop'
import { errorText } from '@/ui'

const shop = useShopContext()
const health = ref<ServiceStatus | null>(null)
const products = ref<ProductCapabilities | null>(null)
const tools = ref<ToolsCapabilities | null>(null)
const schedules = ref<ScheduleCapabilities | null>(null)
const webhooks = ref<WebhookCapabilities | null>(null)
const loading = ref(false)
const error = ref('')

const productBlockers = computed(() => [
  ...(products.value?.blockers ?? []),
  ...(shop.selectedShop.value?.product_write_blockers ?? []),
])
const productWriteReady = computed(
  () => Boolean(products.value) && productBlockers.value.length === 0,
)

const loadGlobal = async (): Promise<void> => {
  try {
    ;[health.value, webhooks.value] = await Promise.all([
      coreApi.health(),
      coreApi.webhookCapabilities(),
    ])
  } catch (reason) {
    error.value = errorText(reason)
  }
}

const loadShop = async (): Promise<void> => {
  products.value = null
  tools.value = null
  schedules.value = null
  if (!shop.shopBindingId.value) return
  loading.value = true
  error.value = ''
  try {
    ;[products.value, tools.value, schedules.value] = await Promise.all([
      coreApi.productCapabilities(shop.shopBindingId.value),
      coreApi.toolsCapabilities(shop.shopBindingId.value),
      coreApi.scheduleCapabilities(shop.shopBindingId.value),
    ])
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

onMounted(loadGlobal)
watch(shop.shopBindingId, loadShop, { immediate: true })
</script>

<template>
  <section>
    <PageHeader title="运营总览" description="只展示 Core API 实际报告的能力，不用前端假数据掩盖配置缺口。">
      <el-button :loading="loading" @click="loadShop">刷新能力</el-button>
    </PageHeader>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />

    <div class="metric-grid">
      <article class="metric-card">
        <span>CORE API</span>
        <strong>{{ health?.status ?? '检查中' }}</strong>
        <small>{{ health?.database ?? '等待服务响应' }}</small>
      </article>
      <article class="metric-card">
        <span>商品写入</span>
        <strong>{{ productWriteReady ? '可用' : '受限' }}</strong>
        <small>{{ shop.selectedShop.value?.listing_mode ?? '尚未选择店铺' }}</small>
      </article>
      <article class="metric-card">
        <span>翻译</span>
        <strong>{{ tools?.translation_configured ? '可用' : '受限' }}</strong>
        <small>{{ tools?.translation_provider ?? '无已配置提供商' }}</small>
      </article>
      <article class="metric-card">
        <span>调度 Worker</span>
        <strong>{{ schedules?.worker_enabled ? '运行中' : '未启用' }}</strong>
        <small>状态与租约保存在 Core SQLite</small>
      </article>
    </div>

    <ShopGate>
      <div v-loading="loading" class="overview-grid">
        <section class="content-card">
          <div class="card-heading">
            <div>
              <p class="page-kicker">PRODUCTS</p>
              <h2>商品能力</h2>
            </div>
          </div>
          <CapabilityPanel v-if="products" :blockers="productBlockers" />
          <el-skeleton v-else :rows="3" animated />
        </section>

        <section class="content-card">
          <div class="card-heading">
            <div>
              <p class="page-kicker">VALUE TOOLS</p>
              <h2>翻译与调度</h2>
            </div>
          </div>
          <CapabilityPanel
            v-if="tools && schedules"
            :blockers="[...tools.blockers, ...schedules.blockers]"
          />
          <el-skeleton v-else :rows="3" animated />
        </section>
      </div>
    </ShopGate>

    <section class="content-card webhook-card">
      <div class="card-heading">
        <div>
          <p class="page-kicker">WEBHOOK</p>
          <h2>事件入口</h2>
        </div>
        <el-tag :type="webhooks?.state_changes_enabled ? 'success' : 'warning'">
          {{ webhooks?.state_changes_enabled ? '状态变更已启用' : '失败关闭' }}
        </el-tag>
      </div>
      <CapabilityPanel v-if="webhooks" :blockers="webhooks.blockers" />
      <el-skeleton v-else :rows="2" animated />
    </section>
  </section>
</template>