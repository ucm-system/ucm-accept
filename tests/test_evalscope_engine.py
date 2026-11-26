from __future__ import annotations

from pathlib import Path

import pytest

from ucm_accept.config import Config
from ucm_accept.engine.base import (
    AccuracyParams,
    AgentParams,
    Endpoint,
    MultiturnParams,
    PerfParams,
    SlaNativeParams,
)
from ucm_accept.engine.evalscope_engine import EvalScopeEngine


@pytest.fixture
def engine(tmp_path: Path, fake_evalscope: Path, config: Config) -> EvalScopeEngine:
    config.set("evalscope.executable", str(fake_evalscope))
    return EvalScopeEngine(config)


def endpoint() -> Endpoint:
    return Endpoint(base_url="http://127.0.0.1:8801/v1", model="test-model")


def test_perf_uses_native_cli_and_parses_output(tmp_path: Path, engine: EvalScopeEngine):
    params = PerfParams(
        parallel=[200],
        number=200,
        min_prompt_length=65536,
        max_prompt_length=65536,
        min_tokens=256,
        max_tokens=256,
        tokenizer_path="/models/tokenizer",
    )
    detail = engine.run_perf(endpoint(), params, tmp_path / "perf")
    assert len(detail.points) == 1
    point = detail.points[0]
    assert point.concurrency == 200
    assert point.ttft_p90_ms == 8500
    assert point.tpot_p90_ms == 45
    assert point.success_rate == 1.0
    command = (tmp_path / "perf" / "command.txt").read_text(encoding="utf-8")
    assert "evalscope perf" in command or "fake_evalscope" in command
    assert "--tokenizer-path" in command
    assert "--outputs-dir" in command


def test_accuracy_parses_native_report(tmp_path: Path, engine: EvalScopeEngine):
    params = AccuracyParams(
        key="gsm8k",
        label="数学推理",
        dataset="gsm8k",
        subset="",
        limit=100,
        run_index=1,
    )
    run = engine.run_accuracy(endpoint(), params, tmp_path / "accuracy")
    assert run.score == 0.864
    command = (tmp_path / "accuracy" / "command.txt").read_text(encoding="utf-8")
    assert "evalscope eval" in command or "fake_evalscope" in command
    assert "--datasets gsm8k" in command
    assert (tmp_path / "accuracy" / "ucm_accuracy_run.json").exists()


def test_multiturn_uses_builtin_dataset_name(tmp_path: Path, engine: EvalScopeEngine):
    params = MultiturnParams(
        scenario_key="share_gpt_zh",
        scenario_label="真实对话数据集（中文）",
        dataset_name="share_gpt_zh_multi_turn",
        dataset_path=None,
        parallel=4,
        conversations=12,
        min_turns=2,
        max_turns=5,
        min_tokens=128,
        max_tokens=512,
    )
    detail = engine.run_multiturn(endpoint(), params, tmp_path / "multiturn")
    assert detail.load_total_conversations == 12
    assert detail.load_success_conversations == 12
    assert detail.total_turns > 0
    command = (tmp_path / "multiturn" / "command.txt").read_text(encoding="utf-8")
    assert "--multi-turn" in command
    assert "--dataset share_gpt_zh_multi_turn" in command
    assert "--dataset-path" not in command


def test_agentx_parses_summary(tmp_path: Path, engine: EvalScopeEngine):
    params = AgentParams(
        target_context=262144,
        context_detected=262144,
        parallel=4,
        duration_s=900,
        degraded=False,
        tokenizer_path="/models/tokenizer",
    )
    detail = engine.run_agent(endpoint(), params, tmp_path / "agent")
    assert detail.completion_rate == pytest.approx(0.98)
    assert detail.sessions == 4
    assert detail.requests == 200
    assert detail.avg_steps == pytest.approx(50)
    assert detail.submission_valid is True


def test_sla_native_uses_avg_p99_thresholds(tmp_path: Path, engine: EvalScopeEngine):
    params = SlaNativeParams(
        target_context=65536,
        upper=200,
        lower=2,
        number=200,
        num_runs=1,
        number_multiplier=2,
        output_tokens=512,
        prefix_ratio=0.9,
        prefix_count=8,
        thresholds={"avg_ttft_s": 30, "avg_tpot_ms": 50},
        tokenizer_path="/models/tokenizer",
    )
    detail = engine.run_sla_native(endpoint(), params, tmp_path / "sla")
    command = (tmp_path / "sla" / "command.txt").read_text(encoding="utf-8")
    assert "--sla-auto-tune" in command
    assert "avg_ttft" in command
    assert "avg_tpot" in command
    assert "<= 30s" in command
    assert "<= 50ms" in command
    # 压测口径：输入/输出/前缀
    assert "--max-model-len" not in command
    assert "--prefix-count 8" in command
    assert "--min-tokens 512" in command
    assert detail.strategy == "native"
    assert detail.recommended_concurrency == 160


def test_escape_api_key_in_command_log(tmp_path: Path, engine: EvalScopeEngine):
    secret = "sk-very-secret"
    ep = Endpoint(base_url="http://127.0.0.1:8801/v1", model="m", api_key=secret)
    params = PerfParams(
        parallel=[20],
        number=10,
        min_prompt_length=100,
        max_prompt_length=100,
        min_tokens=16,
        max_tokens=16,
    )
    engine.run_perf(ep, params, tmp_path / "perf")
    command = (tmp_path / "perf" / "command.txt").read_text(encoding="utf-8")
    assert secret not in command
    assert "***" in command


def test_env_injects_dataset_cache_from_asset_hub(
    engine: EvalScopeEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """资产包内的 evalscope-cache 应自动注入引擎环境变量。"""
    from ucm_accept import assets as hub

    pack = tmp_path / "ucm-assets"
    cache = pack / "evalscope-cache"
    cache.mkdir(parents=True)
    monkeypatch.delenv(hub.ENV_VAR, raising=False)
    monkeypatch.delenv("EVALSCOPE_CACHE", raising=False)
    engine.config.set("app.assets_dir", str(pack))
    env = engine._env()
    assert env["EVALSCOPE_CACHE"] == str(cache.resolve())
    # 资产包缓存强制覆盖镜像内置的只读默认路径
    monkeypatch.setenv("EVALSCOPE_CACHE", "/opt/ucm-accept/assets/evalscope-cache")
    env2 = engine._env()
    assert env2["EVALSCOPE_CACHE"] == str(cache.resolve())
