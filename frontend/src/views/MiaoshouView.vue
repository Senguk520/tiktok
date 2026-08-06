<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import { coreApi, type MiaoshouCapabilities, type MiaoshouShop } from '@/api/core'
import PageHeader from '@/components/PageHeader.vue'
import { errorText, formatDateTime } from '@/ui'

type MiaoshouPlatform = 'tiktok' | 'tiktokGlobal'

const siteOptions: Record<MiaoshouPlatform, string[]> = {
  tiktok: ['ID', 'VN', 'TH', 'MY', 'PH', 'BR', 'MX', 'ES', 'FR', 'GB', 'US', 'DE', 'IT', 'JP'],
  tiktokGlobal: ['TIKTOKGLOBAL', 'TIKTOKGLOBALUS', 'TIKTOKGLOBALEU'],
}

const platform = ref<MiaoshouPlatform>('tiktok')
const site = ref('US')
const capabilities = ref<MiaoshouCapabilities | null>(null)
const items = ref<MiaoshouShop[]>([])
const pageNo = ref(1)
const nextPageNo = ref<number | null>(null)
const loading = ref(false)
const capabilityLoading = ref(false)
const error = ref('')

const sites = computed(() => siteOptions[platform.value])
const platformLabel = computed(() =>
  platform.value === 'tiktok' ? 'TikTok Shop' : 'TikTok Global',
)

const loadCapabilities = async (): Promise<void> => {
  capabilityLoading.value = true
  try {
    capabilities.value = await coreApi.miaoshouCapabilities()
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    capabilityLoading.value = false
  }
}

const queryShops = async (requestedPage = 1): Promise<void> => {
  if (!capabilities.value?.shop_query_enabled) return
  loading.value = true
  error.value = ''
  try {
    const result = await coreApi.miaoshouShops(platform.value, site.value, requestedPage)
    items.value = result.items
    pageNo.value = result.page_no
    nextPageNo.value = result.next_page_no
  } catch (reason) {
    items.value = []
    nextPageNo.value = null
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const onPlatformChange = (): void => {
  site.value = siteOptions[platform.value][0] ?? ''
  items.value = []
  nextPageNo.value = null
}

watch(platform, onPlatformChange)
onMounted(loadCapabilities)
</script>

<template>
  <section>
    <PageHeader
      title="妙手店铺查询"
      description="可选的妙手 JCOP Provider；只读查询，不替换 TikTok 官方 API 主链路。"
    >
      <el-button :loading="capabilityLoading" @click="loadCapabilities">刷新状态</el-button>
    </PageHeader>

    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />

    <section class="content-card miaoshou-query-card">
      <div class="card-heading">
        <div>
          <p class="page-kicker">OPTIONAL PROVIDER</p>
          <h2>只读 Shop Query</h2>
        </div>
        <el-tag :type="capabilities?.shop_query_enabled ? 'success' : 'warning'">
          {{ capabilities?.shop_query_enabled ? '已配置' : '失败关闭' }}
        </el-tag>
      </div>

      <el-alert
        v-if="capabilities && !capabilities.shop_query_enabled"
        :title="capabilities.blockers.join(' · ') || 'Provider 未启用'"
        type="warning"
        :closable="false"
        show-icon
      >
        <template #default>
          妙手未配置时不会回退到伪造数据，也不会影响现有 TikTok 官方 API 能力。
        </template>
      </el-alert>

      <div class="query-toolbar">
        <el-select v-model="platform" aria-label="平台" :disabled="!capabilities?.shop_query_enabled">
          <el-option label="TikTok Shop" value="tiktok" />
          <el-option label="TikTok Global" value="tiktokGlobal" />
        </el-select>
        <el-select v-model="site" aria-label="站点" :disabled="!capabilities?.shop_query_enabled">
          <el-option v-for="item in sites" :key="item" :label="item" :value="item" />
        </el-select>
        <el-button
          type="primary"
          :loading="loading"
          :disabled="!capabilities?.shop_query_enabled"
          @click="queryShops()"
        >
          查询授权店铺
        </el-button>
      </div>

      <div v-if="items.length" v-loading="loading" class="miaoshou-results">
        <div class="card-heading result-heading">
          <div>
            <p class="page-kicker">{{ platformLabel }} · {{ site }}</p>
            <h3>第 {{ pageNo }} 页</h3>
          </div>
          <div class="pagination-actions">
            <el-button
              :disabled="pageNo <= 1 || loading"
              @click="queryShops(pageNo - 1)"
            >
              上一页
            </el-button>
            <el-button
              :disabled="nextPageNo === null || loading"
              @click="queryShops(nextPageNo ?? pageNo)"
            >
              下一页
            </el-button>
          </div>
        </div>
        <el-table :data="items" stripe>
          <el-table-column prop="shop_id" label="Shop ID" min-width="150" />
          <el-table-column label="店铺" min-width="180">
            <template #default="scope">
              {{ scope.row.shop_name || '未返回名称' }}
            </template>
          </el-table-column>
          <el-table-column prop="site_name" label="站点" min-width="130" />
          <el-table-column label="授权状态" min-width="120">
            <template #default="scope">
              <el-tag :type="scope.row.status === 'normal' ? 'success' : 'warning'">
                {{ scope.row.status || '未知' }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="到期时间" min-width="180">
            <template #default="scope">
              {{ formatDateTime(scope.row.authorization_expires_at) }}
            </template>
          </el-table-column>
          <el-table-column label="模式" min-width="120">
            <template #default="scope">
              {{ scope.row.is_global ? 'Global' : scope.row.is_cross_border ? '跨境' : '普通' }}
            </template>
          </el-table-column>
        </el-table>
      </div>
      <el-empty
        v-else-if="capabilities?.shop_query_enabled"
        description="尚未查询；这里不会注入演示店铺"
      />
    </section>
  </section>
</template>