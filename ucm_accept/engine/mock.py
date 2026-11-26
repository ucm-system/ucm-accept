from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

from ..errors import CancelledError
from ..models import (
    AccuracyRun,
    AgentDetail,
    MultiturnDetail,
    PerfDetail,
    PerfPoint,
    SlaDetail,
    SlaTarget,
)
from .base import (
    AccuracyParams,
    AgentParams,
    Endpoint,
    Engine,
    LogFn,
    MultiturnParams,
    PerfParams,
    ProbeResult,
    SlaNativeParams,
)


class MockEngine(Engine):
    """确定性模拟引擎：用于演示、GUI 走查、CI 虚跑与离线自检。

    不产生任何真实请求。所有指标均为构造数据，报告中会醒目标注“模拟”。
    """

    mode = "mock"
    name = "mock"

    def __init__(
        self,
        *,
        profile: str = "nominal",
        delay_ms: int = 180,
        context_window: int = 262144,
    ):
        self.profile = profile if profile in {"nominal", "degraded"} else "nominal"
        self.delay_ms = max(0, int(delay_ms))
        self.context_window = context_window

    # ----------------------------------------------------------------- utils
    def _sleep(self, cancel: threading.Event | None, factor: float = 1.0) -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError("用户已取消")
        delay = self.delay_ms / 1000.0 * factor
        if delay <= 0:
            return
        if cancel is not None:
            if cancel.wait(delay):
                raise CancelledError("用户已取消")
        else:
            time.sleep(delay)

    def _write_command(self, output_dir: Path, description: str) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "command.txt").write_text(
            f"模拟模式：未调用 EvalScope。\n{description}\n", encoding="utf-8"
        )

    def _jitter(self, key: str, spread: float = 0.012) -> float:
        seed = f"{self.profile}:{key}"
        rng = random.Random(seed)
        return 1.0 + rng.uniform(-spread, spread)

    def _scale(self, prompt_length: int) -> float:
        return max(0.25, min(2.0, prompt_length / 65536.0))

    # ----------------------------------------------------------------- point
    def _point(self, concurrency: int, params: PerfParams) -> PerfPoint:
        factor = self._scale(params.max_prompt_length)
        if self.profile == "nominal":
            ttft_p90 = (2800 + 27.0 * concurrency) * factor
            tpot_p90 = 18.0 + 0.115 * concurrency
            success = params.number
        else:
            ttft_p90 = (4200 + 62.0 * concurrency) * factor
            tpot_p90 = 70.0 + 0.30 * concurrency
            success = params.number
        jitter = self._jitter(f"perf:{concurrency}", 0.008)
        ttft_p90 *= jitter
        tpot_p90 *= self._jitter(f"tpot:{concurrency}", 0.01)
        ttft_avg = ttft_p90 * 0.72
        tpot_avg = tpot_p90 * 0.81
        output_tokens = params.max_tokens
        latency_avg = ttft_avg / 1000 + tpot_avg / 1000 * output_tokens
        latency_p90 = ttft_p90 / 1000 + tpot_p90 / 1000 * output_tokens
        throughput = concurrency / max(latency_p90, 0.01) * output_tokens
        rps = concurrency / max(latency_avg, 0.01)
        return PerfPoint(
            concurrency=concurrency,
            requests=params.number,
            success_requests=success,
            throughput_tok_s=round(throughput, 2),
            rps=round(rps, 2),
            ttft_avg_ms=round(ttft_avg, 1),
            ttft_p90_ms=round(ttft_p90, 1),
            ttft_p99_ms=round(ttft_p90 * 1.17, 1),
            tpot_avg_ms=round(tpot_avg, 2),
            tpot_p90_ms=round(tpot_p90, 2),
            tpot_p99_ms=round(tpot_p90 * 1.14, 2),
            latency_avg_s=round(latency_avg, 3),
            latency_p90_s=round(latency_p90, 3),
            latency_p99_s=round(ttft_p90 * 1.17 / 1000 + tpot_p90 * 1.14 / 1000 * output_tokens, 3),
            input_tokens=float(params.max_prompt_length),
            output_tokens=float(output_tokens),
            raw_dir=f"mock/perf/parallel_{concurrency}",
        )

    # ----------------------------------------------------------------- probe
    def probe(self, endpoint: Endpoint) -> ProbeResult:
        self._sleep(None, 0.2)
        return ProbeResult(
            connected=True,
            message=f"模拟引擎已就绪（{endpoint.model}，不会产生真实请求）",
            latency_ms=12.4,
            models=[endpoint.model],
            context_window=self.context_window,
            detail="mock",
        )

    def detect_context(self, endpoint: Endpoint) -> int | None:
        return self.context_window

    # ------------------------------------------------------------------ perf
    def run_perf(
        self,
        endpoint: Endpoint,
        params: PerfParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> PerfDetail:
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_command(
            output_dir,
            f"evalscope perf --model {endpoint.model} --parallel {' '.join(map(str, params.parallel))} "
            f"--number {params.number}（演示数据）",
        )
        points: list[PerfPoint] = []
        for concurrency in params.parallel:
            if cancel is not None and cancel.is_set():
                raise CancelledError("用户已取消")
            if log:
                log(
                    f"[模拟] 压测 {params.max_prompt_length} 输入 / {params.max_tokens} 输出，"
                    f"并发 {concurrency}，请求 {params.number}"
                )
            self._sleep(cancel, 1.0)
            point = self._point(concurrency, params)
            points.append(point)
            if log:
                log(
                    f"[模拟] 并发 {concurrency}: TTFT.P90 {point.ttft_p90_ms / 1000:.2f}s，"
                    f"TPOT.P90 {point.tpot_p90_ms:.1f}ms，吞吐 {point.throughput_tok_s:.0f} tok/s"
                )
            payload = {
                "mode": "mock",
                "profile": self.profile,
                "concurrency": concurrency,
                "number": params.number,
                "point": point.to_dict(),
            }
            (output_dir / f"parallel_{concurrency}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        detail = PerfDetail(
            context_length=params.max_prompt_length,
            points=points,
            config={
                "parallel": params.parallel,
                "number": params.number,
                "min_prompt_length": params.min_prompt_length,
                "max_prompt_length": params.max_prompt_length,
                "max_tokens": params.max_tokens,
                "prefix_length": params.prefix_length,
                "source": "mock",
            },
            output_dir=str(output_dir),
        )
        (output_dir / "perf_summary.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail

    # ------------------------------------------------------------- multiturn
    def run_multiturn(
        self,
        endpoint: Endpoint,
        params: MultiturnParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> MultiturnDetail:
        output_dir.mkdir(parents=True, exist_ok=True)
        if params.dataset_name:
            pass  # 内置场景名由 EvalScope 解析
        elif params.dataset_path is None or not params.dataset_path.exists():
            raise FileNotFoundError(f"多轮对话预设数据不存在：{params.dataset_path}")
        self._write_command(
            output_dir,
            f"evalscope perf --multi-turn --dataset custom_multi_turn --number {params.conversations}（演示数据）",
        )
        if log:
            log(f"[模拟] 调用 EvalScope 多轮压测：{params.scenario_label}，{params.conversations} 组会话")
        self._sleep(cancel, 1.0)
        turns_per_request = 3.2
        total_turns = int(round(params.conversations * turns_per_request))
        detail = MultiturnDetail(
            scenario=params.scenario_key,
            label=params.scenario_label,
            parallel=params.parallel,
            conversations_requested=params.conversations,
            total_turns=total_turns,
            success_turns=total_turns,
            avg_latency_ms=1260.0,
            cache_hit_rate=0.58,
            load_total_conversations=params.conversations,
            load_success_conversations=params.conversations,
            output_dir=str(output_dir),
            note="模拟数据；实机模式由 EvalScope `perf --multi-turn` 直接产出。",
        )
        (output_dir / "ucm_multiturn.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail

    # -------------------------------------------------------------- accuracy
    def run_accuracy(
        self,
        endpoint: Endpoint,
        params: AccuracyParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> AccuracyRun:
        base_scores = {
            "gpqa": 0.512,
            "gsm8k": 0.864,
            "longbench_v2": 0.481,
            "bfcl_v3": 0.782,
        }
        base = base_scores.get(params.key, 0.65)
        delta = 0.0
        if self.profile == "degraded" and params.key == "gpqa" and params.run_index == 2:
            delta = 0.028
        variation = (params.run_index - 1) * 0.003 if params.run_index > 1 else 0.0
        jitter = (self._jitter(f"acc:{params.key}:{params.run_index}", 0.002) - 1.0)
        score = max(0.0, min(1.0, base + variation + delta + jitter))
        if log:
            log(f"[模拟] {params.label} 第 {params.run_index} 遍：得分 {score:.4f}")
        self._sleep(cancel, 0.35)
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_command(
            output_dir,
            f"evalscope eval --datasets {params.dataset} --limit {params.limit}（演示数据）",
        )
        run_file = output_dir / f"{params.key}_run{params.run_index}.json"
        run_file.write_text(
            json.dumps(
                {
                    "mode": "mock",
                    "dataset": params.dataset,
                    "subset": params.subset,
                    "run": params.run_index,
                    "score": round(score, 6),
                    "limit": params.limit,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return AccuracyRun(
            index=params.run_index,
            score=round(score, 6),
            duration_s=round(self.delay_ms * 0.35 / 1000 + 0.8, 2),
            raw_dir=str(output_dir),
        )

    # ----------------------------------------------------------------- agent
    def run_agent(
        self,
        endpoint: Endpoint,
        params: AgentParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> AgentDetail:
        self._sleep(cancel, 1.2)
        degraded = params.degraded
        notes = [
            "模拟数据；实机模式由 EvalScope `perf --scenario agentx` 产出服务级长会话压测结果。",
            "AgentX 不提供任务质量分；本工具不自行计算任务完成率/工具成功率。",
        ]
        if degraded:
            notes.append(
                f"可用上下文 {params.context_detected or 0} < {params.target_context}，"
                "已切换 smoke 模式；smoke 结果不可与标准 256K 结果直接比较。"
            )
        completion = 0.992 if not degraded else 1.0
        sessions = 4 if not degraded else 1
        requests = 58 if not degraded else 12
        metrics = {
            "sessions": sessions,
            "requests": requests,
            "duration_s": params.duration_s,
            "scenario": params.scenario_name,
            "mode": params.degraded_mode if degraded else "standard",
            "submission_valid": not degraded,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_command(
            output_dir,
            f"evalscope perf --scenario {'smoke' if degraded else 'agentx'} --parallel "
            f"{params.degraded_parallel if degraded else params.parallel}（演示数据）",
        )
        (output_dir / "agentx_summary.json").write_text(
            json.dumps(
                {
                    "mode": "mock",
                    "degraded": degraded,
                    "success_rate": completion,
                    "metrics": metrics,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        if log:
            log(f"[模拟] AgentX 服务级成功率 {completion:.1%}，{sessions} 个会话 / {requests} 次请求")
        return AgentDetail(
            dataset_name="AgentX-claudecode",
            context_required=params.target_context,
            context_detected=params.context_detected or self.context_window,
            degraded=degraded,
            completion_rate=completion,
            avg_steps=round(requests / sessions, 1) if sessions else None,
            sessions=sessions,
            requests=requests,
            source="mock",
            submission_valid=not degraded,
            notes=notes,
            metrics=metrics,
            output_dir=str(output_dir),
        )

    # ------------------------------------------------------------ SLA native
    def run_sla_native(
        self,
        endpoint: Endpoint,
        params: SlaNativeParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> SlaDetail:
        probe = PerfParams(
            parallel=[params.lower, params.upper],
            number=params.number,
            min_prompt_length=params.target_context,
            max_prompt_length=params.target_context,
            min_tokens=params.output_tokens,
            max_tokens=params.output_tokens,
            prefix_length=round(params.target_context * max(0.0, min(1.0, params.prefix_ratio))),
            prefix_count=params.prefix_count,
            context_length=params.target_context,
        )
        candidates = sorted({params.lower, max(params.lower, 100), max(params.lower, 150), params.upper})
        points = [self._point(concurrency, probe) for concurrency in candidates]
        output_dir.mkdir(parents=True, exist_ok=True)
        self._write_command(output_dir, "evalscope perf --sla-auto-tune（演示数据）")
        target_point = next(
            (point for point in points if point.concurrency == params.upper),
            max(points, key=lambda point: point.concurrency),
        )
        # 以下比较仅用于生成模拟演示数据（等价于原生 --sla-auto-tune 的输出形态），
        # 真实验收判定完全由 EvalScope --sla-auto-tune 完成，此处不构成判定工具。
        def _mock_pass(point: PerfPoint) -> bool:
            t = params.thresholds or {}
            checks = {
                "avg_ttft_s": (point.ttft_avg_ms, 1000.0),
                "avg_tpot_ms": (point.tpot_avg_ms, 1.0),
                "p99_ttft_s": (point.ttft_p99_ms, 1000.0),
                "p99_tpot_ms": (point.tpot_p99_ms, 1.0),
            }
            active = [(k, v) for k, v in t.items() if v and k in checks]
            return bool(active) and all(actual <= limit * scale for k, limit in active
                                        for actual, scale in [checks[k]])
        target_met = _mock_pass(target_point)
        passing = [point for point in points if _mock_pass(point)]
        best = max(passing, key=lambda point: point.concurrency) if passing else None
        gaps = [] if target_met else ["模拟演示数据：目标并发未达阈值（详见 sla_summary.json）"]
        detail = SlaDetail(
            target=SlaTarget(
                input_length=params.target_context,
                output_tokens=params.output_tokens,
                prefix_ratio=params.prefix_ratio,
                prefix_count=params.prefix_count,
                concurrency=params.upper,
                thresholds=params.thresholds,
            ),
            strategy="native",
            target_met=target_met,
            satisfiable=bool(passing),
            recommended_concurrency=best.concurrency if best else None,
            probes=points,
            best=best,
            gaps=gaps,
            note="模拟 EvalScope 原生 SLA 结果（avg / p99 口径）。",
            output_dir=str(output_dir),
        )
        (output_dir / "sla_summary.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail
