"""异常恢复追踪数据能力测试 — 联合台账、单一终态、冲突策略、处理项幂等。

覆盖：
1. 异常事件与恢复记录共享同一执行键
2. 联合台账汇聚异常事件 / 执行日志 / 恢复记录三类数据
3. 同一异常只有一个当前终态
4. 清理回执与恢复事件冲突时的两种处理方式（都只保留一个确认终态）
5. 处理项：终态收敛、另一个业务入口更新待办、联动核对、重复提交幂等
6. 执行记录按执行编号写入最终恢复结果
7. 按流程名称 / 执行编号 / 恢复状态搜索过滤定位
"""
from __future__ import annotations

import sys
import os
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.store import store
from app.services.anomaly_log import AnomalyLogService
from app.services.execution_log import ExecutionLogService
from app.services.recovery_track import RecoveryTrackService, recovery_track


def _unique_key(prefix: str = "EXEC-RT") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def _make_anomaly(exec_key: str, bot: str = "恢复追踪测试流程") -> dict:
    svc = AnomalyLogService()
    result = svc.log_exception(
        bot_ref=bot, step="元素定位", error_type="选择器超时",
        detail="测试异常", exec_no=exec_key)
    assert result["created"] is True
    row = [
        r for r in store.rows("anomaly_log")
        if r.get("事件编号") == result["event_number"]
    ][0]
    return row


def _make_execution(exec_key: str, bot: str = "恢复追踪测试流程") -> dict:
    svc = ExecutionLogService()
    entry, missing = svc.create_entry({
        "执行编号": exec_key,
        "所属流程": bot,
        "开始时间": "2026-11-18 10:00:00",
        "结束时间": "2026-11-18 10:01:00",
        "执行状态": "执行中",
        "采集行数": "0",
        "错误摘要": "",
    })
    assert not missing
    return entry


class TestSharedExecutionKey:
    """异常事件与恢复记录必须共享同一执行键"""

    def test_log_exception_shares_key_with_recovery(self) -> None:
        exec_key = _unique_key()
        svc = AnomalyLogService()
        result = svc.log_exception(
            bot_ref="共享键流程", step="点击搜索", error_type="超时",
            detail="元素未出现", exec_no=exec_key)
        assert result["exec_key"] == exec_key

        anomaly = [
            r for r in store.rows("anomaly_log")
            if r.get("事件编号") == result["event_number"]
        ][0]
        assert anomaly["执行键"] == exec_key

        recoveries = [
            rec for rec in store.recovery.entries()
            if rec.batch_ref == exec_key
        ]
        assert len(recoveries) == 1
        assert recoveries[0].module == "anomaly_log"

    def test_log_exception_derives_key_from_process(self) -> None:
        exec_key = _unique_key()
        _make_execution(exec_key, bot="派生键流程")
        svc = AnomalyLogService()
        result = svc.log_exception(
            bot_ref="派生键流程", step="提取", error_type="解析失败",
            detail="字段缺失")
        assert result["exec_key"] == exec_key


class TestJointLedger:
    """联合台账：读取异常事件、执行日志、恢复记录，解析同一执行对象终态"""

    def test_ledger_row_carries_evidence_fields(self) -> None:
        exec_key = _unique_key()
        _make_execution(exec_key)
        _make_anomaly(exec_key)
        rows = recovery_track.query_ledger(exec_no=exec_key)
        assert len(rows) == 1
        row = rows[0]
        for field in ("执行键", "执行编号", "流程名称", "事件编号",
                      "恢复状态", "当前终态", "清理证据", "恢复证据",
                      "待执行动作", "冲突"):
            assert field in row, f"台账缺少字段 {field}"
        assert row["执行键"] == exec_key
        assert row["执行编号"] == exec_key
        assert row["恢复状态"] == "未处理"

    def test_single_final_state_per_anomaly(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key)
        svc = AnomalyLogService()
        anomaly = [
            r for r in store.rows("anomaly_log") if r.get("执行键") == exec_key
        ][0]
        svc.run_action(int(anomaly["id"]), "标记恢复")
        # 同一异常只保留一个当前终态
        assert anomaly["status"] == "已恢复"
        assert anomaly["恢复状态"] == "已恢复"
        assert anomaly["pending"] is False
        rows = recovery_track.query_ledger(exec_no=exec_key)
        assert rows[0]["恢复状态"] == "已恢复"
        assert rows[0]["当前终态"] == rows[0]["恢复状态"]

    def test_recovery_action_resolves_recovery_manager(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key)
        anomaly = [
            r for r in store.rows("anomaly_log") if r.get("执行键") == exec_key
        ][0]
        AnomalyLogService().run_action(int(anomaly["id"]), "标记恢复")
        recoveries = [
            rec for rec in store.recovery.entries()
            if rec.batch_ref == exec_key
        ]
        assert recoveries and all(rec.resolved for rec in recoveries)
        rows = recovery_track.query_ledger(exec_no=exec_key)
        assert any("恢复管理器" in ev for ev in rows[0]["恢复证据"])


class TestConflictPolicies:
    """清理回执与恢复事件同时更新同一检查点：只保留一个确认终态"""

    def _make_conflict(self, exec_key: str) -> None:
        """构造冲突：恢复证据（已恢复）+ 清理回执（执行已归档）。"""
        _make_anomaly(exec_key)
        anomaly = [
            r for r in store.rows("anomaly_log") if r.get("执行键") == exec_key
        ][0]
        AnomalyLogService().run_action(int(anomaly["id"]), "标记恢复")
        exec_row = _make_execution(exec_key)
        ExecutionLogService().run_action(int(exec_row["id"]), "归档日志")
        assert exec_row["已归档"] is True

    def test_cleanup_first_defers_recovery(self) -> None:
        exec_key = _unique_key()
        self._make_conflict(exec_key)
        rows = recovery_track.query_ledger(
            exec_no=exec_key, policy=RecoveryTrackService.POLICY_CLEANUP_FIRST)
        row = rows[0]
        assert row["冲突"] is True
        assert row["恢复状态"] == "已忽略"       # 清理终态获胜
        assert row["被延后"] == "恢复"            # 恢复被延后
        assert row["清理证据"] and row["恢复证据"]

    def test_recovery_first_defers_cleanup(self) -> None:
        exec_key = _unique_key()
        self._make_conflict(exec_key)
        rows = recovery_track.query_ledger(
            exec_no=exec_key, policy=RecoveryTrackService.POLICY_RECOVERY_FIRST)
        row = rows[0]
        assert row["冲突"] is True
        assert row["恢复状态"] == "已恢复"       # 恢复终态获胜
        assert row["被延后"] == "清理"            # 清理被延后

    def test_conflict_keeps_single_confirmed_state(self) -> None:
        exec_key = _unique_key()
        self._make_conflict(exec_key)
        for policy in RecoveryTrackService.CONFLICT_POLICIES:
            rows = recovery_track.query_ledger(exec_no=exec_key, policy=policy)
            row = rows[0]
            # 两种处理方式都只保留一个确认终态
            assert row["恢复状态"] in ("已恢复", "已忽略")
            assert row["当前终态"] == row["恢复状态"]
            assert row["被延后"] in ("恢复", "清理")


class TestProcessItem:
    """处理项：终态收敛、待办联动、联动核对、重复提交幂等"""

    def test_process_item_converges_all_objects(self) -> None:
        exec_key = _unique_key()
        _make_execution(exec_key)
        _make_anomaly(exec_key)
        result = recovery_track.process_item(exec_key, "标记恢复")
        assert result["ok"] is True
        assert result["确认终态"] == "已恢复"

        anomaly = [
            r for r in store.rows("anomaly_log") if r.get("执行键") == exec_key
        ][0]
        assert anomaly["status"] == "已恢复"
        assert anomaly["pending"] is False

        exec_row = [
            r for r in store.rows("execution_log")
            if r.get("执行编号") == exec_key
        ][0]
        # 按执行编号写入对应异常的最终恢复结果
        assert exec_row["恢复结果"] == "已恢复"
        # 另一个业务入口跟着更新待办
        assert exec_row["pending"] is False

        recoveries = [
            rec for rec in store.recovery.entries()
            if rec.batch_ref == exec_key
        ]
        assert all(rec.resolved for rec in recoveries)

    def test_process_item_cross_check_consistent(self) -> None:
        exec_key = _unique_key()
        _make_execution(exec_key)
        _make_anomaly(exec_key)
        result = recovery_track.process_item(exec_key, "忽略事件")
        # 处理台账与结果清单联动核对一致
        assert result["consistent"] is True
        assert result["mismatches"] == []
        rows = recovery_track.query_ledger(exec_no=exec_key)
        assert rows[0]["恢复状态"] == "已忽略"

    def test_process_item_deduplicates_resubmission(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key)
        first = recovery_track.process_item(exec_key, "标记恢复")
        second = recovery_track.process_item(exec_key, "标记恢复")
        # 重复提交只保留一条结果
        assert first["deduplicated"] is False
        assert second["deduplicated"] is True
        assert second["确认终态"] == first["确认终态"]
        # 不同动作的重复提交也不产生并列终态
        third = recovery_track.process_item(exec_key, "忽略事件")
        assert third["deduplicated"] is True
        assert third["确认终态"] == "已恢复"

    def test_process_item_unknown_key(self) -> None:
        result = recovery_track.process_item(_unique_key(), "标记恢复")
        assert result["ok"] is False

    def test_process_item_rejects_unknown_action(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key)
        result = recovery_track.process_item(exec_key, "不存在的动作")
        assert result["ok"] is False


class TestExecutionSync:
    """执行记录加载台账并按执行编号写入最终恢复结果"""

    def test_sync_recovery_results_by_exec_no(self) -> None:
        exec_key = _unique_key()
        _make_execution(exec_key)
        _make_anomaly(exec_key)
        recovery_track.process_item(exec_key, "标记恢复")
        svc = ExecutionLogService()
        report = svc.sync_recovery_results()
        assert report["synced"] >= 1
        exec_row = [
            r for r in store.rows("execution_log")
            if r.get("执行编号") == exec_key
        ][0]
        assert exec_row["恢复结果"] == "已恢复"

    def test_archive_action_writes_back_recovery_result(self) -> None:
        exec_key = _unique_key()
        exec_row = _make_execution(exec_key)
        _make_anomaly(exec_key)
        svc = ExecutionLogService()
        svc.run_action(int(exec_row["id"]), "归档日志")
        assert exec_row["已归档"] is True
        assert exec_row["pending"] is False
        assert exec_row.get("恢复结果") == "已忽略"
        # 归档作为清理回执进入台账
        rows = recovery_track.query_ledger(exec_no=exec_key)
        assert rows[0]["恢复状态"] == "已忽略"
        assert any("归档" in ev for ev in rows[0]["清理证据"])


class TestLedgerQuery:
    """按流程名称、执行编号、恢复状态搜索过滤定位"""

    def test_filter_by_process_name(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key, bot="过滤专用流程甲")
        rows = recovery_track.query_ledger(process_name="过滤专用流程甲")
        assert len(rows) == 1
        assert rows[0]["流程名称"] == "过滤专用流程甲"

    def test_filter_by_exec_no(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key)
        rows = recovery_track.query_ledger(exec_no=exec_key)
        assert len(rows) == 1
        assert rows[0]["执行键"] == exec_key

    def test_filter_by_recovery_status(self) -> None:
        exec_key = _unique_key()
        _make_anomaly(exec_key)
        recovery_track.process_item(exec_key, "标记恢复")
        rows = recovery_track.query_ledger(recovery_status="已恢复")
        assert rows
        assert all(r["恢复状态"] == "已恢复" for r in rows)
        assert any(r["执行键"] == exec_key for r in rows)


class TestDomainSnapshot:
    def test_snapshot_fields(self) -> None:
        snapshot = recovery_track.domain_snapshot()
        assert snapshot["module"] == "recovery_track"
        assert snapshot["entity"] == "异常恢复追踪"
        assert snapshot["artifact"] == "异常事件台账数据集"
        assert snapshot["count"] >= 1
        assert "by_status" in snapshot
        assert "conflicts" in snapshot
