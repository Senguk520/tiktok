<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { coreApi, type AuditFact } from '@/api/core'
import PageHeader from '@/components/PageHeader.vue'
import ShopGate from '@/components/ShopGate.vue'
import { useShopContext } from '@/state/shop'
import { errorText, formatDateTime } from '@/ui'

const shop = useShopContext()
const records = ref<AuditFact[]>([])
const loading = ref(false)
const error = ref('')
const eventFilter = ref('')
const outcomeFilter = ref('')
const hasMore = ref(false)

const filtered = computed(() =>
  records.value.filter(
    (record) =>
      (!eventFilter.value || record.event_type.includes(eventFilter.value.trim().toLowerCase())) &&
      (!outcomeFilter.value || record.outcome === outcomeFilter.value),
  ),
)

const load = async (append = false): Promise<void> => {
  if (!shop.shopBindingId.value) return
  loading.value = true
  error.value = ''
  try {
    const before = append ? records.value.at(-1)?.created_at : undefined
    const page = await coreApi.audits(shop.shopBindingId.value, 100, before)
    records.value = append ? [...records.value, ...page] : page
    hasMore.value = page.length === 100
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const outcomeType = (outcome: string): 'success' | 'danger' | 'warning' | 'info' => {
  if (outcome === 'SUCCESS') return 'success'
  if (outcome === 'FAILED' || outcome === 'REJECTED') return 'danger'
  if (outcome === 'BLOCKED') return 'warning'
  return 'info'
}

watch(shop.shopBindingId, () => {
  records.value = []
  load()
}, { immediate: true })
</script>

<template>
  <section>
    <PageHeader title="审计记录" description="这里只读取后端白名单允许的业务事实；密钥、Token、买家信息、完整 URL 与本地路径不会进入详情。">
      <el-button :loading="loading" @click="load(false)">刷新</el-button>
    </PageHeader>

    <ShopGate>
      <el-alert v-if="error" class="section-alert" :title="error" type="error" :closable="false" show-icon />
      <section class="content-card">
        <div class="filter-bar">
          <el-input v-model="eventFilter" clearable placeholder="按事件类型筛选" />
          <el-select v-model="outcomeFilter" clearable placeholder="结果">
            <el-option label="SUCCESS" value="SUCCESS" />
            <el-option label="FAILED" value="FAILED" />
            <el-option label="BLOCKED" value="BLOCKED" />
            <el-option label="REJECTED" value="REJECTED" />
          </el-select>
          <span>{{ filtered.length }} / {{ records.length }} 条</span>
        </div>

        <el-table v-loading="loading" :data="filtered" empty-text="暂无审计事实">
          <el-table-column type="expand">
            <template #default="scope">
              <div class="audit-details">
                <dl>
                  <dt>Audit ID</dt><dd><code>{{ scope.row.id }}</code></dd>
                  <dt>Request ID</dt><dd><code>{{ scope.row.request_id ?? '—' }}</code></dd>
                  <dt>Resource</dt>
                  <dd>{{ scope.row.resource_type ?? '—' }} / <code>{{ scope.row.resource_id ?? '—' }}</code></dd>
                  <dt>白名单详情</dt><dd><pre>{{ JSON.stringify(scope.row.details, null, 2) }}</pre></dd>
                </dl>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="event_type" label="事件" min-width="220" />
          <el-table-column label="结果" width="130">
            <template #default="scope">
              <el-tag :type="outcomeType(scope.row.outcome)">{{ scope.row.outcome }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="resource_type" label="资源类型" min-width="150" />
          <el-table-column prop="resource_id" label="资源 ID" min-width="200" />
          <el-table-column label="时间" min-width="190">
            <template #default="scope">{{ formatDateTime(scope.row.created_at) }}</template>
          </el-table-column>
        </el-table>

        <div class="table-footer">
          <span>审计详情由 Core API 脱敏后返回</span>
          <el-button v-if="hasMore" :loading="loading" @click="load(true)">加载更早记录</el-button>
        </div>
      </section>
    </ShopGate>
  </section>
</template>