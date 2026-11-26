from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class Status(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    WARNING = "warning"
    FAILED = "failed"
    SKIPPED = "skipped"


STATUS_LABELS: dict[Status, str] = {
    Status.PENDING: "待执行",
    Status.RUNNING: "进行中",
    Status.PASSED: "通过",
    Status.WARNING: "关注",
    Status.FAILED: "未通过",
    Status.SKIPPED: "跳过",
}

STATUS_MARKS: dict[Status, str] = {
    Status.PENDING: "○",
    Status.RUNNING: "◐",
    Status.PASSED: "✓",
    Status.WARNING: "!",
    Status.FAILED: "✗",
    Status.SKIPPED: "–",
}

STATUS_COLORS: dict[Status, str] = {
    Status.PENDING: "#64748b",
    Status.RUNNING: "#2563eb",
    Status.PASSED: "#15803d",
    Status.WARNING: "#b45309",
    Status.FAILED: "#b91c1c",
    Status.SKIPPED: "#6b7280",
}


@dataclass(frozen=True)
class TestItemMeta:
    key: str
    name: str
    short: str
    description: str
    eta: str
    icon: str
    order: int


TEST_ITEMS: tuple[TestItemMeta, ...] = (
    TestItemMeta(
        key="perf",
        name="基础性能测试",
        short="性能",
        description="多组配置实测吞吐、TTFT、TPOT 与总延迟（并发/请求数/输入/输出/命中率/前缀种数均可配置），逐组执行并绘制并发趋势。",
        eta="约 5 分钟",
        icon="⚡",
        order=1,
    ),
    TestItemMeta(
        key="multiturn",
        name="多轮对话测试",
        short="多轮对话",
        description="由 EvalScope 多轮压测执行内置数据集（ShareGPT / SWE-smith / trie 轨迹等，可多选），统计完成率、延迟与缓存命中。",
        eta="约 10 分钟",
        icon="💬",
        order=2,
    ),
    TestItemMeta(
        key="sla",
        name="SLA 自动调优",
        short="SLA",
        description="EvalScope --sla-auto-tune 按 avg / P99 阈值自动搜索满足 SLA 的最高并发；判定完全由 EvalScope 原生完成。",
        eta="约 8 分钟",
        icon="🎯",
        order=3,
    ),
    TestItemMeta(
        key="accuracy",
        name="精度测试",
        short="精度",
        description="GPQA、GSM8K、LongBench-v2、BFCL-v3 各跑两轮，核验 KVCache 落盘一致性；BFCL 需服务启用工具调用（vLLM：--enable-auto-tool-choice --tool-call-parser hermes）。",
        eta="约 20 分钟",
        icon="📊",
        order=4,
    ),
    TestItemMeta(
        key="agent",
        name="Agent 场景测试",
        short="Agent",
        description="EvalScope AgentX 长会话服务级压测（数据集支持 256K / full 变体）。要求模型服务上下文不低于数据集所需；不足时自动降级为 smoke 模式跑通完整流程并在报告中标注（指标不作为验收结论）。",
        eta="约 15 分钟",
        icon="🤖",
        order=5,
    ),
)

ITEM_BY_KEY = {item.key: item for item in TEST_ITEMS}
ITEM_KEYS = tuple(item.key for item in TEST_ITEMS)


def utc_now() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def iso_now() -> str:
    return utc_now().isoformat(timespec="seconds")


def human_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def percent(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def number(value: float | None, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


@dataclass
class PerfPoint:
    concurrency: int
    requests: int = 0
    success_requests: int = 0
    throughput_tok_s: float = 0.0
    rps: float = 0.0
    ttft_avg_ms: float = 0.0
    ttft_p90_ms: float = 0.0
    ttft_p99_ms: float = 0.0
    tpot_avg_ms: float = 0.0
    tpot_p90_ms: float = 0.0
    tpot_p99_ms: float = 0.0
    latency_avg_s: float = 0.0
    latency_p90_s: float = 0.0
    latency_p99_s: float = 0.0
    input_tokens: float = 0.0
    output_tokens: float = 0.0
    raw_dir: str = ""

    @property
    def success_rate(self) -> float:
        if not self.requests:
            return 1.0
        return self.success_requests / self.requests



    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class PerfDetail:
    context_length: int
    points: list[PerfPoint] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    output_dir: str = ""

    @property
    def primary(self) -> PerfPoint | None:
        if not self.points:
            return None
        # 默认展示“验收并发点”（最大并发点）的数据
        return max(self.points, key=lambda point: point.concurrency)

    @property
    def all_successful(self) -> bool:
        return all(point.success_rate > 0 for point in self.points)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class SlaTarget:
    input_length: int
    output_tokens: int
    prefix_ratio: float
    prefix_count: int
    concurrency: int
    thresholds: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class SlaDetail:
    target: SlaTarget
    strategy: str = "search"
    target_met: bool = False
    satisfiable: bool = False
    recommended_concurrency: int | None = None
    probes: list[PerfPoint] = field(default_factory=list)
    best: PerfPoint | None = None
    gaps: list[str] = field(default_factory=list)
    note: str = ""
    output_dir: str = ""

    @property
    def status(self) -> Status:
        if self.target_met:
            return Status.PASSED
        if self.satisfiable:
            return Status.WARNING
        return Status.FAILED

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class MultiturnDetail:
    scenario: str
    label: str
    parallel: int = 0
    conversations_requested: int = 0
    total_turns: int = 0
    success_turns: int = 0
    avg_latency_ms: float = 0.0
    cache_hit_rate: float | None = None
    load_total_conversations: int = 0
    load_success_conversations: int = 0
    output_dir: str = ""
    note: str = ""

    @property
    def completion_rate(self) -> float:
        if self.load_total_conversations:
            return self.load_success_conversations / self.load_total_conversations
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AccuracyRun:
    index: int
    score: float | None = None
    duration_s: float = 0.0
    raw_dir: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class AccuracyDataset:
    key: str
    label: str
    dataset: str
    subset: str = ""
    runs: list[AccuracyRun] = field(default_factory=list)
    mean: float | None = None
    std: float | None = None
    delta: float | None = None
    stable: bool = True
    description: str = ""

    def recompute(self) -> None:
        scores = [run.score for run in self.runs if run.score is not None]
        if not scores:
            self.mean = self.std = self.delta = None
            self.stable = False
            return
        self.mean = sum(scores) / len(scores)
        if len(scores) > 1:
            variance = sum((score - self.mean) ** 2 for score in scores) / len(scores)
            self.std = variance**0.5
            self.delta = max(scores) - min(scores)
        else:
            self.std = 0.0
            self.delta = 0.0

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["mean"] = self.mean
        data["std"] = self.std
        data["delta"] = self.delta
        data["stable"] = self.stable
        return data


@dataclass
class AccuracyDetail:
    datasets: list[AccuracyDataset] = field(default_factory=list)
    runs_per_dataset: int = 2
    stability_threshold: float = 0.01
    all_stable: bool = True
    total_runs: int = 0
    note: str = ""
    commands: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["all_stable"] = self.all_stable
        return data


@dataclass
class AgentDetail:
    dataset_name: str = "AgentX-claudecode"
    context_required: int = 262144
    context_detected: int | None = None
    degraded: bool = False
    completion_rate: float | None = None
    avg_steps: float | None = None
    sessions: int = 0
    requests: int = 0
    source: str = "evalscope-agentx"
    submission_valid: bool | None = None
    notes: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    output_dir: str = ""

    @property
    def context_label(self) -> str:
        if self.context_detected is None:
            return "未知"
        if self.context_detected >= 1024:
            return f"{self.context_detected / 1024:.0f}K"
        return str(self.context_detected)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass
class ItemResult:
    key: str
    name: str
    status: Status = Status.PENDING
    started_at: str = ""
    finished_at: str = ""
    duration_s: float = 0.0
    summary: str = ""
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)

    @property
    def meta(self) -> TestItemMeta:
        return ITEM_BY_KEY[self.key]

    def to_dict(self) -> dict[str, Any]:
        data = to_jsonable(self)
        data["status"] = self.status.value
        data["status_label"] = STATUS_LABELS[self.status]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ItemResult:
        payload = dict(data)
        payload["status"] = Status(payload.get("status", "pending"))
        return cls(**{key: value for key, value in payload.items() if key in cls.__dataclass_fields__})


@dataclass
class AcceptanceReport:
    run_id: str
    mode: str
    endpoint: dict[str, Any]
    started_at: str
    finished_at: str = ""
    items: dict[str, ItemResult] = field(default_factory=dict)
    selected: list[str] = field(default_factory=lambda: list(ITEM_KEYS))
    conclusion: str = "未完成"
    reasons: list[str] = field(default_factory=list)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    output_dir: str = ""
    version: str = "1.2.0"
    title: str = "UCM 推理加速产品验收报告"

    def ordered_items(self) -> list[ItemResult]:
        return sorted(self.items.values(), key=lambda item: ITEM_BY_KEY[item.key].order)

    def selected_keys(self) -> list[str]:
        """本次验收声明的测试范围；与 items 的交集参与结论计算。"""
        keys = [key for key in (self.selected or list(ITEM_KEYS)) if key in ITEM_BY_KEY]
        return keys or list(ITEM_KEYS)

    def compute_conclusion(self) -> tuple[str, list[str]]:
        scope = self.selected_keys()
        reasons: list[str] = []
        missing = [ITEM_BY_KEY[key].name for key in scope if key not in self.items]
        if missing:
            return "未完成", [f"尚未执行：{'、'.join(missing)}"]
        incomplete = [
            self.items[key]
            for key in scope
            if self.items[key].status in {Status.PENDING, Status.RUNNING, Status.SKIPPED}
        ]
        if incomplete:
            reasons.extend(f"{item.name}未完成：{item.summary or '已取消或尚未执行'}" for item in incomplete)
            return "未完成", reasons
        failed = [self.items[key] for key in scope if self.items[key].status == Status.FAILED]
        warnings = [self.items[key] for key in scope if self.items[key].status == Status.WARNING]
        if failed:
            reasons.extend(f"{item.name}未通过：{item.summary or item.error}" for item in failed)
            return "不通过", reasons
        if warnings:
            reasons.extend(f"{item.name}需关注：{item.summary}" for item in warnings)
            return "有条件通过", reasons
        scope_note = "五项验收测试" if len(scope) == len(ITEM_KEYS) else f"所选 {len(scope)} 项测试"
        reasons.append(f"{scope_note}均通过，满足本次验收口径。")
        return "通过", reasons

    def finalize(self) -> None:
        self.conclusion, self.reasons = self.compute_conclusion()
        self.finished_at = iso_now()

    @property
    def conclusion_color(self) -> str:
        return {
            "通过": "#15803d",
            "有条件通过": "#b45309",
            "不通过": "#b91c1c",
            "未完成": "#475569",
        }.get(self.conclusion, "#475569")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "version": self.version,
            "run_id": self.run_id,
            "title": self.title,
            "mode": self.mode,
            "endpoint": self.endpoint,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "conclusion": self.conclusion,
            "reasons": list(self.reasons),
            "items": {key: item.to_dict() for key, item in self.items.items()},
            "selected": self.selected_keys(),
            "config_snapshot": self.config_snapshot,
            "output_dir": self.output_dir,
        }

    def save_json(self, path: str | Path) -> Path:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    @classmethod
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AcceptanceReport:
        """从 report.json 反序列化后的字典构建报告对象（上传对比用）。"""
        items = {
            key: ItemResult.from_dict(value)
            for key, value in (data.get("items") or {}).items()
        }
        return cls(
            run_id=data.get("run_id", ""),
            mode=data.get("mode", "live"),
            endpoint=data.get("endpoint", {}),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            items=items,
            selected=[str(key) for key in (data.get("selected") or [])],
            conclusion=data.get("conclusion", "未完成"),
            reasons=list(data.get("reasons") or []),
            config_snapshot=data.get("config_snapshot") or {},
            output_dir=data.get("output_dir", ""),
            title=data.get("title", "UCM 推理加速产品验收报告"),
        )

    @classmethod
    def load_json(cls, path: str | Path) -> AcceptanceReport:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        items = {
            key: ItemResult.from_dict(value)
            for key, value in (data.get("items") or {}).items()
        }
        return cls(
            run_id=data.get("run_id", ""),
            mode=data.get("mode", "live"),
            endpoint=data.get("endpoint", {}),
            started_at=data.get("started_at", ""),
            finished_at=data.get("finished_at", ""),
            items=items,
            selected=[str(key) for key in (data.get("selected") or [])],
            conclusion=data.get("conclusion", "未完成"),
            reasons=list(data.get("reasons") or []),
            config_snapshot=data.get("config_snapshot") or {},
            output_dir=data.get("output_dir", ""),
            version=data.get("version", "1.2.0"),
            title=data.get("title", "UCM 推理加速产品验收报告"),
        )


def to_jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {key: to_jsonable(val) for key, val in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value
