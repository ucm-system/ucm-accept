from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .config import Config
from .engine.base import Endpoint, ProbeResult
from .engine.chat import ChatClient
from .engine.evalscope_engine import EvalScopeEngine
from .engine.mock import MockEngine
from .errors import UCMAcceptError
from .events import ProgressCallback
from .models import AcceptanceReport, ItemResult
from .orchestrator import Orchestrator
from .report import export_pdf
from .utils import find_latest_report


class AcceptanceService:
    """GUI / CLI 共用的会话式验收服务。

    - 同一会话内多次单项运行会累积到同一份报告；
    - 修改服务地址/高级参数后自动开启新的一份报告；
    - 同一时刻只允许一个验收任务。
    """

    def __init__(
        self,
        config: Config,
        *,
        mode: str = "live",
        mock_profile: str = "nominal",
        mock_delay_ms: int | None = None,
        outputs_root: str | Path | None = None,
    ):
        self.config = config
        self.mode = mode if mode in {"live", "mock"} else "live"
        self.mock_profile = mock_profile
        self.mock_delay_ms = (
            int(config.get("app.mock.delay_ms", 180)) if mock_delay_ms is None else int(mock_delay_ms)
        )
        self.outputs_root = Path(outputs_root) if outputs_root else config.report_dir
        self.outputs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._orchestrator: Orchestrator | None = None
        self._engine = None
        self._config_fingerprint = self._fingerprint()
        self._running = False
        self._last_probe: ProbeResult | None = None

    # ---------------------------------------------------------------- session
    def _fingerprint(self) -> str:
        endpoint = self.config.get("endpoint", {}) or {}
        relevant = {
            "base_url": endpoint.get("base_url"),
            "model": endpoint.get("model"),
            "api_key": endpoint.get("api_key"),
            "outputs": str(self.outputs_root),
            "perf": self.config.get("perf", {}),
            "sla": self.config.get("sla", {}),
            "accuracy": self.config.get("accuracy", {}),
            "agent": self.config.get("agent", {}),
            "multiturn": self.config.get("multiturn", {}),
        }
        return repr(relevant)

    def _reset_if_config_changed(self) -> None:
        fingerprint = self._fingerprint()
        if fingerprint != self._config_fingerprint:
            self._config_fingerprint = fingerprint
            self._orchestrator = None
            self._engine = None

    def _build_engine(self):
        if self.mode == "mock":
            return MockEngine(profile=self.mock_profile, delay_ms=self.mock_delay_ms)
        return EvalScopeEngine(self.config)

    def _endpoint(self) -> Endpoint:
        return Endpoint(
            base_url=str(self.config.get("endpoint.base_url")),
            model=str(self.config.get("endpoint.model")),
            api_key=str(self.config.get("endpoint.api_key", "") or ""),
            timeout_s=float(self.config.get("endpoint.timeout_s", 180)),
            connect_timeout_s=float(self.config.get("endpoint.connect_timeout_s", 8)),
            verify_ssl=bool(self.config.get("endpoint.verify_ssl", False)),
            probe_mode=str(self.config.get("endpoint.probe_mode", "auto")),
        )

    def _ensure_orchestrator(self, progress: ProgressCallback | None) -> Orchestrator:
        self._reset_if_config_changed()
        if self._orchestrator is None:
            self._engine = self._build_engine()
            self._orchestrator = Orchestrator(
                self.config,
                self._engine,
                outputs_root=self.outputs_root,
                progress=progress,
            )
        else:
            self._orchestrator.progress = progress
        return self._orchestrator

    # ------------------------------------------------------------- operations
    def check_connection(self) -> ProbeResult:
        if self.mode == "mock":
            result = MockEngine(
                profile=self.mock_profile, delay_ms=0
            ).probe(self._endpoint())
        else:
            result = ChatClient(self._endpoint()).probe()
        self._last_probe = result
        return result

    @property
    def last_probe(self) -> ProbeResult | None:
        return self._last_probe

    def run_item(
        self,
        key: str,
        *,
        scenario: str | None = None,
        progress: ProgressCallback | None = None,
    ) -> ItemResult:
        if not self._lock.acquire(blocking=False):
            raise UCMAcceptError("已有验收任务正在执行，请等待完成")
        self._running = True
        try:
            # 每次执行都使用全新的 run（新 run_id/目录/报告），避免多次执行互相覆盖
            self._orchestrator = None
            self._engine = None
            orchestrator = self._ensure_orchestrator(progress)
            return orchestrator.run_item(key, scenario=scenario)
        finally:
            self._running = False
            self._lock.release()

    def run_all(
        self,
        progress: ProgressCallback | None = None,
        keys: list[str] | None = None,
    ) -> AcceptanceReport:
        """执行验收；keys 为空时使用配置 run.items 声明的测试范围。"""
        if not self._lock.acquire(blocking=False):
            raise UCMAcceptError("已有验收任务正在执行，请等待完成")
        self._running = True
        try:
            # 每次执行都使用全新的 run（新 run_id/目录/报告），避免多次执行互相覆盖
            self._orchestrator = None
            self._engine = None
            orchestrator = self._ensure_orchestrator(progress)
            return orchestrator.run_all(keys)
        finally:
            self._running = False
            self._lock.release()

    def cancel(self) -> None:
        """请求停止当前验收任务（置位取消事件，由执行线程终止子进程并落盘）。"""
        if self._orchestrator is not None:
            self._orchestrator.cancel()

    def shutdown(self, timeout: float = 10.0) -> None:
        """请求停止并等待当前任务收尾（报告落盘），供进程退出前的信号处理调用。"""
        if not self._running:
            return
        self.cancel()
        deadline = time.monotonic() + max(0.0, timeout)
        while self._running and time.monotonic() < deadline:
            time.sleep(0.1)

    def load_latest_report(self) -> bool:
        """从输出目录加载最近一次报告（页面重开 / 进程重启后恢复现场）。

        成功返回 True；无报告、读取失败或任务执行中返回 False。
        """
        if self._running:
            return False
        path = find_latest_report(self.outputs_root)
        if not path:
            return False
        try:
            report = AcceptanceReport.load_json(path)
        except Exception:  # noqa: BLE001 - 报告损坏时静默回退到空现场
            return False
        orchestrator = self._ensure_orchestrator(None)
        orchestrator.report = report
        return True

    def reset(self) -> None:
        if self._running:
            raise UCMAcceptError("任务执行中，无法重置")
        self._orchestrator = None
        self._engine = None

    def update_endpoint(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        if self._running:
            raise UCMAcceptError("任务执行中，无法修改服务配置")
        self.config.apply_endpoint(base_url, model, api_key)
        self._reset_if_config_changed()

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        if self._running:
            raise UCMAcceptError("任务执行中，无法修改高级参数")
        if overrides:
            merged = self.config.with_overrides(overrides)
            self.config = merged
            self._reset_if_config_changed()

    def replace_config(self, config: Config) -> None:
        """整体替换会话配置（GUI 导入配置文件 / 恢复默认值时使用）。"""
        if self._running:
            raise UCMAcceptError("任务执行中，无法替换配置")
        self.config = config
        self._reset_if_config_changed()

    # ------------------------------------------------------------------ state
    @property
    def running(self) -> bool:
        return self._running

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    def current_report(self) -> AcceptanceReport | None:
        if self._orchestrator is None:
            return None
        return self._orchestrator.current_report()

    def current_report_html(self) -> str:
        if self._orchestrator is None:
            return ""
        path = self._orchestrator.run_dir / "report.html"
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def report_path(self) -> Path | None:
        if self._orchestrator is None:
            return None
        path = self._orchestrator.run_dir / "report.html"
        return path if path.exists() else None

    def report_json_path(self) -> Path | None:
        if self._orchestrator is None:
            return None
        path = self._orchestrator.run_dir / "report.json"
        return path if path.exists() else None

    def latest_report(self) -> Path | None:
        return find_latest_report(self.outputs_root)

    def export_pdf(self) -> Path:
        path = self.report_path()
        if path is None:
            path = self.latest_report()
        if path is None:
            raise UCMAcceptError("暂无可用报告，请先执行验收测试")
        return export_pdf(path)
