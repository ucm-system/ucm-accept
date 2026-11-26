"""FastAPI Web 前端（替代 Gradio）：单页 HTML + REST/SSE API。

页面：GET /（单文件 index.html，Apple 风格，无外部依赖）
接口：
    GET  /api/state            当前配置 + 最近报告摘要（页面初始化）
    POST /api/probe            检测连通（真实探测 /v1/models + 上下文）
    POST /api/run              应用配置并启动验收（后台线程；执行中返回 409）
    POST /api/cancel           请求停止当前任务（当前项标记已取消，报告落盘）
    GET  /api/events           SSE：log / status / done 三类事件
    GET  /api/report           最近报告 HTML（iframe 内嵌）
    GET  /api/report/download  报告 HTML 下载
    GET  /api/config/export    当前生效配置 YAML 下载
    POST /api/config/save      保存配置到指定路径

达标判定全部由 EvalScope 原生命令完成；本模块只做参数编排与结果呈现。
"""

from __future__ import annotations

import io
import json
import queue
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import __version__
from . import assets as assets_hub
from .config import Config, ConfigError
from .errors import UCMAcceptError
from .events import ProgressEvent
from .models import ITEM_BY_KEY, ITEM_KEYS, AcceptanceReport
from .service import AcceptanceService
from .utils import find_latest_report

_WEB_DIR = Path(__file__).resolve().parent / "resources" / "web"


class ProbeBody(BaseModel):
    base_url: str = ""
    model: str = ""
    api_key: str = ""


class RunBody(BaseModel):
    payload: dict[str, Any] = {}


class SaveBody(BaseModel):
    path: str = ""


def _tokenizer_value(service: AcceptanceService, use_builtin: bool, builtin: str | None, custom: str) -> str:
    if use_builtin and builtin:
        hub = assets_hub.find_assets_root(str(service.config.get("app.assets_dir", "") or "") or None)
        if hub is not None:
            return str(hub / "tokenizers" / str(builtin).strip())
        return str(builtin).strip()
    return str(custom or "").strip()


def _apply_payload(service: AcceptanceService, payload: dict[str, Any]) -> list[str]:
    """前端提交的运行载荷 → 会话配置；返回本次执行范围 keys。"""
    config = service.config
    endpoint = payload.get("endpoint") or {}
    service.update_endpoint(
        base_url=str(endpoint.get("base_url", "") or "").strip(),
        model=str(endpoint.get("model", "") or "").strip(),
        api_key=str(endpoint.get("api_key", "") or ""),
    )

    tok = payload.get("tokenizer") or {}
    config.set(
        "perf.tokenizer_path",
        _tokenizer_value(service, bool(tok.get("use_builtin")), tok.get("builtin"), tok.get("custom", "")),
    )

    items = [key for key in (payload.get("items") or []) if key in ITEM_BY_KEY]
    if not items:
        raise UCMAcceptError("请至少勾选一个测试项")
    config.set("run.items", items)

    perf = payload.get("perf") or {}
    groups = []
    for group in perf.get("groups") or []:
        ratio = float(group.get("prefix_ratio", 90) or 0)
        raw_parallel = group.get("parallel", "8")
        if isinstance(raw_parallel, (list, tuple)):  # 配置直填列表时原样接受
            parallel_values = [int(v) for v in raw_parallel]
        else:
            parallel_values = [int(v) for v in str(raw_parallel).replace("，", ",").split(",") if str(v).strip()]
        groups.append(
            {
                "parallel": parallel_values,
                "number": max(1, int(group.get("number", 64))),
                "input": int(group.get("input", 65536)),
                "output": int(group.get("output", 512)),
                "prefix_ratio": max(0.0, min(1.0, ratio / 100 if ratio > 1 else ratio)),
                "prefix_count": max(1, int(group.get("prefix_count", 32))),
            }
        )
    if "perf" in items and not groups:
        raise UCMAcceptError("基础性能测试至少需要一组压测数据")
    config.set("perf.groups", groups)
    config.set("perf.dataset", str(perf.get("dataset", "random")).strip() or "random")
    config.set("perf.warmup_num", max(0, int(perf.get("warmup", 0) or 0)))
    config.set("endpoint.timeout_s", float(perf.get("timeout_s", 180) or 180))

    mt = payload.get("multiturn") or {}
    presets = config.get("multiturn.scenarios", {}) or {}
    label_to_key = {str(item.get("label", key)): key for key, item in presets.items()}
    selected = [label_to_key.get(sel, sel) for sel in (mt.get("scenarios") or [])]
    config.set("multiturn.scenarios_selected", selected)
    config.set("multiturn.conversations", max(1, int(mt.get("conversations", 12))))
    config.set("multiturn.parallel", max(1, int(mt.get("parallel", 4))))
    config.set("multiturn.min_turns", max(1, int(mt.get("min_turns") or 2)))
    config.set("multiturn.max_turns", max(1, int(mt.get("max_turns") or 5)))

    sla = payload.get("sla") or {}
    config.set("sla.input_length", int(sla.get("input", 65536)))
    config.set("sla.output_tokens", int(sla.get("output", 512)))
    ratio = float(sla.get("prefix_ratio", 90) or 0)
    config.set("sla.prefix_ratio", max(0.0, min(1.0, ratio / 100 if ratio > 1 else ratio)))
    config.set("sla.prefix_count", max(1, int(sla.get("prefix_count", 8))))
    config.set("sla.target.concurrency", max(1, int(sla.get("upper", 256))))
    config.set("sla.auto_tune.upper", max(1, int(sla.get("upper", 256))))
    config.set("sla.auto_tune.lower", max(1, int(sla.get("lower") or 2)))
    for key, field_name in (
        ("avg_ttft_s", "avg_ttft_s"),
        ("avg_tpot_ms", "avg_tpot_ms"),
        ("p99_ttft_s", "p99_ttft_s"),
        ("p99_tpot_ms", "p99_tpot_ms"),
    ):
        value = sla.get(field_name)
        config.set(f"sla.thresholds.{key}", float(value) if value not in (None, "", 0) else None)

    acc = payload.get("accuracy") or {}
    datasets_cfg = config.get("accuracy.datasets", {}) or {}
    label_map = {str(item.get("label", key)): key for key, item in datasets_cfg.items()}
    chosen = {label_map.get(sel, sel) for sel in (acc.get("datasets") or [])}
    for key, item in datasets_cfg.items():
        item["enabled"] = key in chosen
    config.set("accuracy.datasets", datasets_cfg)
    config.set("accuracy.runs", max(1, int(acc.get("runs", 2))))
    limit = int(acc.get("limit", 0) or 0)
    if limit > 0:
        for _ds_key, item in datasets_cfg.items():
            item["limit"] = limit
        config.set("accuracy.datasets", datasets_cfg)

    agent = payload.get("agent") or {}
    config.set("agent.context_required", int(agent.get("context", 262144)))
    config.set("agent.duration_s", max(1, int(agent.get("duration", 15))) * 60)
    agent = payload.get("agent") or {}
    config.set("agent.dataset_name", str(agent.get("dataset_name", "AgentX-claudecode")).strip())

    report_cfg = payload.get("report") or {}
    config.set("report.title", str(report_cfg.get("title", "") or "").strip())
    config.set("report.company", str(report_cfg.get("company", "") or "").strip())
    config.set("report.operator", str(report_cfg.get("operator", "") or "").strip())
    return items


def create_app(service: AcceptanceService) -> FastAPI:
    app = FastAPI(title="UCM Acceptance", version=__version__, docs_url=None, redoc_url=None)
    state: dict[str, Any] = {
        "thread": None,
        "events": None,  # 当前运行的 SSE 队列
        "log_lines": [],  # 当前执行的日志（轮询通道用）
        "error": None,
    }
    # 页面重开 / 进程重启后恢复最近一次执行的日志现场
    _last_log = find_latest_report(service.outputs_root)
    if _last_log:
        _run_log = _last_log.parent / "run.log"
        if _run_log.exists():
            try:
                _lines = _run_log.read_text(encoding="utf-8").splitlines()
                state["log_lines"] = [f"[恢复] {line}" for line in _lines[-2000:]]
            except OSError:
                pass

    def _report_snapshot() -> dict[str, Any]:
        report = service.current_report()
        if report is None:
            return {}
        return {
            "run_id": report.run_id,
            "conclusion": report.conclusion,
            "reasons": report.reasons,
            "mode": report.mode,
            "started_at": report.started_at,
            "output_dir": str(report.output_dir or ""),
            "items": {
                key: {"status": item.status.value, "summary": item.summary or ""}
                for key, item in report.items.items()
            },
        }

    def _state_payload() -> dict[str, Any]:
        config: Config = service.config
        hub = None
        try:
            hub = assets_hub.find_assets_root(str(config.get("app.assets_dir", "") or "") or None)
        except Exception:  # noqa: BLE001
            hub = None
        builtin = assets_hub.builtin_tokenizers(hub) if hub else []
        tok_path = str(config.get("perf.tokenizer_path", "") or "")
        builtin_match = next(
            (name for name in builtin
             if name and (name in tok_path or tok_path.endswith("/" + name))),
            None,
        )
        if builtin_match not in builtin:  # 路径残留已不存在的候选时视为自定义路径
            builtin_match = None
        presets = config.get("multiturn.scenarios", {}) or {}
        datasets_cfg = config.get("accuracy.datasets", {}) or {}
        groups = config.get("perf.groups", []) or []
        if not groups:
            groups = [
                {
                    "parallel": ",".join(str(v) for v in config.get("perf.parallel", [8])),
                    "number": config.get("perf.number", 64),
                    "input": config.get("perf.max_prompt_length", 65536),
                    "output": config.get("perf.max_tokens", 512),
                    "prefix_ratio": round(float(config.get("defaults.hit_rate", 0.9)) * 100),
                    "prefix_count": config.get("perf.prefix_count", 32),
                }
            ]
        selected = [str(k) for k in (config.get("multiturn.scenarios_selected", []) or [])]
        return {
            "version": __version__,
            "mode": service.mode,
            "title": str(config.get("app.title")),
            "subtitle": str(config.get("app.subtitle")),
            "endpoint": {
                "base_url": str(config.get("endpoint.base_url", "")),
                "model": str(config.get("endpoint.model", "")),
                "probe": None,
            },
            "tokenizer": {
                "has_builtin": bool(builtin),
                "builtin_choices": builtin,
                "use_builtin": builtin_match is not None,
                "builtin": builtin_match,
                "custom": "" if builtin_match else tok_path,
            },
            "items_default": [key for key in ITEM_KEYS if key in (config.run_items() or [])],
            "perf": {
                "groups": groups,
                "dataset": str(config.get("perf.dataset", "random")),
                "warmup": int(config.get("perf.warmup_num", 0)),
                "timeout_s": float(config.get("endpoint.timeout_s", 180)),
            },
            "multiturn": {
                "scenarios": [
                    {
                        "key": key,
                        "label": str(item.get("label", key)),
                        "dataset": str(item.get("dataset", "")),
                        "description": str(item.get("description", "")),
                    }
                    for key, item in presets.items()
                ],
                "selected": selected
                or [str(config.get("multiturn.scenario", ""))],
                "conversations": int(config.get("multiturn.conversations", 12)),
                "parallel": int(config.get("multiturn.parallel", 4)),
                "min_turns": int(config.get("multiturn.min_turns", 2)),
                "max_turns": int(config.get("multiturn.max_turns", 5)),
            },
            "sla": {
                "input": int(config.get("sla.input_length", 65536)),
                "output": int(config.get("sla.output_tokens", 512)),
                "prefix_ratio": round(float(config.get("sla.prefix_ratio", 0.9)) * 100),
                "prefix_count": int(config.get("sla.prefix_count", 8)),
                "upper": int(config.get("sla.auto_tune.upper", 256)),
                "lower": int(config.get("sla.auto_tune.lower", 2)),
                "thresholds": config.get("sla.thresholds", {}) or {},
            },
            "accuracy": {
                "datasets": [
                    {
                        "key": key,
                        "label": str(item.get("label", key)),
                        "dataset": str(item.get("dataset", key)),
                        "description": str(item.get("description", "")),
                        "enabled": bool(item.get("enabled", True)),
                        "limit": item.get("limit"),
                    }
                    for key, item in datasets_cfg.items()
                ],
                "runs": int(config.get("accuracy.runs", 2)),
            },
            "agent": {
                "context": int(config.get("agent.context_required", 262144)),
                "duration": max(1, int(config.get("agent.duration_s", 900)) // 60),
                "dataset_name": str(config.get("agent.dataset_name", "AgentX-claudecode")),
            },
            "running": service.running,
            "report": _report_snapshot(),
        }

    @app.post("/api/probe")
    def probe(body: ProbeBody) -> dict[str, Any]:
        try:
            service.update_endpoint(
                base_url=body.base_url.strip(), model=body.model.strip(), api_key=body.api_key
            )
        except (UCMAcceptError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        result = service.check_connection()
        return {
            "connected": result.connected,
            "message": result.message,
            "detail": result.detail,
            "latency_ms": result.latency_ms,
            "context_window": result.context_window,
        }

    @app.post("/api/run")
    def run(body: RunBody) -> JSONResponse:
        if service.running:
            raise HTTPException(status_code=409, detail="已有验收任务正在执行")
        try:
            items = _apply_payload(service, body.payload)
        except (UCMAcceptError, ConfigError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        events: queue.Queue = queue.Queue()
        state["events"] = events
        state["error"] = None
        state["log_lines"] = []

        def on_progress(event: ProgressEvent) -> None:
            line = f"[{event.overall * 100:5.1f}%] {event.message}"
            state["log_lines"].append(line)
            events.put({"type": "log", "line": line})
            events.put({"type": "status"})

        def runner() -> None:
            try:
                report: AcceptanceReport = service.run_all(keys=items, progress=on_progress)
                events.put({"type": "done", "conclusion": report.conclusion, "run_id": report.run_id})
            except Exception as exc:  # noqa: BLE001 - 执行异常转为 SSE 事件
                state["error"] = str(exc)
                state["log_lines"].append(f"✗ 执行失败：{exc}")
                events.put({"type": "error", "message": str(exc)})

        thread = threading.Thread(target=runner, name="ucm-run", daemon=True)
        state["thread"] = thread
        thread.start()
        return JSONResponse({"started": True, "items": items})

    @app.post("/api/cancel")
    def cancel() -> dict[str, Any]:
        service.cancel()
        return {"canceled": True}

    @app.get("/api/events")
    def events() -> StreamingResponse:
        def stream() -> Iterator[str]:
            q: queue.Queue | None = state.get("events")
            if q is None:
                yield "data: " + json.dumps({"type": "idle"}, ensure_ascii=False) + "\n\n"
                return
            import time as _time

            deadline_idle = 600.0
            last = _time.monotonic()
            while True:
                try:
                    item = q.get(timeout=1.0)
                    last = _time.monotonic()
                except queue.Empty:
                    if not service.running and q.empty():
                        yield "data: " + json.dumps({"type": "status"}) + "\n\n"
                        yield "data: " + json.dumps({"type": "done"}, ensure_ascii=False) + "\n\n"
                        return
                    if _time.monotonic() - last > deadline_idle:
                        return
                    continue
                yield "data: " + json.dumps(item, ensure_ascii=False) + "\n\n"
                if item.get("type") in {"done", "error"}:
                    yield "data: " + json.dumps({"type": "status"}, ensure_ascii=False) + "\n\n"
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                     "Connection": "keep-alive"},
        )

    @app.get("/api/log")
    def run_log(offset: int = 0) -> dict[str, Any]:
        """增量日志：offset 为前端已接收行数，返回之后的行；供前端 1s 轮询（公网代理可能缓冲 SSE）。"""
        lines = state.get("log_lines", [])
        new_lines = lines[offset:] if 0 <= offset < len(lines) else []
        return {"total": len(lines), "lines": new_lines, "running": service.running}

    @app.get("/api/state")
    def state_api() -> dict[str, Any]:
        return _state_payload()

    def _report_paths(run_id: str = "") -> tuple[Path | None, Path | None]:
        """报告 JSON/HTML 路径：优先会话当前报告（与 /api/state 同源），
        其次 ?run_id= 指定的历史 run，最后回退磁盘最新。"""
        report = service.current_report()
        if report is not None and not run_id:
            run_dir = Path(report.output_dir) if report.output_dir else None
            if run_dir and (run_dir / "report.json").exists():
                return run_dir / "report.json", run_dir / "report.html"
        if run_id:
            run_dir = service.outputs_root / "runs" / run_id
            if (run_dir / "report.json").exists():
                return run_dir / "report.json", run_dir / "report.html"
        latest = find_latest_report(service.outputs_root)
        if latest:
            return latest, latest.with_name("report.html")
        return None, None

    @app.get("/api/runs")
    def runs_list() -> list[dict[str, Any]]:
        """列出输出目录中所有已完成验收 run 的摘要（供结果对比选择）。"""
        out = []
        runs_root = service.outputs_root / "runs"
        if runs_root.is_dir():
            for run_dir in sorted(runs_root.iterdir(), reverse=True):
                rj = run_dir / "report.json"
                if not rj.exists():
                    continue
                try:
                    report = AcceptanceReport.load_json(rj)
                except Exception:  # noqa: BLE001 - 损坏报告跳过
                    continue
                items = {
                    key: item.status.value
                    for key, item in report.items.items()
                }
                out.append({
                    "run_id": report.run_id,
                    "conclusion": report.conclusion,
                    "mode": report.mode,
                    "started_at": report.started_at,
                    "items": items,
                })
        return out

    @app.post("/api/compare", response_class=HTMLResponse)
    def compare_page(payload: dict[str, Any] = Body(default={})) -> HTMLResponse:  # noqa: B008
        """多结果对比报告（1~5 个结果，标签着色；支持盘上 run_id 与上传 JSON）。"""
        entries_raw = (payload or {}).get("entries") or []
        entries = []
        for entry in entries_raw[:5]:
            label = str(entry.get("label") or "").strip() or str(entry.get("run_id") or "结果")
            report: AcceptanceReport | None = None
            if entry.get("report_json"):
                try:
                    report = AcceptanceReport.from_dict(entry["report_json"])
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400,
                                        detail=f"{label}：上传的 report.json 无效（{exc}）") from exc
            elif entry.get("run_id"):
                rj = service.outputs_root / "runs" / str(entry["run_id"]) / "report.json"
                if not rj.exists():
                    raise HTTPException(status_code=404, detail=f"{label}：找不到 {entry['run_id']} 的报告")
                try:
                    report = AcceptanceReport.load_json(rj)
                except Exception as exc:  # noqa: BLE001
                    raise HTTPException(status_code=400,
                                        detail=f"{label}：报告读取失败（{exc}）") from exc
            if report is not None:
                entries.append({"label": label, "report": report})
        if not 1 <= len(entries) <= 5:
            raise HTTPException(status_code=400, detail="请选择 1~5 个结果进行对比")
        from .compare import build_compare_html
        return HTMLResponse(build_compare_html(entries))

    @app.get("/api/report", response_class=HTMLResponse)
    def report_html(run_id: str = "") -> HTMLResponse:
        _json_path, html_path = _report_paths(run_id)
        if not html_path or not html_path.exists():
            raise HTTPException(status_code=404, detail="尚未生成报告")
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/api/report/download")
    def report_download(run_id: str = "") -> Response:
        _json_path, html_path = _report_paths(run_id)
        if not html_path or not html_path.exists():
            raise HTTPException(status_code=404, detail="尚未生成报告")
        return Response(
            content=html_path.read_bytes(),
            media_type="text/html",
            headers={"Content-Disposition": f'attachment; filename="report-{html_path.parent.name}.html"'},
        )

    @app.post("/api/config/save")
    def config_save(body: SaveBody) -> dict[str, Any]:
        target = body.path.strip() or "./ucm-accept.user.yaml"
        try:
            saved = service.config.save(target)
        except (OSError, ConfigError) as exc:
            raise HTTPException(status_code=400, detail=f"保存失败：{exc}") from exc
        return {"saved": str(saved)}

    @app.get("/api/report/json")
    def report_json_download(run_id: str = "") -> Response:
        json_path, _html_path = _report_paths(run_id)
        if not json_path or not json_path.exists():
            raise HTTPException(status_code=404, detail="尚未生成报告")
        return Response(
            content=json_path.read_bytes(),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="report-{json_path.parent.name}.json"'},
        )

    @app.get("/api/log/download")
    def log_download() -> Response:
        text = "\n".join(state.get("log_lines", []))
        return Response(
            content=text,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="ucm-run.log"'},
        )

    @app.get("/api/report/bundle")
    def report_bundle(run_id: str = "") -> Response:
        """把当前 run 目录整体打包为 zip：UCM 报告（HTML/JSON）+ EvalScope
        运行时产物（performance_summary.txt / perf_report.html / benchmark_*.json /
        workload_* / command.txt / 各项原始输出）+ run.log。"""
        _json_path, html_path = _report_paths(run_id)
        run_dir = html_path.parent if html_path else None
        if not run_dir or not run_dir.is_dir():
            raise HTTPException(status_code=404, detail="报告目录不存在")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file_path in sorted(run_dir.rglob("*")):
                if file_path.is_file() and not file_path.is_symlink():
                    zf.write(file_path, file_path.relative_to(run_dir.parent))
        filename = f"ucm-accept-run-{run_dir.name}.zip"
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/report/export/pdf")
    def report_pdf(run_id: str = "") -> Response:
        _json_path, html_path = _report_paths(run_id)
        if not html_path or not html_path.exists():
            raise HTTPException(status_code=404, detail="尚未生成报告")
        try:
            from playwright.sync_api import sync_playwright

            pdf_path = html_path.with_suffix(".pdf")
            if not pdf_path.exists():
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(args=["--no-sandbox"])
                    page = browser.new_page()
                    page.goto(html_path.as_uri(), wait_until="load")
                    page.pdf(path=str(pdf_path), format="A4", print_background=True,
                             margin={"top": "12mm", "bottom": "12mm", "left": "10mm", "right": "10mm"})
                    browser.close()
        except Exception as exc:  # noqa: BLE001 - chromium 缺失等环境问题
            raise HTTPException(status_code=500, detail=f"PDF 导出失败：{exc}") from exc
        return Response(
            content=pdf_path.read_bytes(),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="report-{html_path.parent.name}.pdf"'},
        )

    @app.get("/api/config/export")
    def config_export() -> Response:
        content = service.config.to_yaml() if hasattr(service.config, "to_yaml") else None
        if content is None:
            raise HTTPException(status_code=500, detail="导出失败")
        return Response(
            content=content,
            media_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="ucm-accept.export.yaml"'},
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse((_WEB_DIR / "index.html").read_text(encoding="utf-8"))

    return app


def main(
    config: Config | None = None,
    *,
    mode: str = "live",
    mock_profile: str = "nominal",
    mock_delay_ms: int | None = None,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> int:
    import signal
    import socket
    import time as _time
    import webbrowser

    import uvicorn

    from .utils import has_desktop

    config = config or Config.load()
    service = AcceptanceService(config, mode=mode, mock_profile=mock_profile, mock_delay_ms=mock_delay_ms)
    if service.load_latest_report():
        print("已加载最近一次验收报告。")

    def _find_free_port(host_: str) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host_, 0))
            return sock.getsockname()[1]

    chosen_port = port or _find_free_port(host)
    app = create_app(service)

    def _graceful(signum: int, _frame: Any) -> None:
        print("\n收到退出信号，正在停止当前验收任务并保存报告…")
        service.shutdown(timeout=10.0)
        signal.signal(signum, signal.SIG_DFL)
        import os

        os.kill(os.getpid(), signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _graceful)
        except (ValueError, OSError):  # noqa: BLE001
            pass

    url = f"http://{host}:{chosen_port}"
    print(f"UCM 验收工具已启动：{url}")
    if open_browser and has_desktop():
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=chosen_port, log_level="warning", access_log=False)
    )
    try:
        server.run()
    except KeyboardInterrupt:
        service.shutdown(timeout=10.0)
        return 0
    _time.sleep(0.1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
