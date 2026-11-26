from __future__ import annotations

import json
from pathlib import Path

import pytest

from ucm_accept.cli import _parse_selection, main


def test_parse_selection_numbers_and_names():
    assert _parse_selection("1,3-5") == ["perf", "sla", "accuracy", "agent"]
    assert _parse_selection("perf,sla") == ["perf", "sla"]
    assert _parse_selection("5,1") == ["perf", "agent"]
    assert _parse_selection("2") == ["multiturn"]


def test_parse_selection_rejects_bad_input():
    with pytest.raises(ValueError):
        _parse_selection("")
    with pytest.raises(ValueError):
        _parse_selection("9")
    with pytest.raises(ValueError):
        _parse_selection("perf,boom")
    with pytest.raises(ValueError):
        _parse_selection("5-2")


def test_cli_all_mock(tmp_path: Path, capsys):
    outputs = tmp_path / "outputs"
    code = main(["--mock", "--fast", "--all", "--outputs", str(outputs)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "验收结论：通过" in captured
    report = outputs / "runs"
    reports = list(report.glob("*/report.json"))
    assert reports
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["conclusion"] == "通过"
    assert len(data["items"]) == 5


def test_cli_items_subset(tmp_path: Path, capsys):
    outputs = tmp_path / "outputs"
    code = main(["--mock", "--fast", "--items", "perf,sla", "--outputs", str(outputs)])
    assert code == 0
    captured = capsys.readouterr().out
    assert "验收结论：通过" in captured
    reports = list((outputs / "runs").glob("*/report.json"))
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert set(data["items"]) == {"perf", "sla"}
    assert data["selected"] == ["perf", "sla"]


def test_cli_save_config(tmp_path: Path, capsys):
    target = tmp_path / "saved.yaml"
    code = main(
        [
            "--base-url",
            "http://10.2.2.2:9000/v1",
            "--model",
            "demo-model",
            "--save-config",
            str(target),
        ]
    )
    assert code == 0
    assert target.exists()
    text = target.read_text(encoding="utf-8")
    assert "10.2.2.2" in text
    # 复用保存的配置：run.items 范围内跑 mock，并确认报告正常生成
    outputs = tmp_path / "outputs"
    code = main(
        ["--mock", "--fast", "--config", str(target), "--outputs", str(outputs), "--all"]
    )
    assert code == 0
    assert "验收结论：通过" in capsys.readouterr().out


def test_cli_single_item_with_scenario(tmp_path: Path, capsys):
    outputs = tmp_path / "outputs"
    code = main(
        [
            "--mock",
            "--fast",
            "--item",
            "multiturn",
            "--scenario",
            "share_gpt_zh",
            "--outputs",
            str(outputs),
        ]
    )
    assert code == 0
    assert "多轮对话" in capsys.readouterr().out
    reports = list((outputs / "runs").glob("*/report.json"))
    data = json.loads(reports[0].read_text(encoding="utf-8"))
    assert data["items"]["multiturn"]["detail"]["scenario"] == "share_gpt_zh"
    assert data["conclusion"] == "未完成"


def test_cli_doctor_mock(tmp_path: Path, capsys):
    code = main(["--mock", "--doctor"])
    assert code == 0
    assert "自检" in capsys.readouterr().out


def test_cli_report_export(tmp_path: Path, capsys):
    outputs = tmp_path / "outputs"
    target = tmp_path / "deliver" / "report.html"
    code = main(
        [
            "--mock",
            "--fast",
            "--item",
            "perf",
            "--outputs",
            str(outputs),
            "--report",
            str(target),
        ]
    )
    assert code == 0
    assert target.exists()
    assert "验收结论" in target.read_text(encoding="utf-8")


def test_cli_no_tty_requires_mode(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    code = main([])
    assert code == 2
