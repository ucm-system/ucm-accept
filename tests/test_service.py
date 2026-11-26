from __future__ import annotations

from pathlib import Path

from ucm_accept.config import Config
from ucm_accept.errors import UCMAcceptError
from ucm_accept.service import AcceptanceService


def test_service_probe_and_run(config: Config):
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    probe = service.check_connection()
    assert probe.connected
    item = service.run_item("perf")
    assert item.status.value == "passed"
    assert service.current_report() is not None
    assert service.report_path() is not None


def test_service_each_run_is_fresh(config: Config):
    """每次执行都是全新 run：独立 run_id、独立报告，互不覆盖（避免同目录二跑冲突）。"""
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    service.run_item("perf")
    first = service.current_report()
    service.run_item("multiturn")
    second = service.current_report()
    assert first.run_id != second.run_id
    assert set(second.items.keys()) == {"multiturn"}


def test_endpoint_change_starts_new_report(config: Config):
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    service.run_item("perf")
    first = service.current_report().run_id
    service.update_endpoint(base_url="http://127.0.0.1:9999/v1", model="other")
    item = service.run_item("perf")
    second = service.current_report().run_id
    assert first != second
    assert item.detail["config"]["source"] == "mock"


def test_invalid_override_rejected(config: Config):
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    try:
        service.apply_overrides({"perf": {"parallel": [0]}})
    except Exception as exc:  # noqa: BLE001
        assert "并发" in str(exc)
    else:
        raise AssertionError("应当拒绝非法并发")


def test_service_export_pdf_without_report_errors(config: Config):
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    try:
        service.export_pdf()
    except UCMAcceptError:
        pass
    else:
        raise AssertionError("没有报告时应报错")


def test_reset_clears_session(config: Config):
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    service.run_item("perf")
    service.reset()
    assert service.current_report() is None


def test_load_latest_report_restores_session(config: Config):
    """进程重启后应能从输出目录恢复最近一次报告。"""
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0)
    service.run_item("perf")
    assert service.current_report() is not None
    # 模拟重启：全新 service 实例指向同一输出目录
    restored = AcceptanceService(config, mode="mock", mock_delay_ms=0,
                                 outputs_root=service.outputs_root)
    assert restored.load_latest_report() is True
    report = restored.current_report()
    assert report is not None and "perf" in report.items
    # 无报告的空目录返回 False
    empty = AcceptanceService(config, mode="mock", mock_delay_ms=0,
                              outputs_root=str(Path(config.report_dir).parent / "empty-outputs"))
    assert empty.load_latest_report() is False


def test_shutdown_stops_running_task_and_saves(config: Config):
    """shutdown 应停止执行中的任务并把报告落盘（对应进程被关闭的场景）。"""
    import threading

    service = AcceptanceService(config, mode="mock", mock_delay_ms=3000)
    worker = threading.Thread(target=lambda: service.run_item("perf"))
    worker.start()
    deadline = 20.0
    import time

    while not service.running and deadline > 0:
        time.sleep(0.05)
        deadline -= 0.05
    assert service.running, "任务应已进入执行状态"
    service.shutdown(timeout=10.0)
    worker.join(timeout=10)
    assert not service.running
    report = service.current_report()
    # run_item 取消路径写入 SKIPPED；mock 引擎在项间检查取消点
    assert report is not None and report.items
