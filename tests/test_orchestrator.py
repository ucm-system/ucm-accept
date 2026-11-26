from __future__ import annotations

import json

import pytest

from ucm_accept.config import Config
from ucm_accept.engine.mock import MockEngine
from ucm_accept.models import ITEM_KEYS, Status
from ucm_accept.orchestrator import Orchestrator


def test_mock_full_acceptance(config: Config):
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(profile="nominal", delay_ms=0))
    report = orchestrator.run_all()
    assert report.conclusion == "通过"
    assert set(report.items) == set(ITEM_KEYS)
    assert all(item.status == Status.PASSED for item in report.items.values())
    assert (orchestrator.run_dir / "report.html").exists()
    assert (orchestrator.run_dir / "report.json").exists()
    data = json.loads((orchestrator.run_dir / "report.json").read_text(encoding="utf-8"))
    assert data["conclusion"] == "通过"
    assert len(data["items"]) == 5
    # 模拟模式也保留可追溯的执行命令
    assert data["items"]["perf"]["detail"]["command"]
    assert "模拟模式" in data["items"]["perf"]["detail"]["command"]


def test_mock_partial_report_is_incomplete(config: Config):
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    orchestrator.run_item("perf")
    assert orchestrator.report.conclusion == "未完成"
    assert set(orchestrator.report.items) == {"perf"}


def test_run_all_with_subset_keys(config: Config):
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    report = orchestrator.run_all(["sla", "perf"])
    assert set(report.items) == {"perf", "sla"}
    assert report.selected == ["perf", "sla"]
    # 结论只按所选范围计算，不会因为其它三项未跑而判“未完成”
    assert report.conclusion == "通过"
    data = json.loads((orchestrator.run_dir / "report.json").read_text(encoding="utf-8"))
    assert data["selected"] == ["perf", "sla"]
    assert data["conclusion"] == "通过"


def test_run_all_respects_config_run_items(config: Config):
    config.set("run.items", ["perf", "multiturn"])
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    report = orchestrator.run_all()
    assert set(report.items) == {"perf", "multiturn"}
    assert report.conclusion == "通过"


def test_run_all_subset_drops_stale_items(config: Config):
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    orchestrator.run_all()          # 先跑全部
    report = orchestrator.run_all(["perf", "sla"])  # 再跑子集
    assert set(report.items) == {"perf", "sla"}
    assert report.selected == ["perf", "sla"]
    assert report.conclusion == "通过"


def test_run_all_empty_keys_rejected(config: Config):
    from ucm_accept.errors import UCMAcceptError

    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    with pytest.raises(UCMAcceptError):
        orchestrator.run_all([])


def test_accuracy_respects_dataset_enabled(config: Config):
    config.set("accuracy.datasets.gpqa.enabled", False)
    config.set("accuracy.datasets.longbench_v2.enabled", False)
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    item = orchestrator.run_item("accuracy")
    assert item.status == Status.PASSED
    ran = {dataset["key"] for dataset in item.detail["datasets"]}
    assert ran == {"gsm8k", "bfcl_v3"}


def test_mock_degraded_profile_fails_sla(config: Config):
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(profile="degraded", delay_ms=0))
    report = orchestrator.run_all()
    assert report.items["sla"].status == Status.FAILED
    assert report.items["accuracy"].status == Status.WARNING
    assert report.conclusion == "不通过"


def test_multiturn_scenario_builtin(config: Config):
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    item = orchestrator.run_item("multiturn", scenario="share_gpt_zh")
    assert item.status == Status.PASSED
    assert item.detail["scenario"] == "share_gpt_zh"
    assert item.detail["label"] == "真实对话数据集（中文）"


def test_accuracy_single_run_is_warning(config: Config):
    config.set("accuracy.runs", 1)
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    item = orchestrator.run_item("accuracy")
    assert item.status == Status.WARNING
    assert "无法判定" in item.summary


def test_agent_degraded_is_warning(config: Config):
    config.set("agent.context_detected", 32768)
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    item = orchestrator.run_item("agent")
    assert item.status == Status.WARNING
    assert item.detail["degraded"] is True
    assert "smoke" in item.summary


def test_sla_auto_tune_mode(config: Config):
    config.set("sla.mode", "auto_tune")
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    item = orchestrator.run_item("sla")
    assert item.detail["strategy"] == "native"
    assert item.status in {Status.PASSED, Status.WARNING, Status.FAILED}
    assert "avg / p99" in item.detail["note"]


def test_progress_events_are_monotonic(config: Config):
    events = []
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0), progress=events.append)
    orchestrator.run_item("perf")
    assert events
    overall = [event.overall for event in events]
    assert all(0 <= value <= 1 for value in overall)
    assert overall[-1] == 1.0


def test_cancel_flag_is_reset_for_next_run(config: Config):
    orchestrator = Orchestrator(config, MockEngine(delay_ms=0))
    orchestrator.cancel()
    item = orchestrator.run_item("perf")
    assert item.status == Status.PASSED
