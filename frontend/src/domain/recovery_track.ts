/**
 * 异常恢复追踪 — 纯前端领域逻辑。
 *
 * 与后端 app/services/recovery_track.py 同一口径：
 * 异常事件 × 执行日志 × 恢复记录共享同一执行键，联合台账上同一异常
 * 只保留一个当前终态；清理回执与恢复事件冲突时按策略二选一、另一方延后。
 */

/** 台账行：带清理与恢复证据的异常事件台账数据集记录 */
export interface RecoveryLedgerRow {
  id: number
  执行键: string
  执行编号: string
  流程名称: string
  事件编号: string
  异常类型: string
  恢复状态: string
  当前终态: string
  清理证据: string[]
  恢复证据: string[]
  待执行动作: string
  冲突: boolean
  冲突策略: string
  被延后: string
}

/** 沿用异常日志既有版本口径的终态词表 */
export const FINAL_STATES = ['未处理', '处理中', '已恢复', '已忽略'] as const

/** 冲突处理方式：都只保留一个确认终态 */
export const CONFLICT_POLICIES: Record<string, string> = {
  cleanup_first: '清理终态获胜并延后恢复',
  recovery_first: '恢复终态获胜并延后清理',
}

/** 处理项动作 → 确认终态（执行归档属于清理口径） */
export const ACTION_FINAL_STATE: Record<string, string> = {
  标记恢复: '已恢复',
  忽略事件: '已忽略',
  执行归档: '已忽略',
}

/** 处理项可执行动作 */
export const PROCESS_ACTIONS = Object.keys(ACTION_FINAL_STATE)

export interface LedgerFilters {
  流程名称?: string
  执行编号?: string
  恢复状态?: string
  keyword?: string
}

/** 终态解析：同一执行对象只保留一个当前终态 */
export function resolveFinalState(input: {
  status: string
  cleanupEvidence: string[]
  recoveryEvidence: string[]
  policy?: string
}): { finalState: string; conflict: boolean; deferred: string } {
  const hasCleanup = input.cleanupEvidence.length > 0
  const hasRecovery = input.recoveryEvidence.length > 0
  const conflict = hasCleanup && hasRecovery
  if (conflict) {
    return input.policy === 'recovery_first'
      ? { finalState: '已恢复', conflict, deferred: '清理' }
      : { finalState: '已忽略', conflict, deferred: '恢复' }
  }
  if (hasRecovery) return { finalState: '已恢复', conflict, deferred: '' }
  if (hasCleanup) return { finalState: '已忽略', conflict, deferred: '' }
  const finalState = input.status === '处理中' ? '处理中' : '未处理'
  return { finalState, conflict, deferred: '' }
}

/** 按当前终态推导待执行动作 */
export function pendingActionFor(finalState: string, conflict: boolean, deferred: string): string {
  if (conflict) return `延后${deferred}确认`
  if (finalState === '未处理') return '标记恢复 / 忽略事件'
  if (finalState === '处理中') return '确认恢复或清理'
  return '无'
}

/** 搜索过滤定位：按流程名称、执行编号、恢复状态筛选联合台账 */
export function filterLedger(rows: RecoveryLedgerRow[], filters: LedgerFilters): RecoveryLedgerRow[] {
  let result = rows
  const processName = filters.流程名称?.trim().toLowerCase()
  const execNo = filters.执行编号?.trim().toLowerCase()
  const keyword = filters.keyword?.trim().toLowerCase()
  if (processName) {
    result = result.filter((row) => row.流程名称.toLowerCase().includes(processName))
  }
  if (execNo) {
    result = result.filter(
      (row) =>
        row.执行键.toLowerCase().includes(execNo) ||
        row.执行编号.toLowerCase().includes(execNo),
    )
  }
  if (filters.恢复状态) {
    result = result.filter((row) => row.恢复状态 === filters.恢复状态)
  }
  if (keyword) {
    result = result.filter(
      (row) =>
        row.事件编号.toLowerCase().includes(keyword) ||
        row.流程名称.toLowerCase().includes(keyword) ||
        row.执行键.toLowerCase().includes(keyword),
    )
  }
  return result
}

/** 台账汇总：按终态统计，供看板卡片使用 */
export function summarizeLedger(rows: RecoveryLedgerRow[]): Record<string, number> {
  const summary: Record<string, number> = { 总数: rows.length, 冲突: 0 }
  for (const state of FINAL_STATES) summary[state] = 0
  for (const row of rows) {
    summary[row.恢复状态] = (summary[row.恢复状态] ?? 0) + 1
    if (row.冲突) summary.冲突 += 1
  }
  return summary
}
