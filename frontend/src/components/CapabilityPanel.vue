<script setup lang="ts">
import { blockerLabel } from '@/ui'

defineProps<{
  blockers: readonly string[]
  readyText?: string
}>()
</script>

<template>
  <el-alert
    v-if="blockers.length"
    title="当前能力受限"
    type="warning"
    :closable="false"
    show-icon
  >
    <ul class="blocker-list">
      <li v-for="blocker in blockers" :key="blocker">
        {{ blockerLabel(blocker) }}
        <code>{{ blocker }}</code>
      </li>
    </ul>
  </el-alert>
  <el-alert
    v-else
    :title="readyText ?? '所需能力已就绪'"
    type="success"
    :closable="false"
    show-icon
  />
</template>