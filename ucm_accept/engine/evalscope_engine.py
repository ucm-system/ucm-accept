from __future__ import annotations

import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import assets as assets_hub
from ..config import Config
from ..errors import AssetError, CancelledError, EvalScopeError, EvalScopeNotFound
from ..models import (
    AccuracyRun,
    AgentDetail,
    MultiturnDetail,
    PerfDetail,
    SlaDetail,
    SlaTarget,
)
from ..utils import coerce_float, redact_secret, shell_join
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
from .chat import ChatClient
from .parser import (
    extract_score,
    iter_json_files,
    parse_accuracy_output,
    parse_agent_output,
    parse_multiturn_metrics,
    parse_perf_output_dir,
    parse_perf_tables,
    parse_sla_output,
    parse_sla_selection,
    read_json_safe,
    strip_ansi,
)


@dataclass
class CommandResult:
    returncode: int
    text: str
    log_path: Path


class EvalScopeEngine(Engine):
    """通过官方 CLI 调用 EvalScope，避免绑死其内部 Python API。

    CLI 参数以当前稳定版本为准；每个测试项均支持在 config.yaml 中整体覆写 argv，
    以适配客户现场不同版本的 EvalScope。
    """

    mode = "live"
    name = "evalscope"

    def __init__(self, config: Config):
        self.config = config
        executable = str(config.get("evalscope.executable", "evalscope")).strip()
        if executable.startswith('"') and executable.endswith('"'):
            executable = executable[1:-1]
        if executable and Path(executable).exists():
            self.base_argv = [executable]
        else:
            self.base_argv = (
                shlex.split(executable, posix=os.name != "nt") if executable else ["evalscope"]
            )
        self._validate_executable()
        self.env_extra = {
            str(key): str(value)
            for key, value in (config.get("evalscope.env", {}) or {}).items()
        }
        self.command_overrides = {
            "perf": list(config.get("evalscope.perf_command", []) or []),
            "accuracy": list(config.get("evalscope.accuracy_command", []) or []),
            "multiturn": list(config.get("evalscope.multiturn_command", []) or []),
            "agent": list(config.get("evalscope.agent_command", []) or []),
        }

    # ------------------------------------------------------------------ setup
    def _validate_executable(self) -> None:
        first = self.base_argv[0]
        if os.path.sep in first or (os.path.altsep and os.path.altsep in first):
            if Path(first).exists():
                return
            raise EvalScopeNotFound(f"EvalScope 可执行文件不存在：{first}")
        if shutil.which(first) is None:
            raise EvalScopeNotFound(
                "未找到 EvalScope 命令。请在离线包内安装 evalscope，或修改 "
                "config.yaml 中 evalscope.executable（例如指向虚拟环境内的可执行文件）。"
            )

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(self.env_extra)
        # 统一资产包内的数据集缓存自动注入（显式设置的环境变量 / config env 优先）
        try:
            hub = assets_hub.find_assets_root(
                str(self.config.get("app.assets_dir", "") or "") or None
            )
        except Exception:  # noqa: BLE001 - 资产发现失败不应影响压测启动
            hub = None
        if hub is not None:
            # 资产包缓存路径覆盖镜像内置默认（容器 ENV 指向只读路径时必须强制切换）
            for var, value in assets_hub.cache_env(hub).items():
                env[var] = value
        env.setdefault("PYTHONUNBUFFERED", "1")
        # 精度/压测均为 API 驱动，子进程不需要本机 GPU：禁用 CUDA 避免
        # torch.cuda.is_available() 在部分 WSL/CUDA 组合下段错误（bfcl 依赖链带入 torch）
        env.setdefault("CUDA_VISIBLE_DEVICES", "")
        env.setdefault("EVALSCOPE_LANGUAGE", "zh")
        env.setdefault("TERM", "dumb")
        return env

    # ------------------------------------------------------------------ probe
    def probe(self, endpoint: Endpoint) -> ProbeResult:
        return ChatClient(endpoint).probe()

    def detect_context(self, endpoint: Endpoint) -> int | None:
        return ChatClient(endpoint).detect_context()

    # --------------------------------------------------------------- command
    def _render(self, tokens: Iterable[str], mapping: dict[str, Any]) -> list[str]:
        rendered: list[str] = []
        pattern = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
        for token in tokens:
            key_match = pattern.fullmatch(str(token))
            if key_match:
                value = mapping.get(key_match.group(1))
                if isinstance(value, (list, tuple)):
                    rendered.extend(str(item) for item in value)
                elif value is not None:
                    rendered.append(str(value))
                continue

            def repl(match: re.Match[str]) -> str:
                value = mapping.get(match.group(1))
                if value is None:
                    return match.group(0)
                if isinstance(value, (list, tuple)):
                    return " ".join(str(item) for item in value)
                return str(value)

            rendered.append(pattern.sub(repl, str(token)))
        return rendered

    def _override_argv(self, kind: str, mapping: dict[str, Any]) -> list[str] | None:
        override = self.command_overrides.get(kind) or []
        if not override:
            return None
        rendered = self._render([str(token) for token in override], mapping)
        if not rendered:
            return None
        first = Path(rendered[0]).name
        if first in {"evalscope", Path(self.base_argv[0]).name} or rendered[0].startswith("python"):
            return rendered
        if rendered[0] in {"perf", "eval"}:
            return [*self.base_argv, *rendered]
        return [*self.base_argv, *rendered]

    def _base_mapping(self, endpoint: Endpoint, output_dir: Path) -> dict[str, Any]:
        return {
            "model": endpoint.model,
            "url": endpoint.chat_url,
            "base_url": endpoint.base_url,
            "api_url": endpoint.base_url,
            "api_key": endpoint.api_key or "EMPTY",
            "output_dir": str(output_dir),
            "work_dir": str(output_dir),
        }

    # ------------------------------------------------------------------- exec
    def _run(
        self,
        argv: list[str],
        output_dir: Path,
        log: LogFn | None,
        cancel: threading.Event | None,
        *,
        timeout_s: float,
        secret: str = "",
        cwd: str | None = None,
    ) -> CommandResult:
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / "evalscope.log"
        effective_secret = secret or self.config.get("endpoint.api_key", "")
        redacted_argv = []
        for index, arg in enumerate(argv):
            if index > 0 and argv[index - 1] == "--api-key":
                redacted_argv.append("***")
            else:
                redacted_argv.append(redact_secret(str(arg), effective_secret))
        command_line = shell_join(redacted_argv)
        if log:
            log(f"[EvalScope] $ {command_line}")
        try:
            (output_dir / "command.txt").write_text(command_line + "\n", encoding="utf-8")
        except OSError:
            pass
        with log_path.open("w", encoding="utf-8") as log_file:
            log_file.write(f"$ {command_line}\n\n")
            log_file.flush()
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    env=self._env(),
                    start_new_session=not os.name == "nt",
                )
            except OSError as exc:
                raise EvalScopeError(f"启动 EvalScope 失败：{exc}") from exc

            lines: queue.Queue[str | None] = queue.Queue()

            def reader() -> None:
                assert process.stdout is not None
                try:
                    for line in process.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            thread = threading.Thread(target=reader, name="evalscope-reader", daemon=True)
            thread.start()
            collected: list[str] = []
            deadline = time.monotonic() + timeout_s if timeout_s and timeout_s > 0 else None
            timed_out = False
            try:
                while True:
                    try:
                        item = lines.get(timeout=0.25)
                    except queue.Empty:
                        item = ""
                    if item is None:
                        break
                    if item:
                        clean = redact_secret(strip_ansi(item), effective_secret)
                        log_file.write(clean)
                        log_file.flush()
                        if len(collected) < 800_000:
                            collected.append(clean)
                        stripped = clean.rstrip()
                        if log and stripped:
                            log(stripped[:1200])
                    if cancel is not None and cancel.is_set():
                        self._terminate(process)
                        raise CancelledError("用户已取消，已终止 EvalScope 进程")
                    if deadline is not None and time.monotonic() > deadline:
                        timed_out = True
                        self._terminate(process)
                        break

                try:
                    returncode = process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._terminate(process)
                    returncode = -9
            except KeyboardInterrupt:
                # 进程级中断（Ctrl+C / 关闭终端）：终止子进程并转为可处理的取消语义，
                # 由上层 _execute_one 的 finally 落盘报告
                self._terminate(process)
                if cancel is not None:
                    cancel.set()
                raise CancelledError("执行被中断，已终止 EvalScope 进程") from None
            finally:
                # 任何异常退出路径都不允许留下孤儿子进程
                if process.poll() is None:
                    self._terminate(process)
            text = "".join(collected)
            if timed_out:
                raise EvalScopeError(f"EvalScope 执行超时（{int(timeout_s)}s），详见 {log_path}")
            if cancel is not None and cancel.is_set():
                raise CancelledError("用户已取消")
            if returncode != 0:
                tail = "\n".join(text.strip().splitlines()[-30:])
                raise EvalScopeError(
                    f"EvalScope 执行失败（退出码 {returncode}）。日志：{log_path}\n{tail}"
                )
            return CommandResult(returncode=returncode, text=text, log_path=log_path)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError):
            process.terminate()
        try:
            process.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError):
            process.kill()

    # ------------------------------------------------------------------- perf
    def _perf_argv(self, endpoint: Endpoint, params: PerfParams, output_dir: Path) -> list[str]:
        mapping = self._base_mapping(endpoint, output_dir)
        mapping.update(
            {
                "dataset": params.dataset,
                "tokenizer": params.tokenizer_path,
                "tokenizer_path": params.tokenizer_path,
                "limit": params.number,
                "parallel": params.parallel,
                "number": [params.number] * len(params.parallel),
                "prompt_length": params.max_prompt_length,
                "max_tokens": params.max_tokens,
            }
        )
        override = self._override_argv("perf", mapping)
        if override:
            return override
        argv = [
            *self.base_argv,
            "perf",
            "--model",
            endpoint.model,
            "--url",
            endpoint.chat_url,
            "--api",
            "openai",
            "--dataset",
            params.dataset,
            "--parallel",
            *[str(value) for value in params.parallel],
            "--number",
            *[str(params.number)] * len(params.parallel),
            "--min-tokens",
            str(params.min_tokens),
            "--max-tokens",
            str(params.max_tokens),
            "--stream",
            "--outputs-dir",
            str(output_dir),
            "--no-timestamp",
            "--total-timeout",
            str(params.request_timeout_s),
        ]
        if endpoint.api_key:
            argv.extend(["--api-key", endpoint.api_key])
        if params.tokenizer_path:
            argv.extend(["--tokenizer-path", params.tokenizer_path])
        if params.dataset == "random":
            argv.extend(
                [
                    "--min-prompt-length",
                    str(params.min_prompt_length),
                    "--max-prompt-length",
                    str(params.max_prompt_length),
                ]
            )
            if params.prefix_length > 0:
                argv.extend(["--prefix-length", str(params.prefix_length)])
                if params.prefix_count > 1:
                    argv.extend(["--prefix-count", str(params.prefix_count)])
        if params.warmup_num > 0:
            argv.extend(["--warmup-num", str(params.warmup_num)])
        argv.extend([str(arg) for arg in params.extra_args])
        return argv

    def run_perf(
        self,
        endpoint: Endpoint,
        params: PerfParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> PerfDetail:
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = self._perf_argv(endpoint, params, output_dir)
        result = self._run(
            argv,
            output_dir,
            log,
            cancel,
            timeout_s=max(600, int(params.request_timeout_s) + 300),
            secret=endpoint.api_key,
        )
        points = parse_perf_tables(result.text)
        if not points:
            points = parse_perf_output_dir(output_dir)
        if not points:
            tail = "\n".join(result.text.strip().splitlines()[-20:])
            raise EvalScopeError(
                f"未能从 EvalScope 输出中解析性能指标。原始输出：{output_dir}\n{tail}"
            )
        for point in points:
            if not point.requests:
                point.requests = params.number
                point.success_requests = params.number
            point.input_tokens = point.input_tokens or float(params.max_prompt_length)
            point.output_tokens = point.output_tokens or float(params.max_tokens)
            point.raw_dir = str(output_dir)
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
                "source": "evalscope",
            },
            output_dir=str(output_dir),
        )
        (output_dir / "ucm_perf.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail

    # -------------------------------------------------------------- multiturn
    def _multiturn_argv(self, endpoint: Endpoint, params: MultiturnParams, output_dir: Path) -> list[str]:
        builtin = bool(params.dataset_name)
        mapping = self._base_mapping(endpoint, output_dir)
        mapping.update(
            {
                "dataset": params.dataset_name or "custom_multi_turn",
                "dataset_path": str(params.dataset_path or ""),
                "parallel": params.parallel,
                "number": params.conversations,
                "max_turns": params.max_turns,
                "scenario": params.scenario_key,
            }
        )
        override = self._override_argv("multiturn", mapping)
        if override:
            return override
        argv = [
            *self.base_argv,
            "perf",
            "--model",
            endpoint.model,
            "--url",
            endpoint.chat_url,
            "--api",
            "openai",
            "--dataset",
            params.dataset_name or "custom_multi_turn",
        ]
        if not builtin:
            argv.extend(["--dataset-path", str(params.dataset_path)])
        if params.tokenizer_path:
            argv.extend(["--tokenizer-path", params.tokenizer_path])
        argv.extend([
            "--multi-turn",
            "--min-turns",
            str(params.min_turns),
            "--max-turns",
            str(params.max_turns),
            "--parallel",
            str(params.parallel),
            "--number",
            str(params.conversations),
            "--max-tokens",
            str(params.max_tokens),
            "--stream",
            "--outputs-dir",
            str(output_dir),
            "--no-timestamp",
            "--total-timeout",
            str(params.request_timeout_s),
        ])
        if endpoint.api_key:
            argv.extend(["--api-key", endpoint.api_key])
        argv.extend([str(arg) for arg in params.extra_args])
        return argv

    def run_multiturn(
        self,
        endpoint: Endpoint,
        params: MultiturnParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> MultiturnDetail:
        if not params.dataset_name:
            if params.dataset_path is None or not params.dataset_path.exists():
                raise AssetError(f"多轮对话预设数据不存在：{params.dataset_path}")
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = self._multiturn_argv(endpoint, params, output_dir)
        result = self._run(
            argv,
            output_dir,
            log,
            cancel,
            timeout_s=max(900, int(params.request_timeout_s) * max(1, params.max_turns) + 300),
            secret=endpoint.api_key,
        )
        metrics = parse_multiturn_metrics(result.text)
        for summary_path in sorted(output_dir.rglob("benchmark_summary.json")):
            summary = read_json_safe(summary_path)
            if not isinstance(summary, dict):
                continue
            turns_value = coerce_float(summary.get("Avg Turns/Request"))
            if turns_value:
                metrics["turns_per_request"] = turns_value
            cache_value = coerce_float(summary.get("KV Cache Hit Rate (%)"))
            if cache_value is not None:
                metrics["cache_hit_rate"] = cache_value / 100.0
            break
        turns_per_request = metrics.get("turns_per_request", 0.0)
        total_turns = (
            int(round(turns_per_request * params.conversations))
            if turns_per_request
            else params.conversations
        )
        success = params.conversations
        perf_points = parse_perf_tables(result.text)
        if perf_points and perf_points[0].requests:
            success = round(params.conversations * perf_points[0].success_rate)
        avg_latency = perf_points[0].latency_avg_s * 1000 if perf_points else 0.0
        detail = MultiturnDetail(
            scenario=params.scenario_key,
            label=params.scenario_label,
            parallel=params.parallel,
            conversations_requested=params.conversations,
            total_turns=total_turns,
            success_turns=success,
            avg_latency_ms=round(avg_latency, 1),
            cache_hit_rate=metrics.get("cache_hit_rate"),
            load_total_conversations=params.conversations,
            load_success_conversations=success,
            output_dir=str(output_dir),
            note="对话回放不属于 EvalScope 原生输出，本工具不再自行编造；详见 EvalScope 原始报告。",
        )
        (output_dir / "ucm_multiturn.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail

    # --------------------------------------------------------------- accuracy
    def _accuracy_argv(self, endpoint: Endpoint, params: AccuracyParams, output_dir: Path) -> list[str]:
        mapping = self._base_mapping(endpoint, output_dir)
        mapping.update(
            {
                "dataset": params.dataset,
                "limit": params.limit,
                "run": params.run_index,
                "dataset_dir": params.dataset_dir,
            }
        )
        override = self._override_argv("accuracy", mapping)
        if override:
            return override
        argv = [
            *self.base_argv,
            "eval",
            "--model",
            endpoint.model,
            "--api-url",
            endpoint.base_url,
            "--datasets",
            params.dataset,
            "--work-dir",
            str(output_dir),
            "--no-timestamp",
        ]
        if endpoint.api_key:
            argv.extend(["--api-key", endpoint.api_key])
        if params.dataset_dir:
            # 资产包内数据集缓存直读本地，离线环境不访问 ModelScope hub
            argv.extend(["--dataset-dir", params.dataset_dir])
        if params.limit and params.limit > 0:
            argv.extend(["--limit", str(params.limit)])
        dataset_args: dict[str, Any] = dict(params.dataset_args or {})
        if params.subset:
            existing = dataset_args.get("subset_list") or []
            if not isinstance(existing, list):
                existing = [existing]
            if params.subset not in existing:
                existing.append(params.subset)
            dataset_args["subset_list"] = existing
        if dataset_args:
            argv.extend(["--dataset-args", json.dumps({params.dataset: dataset_args}, ensure_ascii=False)])
        if params.request_timeout_s:
            argv.extend(
                [
                    "--generation-config",
                    json.dumps({"timeout": params.request_timeout_s}, ensure_ascii=False),
                ]
            )
        argv.extend([str(arg) for arg in params.extra_args])
        return argv

    def run_accuracy(
        self,
        endpoint: Endpoint,
        params: AccuracyParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> AccuracyRun:
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = self._accuracy_argv(endpoint, params, output_dir)
        started = time.monotonic()
        try:
            result = self._run(
                argv,
                output_dir,
                log,
                cancel,
                timeout_s=max(600, int(params.request_timeout_s) + 300),
                secret=endpoint.api_key,
                cwd=str(output_dir),
            )
        except CancelledError:
            raise
        duration = time.monotonic() - started
        score, source = parse_accuracy_output(output_dir, params.dataset, result.text)
        if score is None:
            # 最后尝试直接扫描所有 JSON
            for path in iter_json_files(output_dir):
                data = read_json_safe(path)
                if data is None:
                    continue
                candidate = extract_score(data, params.dataset)
                if candidate is not None:
                    score, source = candidate, str(path)
                    break
        if score is None:
            raise EvalScopeError(
                f"未能在 EvalScope 输出中解析到 {params.dataset} 得分，请查看 {output_dir}"
            )
        if log:
            log(f"{params.label} 第 {params.run_index} 遍得分：{score:.4f}（来源：{source}）")
        run = AccuracyRun(
            index=params.run_index,
            score=score,
            duration_s=round(duration, 2),
            raw_dir=str(output_dir),
        )
        (output_dir / "ucm_accuracy_run.json").write_text(
            json.dumps(
                {
                    "dataset": params.dataset,
                    "subset": params.subset,
                    "run": params.run_index,
                    "score": score,
                    "source": source,
                    "duration_s": round(duration, 2),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return run

    # ------------------------------------------------------------------ agent
    def _agent_argv(self, endpoint: Endpoint, params: AgentParams, output_dir: Path) -> list[str]:
        # AgentX 场景统一以 JSON 传入（evalscope 支持完整字段）：
        # - variant 按目标上下文自动选择（≥128K 用 256k 变体，否则 full）
        # - max_context_length 交给 AgentX 原生裁剪 trace
        # - 上下文不足/短时长时进入 smoke 模式（trace_limit 用内置默认）
        scenario_obj: dict[str, Any] = {
            "name": params.scenario_name,
            "variant": "256k" if params.target_context >= 131072 else "full",
            "max_context_length": params.target_context,
        }
        if params.degraded:
            scenario_obj["mode"] = params.degraded_mode
            if params.degraded_mode == "smoke":
                scenario_obj["trace_limit"] = 4
        scenario = json.dumps(scenario_obj, ensure_ascii=False)
        mapping = self._base_mapping(endpoint, output_dir)
        mapping.update(
            {
                "scenario": scenario,
                "parallel": params.degraded_parallel if params.degraded else params.parallel,
                "duration": params.duration_s,
            }
        )
        override = self._override_argv("agent", mapping)
        if override:
            return override
        argv = [
            *self.base_argv,
            "perf",
            "--scenario",
            scenario,
            "--model",
            endpoint.model,
            "--url",
            endpoint.chat_url,
            "--api",
            "openai",
            "--parallel",
            str(params.degraded_parallel if params.degraded else params.parallel),
            "--outputs-dir",
            str(output_dir),
            "--no-timestamp",
        ]
        if params.tokenizer_path:
            argv.extend(["--tokenizer-path", params.tokenizer_path])
        if params.duration_s > 0 and not params.degraded:
            argv.extend(["--duration", str(params.duration_s)])
        if endpoint.api_key:
            argv.extend(["--api-key", endpoint.api_key])
        argv.extend([str(arg) for arg in params.extra_args])
        return argv

    def run_agent(
        self,
        endpoint: Endpoint,
        params: AgentParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> AgentDetail:
        if not endpoint.chat_url.rstrip("/").endswith("/v1/chat/completions"):
            raise EvalScopeError(
                "EvalScope AgentX 要求服务地址以 /v1 结尾（实际请求 /v1/chat/completions）；"
                f"当前为 {endpoint.chat_url}"
            )
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = self._agent_argv(endpoint, params, output_dir)
        result = self._run(
            argv, output_dir, log, cancel,
            timeout_s=max(900, params.duration_s + 600), secret=endpoint.api_key,
        )
        metrics = parse_agent_output(output_dir, result.text)
        completion = metrics.get("completion_rate")
        sessions = int(metrics.get("sessions") or 0)
        requests = int(metrics.get("requests") or 0)
        avg_steps = metrics.get("avg_steps")
        if avg_steps is None and sessions and requests:
            avg_steps = requests / sessions
        notes: list[str] = [
            "数据来源：EvalScope `perf --scenario agentx`，属于服务级 256K 长会话压测。"
        ]
        notes.append(
            "AgentX 不输出任务质量分；本工具不自行计算任务完成率或工具调用成功率，"
            "如需任务级指标请使用独立 Agent 评测流程。"
        )
        if params.degraded:
            notes.append(
                f"检测到可用上下文 {params.context_detected or '未知'} < {params.target_context}，"
                "已切换 smoke 模式；smoke 指标不可与标准 256K 结果直接比较。"
            )
        status = str(metrics.get("status") or "completed")
        if status != "completed":
            notes.append(f"AgentX 运行状态为 {status}；该结果不可用于验收结论。")
        if metrics.get("submission_valid") is False and not params.degraded:
            notes.append("EvalScope 标记 submission_valid=false；请检查数据集快照、时长或运行环境。")
        if completion is None:
            notes.append("未能从 AgentX 输出中解析成功率，请核对 EvalScope 版本与结果目录。")
        detail = AgentDetail(
            dataset_name="AgentX-claudecode",
            context_required=params.target_context,
            context_detected=params.context_detected,
            degraded=params.degraded,
            completion_rate=completion,
            avg_steps=avg_steps,
            sessions=sessions,
            requests=requests,
            source="evalscope-agentx",
            submission_valid=metrics.get("submission_valid"),
            notes=notes,
            metrics={key: value for key, value in metrics.items() if key != "raw"},
            output_dir=str(output_dir),
        )
        (output_dir / "ucm_agent.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail

    # -------------------------------------------------------------- SLA native
    def _sla_argv(self, endpoint: Endpoint, params: SlaNativeParams, output_dir: Path) -> list[str]:
        # EvalScope SLA 指标：avg_latency/avg_ttft/avg_tpot/p99_latency/p99_ttft/p99_tpot
        # 阈值键单位：ttft 秒、tpot 毫秒；同一组内全部满足才算达标
        unit = {"avg_ttft_s": "s", "p99_ttft_s": "s", "avg_tpot_ms": "ms", "p99_tpot_ms": "ms"}
        criteria: dict[str, str] = {}
        for key, value in params.thresholds.items():
            if not value or key not in unit:
                continue
            criteria[key.removesuffix("_s").removesuffix("_ms")] = f"<= {value:g}{unit[key]}"
        if not criteria:
            raise EvalScopeError("SLA 阈值未配置（avg_ttft_s / avg_tpot_ms / p99_ttft_s / p99_tpot_ms 至少一项）")
        sla_params = json.dumps([criteria], ensure_ascii=False)
        mapping = self._base_mapping(endpoint, output_dir)
        mapping.update(
            {
                "sla_params": sla_params,
                "lower": params.lower,
                "upper": params.upper,
                "parallel": params.lower,
                "number": params.number,
                "context": params.target_context,
            }
        )
        override = self._override_argv("perf", mapping)
        if override:
            return override
        argv = [
            *self.base_argv,
            "perf",
            "--model",
            endpoint.model,
            "--url",
            endpoint.chat_url,
            "--api",
            "openai",
            "--dataset",
            "random",
            "--tokenizer-path",
            params.tokenizer_path,
            "--parallel",
            str(params.lower),
            "--min-prompt-length",
            str(params.target_context),
            "--max-prompt-length",
            str(params.target_context),
            "--min-tokens",
            str(params.output_tokens),
            "--max-tokens",
            str(params.output_tokens),
            "--sla-auto-tune",
            "--sla-variable",
            params.variable,
            "--sla-params",
            sla_params,
            "--sla-lower-bound",
            str(params.lower),
            "--sla-upper-bound",
            str(params.upper),
            "--sla-num-runs",
            str(params.num_runs),
            "--sla-number-multiplier",
            str(params.number_multiplier),
            "--outputs-dir",
            str(output_dir),
            "--no-timestamp",
        ]
        if endpoint.api_key:
            argv.extend(["--api-key", endpoint.api_key])
        prefix_length = round(params.target_context * max(0.0, min(1.0, params.prefix_ratio)))
        if params.prefix_ratio > 0 and prefix_length > 0:
            argv.extend(["--prefix-length", str(prefix_length)])
            if params.prefix_count > 1:
                argv.extend(["--prefix-count", str(params.prefix_count)])
        argv.extend([str(arg) for arg in params.extra_args])
        return argv

    def run_sla_native(
        self,
        endpoint: Endpoint,
        params: SlaNativeParams,
        output_dir: Path,
        log: LogFn | None = None,
        cancel: threading.Event | None = None,
    ) -> SlaDetail:
        output_dir.mkdir(parents=True, exist_ok=True)
        argv = self._sla_argv(endpoint, params, output_dir)
        result = self._run(argv, output_dir, log, cancel, timeout_s=12 * 3600, secret=endpoint.api_key)
        points = parse_sla_output(output_dir, result.text)
        selection = parse_sla_selection(result.text)
        recommended = selection.get("recommended_concurrency")
        target_point = next((point for point in points if point.concurrency == params.upper), None)
        if recommended is None:
            passing = [point for point in points if point.meets(params.ttft_p90_ms, params.tpot_p90_ms)]
            if passing:
                recommended = max(point.concurrency for point in passing)
        if recommended is not None:
            target_met = recommended >= params.upper
        else:
            target_met = bool(target_point and target_point.meets(params.ttft_p90_ms, params.tpot_p90_ms))
        best = None
        if recommended is not None:
            best = next((point for point in points if point.concurrency == recommended), None)
        gaps: list[str] = []
        if recommended is not None and recommended < params.upper:
            gaps.append(
                f"EvalScope 原生建议并发 {recommended} 低于目标 {params.upper}，目标并发未达标"
            )
        if target_point is None and not target_met:
            gaps.append(
                f"EvalScope 未探测目标并发 {params.upper}；原生建议并发为 "
                f"{recommended if recommended is not None else '无'}"
            )
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
            satisfiable=recommended is not None,
            recommended_concurrency=recommended,
            probes=points,
            best=best,
            gaps=gaps,
            note=(
                "数值与达标判定均来自 EvalScope 原生 `perf --sla-auto-tune`（avg / p99 口径）；"
                "建议并发为 EvalScope 原生搜索结果，可原样复现。"
            ),
            output_dir=str(output_dir),
        )
        (output_dir / "ucm_sla.json").write_text(
            json.dumps(detail.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return detail
