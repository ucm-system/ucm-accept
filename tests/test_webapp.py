"""FastAPI Web 前端接口测试（mock 引擎，不产生真实请求）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from ucm_accept.config import Config
from ucm_accept.service import AcceptanceService
from ucm_accept.webapp import create_app


def _client(tmp_path) -> tuple[TestClient, AcceptanceService]:
    config = Config.load()
    service = AcceptanceService(config, mode="mock", mock_delay_ms=0,
                                outputs_root=str(tmp_path / "outputs"))
    return TestClient(create_app(service)), service


def test_state_and_index(tmp_path):
    client, _ = _client(tmp_path)
    r = client.get("/")
    assert r.status_code == 200 and "UCM" in r.text
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["mode"] == "mock"
    assert len(data["multiturn"]["scenarios"]) >= 5
    assert data["perf"]["groups"]
    assert {item["key"] for item in data["accuracy"]["datasets"]} >= {"gsm8k", "gpqa"}


def test_probe_validation(tmp_path):
    client, _ = _client(tmp_path)
    r = client.post("/api/probe", json={"base_url": "", "model": ""})
    assert r.status_code in (200, 400)


def test_run_rejects_empty_items(tmp_path):
    client, _ = _client(tmp_path)
    payload = {"items": [], "endpoint": {"base_url": "http://127.0.0.1:8801/v1", "model": "m"}}
    r = client.post("/api/run", json={"payload": payload})
    assert r.status_code == 400


def test_run_mock_perf_end_to_end(tmp_path):
    client, service = _client(tmp_path)
    payload = {
        "items": ["perf"],
        "endpoint": {"base_url": "http://127.0.0.1:8801/v1", "model": "m", "api_key": ""},
        "tokenizer": {"use_builtin": False, "custom": "/models/tokenizer"},
        "perf": {"groups": [
            {"parallel": "1,2", "number": 8, "input": 512, "output": 64,
             "prefix_ratio": 90, "prefix_count": 4},
        ]},
    }
    r = client.post("/api/run", json={"payload": payload})
    assert r.status_code == 200
    assert service.config.get("perf.groups")[0]["input"] == 512
    assert service.config.get("perf.groups")[0]["prefix_ratio"] == 0.9
    # 等待执行完成
    import time
    for _ in range(120):
        if not service.running:
            break
        time.sleep(0.1)
    report = service.current_report()
    assert report is not None and "perf" in report.items
    assert report.items["perf"].status.value == "passed"
    # 报告可下载
    r = client.get("/api/report/download")
    assert r.status_code == 200 and b"report" in r.content.lower()
