import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import RecoveryTrackView from '@/views/RecoveryTrackView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 异常恢复追踪联合台账是固定路由，必须先于动态模块路由注册
    { path: '/recovery-track', name: 'recovery-track', component: RecoveryTrackView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
