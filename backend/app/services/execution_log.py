"""执行记录领域服务 — 记录和查询 RPA 执行日志，输出 domain_snapshot。

数据工程核心：数据校验与去重策略执行 — 跨模块数据产物

异常恢复追踪口径：
- 执行记录加载处理项关联的异常事件台账，按执行编号写入对应异常
  的最终恢复结果；
- 归档日志作为清理回执进入联合台账，与恢复事件共享同一执行键。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.recovery_track import recovery_track
from app.store import store


class ExecutionLogService(BaseModuleService):
    MODULE = "execution_log"
    ENTITY = "执行日志"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["执行编号", "所属流程", "开始时间", "结束时间", "执行状态", "采集行数", "错误摘要"]
    REQUIRED_FIELDS = ["执行编号", "所属流程", "开始时间"]
    STATUS_ORDER = ["执行中", "执行成功", "执行失败", "已超时", "已取消"]
    ACTION_RULES = {"归档日志": "执行成功"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """归档日志：生成清理回执并回写关联异常的最终恢复结果。"""
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "归档日志":
            entry["已归档"] = True
            entry["pending"] = False
            self._write_back_recovery_result(entry)
        return entry, message

    def load_recovery_ledger(self, policy: str | None = None) -> list[dict[str, Any]]:
        """加载处理项关联的异常事件台账（执行记录口径）。"""
        return recovery_track.build_ledger(policy=policy)

    def sync_recovery_results(self, policy: str | None = None) -> dict[str, Any]:
        """按执行编号把对应异常的最终恢复结果写回执行记录。"""
        return recovery_track.sync_execution_results(policy=policy)

    def _write_back_recovery_result(self, entry: dict[str, Any]) -> None:
        """按执行编号从联合台账读取确认终态，写入本条执行记录。"""
        exec_no = str(entry.get("执行编号") or "")
        if not exec_no:
            return
        rows = recovery_track.query_ledger(exec_no=exec_no)
        if rows:
            entry["恢复结果"] = rows[0]["恢复状态"]

    def query_execution_history(self,
                                 bot_name: str | None = None,
                                 days: int = 7) -> list[dict[str, Any]]:
        """查询执行历史记录。"""
        rows = store.rows(self.MODULE)
        if bot_name:
            rows = [
                r for r in rows if bot_name in r.get("所属流程", "")
            ]
        return [
            {
                "id": r.get("id"), "exec_no": r.get("执行编号"),
                "bot": r.get("所属流程"), "status": r.get("status"),
                "start": r.get("开始时间"), "end": r.get("结束时间"),
                "error": r.get("错误摘要"),
            }
            for r in rows
        ]

    def get_execution_stats(self) -> dict[str, Any]:
        """获取执行统计概览。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        success = sum(1 for r in rows if r.get("status") == "执行成功")
        failed = sum(1 for r in rows if r.get("status") == "执行失败")
        return {
            "total_executions": total,
            "success_rate": round(success / max(total, 1), 2),
            "success": success,
            "failed": failed,
            "in_progress": sum(1 for r in rows if r.get("status") == "执行中"),
            "timeout": sum(1 for r in rows if r.get("status") == "已超时"),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        stats = self.get_execution_stats()
        rows = store.rows(self.MODULE)
        return {
            **base,
            **stats,
            "recovery_result_written": sum(
                1 for r in rows if r.get("恢复结果")),
        }
