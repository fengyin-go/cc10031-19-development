"""异常恢复追踪数据能力 — 异常事件 × 执行日志 × 恢复记录的联合台账。

数据工程核心：异常恢复追踪 — 跨模块数据产物

背景：
    异常事件(anomaly_log)、执行日志(execution_log)、恢复记录(store.recovery)
    分散存储。异常写入恢复管理器后，忽略、恢复和执行归档分别修改不同对象，
    任何单一列表都无法说明一次异常最终停在哪里。

能力：
    1. 异常事件与恢复记录共享同一执行键（执行编号），按执行键把三类数据
       连成一张可筛选的异常事件台账数据集；
    2. 同一异常只解析出一个当前终态（确认终态），清理回执与恢复事件同时
       更新同一检查点时，按策略让一方终态获胜、另一方延后，只保留一个
       确认终态；
    3. 处理项动作把确认终态回写各关联对象并联动核对，重复提交只保留
       一条结果；执行记录按执行编号加载台账并写入最终恢复结果。
"""
from __future__ import annotations

from typing import Any

from app.store import store


class RecoveryTrackService:
    """异常恢复追踪 — 跨模块联合台账服务（纯数据能力，不占用 spec 模块位）。"""

    SERVICE_NAME = "recovery_track"
    ENTITY = "异常恢复追踪"
    ARTIFACT = "异常事件台账数据集"

    # 沿用异常日志既有版本口径的终态词表
    FINAL_STATES = ["未处理", "处理中", "已恢复", "已忽略"]
    TERMINAL_STATES = ("已恢复", "已忽略")

    # 清理回执与恢复事件冲突时的两种处理方式：
    #   cleanup_first  — 清理终态获胜并延后恢复
    #   recovery_first — 恢复终态获胜并延后清理
    POLICY_CLEANUP_FIRST = "cleanup_first"
    POLICY_RECOVERY_FIRST = "recovery_first"
    CONFLICT_POLICIES = (POLICY_CLEANUP_FIRST, POLICY_RECOVERY_FIRST)
    DEFAULT_POLICY = POLICY_CLEANUP_FIRST

    # 处理项动作 → 确认终态（执行归档属于清理口径）
    ACTION_FINAL_STATE = {
        "标记恢复": "已恢复",
        "忽略事件": "已忽略",
        "执行归档": "已忽略",
    }

    def __init__(self) -> None:
        # 执行键 → 处理结果：重复提交只保留一条结果（幂等）
        self._processed: dict[str, dict[str, Any]] = {}
        # 执行键 → 冲突中被延后的一方（"恢复" / "清理"）
        self._deferred: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 执行键解析：异常事件与恢复记录共享同一执行键
    # ------------------------------------------------------------------

    def _execution_index(self) -> dict[str, dict[str, Any]]:
        """执行编号 → 执行日志行。"""
        index: dict[str, dict[str, Any]] = {}
        for row in store.rows("execution_log"):
            exec_no = str(row.get("执行编号") or "").strip()
            if exec_no:
                index[exec_no] = row
        return index

    def exec_key_for_process(self, bot_name: str) -> str | None:
        """按所属流程找到最近一次执行编号，作为流程口径的执行键。"""
        exec_no: str | None = None
        for row in store.rows("execution_log"):
            if str(row.get("所属流程") or "") == bot_name:
                exec_no = str(row.get("执行编号") or "") or exec_no
        return exec_no

    def execution_key_for_anomaly(self, row: dict[str, Any]) -> str:
        """解析异常事件所属的执行键。

        优先使用异常登记时写入的执行键；其次按关联流程匹配执行记录；
        都没有时回退到事件编号，保证台账键始终稳定。
        """
        for field in ("执行键", "执行编号"):
            key = str(row.get(field) or "").strip()
            if key:
                return key
        matched = self.exec_key_for_process(str(row.get("关联流程") or ""))
        if matched:
            return matched
        return str(row.get("事件编号") or row.get("id") or "")

    def _recoveries_for(self, exec_key: str) -> list[Any]:
        return [
            entry for entry in store.recovery.entries()
            if entry.batch_ref == exec_key
        ]

    # ------------------------------------------------------------------
    # 证据采集与终态解析
    # ------------------------------------------------------------------

    def _collect_evidence(
        self,
        anomaly: dict[str, Any],
        exec_key: str,
        exec_row: dict[str, Any] | None,
        recoveries: list[Any],
    ) -> tuple[list[str], list[str]]:
        """收集同一检查点的清理证据与恢复证据。"""
        cleanup: list[str] = []
        recovery: list[str] = []

        status = str(anomaly.get("status") or "")
        declared = str(anomaly.get("恢复状态") or "")

        # 恢复证据：异常被标记恢复 / 恢复管理器已确认
        if status == "已恢复":
            recovery.append("异常事件已标记恢复")
        if declared == "已恢复":
            recovery.append("恢复状态登记为已恢复")
        resolved = [rec for rec in recoveries if rec.resolved]
        if resolved:
            recovery.append(f"恢复管理器已确认×{len(resolved)}")

        # 清理证据：异常被忽略 / 执行记录已归档（清理回执）
        if status == "已忽略":
            cleanup.append("异常事件已忽略")
        if declared == "已忽略":
            cleanup.append("恢复状态登记为已忽略")
        if exec_row is not None and exec_row.get("已归档"):
            cleanup.append("执行记录已归档")

        # 处理项已确认的终态也是证据
        processed = self._processed.get(exec_key)
        if processed:
            final = processed.get("确认终态")
            if final == "已恢复":
                recovery.append("处理项已确认恢复")
            elif final == "已忽略":
                cleanup.append("处理项已确认清理")

        return cleanup, recovery

    def _resolve_final_state(
        self,
        status: str,
        cleanup_evidence: list[str],
        recovery_evidence: list[str],
        policy: str,
    ) -> tuple[str, bool, str]:
        """解析同一执行对象的当前终态。

        清理回执与恢复事件同时更新同一检查点时发生冲突：
        按策略让一方终态获胜、另一方延后，只保留一个确认终态。

        Returns:
            (当前终态, 是否冲突, 被延后的一方)
        """
        conflict = bool(cleanup_evidence) and bool(recovery_evidence)
        deferred = ""
        if conflict:
            if policy == self.POLICY_RECOVERY_FIRST:
                final, deferred = "已恢复", "清理"
            else:
                final, deferred = "已忽略", "恢复"
        elif recovery_evidence:
            final = "已恢复"
        elif cleanup_evidence:
            final = "已忽略"
        else:
            final = status if status in ("未处理", "处理中") else "未处理"
        return final, conflict, deferred

    @staticmethod
    def _pending_action(final: str, conflict: bool, deferred: str) -> str:
        """按当前终态推导待执行动作。"""
        if conflict:
            return f"延后{deferred}确认"
        if final == "未处理":
            return "标记恢复 / 忽略事件"
        if final == "处理中":
            return "确认恢复或清理"
        return "无"

    # ------------------------------------------------------------------
    # 台账构建与查询（搜索过滤定位）
    # ------------------------------------------------------------------

    def build_ledger(self, policy: str | None = None) -> list[dict[str, Any]]:
        """生成异常事件台账数据集：每个异常事件一行，带清理与恢复证据。

        读取异常事件数据、执行日志、恢复记录数据，按共享执行键解析
        同一执行对象的终态，让使用者可按流程定位恢复过程的完整终点。
        """
        policy = policy or self.DEFAULT_POLICY
        exec_index = self._execution_index()
        ledger: list[dict[str, Any]] = []

        for anomaly in store.rows("anomaly_log"):
            exec_key = self.execution_key_for_anomaly(anomaly)
            exec_row = exec_index.get(exec_key)
            recoveries = self._recoveries_for(exec_key)
            cleanup_ev, recovery_ev = self._collect_evidence(
                anomaly, exec_key, exec_row, recoveries)
            final, conflict, deferred = self._resolve_final_state(
                str(anomaly.get("status") or ""), cleanup_ev, recovery_ev, policy)

            ledger.append({
                "id": anomaly.get("id"),
                "执行键": exec_key,
                "执行编号": str(exec_row.get("执行编号") or "") if exec_row else (
                    exec_key if exec_key.startswith("EXEC-") else ""),
                "流程名称": str(anomaly.get("关联流程") or ""),
                "事件编号": str(anomaly.get("事件编号") or ""),
                "异常类型": str(anomaly.get("异常类型") or ""),
                "恢复状态": final,
                "当前终态": final,
                "清理证据": cleanup_ev,
                "恢复证据": recovery_ev,
                "待执行动作": self._pending_action(final, conflict, deferred),
                "冲突": conflict,
                "冲突策略": policy if conflict else "",
                "被延后": deferred,
            })
        return ledger

    def query_ledger(
        self,
        *,
        process_name: str | None = None,
        exec_no: str | None = None,
        recovery_status: str | None = None,
        keyword: str | None = None,
        policy: str | None = None,
    ) -> list[dict[str, Any]]:
        """按流程名称、执行编号和恢复状态筛选联合台账。"""
        rows = self.build_ledger(policy=policy)
        if process_name:
            rows = [r for r in rows
                    if process_name.casefold() in r["流程名称"].casefold()]
        if exec_no:
            rows = [r for r in rows
                    if exec_no.casefold() in r["执行键"].casefold()
                    or exec_no.casefold() in r["执行编号"].casefold()]
        if recovery_status:
            rows = [r for r in rows if r["恢复状态"] == recovery_status]
        if keyword:
            rows = [
                r for r in rows
                if keyword.casefold() in r["事件编号"].casefold()
                or keyword.casefold() in r["流程名称"].casefold()
                or keyword.casefold() in r["执行键"].casefold()
            ]
        return rows

    def get_ledger_row(self, exec_key: str,
                       policy: str | None = None) -> dict[str, Any] | None:
        for row in self.build_ledger(policy=policy):
            if row["执行键"] == exec_key:
                return row
        return None

    # ------------------------------------------------------------------
    # 处理项：确认终态收敛 + 联动核对 + 幂等
    # ------------------------------------------------------------------

    def process_item(
        self,
        exec_key: str,
        action: str,
        policy: str | None = None,
    ) -> dict[str, Any]:
        """处理一个异常恢复追踪项，把确认终态收敛到所有关联对象。

        - 同一异常只保留一个当前终态：忽略、恢复、执行归档都落到
          同一执行键下的确认终态；
        - 另一个业务入口（执行记录）跟着更新待办，并按执行编号写入
          对应异常的最终恢复结果；
        - 重复提交只保留一条结果（幂等）；
        - 处理后联动核对处理台账与结果清单。
        """
        exec_key = str(exec_key or "").strip()
        target = self.ACTION_FINAL_STATE.get(action)
        if not exec_key:
            return {"ok": False, "message": "缺少执行键，无法定位处理项"}
        if target is None:
            return {"ok": False,
                    "message": f"动作「{action}」不属于异常恢复追踪可执行范围"}

        # 幂等：重复提交只保留一条结果
        if exec_key in self._processed:
            kept = self._processed[exec_key]
            return {
                **kept,
                "deduplicated": True,
                "message": f"处理项 {exec_key} 重复提交，已保留首次处理结果",
            }

        policy = policy or self.DEFAULT_POLICY
        anomalies = [
            row for row in store.rows("anomaly_log")
            if self.execution_key_for_anomaly(row) == exec_key
        ]
        exec_row = self._execution_index().get(exec_key)
        recoveries = self._recoveries_for(exec_key)
        if not anomalies and exec_row is None and not recoveries:
            return {"ok": False,
                    "message": f"执行键 {exec_key} 没有关联的异常恢复数据"}

        # 冲突登记：处理前若两种证据并存，被延后的一方保持待确认
        if anomalies:
            cleanup_ev, recovery_ev = self._collect_evidence(
                anomalies[0], exec_key, exec_row, recoveries)
            if cleanup_ev and recovery_ev:
                self._deferred[exec_key] = (
                    "清理" if target == "已恢复" else "恢复")

        # 确认终态收敛：异常事件、恢复记录、执行记录共享同一终态
        for row in anomalies:
            row["status"] = target
            row["恢复状态"] = target
            row["pending"] = False
            row["abnormal"] = target == "已忽略"
            row["执行键"] = exec_key
        for rec in recoveries:
            if target == "已恢复":
                store.recovery.resolve(rec.recovery_id)
        if exec_row is not None:
            # 另一个业务入口跟着更新待办，并按执行编号写入最终恢复结果
            exec_row["pending"] = False
            exec_row["恢复结果"] = target
            if action == "执行归档":
                exec_row["已归档"] = True
                exec_row["status"] = "执行成功"

        # 联动核对：处理台账与结果清单一致性
        consistent, mismatches = self._cross_check(exec_key, target)

        result = {
            "ok": True,
            "exec_key": exec_key,
            "action": action,
            "确认终态": target,
            "终态": target,
            "policy": policy,
            "deduplicated": False,
            "consistent": consistent,
            "mismatches": mismatches,
            "updated": {
                "anomalies": len(anomalies),
                "recoveries": len(recoveries),
                "execution": exec_row is not None,
            },
            "message": f"处理项 {exec_key} 已确认终态：{target}",
        }
        self._processed[exec_key] = result
        return result

    def _cross_check(self, exec_key: str,
                     expected: str) -> tuple[bool, list[str]]:
        """联动核对：处理台账与结果清单（异常事件 / 执行记录）是否一致。"""
        mismatches: list[str] = []
        for row in store.rows("anomaly_log"):
            if self.execution_key_for_anomaly(row) != exec_key:
                continue
            if row.get("status") != expected:
                mismatches.append(
                    f"异常事件{row.get('事件编号')}状态为{row.get('status')}，"
                    f"应为{expected}")
        exec_row = self._execution_index().get(exec_key)
        if exec_row is not None and exec_row.get("恢复结果") != expected:
            mismatches.append(
                f"执行记录{exec_key}恢复结果为"
                f"{exec_row.get('恢复结果') or '空'}，应为{expected}")
        ledger_row = self.get_ledger_row(exec_key)
        if ledger_row is not None and ledger_row["恢复状态"] != expected:
            mismatches.append(
                f"台账终态为{ledger_row['恢复状态']}，应为{expected}")
        return not mismatches, mismatches

    # ------------------------------------------------------------------
    # 执行记录口径：加载台账并按执行编号回写最终恢复结果
    # ------------------------------------------------------------------

    def sync_execution_results(self, policy: str | None = None) -> dict[str, Any]:
        """执行记录加载处理项关联的异常事件台账，按执行编号写入最终恢复结果。"""
        ledger = self.build_ledger(policy=policy)
        by_exec = {
            row["执行键"]: row for row in ledger if row["执行编号"]
        }
        synced: list[dict[str, str]] = []
        for exec_no, exec_row in self._execution_index().items():
            ledger_row = by_exec.get(exec_no)
            if ledger_row is None:
                continue
            final = ledger_row["恢复状态"]
            if exec_row.get("恢复结果") != final:
                exec_row["恢复结果"] = final
            synced.append({"执行编号": exec_no, "恢复结果": final})
        return {"synced": len(synced), "results": synced}

    # ------------------------------------------------------------------
    # 领域快照
    # ------------------------------------------------------------------

    def domain_snapshot(self) -> dict[str, Any]:
        ledger = self.build_ledger()
        by_status = {
            state: sum(1 for row in ledger if row["恢复状态"] == state)
            for state in self.FINAL_STATES
        }
        return {
            "module": self.SERVICE_NAME,
            "entity": self.ENTITY,
            "artifact": self.ARTIFACT,
            "count": len(ledger),
            "by_status": by_status,
            "conflicts": sum(1 for row in ledger if row["冲突"]),
            "processed": len(self._processed),
            "deferred": len(self._deferred),
            "recovery_pending": store.recovery.pending_count(),
        }


# 全局异常恢复追踪实例
recovery_track = RecoveryTrackService()
