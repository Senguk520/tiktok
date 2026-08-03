import { createRouter, createWebHistory } from 'vue-router'

import { onUnauthorized } from '@/api/client'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import WorkspaceLayout from '@/views/WorkspaceLayout.vue'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('@/views/LoginView.vue'),
    },
    {
      path: '/',
      component: WorkspaceLayout,
      children: [
        { path: '', redirect: '/overview' },
        { path: 'overview', name: 'overview', component: () => import('@/views/OverviewView.vue') },
        { path: 'products', name: 'products', component: () => import('@/views/ProductsView.vue') },
        { path: 'orders', name: 'orders', component: () => import('@/views/OrdersView.vue') },
        { path: 'tools', name: 'tools', component: () => import('@/views/ToolsView.vue') },
        { path: 'schedules', name: 'schedules', component: () => import('@/views/SchedulesView.vue') },
        { path: 'audits', name: 'audits', component: () => import('@/views/AuditsView.vue') },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/overview' },
  ],
})

let sessionChecked = false

onUnauthorized(() => {
  useAdminSession().expire()
  useShopContext().resetShops()
  if (router.currentRoute.value.name && router.currentRoute.value.name !== 'login') {
    void router.replace({ name: 'login' })
  }
})

router.beforeEach(async (to) => {
  const session = useAdminSession()
  if (!sessionChecked) {
    await session.check()
    sessionChecked = true
  }
  if (to.name === 'login') {
    return session.authenticated.value && to.query.reauth !== '1' ? { name: 'overview' } : true
  }
  if (!session.canRead.value) {
    return { name: 'login' }
  }
  return true
})

export default router