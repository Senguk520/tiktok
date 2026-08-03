<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import {
  coreApi,
  type Order as ShopOrder,
  type OrderSyncResult,
  type ScheduleCapabilities,
} from '@/api/core'
import CapabilityPanel from '@/components/CapabilityPanel.vue'
import PageHeader from '@/components/PageHeader.vue'
import ShopGate from '@/components/ShopGate.vue'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import { commaSeparated, errorText, formatDateTime } from '@/ui'

const shop = useShopContext()
const session = useAdminSession()
const capabilities = ref<ScheduleCapabilities | null>(null)
const activeTab = ref('remote')
const orders = ref<ShopOrder[]>([])
const nextPageToken = ref<string | null>(null)
const totalCount = ref<number | null>(null)
const selectedIds = ref<string[]>([])
const localIds = ref('')
const loading = ref(false)
const error = ref('')
const syncResult = ref<OrderSyncResult | null>(null)

const end = new Date()
const start = new Date(end.getTime() - 24 * 60 * 60 * 1000)
const syncForm = reactive({
  windowStart: start.toISOString().slice(0, 16),
  windowEnd: end.toISOString().slice(0, 16),
  pageSize: 100,
  maxPages: 10,
})

const csrf = (): string => {
  if (!session.csrfToken.value) throw new Error('当前页面没有写入令牌，请重新认证')
  return session.csrfToken.value
}

const loadRemote = async (pageToken?: string | null): Promise<void> => {
  loading.value = true
  error.value = ''
  try {
    const page = await coreApi.remoteOrders(shop.shopBindingId.value, 20, pageToken)
    orders.value = page.orders
    nextPageToken.value = page.next_page_token
    totalCount.value = page.total_count
    selectedIds.value = []
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const loadDetails = async (source: 'remote' | 'local', ids: readonly string[]): Promise<void> => {
  if (!ids.length) {
    ElMessage.warning('请至少选择或输入一个订单 ID')
    return
  }
  loading.value = true
  error.value = ''
  try {
    const details =
      source === 'remote'
        ? await coreApi.remoteOrderDetails(shop.shopBindingId.value, ids)
        : await coreApi.localOrderDetails(shop.shopBindingId.value, ids)
    orders.value = details.orders
    nextPageToken.value = null
    totalCount.value = details.orders.length
    ElMessage.success(`已读取 ${details.orders.length} 个订单详情`)
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const loadLocal = async (): Promise<void> => {
  await loadDetails('local', commaSeparated(localIds.value))
}

const syncOrders = async (): Promise<void> => {
  loading.value = true
  error.value = ''
  syncResult.value = null
  try {
    syncResult.value = await coreApi.syncOrders(
      shop.shopBindingId.value,
      {
        window_start: new Date(syncForm.windowStart).toISOString(),
        window_end: new Date(syncForm.windowEnd).toISOString(),
        page_size: syncForm.pageSize,
        max_pages: syncForm.maxPages,
      },
      csrf(),
    )
    ElMessage.success(syncResult.value.completed ? '订单窗口同步完成' : '订单窗口部分同步，保留了断点')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const onSelection = (rows: ShopOrder[]): void => {
  selectedIds.value = rows.map((row) => row.order_id)
}

watch(shop.shopBindingId, () => {
  orders.value = []
  selectedIds.value = []
  nextPageToken.value = null
  syncResult.value = null
})
</script>

<template>
  <section>
    <PageHeader title="订单中心" description="远端查询与本地快照分开呈现；响应只包含履约所需的最小化订单字段。">
      <el-tag effect="plain">详情批次最多 50 个 ID</el-tag>
    </PageHeader>

    <ShopGate>
      <el-alert
        title="页面不展示买家姓名、邮箱、电话或完整地址；订单同步也不会将这些字段写入 Core SQLite。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-alert v-if="error" class="section-alert" :title="error" type="error" :closable="false" show-icon />

      <el-tabs v-model="activeTab" class="work-tabs">
        <el-tab-pane label="远端订单" name="remote">
          <section class="content-card">
            <div class="card-heading">
              <div>
                <p class="page-kicker">TIKTOK LIVE</p>
                <h2>订单列表与详情</h2>
              </div>
              <div class="inline-actions">
                <el-button :loading="loading" @click="loadRemote(null)">查询第一页</el-button>
                <el-button
                  type="primary"
                  plain
                  :disabled="!selectedIds.length"
                  @click="loadDetails('remote', selectedIds)"
                >
                  获取已选详情
                </el-button>
              </div>
            </div>
            <el-table v-loading="loading" :data="orders" empty-text="暂无订单或尚未查询" @selection-change="onSelection">
              <el-table-column type="selection" width="48" />
              <el-table-column type="expand">
                <template #default="scope">
                  <div class="order-lines">
                    <p v-if="!scope.row.lines.length" class="muted">列表响应不含行项目，请选择后获取详情。</p>
                    <el-table v-else :data="scope.row.lines" size="small">
                      <el-table-column prop="line_id" label="Line ID" min-width="160" />
                      <el-table-column prop="seller_sku" label="Seller SKU" min-width="150" />
                      <el-table-column prop="status" label="状态" width="130" />
                      <el-table-column prop="quantity" label="数量" width="80" />
                      <el-table-column label="售价" width="130">
                        <template #default="lineScope">
                          {{ lineScope.row.sale_price ?? '—' }} {{ lineScope.row.currency ?? '' }}
                        </template>
                      </el-table-column>
                    </el-table>
                  </div>
                </template>
              </el-table-column>
              <el-table-column prop="order_id" label="Order ID" min-width="190" />
              <el-table-column prop="status" label="状态" width="150" />
              <el-table-column label="金额" width="150">
                <template #default="scope">{{ scope.row.total_amount ?? '—' }} {{ scope.row.currency ?? '' }}</template>
              </el-table-column>
              <el-table-column prop="item_count" label="件数" width="80" />
              <el-table-column label="更新时间" min-width="180">
                <template #default="scope">{{ formatDateTime(scope.row.updated_at) }}</template>
              </el-table-column>
            </el-table>
            <div class="table-footer">
              <span>本页 {{ orders.length }} 条<span v-if="totalCount !== null"> · 总计 {{ totalCount }}</span></span>
              <el-button v-if="nextPageToken" :loading="loading" @click="loadRemote(nextPageToken)">下一页</el-button>
            </div>
          </section>
        </el-tab-pane>

        <el-tab-pane label="本地快照" name="local">
          <section class="content-card narrow-card">
            <div class="card-heading">
              <div>
                <p class="page-kicker">CORE SQLITE</p>
                <h2>按 ID 读取已同步订单</h2>
              </div>
            </div>
            <el-form label-position="top" @submit.prevent="loadLocal">
              <el-form-item label="订单 ID（逗号或换行分隔，最多 50 个）">
                <el-input v-model="localIds" type="textarea" :rows="4" />
              </el-form-item>
              <el-button native-type="submit" type="primary" plain :loading="loading">读取本地详情</el-button>
            </el-form>
          </section>
        </el-tab-pane>

        <el-tab-pane label="窗口同步" name="sync">
          <section class="content-card narrow-card">
            <div class="card-heading">
              <div>
                <p class="page-kicker">RESUMABLE SYNC</p>
                <h2>增量同步订单窗口</h2>
              </div>
            </div>
            <el-form class="spaced-form" label-position="top" @submit.prevent="syncOrders">
              <div class="form-grid">
                <el-form-item label="窗口开始">
                  <el-input v-model="syncForm.windowStart" type="datetime-local" />
                </el-form-item>
                <el-form-item label="窗口结束">
                  <el-input v-model="syncForm.windowEnd" type="datetime-local" />
                </el-form-item>
                <el-form-item label="每页数量">
                  <el-input-number v-model="syncForm.pageSize" :min="1" :max="100" />
                </el-form-item>
                <el-form-item label="本次最多页数">
                  <el-input-number v-model="syncForm.maxPages" :min="1" :max="1000" />
                </el-form-item>
              </div>
              <el-button type="primary" native-type="submit" :loading="loading" :disabled="!session.canWrite.value">
                开始同步
              </el-button>
            </el-form>
            <el-descriptions v-if="syncResult" class="result-panel" :column="2" border>
              <el-descriptions-item label="已读页数">{{ syncResult.pages }}</el-descriptions-item>
              <el-descriptions-item label="列表订单">{{ syncResult.listed_orders }}</el-descriptions-item>
              <el-descriptions-item label="详情订单">{{ syncResult.detailed_orders }}</el-descriptions-item>
              <el-descriptions-item label="状态">{{ syncResult.completed ? '完成' : '保留断点' }}</el-descriptions-item>
              <el-descriptions-item label="下页令牌">{{ syncResult.next_page_token ?? '—' }}</el-descriptions-item>
            </el-descriptions>
          </section>
        </el-tab-pane>
      </el-tabs>
    </ShopGate>
  </section>
</template>