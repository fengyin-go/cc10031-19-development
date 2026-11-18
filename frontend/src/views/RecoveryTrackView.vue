<template>
  <section class="page" data-module="recovery_track">
    <header class="page-head">
      <div>
        <h2>异常恢复追踪</h2>
        <p class="page-desc">
          异常事件 × 执行日志 × 恢复记录的联合台账：按共享执行键解析同一执行对象的
          当前终态，核对清理与恢复证据，定位恢复过程的完整终点。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="reload-ledger" type="button" @click="reload">刷新台账</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>流程名称</span>
        <input v-model="filters.流程名称" data-testid="filter-流程名称" placeholder="按流程名称检索" />
      </label>
      <label class="filter-item">
        <span>执行编号</span>
        <input v-model="filters.执行编号" data-testid="filter-执行编号" placeholder="按执行编号检索" />
      </label>
      <label class="filter-item">
        <span>恢复状态</span>
        <select v-model="filters.恢复状态" data-testid="filter-恢复状态">
          <option value="">全部</option>
          <option v-for="state in FINAL_STATES" :key="state" :value="state">{{ state }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>冲突策略</span>
        <select v-model="policy" data-testid="filter-policy">
          <option v-for="(label, key) in CONFLICT_POLICIES" :key="key" :value="key">{{ label }}</option>
        </select>
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th>事件编号</th>
          <th>流程名称</th>
          <th>执行编号</th>
          <th>清理证据</th>
          <th>恢复证据</th>
          <th>当前终态</th>
          <th>待执行动作</th>
          <th>处理项</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.执行键" :data-testid="`ledger-${row.执行键}`" :data-status="row.恢复状态">
          <td>{{ row.事件编号 || '—' }}</td>
          <td>{{ row.流程名称 || '—' }}</td>
          <td>{{ row.执行编号 || row.执行键 }}</td>
          <td>{{ row.清理证据.length ? row.清理证据.join('；') : '—' }}</td>
          <td>{{ row.恢复证据.length ? row.恢复证据.join('；') : '—' }}</td>
          <td>
            {{ row.恢复状态 }}
            <span v-if="row.冲突" class="conflict-flag">冲突·{{ row.被延后 }}延后</span>
          </td>
          <td>{{ row.待执行动作 }}</td>
          <td class="row-actions">
            <button
              v-for="action in PROCESS_ACTIONS"
              :key="action"
              class="link"
              type="button"
              :data-testid="`process-${row.执行键}-${action}`"
              @click="process(row.执行键, action)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td colspan="8" class="empty-state">暂无异常恢复追踪数据</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条台账记录，同一异常只保留一个当前终态</span>
      <span v-if="message" class="error-text">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { loadRecoveryLedger, processRecoveryItem } from '@/api/backend-service'
import {
  CONFLICT_POLICIES,
  FINAL_STATES,
  PROCESS_ACTIONS,
  summarizeLedger,
  type RecoveryLedgerRow,
} from '@/domain/recovery_track'

const rows = ref<RecoveryLedgerRow[]>([])
const total = ref(0)
const message = ref('')
const policy = ref('cleanup_first')
const filters = ref<Record<string, string>>({ 流程名称: '', 执行编号: '', 恢复状态: '' })

const stats = computed(() => {
  const summary = summarizeLedger(rows.value)
  return [
    { label: '台账记录', value: summary.总数 ?? 0 },
    { label: '已恢复', value: summary.已恢复 ?? 0 },
    { label: '已忽略', value: summary.已忽略 ?? 0 },
    { label: '冲突待决', value: summary.冲突 ?? 0 },
  ]
})

function resetFilters() {
  filters.value = { 流程名称: '', 执行编号: '', 恢复状态: '' }
  void reload()
}

async function reload() {
  message.value = ''
  try {
    const payload = await loadRecoveryLedger({ ...filters.value, policy: policy.value })
    rows.value = payload.items
    total.value = payload.count
  } catch (error) {
    message.value = error instanceof Error ? error.message : '台账读取失败'
  }
}

async function process(execKey: string, action: string) {
  message.value = ''
  try {
    const result = await processRecoveryItem(execKey, action, policy.value)
    if (!result.ok) throw new Error(String(result.message ?? '处理失败'))
    await reload()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '处理失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.conflict-flag {
  display: inline-block;
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  background: #fef3c7;
  color: #92400e;
  font-size: 12px;
}

.filter-item select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 6px 8px;
  min-width: 140px;
}
</style>
