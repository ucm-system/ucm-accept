from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import AccuracyRun, AgentDetail, MultiturnDetail, PerfDetail, SlaDetail

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


@dataclass
class Endpoint:
    base_url: str
    model: str
    api_key: str = ""
    timeout_s: float = 180.0
    connect_timeout_s: float = 8.0
    verify_ssl: bool = False
    probe_mode: str = "auto"

    @property
    def normalized(self) -> str:
        return self.base_url.rstrip("/")

    @property
    def models_url(self) -> str:
        base = self.normalized
        if base.endswith("/chat/completions"):
            base = base[: -len("/chat/completions")]
        elif base.endswith("/completions"):
            base = base[: -len("/completions")]
        if not base.endswith("/v1") and "/v1/" not in base:
            # 兼容少数只暴露 /models 的网关
            return base + "/models"
        return base + "/models"

    @property
    def chat_url(self) -> str:
        base = self.normalized
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return base + "/chat/completions"
        return base + "/chat/completions"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key": "***" if self.api_key else "",
            "timeout_s": self.timeout_s,
        }


@dataclass
class ProbeResult:
    connected: bool
    message: str
    latency_ms: float | None = None
    models: list[str] = field(default_factory=list)
    context_window: int | None = None
    detail: str = ""


@dataclass
class PerfParams:
    parallel: list[int]
    number: int
    min_prompt_length: int
    max_prompt_length: int
    min_tokens: int
    max_tokens: int
    prefix_length: int = 0
    prefix_count: int = 0
    tokenizer_path: str = ""
    warmup_num: int = 0
    request_timeout_s: int = 21600
    dataset: str = "random"
    extra_args: list[str] = field(default_factory=list)
    dataset_args: dict[str, Any] = field(default_factory=dict)
    context_length: int = 65536


@dataclass
class MultiturnParams:
    scenario_key: str
    scenario_label: str
    # dataset_name：EvalScope 内置多轮场景名（share_gpt_*_multi_turn / swe_smith / trie_*）；
    # dataset_path：自定义 jsonl（custom_multi_turn）。二选一，name 优先。
    dataset_name: str
    dataset_path: Path | None
    parallel: int
    conversations: int
    min_turns: int
    max_turns: int
    min_tokens: int
    max_tokens: int
    tokenizer_path: str = ""
    request_timeout_s: int = 180
    extra_args: list[str] = field(default_factory=list)


@dataclass
class AccuracyParams:
    key: str
    label: str
    dataset: str
    subset: str
    limit: int
    run_index: int
    dataset_dir: str = ""
    request_timeout_s: int = 14400
    extra_args: list[str] = field(default_factory=list)
    dataset_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentParams:
    target_context: int
    context_detected: int | None
    parallel: int
    duration_s: int
    degraded: bool
    degraded_mode: str = "smoke"
    degraded_parallel: int = 1
    scenario_name: str = "agentx"
    tokenizer_path: str = ""
    extra_args: list[str] = field(default_factory=list)


@dataclass
class SlaNativeParams:
    target_context: int
    upper: int
    lower: int
    number: int
    num_runs: int
    number_multiplier: float
    # 压测口径与达标阈值（avg/p99 任意档位组合；ttft 单位秒，tpot 单位毫秒）
    output_tokens: int = 512
    prefix_ratio: float = 0.9
    prefix_count: int = 8
    thresholds: dict[str, float] = field(default_factory=dict)
    tokenizer_path: str = ""
    variable: str = "parallel"
    extra_args: list[str] = field(default_factory=list)


class Engine(ABC):
    """测试引擎接口。真实实现调用 EvalScope；Mock 实现用于演示 / CI 虚跑。"""

    mode: str = "live"
    name: str = "engine"

    @abstractmethod
    def probe(self, endpoint: Endpoint) -> ProbeResult: ...

    @abstractmethod
    def detect_context(self, endpoint: Endpoint) -> int | None: ...

    @abstractmethod
    def run_perf(
        self,
        endpoint: Endpoint,
        params: PerfParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> PerfDetail: ...

    @abstractmethod
    def run_multiturn(
        self,
        endpoint: Endpoint,
        params: MultiturnParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> MultiturnDetail: ...

    @abstractmethod
    def run_accuracy(
        self,
        endpoint: Endpoint,
        params: AccuracyParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> AccuracyRun: ...

    @abstractmethod
    def run_agent(
        self,
        endpoint: Endpoint,
        params: AgentParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> AgentDetail: ...

    @abstractmethod
    def run_sla_native(
        self,
        endpoint: Endpoint,
        params: SlaNativeParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> SlaDetail: ...

    def close(self) -> None:
        return None
