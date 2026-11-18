"""异常日志领域服务 — 记录和追踪 RPA 执行异常，输出 domain_snapshot。

数据工程核心：水位追踪与增量采集调度 — 跨模块数据产物

异常恢复追踪口径：
- 异常事件与恢复记录共享同一执行键（执行编号）；
- 忽略、恢复动作落到同一异常的当前终态（已忽略 / 已恢复），
  不再保留多个并列状态；
- 终态证据同步写入恢复管理器，供异常恢复追踪联合台账解析。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.recovery_track import recovery_track
from app.store import store


class AnomalyLogService(BaseModuleService):
    MODULE = "anomaly_log"
    ENTITY = "异常事件"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["事件编号", "关联流程", "异常类型", "异常详情", "发生步骤", "发生时间", "恢复状态"]
    REQUIRED_FIELDS = ["事件编号", "关联流程", "异常类型"]
    STATUS_ORDER = ["未处理", "处理中", "已恢复", "已忽略"]
    ACTION_RULES = {"标记恢复": "已恢复", "忽略事件": "已忽略"}
    NEGATIVE_ACTIONS = ["忽略事件"]

    def log_exception(self, bot_ref: str, step: str,
                       error_type: str, detail: str,
                       exec_no: str | None = None) -> dict[str, Any]:
        """记录一条执行异常事件（增强版创建）。

        异常事件与恢复记录共享同一执行键：优先使用调用方给定的
        执行编号，其次按关联流程匹配最近一次执行记录，最后回退到
        事件编号，保证恢复管理器与异常事件落在同一检查点。
        """
        rows = store.rows(self.MODULE)
        event_number = f"ANOM-{len(rows) + 1:04d}"
        exec_key = (
            exec_no
            or recovery_track.exec_key_for_process(bot_ref)
            or event_number
        )
        values = {
            "事件编号": event_number,
            "关联流程": bot_ref,
            "异常类型": error_type,
            "异常详情": detail,
            "发生步骤": step,
            "发生时间": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "恢复状态": "未处理",
        }
        entry, missing = self.create_entry(values)
        if entry:
            entry["执行键"] = exec_key
            # 同时写入恢复管理器（共享同一执行键）
            store.recovery.log(
                module=self.MODULE,
                batch_ref=exec_key,
                step=step,
                error_detail=detail,
            )
        return {
            "event_number": event_number,
            "exec_key": exec_key,
            "created": entry is not None,
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """忽略 / 标记恢复：把同一异常收敛到唯一当前终态。

        终态证据同步写入恢复管理器，让异常恢复追踪台账能按执行键
        解析出恢复过程的完整终点。
        """
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message
        target = entry.get("status")
        if target in ("已恢复", "已忽略"):
            # 同一异常只保留一个当前终态，待办同步清零
            entry["pending"] = False
            entry["恢复状态"] = target
            exec_key = recovery_track.execution_key_for_anomaly(entry)
            entry["执行键"] = exec_key
            if target == "已恢复":
                for rec in store.recovery.entries():
                    if rec.batch_ref == exec_key and not rec.resolved:
                        store.recovery.resolve(rec.recovery_id)
        return entry, message

    def get_pending_anomalies(self) -> list[dict[str, Any]]:
        """获取未处理的异常事件列表。"""
        rows = store.rows(self.MODULE)
        return [
            {
                "id": r.get("id"), "event_no": r.get("事件编号"),
                "bot": r.get("关联流程"), "type": r.get("异常类型"),
                "step": r.get("发生步骤"), "detail": r.get("异常详情"),
                "time": r.get("发生时间"),
            }
            for r in rows if r.get("status") in ("未处理", "处理中")
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        return {
            **base,
            "unresolved": sum(1 for r in rows if r.get("status") in ("未处理", "处理中")),
            "resolved": sum(1 for r in rows if r.get("status") == "已恢复"),
            "ignored": sum(1 for r in rows if r.get("status") == "已忽略"),
            "recovery_pending": store.recovery.pending_count(),
        }
