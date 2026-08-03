<script setup lang="ts">
import { useShopContext } from '@/state/shop'

const { hasShop } = useShopContext()
</script>

<template>
  <slot v-if="hasShop" />
  <el-empty v-else description="请先在顶部选择注册表中的已授权本人店铺">
    <template #image>
      <div class="empty-mark">SHOP</div>
    </template>
  </el-empty>
</template>