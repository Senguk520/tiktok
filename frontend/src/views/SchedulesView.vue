<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, reactive, ref, watch } from 'vue'

import {
  coreApi,
  type ScheduleCapabilities,
  type ScheduleJob,
  type ScheduleJobType,
  type ScheduleRun,
} from '@/api/core'
import CapabilityPanel from '@/components/CapabilityPanel.vue'
import PageHeader from '@/components/PageHeader.vue'
import ShopGate from '@/components/ShopGate.vue'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import { errorText, formatDateTime } from '@/ui'

const shop = useShopContext()
const session = useAdminSession()
const capabilities = ref<ScheduleCapabilities | null>(null)
const jobs = ref<ScheduleJob[]>([])
const runs = ref<ScheduleRun[]>([])
const selectedJob = ref<ScheduleJob | null>(null)
const loading = ref(false)
const runsLoading = ref(false)
const error = ref('')

const firstRun = new Date(Date.now() + 5 * 60 * 1000)
const form = reactive({
  jobType: 'SYNC_ORDERS' as ScheduleJobType,
  scheduleKind: 'INTERVAL' as 'ONCE' | 'INTERVAL',
  runAt: firstRun.toISOString().slice(0, 16),
  intervalSeconds: 3600,
  draftId: '',
  windowSeconds: 3600,
  pageSize: 100,
  maxPages: 10,
})

const createBlocked = computed(() => {
  if (!session.canWrite.value || !capabilities.value) return true
  return form.jobType === 'PUBLISH_DRAFT'
    ? !capabilities.value.publish_draft_enabled
    : !capabilities.value.order_sync_enabled
})

const csrf = (): string => {
  if (!session.csrfToken.value) throw new Error('当前页面没有写入令牌，请重新认证')
  return session.csrfToken.value
}

const load = async (): Promise<void> => {
  capabilities.value = null
  jobs.value = []
  selectedJob.value = null
  runs.value = []
  if (!shop.shopBindingId.value) return
  loading.value = true
  error.value = ''
  try {
    ;[capabilities.value, jobs.value] = await Promise.all([
      coreApi.scheduleCapabilities(shop.shopBindingId.value),
      coreApi.schedules(shop.shopBindingId.value),
    ])
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const createJob = async (): Promise<void> => {
  loading.value = true
  error.value = ''
  try {
    const payload =
      form.jobType === 'PUBLISH_DRAFT'
        ? { draft_id: form.draftId.trim() }
        : {
            window_seconds: form.windowSeconds,
            page_size: form.pageSize,
            max_pages: form.maxPages,
          }
    const created = await coreApi.createSchedule(
      shop.shopBindingId.value,
      {
        job_type: form.jobType,
        schedule_kind: form.jobType === 'PUBLISH_DRAFT' ? 'ONCE' : form.scheduleKind,
        run_at: new Date(form.runAt).toISOString(),
        interval_seconds:
          form.jobType === 'PUBLISH_DRAFT' || form.scheduleKind === 'ONCE'
            ? null
            : form.intervalSeconds,
        payload,
      },
      csrf(),
    )
    jobs.value = [created, ...jobs.value.filter((job) => job.id !== created.id)]
    ElMessage.success('调度任务已创建')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const toggle = async (job: ScheduleJob): Promise<void> => {
  loading.value = true
  error.value = ''
  try {
    const updated = await coreApi.setScheduleState(
      shop.shopBindingId.value,
      job.id,
      !job.enabled,
      csrf(),
    )
    jobs.value = jobs.value.map((item) => (item.id === updated.id ? updated : item))
    ElMessage.success(updated.enabled ? '任务已启用并重新排期' : '任务已停用')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const showRuns = async (job: ScheduleJob): Promise<void> => {
  selectedJob.value = job
  runs.value = []
  runsLoading.value = true
  error.value = ''
  try {
    runs.value = await coreApi.scheduleRuns(shop.shopBindingId.value, job.id)
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    runsLoading.value = false
  }
}

const stateType = (state: string): 'success' | 'warning' | 'danger' | 'info' => {
  if (state === 'SUCCEEDED') return 'success'
  if (state === 'RUNNING') return 'warning'
  if (state === 'FAILED' || state === 'LEASE_EXPIRED') return 'danger'
  return 'info'
}

watch(shop.shopBindingId, load, { immediate: true })
</script>

<template>
  <section>
    <PageHeader title="自动调度" description="任务、租约和每次运行都持久化在 SQLite；执行前会重新检查授权、Scope、刊登模式和配额。">
      <el-button :loading="loading" @click="load">刷新</el-button>
    </PageHeader>

    <ShopGate>
      <CapabilityPanel v-if="capabilities" :blockers="capabilities.blockers" />
      <el-alert v-if="error" class="section-alert" :title="error" type="error" :closable="false" show-icon />

      <div class="two-column-grid schedule-grid">
        <section class="content-card">
          <div class="card-heading">
            <div>
              <p class="page-kicker">SQLITE SCHEDULE</p>
              <h2>新建任务</h2>
            </div>
          </div>
          <el-form label-position="top" @submit.prevent="createJob">
            <div class="form-grid">
              <el-form-item label="任务类型">
                <el-select v-model="form.jobType">
                  <el-option label="同步订单" value="SYNC_ORDERS" />
                  <el-option label="发布已确认草稿" value="PUBLISH_DRAFT" />
                </el-select>
              </el-form-item>
              <el-form-item label="调度类型">
                <el-select v-model="form.scheduleKind" :disabled="form.jobType === 'PUBLISH_DRAFT'">
                  <el-option label="单次" value="ONCE" />
                  <el-option label="固定间隔" value="INTERVAL" />
                </el-select>
              </el-form-item>
              <el-form-item label="首次运行时间">
                <el-input v-model="form.runAt" type="datetime-local" />
              </el-form-item>
              <el-form-item v-if="form.jobType === 'SYNC_ORDERS' && form.scheduleKind === 'INTERVAL'" label="间隔秒数（60 秒至 31 天）">
                <el-input-number v-model="form.intervalSeconds" :min="60" :max="2678400" />
              </el-form-item>
            </div>

            <template v-if="form.jobType === 'PUBLISH_DRAFT'">
              <el-form-item label="已就绪并人工确认的草稿 UUID">
                <el-input v-model="form.draftId" maxlength="36" />
              </el-form-item>
            </template>
            <div v-else class="form-grid">
              <el-form-item label="向前同步窗口（秒）">
                <el-input-number v-model="form.windowSeconds" :min="60" :max="604800" />
              </el-form-item>
              <el-form-item label="每页数量">
                <el-input-number v-model="form.pageSize" :min="1" :max="100" />
              </el-form-item>
              <el-form-item label="每次最多页数">
                <el-input-number v-model="form.maxPages" :min="1" :max="100" />
              </el-form-item>
            </div>

            <el-button
              type="primary"
              native-type="submit"
              :loading="loading"
              :disabled="createBlocked || (form.jobType === 'PUBLISH_DRAFT' && !form.draftId)"
            >
              创建持久化任务
            </el-button>
          </el-form>
        </section>

        <section class="content-card">
          <div class="card-heading">
            <div>
              <p class="page-kicker">LEASED WORKER</p>
              <h2>任务列表</h2>
            </div>
            <el-tag :type="capabilities?.worker_enabled ? 'success' : 'warning'">
              Worker {{ capabilities?.worker_enabled ? '已启用' : '未启用' }}
            </el-tag>
          </div>
          <div v-loading="loading" class="job-list">
            <article v-for="job in jobs" :key="job.id" :class="{ disabled: !job.enabled }">
              <div class="job-main">
                <div>
                  <el-tag size="small" :type="job.enabled ? 'success' : 'info'">
                    {{ job.enabled ? 'ENABLED' : 'DISABLED' }}
                  </el-tag>
                  <strong>{{ job.job_type }}</strong>
                </div>
                <small>{{ job.schedule_kind }} · 下次 {{ formatDateTime(job.next_run_at) }}</small>
                <code>{{ job.id }}</code>
              </div>
              <div class="job-actions">
                <el-button size="small" @click="showRuns(job)">运行记录</el-button>
                <el-button
                  size="small"
                  :type="job.enabled ? 'danger' : 'primary'"
                  plain
                  :disabled="!session.canWrite.value"
                  @click="toggle(job)"
                >
                  {{ job.enabled ? '停用' : '启用' }}
                </el-button>
              </div>
            </article>
            <el-empty v-if="!loading && !jobs.length" description="暂无调度任务" />
          </div>
        </section>
      </div>

      <section v-if="selectedJob" class="content-card runs-card">
        <div class="card-heading">
          <div>
            <p class="page-kicker">RUN HISTORY</p>
            <h2>{{ selectedJob.job_type }} 运行记录</h2>
          </div>
          <code>{{ selectedJob.id }}</code>
        </div>
        <el-table v-loading="runsLoading" :data="runs" empty-text="暂无运行记录">
          <el-table-column label="状态" width="130">
            <template #default="scope">
              <el-tag :type="stateType(scope.row.state)">{{ scope.row.state }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="worker_id" label="Worker" min-width="190" />
          <el-table-column label="开始时间" min-width="180">
            <template #default="scope">{{ formatDateTime(scope.row.started_at) }}</template>
          </el-table-column>
          <el-table-column label="结束时间" min-width="180">
            <template #default="scope">{{ formatDateTime(scope.row.finished_at) }}</template>
          </el-table-column>
          <el-table-column prop="error_code" label="错误码" min-width="210" />
          <el-table-column label="脱敏摘要" min-width="260">
            <template #default="scope"><code>{{ JSON.stringify(scope.row.summary) }}</code></template>
          </el-table-column>
        </el-table>
      </section>
    </ShopGate>
  </section>
</template>