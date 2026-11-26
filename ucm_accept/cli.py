from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .doctor import run_doctor
from .errors import UCMAcceptError
from .events import ProgressEvent
from .models import (
    ITEM_BY_KEY,
    ITEM_KEYS,
    STATUS_LABELS,
    STATUS_MARKS,
    AcceptanceReport,
    Status,
    human_duration,
)
from .report import export_pdf
from .service import AcceptanceService
from .utils import find_latest_report, render_table, shorten

# Windows 控制台默认可能是 GBK；统一为 UTF-8，避免中文表格/✓ 输出异常。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

_USE_COLOR = sys.stdout.isatty() and not bool(os.environ.get("NO_COLOR"))


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else text


GREEN = lambda text: _c(text, "32")  # noqa: E731
RED = lambda text: _c(text, "31")  # noqa: E731
YELLOW = lambda text: _c(text, "33")  # noqa: E731
DIM = lambda text: _c(text, "2")  # noqa: E731
BOLD = lambda text: _c(text, "1")  # noqa: E731
CYAN = lambda text: _c(text, "36")  # noqa: E731


def _status_text(status: Status) -> str:
    return f"{STATUS_MARKS[status]} {STATUS_LABELS[status]}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ucm-accept",
        description="UCM 推理加速产品 · 客户现网验收工具（EvalScope 统一承载）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  ucm-accept                         # 交互式菜单（容器 / 堡垒机）\n"
            "  ucm-accept --all                   # 按 run.items 范围跑完并生成报告\n"
            "  ucm-accept --items perf,sla        # 只跑基础性能 + SLA\n"
            "  ucm-accept --item perf             # 只跑基础性能\n"
            "  ucm-accept --save-config my.yaml   # 保存当前生效配置，之后 --config my.yaml 复用\n"
            "  ucm-accept --gui                   # 启动浏览器 GUI\n"
            "  ucm-accept --doctor --probe        # 现场自检\n"
            "  ucm-accept --mock --fast --all     # 演示 / CI 虚跑\n"
        ),
    )
    parser.add_argument("--config", help="外部 config.yaml 路径")
    parser.add_argument("--outputs", help="报告输出根目录（默认 ./outputs）")
    parser.add_argument("--base-url", help="覆盖服务地址，例如 http://127.0.0.1:8801/v1")
    parser.add_argument("--model", help="覆盖模型名称")
    parser.add_argument("--api-key", help="覆盖 API Key")
    parser.add_argument("--item", choices=list(ITEM_KEYS), help="只执行单个测试项")
    parser.add_argument(
        "--items",
        help="只执行指定测试项，逗号分隔，可用编号或名称，例如 perf,sla 或 1,3",
    )
    parser.add_argument("--save-config", help="把当前生效配置另存为 YAML 后退出（API Key 不落盘）")
    try:
        _scenario_keys = list((load_config(None).get("multiturn.scenarios", {}) or {}).keys())
    except Exception:  # noqa: BLE001 - 配置不可读时退化为自由文本
        _scenario_keys = None
    parser.add_argument(
        "--scenario",
        choices=_scenario_keys,
        help="多轮对话场景预设（来自 config multiturn.scenarios）",
    )
    parser.add_argument("--all", action="store_true", help="按配置 run.items 的范围顺序执行验收")
    parser.add_argument("--sla-mode", choices=["verify", "auto_tune"], help="覆盖 SLA 模式")
    parser.add_argument("--report", help="将本次 HTML 报告另存到指定路径")
    parser.add_argument("--pdf", action="store_true", help="同时导出 PDF（需 weasyprint 或 chromium）")
    parser.add_argument("--mock", action="store_true", help="使用模拟引擎（演示 / 虚跑，报告会标注）")
    parser.add_argument("--fast", action="store_true", help="模拟模式零延迟")
    parser.add_argument("--profile", choices=["nominal", "degraded"], default="nominal", help="模拟画像")
    parser.add_argument("--gui", action="store_true", help="启动 Gradio GUI")
    parser.add_argument("--host", default="127.0.0.1", help="GUI 监听地址")
    parser.add_argument("--port", type=int, default=0, help="GUI 端口，0 表示随机可用端口")
    parser.add_argument("--no-browser", action="store_true", help="GUI 启动时不自动打开浏览器")
    parser.add_argument("--doctor", action="store_true", help="执行现场环境自检")
    parser.add_argument("--probe", action="store_true", help="自检时探测服务连通性")
    parser.add_argument("--version", action="version", version=f"ucm-accept {__version__}")
    return parser


def _apply_args(config: Config, args: argparse.Namespace) -> Config:
    if args.outputs:
        config.set("app.report_dir", str(Path(args.outputs).expanduser()))
    config.apply_endpoint(args.base_url, args.model, args.api_key)
    if args.sla_mode:
        config.set("sla.mode", args.sla_mode)
    return config


def _parse_selection(text: str) -> list[str]:
    """把 1,3-5 / perf,sla 之类的输入解析为去重且有序的测试项列表。"""
    tokens = [token for token in re.split(r"[,，、\s]+", str(text).strip()) if token]
    if not tokens:
        raise ValueError("测试项选择不能为空，例如 1,3-5 或 perf,sla")
    picked: set[str] = set()
    for token in tokens:
        for part in _expand_range(token):
            if part.isdigit():
                index = int(part)
                if not 1 <= index <= len(ITEM_KEYS):
                    raise ValueError(f"测试项编号超出范围：{index}（有效 1-{len(ITEM_KEYS)}）")
                picked.add(ITEM_KEYS[index - 1])
            elif part.lower() in ITEM_KEYS:
                picked.add(part.lower())
            else:
                known = "、".join(f"{index + 1}={key}" for index, key in enumerate(ITEM_KEYS))
                raise ValueError(f"未知测试项：{part}（可用：{known}）")
    ordered = [key for key in ITEM_KEYS if key in picked]
    return ordered


def _expand_range(token: str) -> list[str]:
    match = re.fullmatch(r"(\d+)-(\d+)", token)
    if not match:
        return [token]
    start, end = int(match.group(1)), int(match.group(2))
    if start > end:
        raise ValueError(f"编号区间写反：{token}（应为 起始-结束）")
    return [str(value) for value in range(start, end + 1)]


def _progress_printer():
    last_shown = {"fraction": -1.0}

    def callback(event: ProgressEvent) -> None:
        prefix = f"[{event.overall * 100:5.1f}%]"
        if event.level == "start":
            print()
            print(BOLD(f"▶ {event.message}"))
        elif event.level == "success":
            print(GREEN(f"  ✓ {event.message}"))
        elif event.level == "warning":
            print(YELLOW(f"  ! {event.message}"))
        elif event.level == "error":
            print(RED(f"  ✗ {event.message}"))
        elif event.level == "command":
            print(DIM(f"  {prefix} {shorten(event.message, 180)}"))
        else:
            if event.fraction - last_shown["fraction"] >= 0.2 or event.fraction >= 1.0:
                last_shown["fraction"] = event.fraction
                print(DIM(f"  {prefix} {shorten(event.message, 160)}"))
            elif event.message:
                print(DIM(f"  {prefix} {shorten(event.message, 160)}"))
        last_shown["fraction"] = max(last_shown["fraction"], event.fraction)

    return callback


def _print_report_summary(report: AcceptanceReport, report_path: Path | None = None) -> None:
    print()
    print(BOLD("=" * 74))
    color = {"通过": GREEN, "有条件通过": YELLOW, "不通过": RED}.get(report.conclusion, DIM)
    print(BOLD("验收结论：") + color(report.conclusion))
    for reason in report.reasons:
        print(f"  · {reason}")
    print(BOLD("=" * 74))
    rows = []
    for key in ITEM_KEYS:
        item = report.items.get(key)
        if not item:
            rows.append([str(ITEM_KEYS.index(key) + 1), ITEM_BY_KEY[key].name, "○ 待执行", "—", "—"])
            continue
        rows.append(
            [
                str(ITEM_KEYS.index(key) + 1),
                item.name,
                _status_text(item.status),
                human_duration(item.duration_s) if item.finished_at else "—",
                shorten(item.summary or item.error or "—", 46),
            ]
        )
    print(render_table(["#", "测试项", "状态", "耗时", "结论摘要"], rows, aligns=["center", "left", "center", "right", "left"]))
    if report_path:
        print(f"报告：{report_path}")
        print(f"原始数据：{report.output_dir}")
    print()


def _exit_code(report: AcceptanceReport | None) -> int:
    if report is None:
        return 3
    return {"通过": 0, "有条件通过": 0, "不通过": 2, "未完成": 3}.get(report.conclusion, 3)


def _save_extra(report_path: Path | None, args: argparse.Namespace) -> None:
    if not report_path or not report_path.exists():
        return
    target = None
    if args.report:
        destination = Path(args.report).expanduser()
        if destination.is_dir() or args.report.endswith(("/", "\\")):
            destination = destination / "report.html"
        if destination.suffix.lower() not in {".html", ".htm"}:
            destination = destination.with_suffix(".html")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(report_path, destination)
        target = destination
        print(f"HTML 报告已另存：{target}")
    if args.pdf:
        html_source = target or report_path
        try:
            pdf = export_pdf(html_source)
            print(f"PDF 报告已导出：{pdf}")
        except Exception as exc:  # noqa: BLE001
            print(YELLOW(f"PDF 导出失败：{exc}"))
            print(DIM("可使用浏览器打开 HTML 后打印为 PDF。"))


def _doctor(config: Config, args: argparse.Namespace) -> int:
    print(BOLD("UCM 验收工具 · 现场自检"))
    print(DIM(f"配置：{config.source or '内置默认配置'}"))
    results = run_doctor(config, probe=args.probe, mock=args.mock)
    marks = {"ok": GREEN("✓ 通过"), "warn": YELLOW("! 关注"), "fail": RED("✗ 失败"), "skip": DIM("– 跳过")}
    rows = [[item.name, marks.get(item.status, item.status), item.detail, item.hint] for item in results]
    print(render_table(["检查项", "状态", "说明", "建议"], rows, aligns=["left", "left", "left", "left"]))
    failures = sum(1 for item in results if item.status == "fail")
    warnings = sum(1 for item in results if item.status == "warn")
    if failures:
        print(RED(f"自检发现 {failures} 项失败、{warnings} 项需关注。"))
        return 1
    print(GREEN(f"自检通过（{warnings} 项需关注）。"))
    return 0


def _run_noninteractive(service: AcceptanceService, args: argparse.Namespace) -> int:
    progress = _progress_printer()
    item_status: Status | None = None
    try:
        if args.items and args.item:
            print(RED("--item 与 --items 不能同时使用"))
            return 2
        if args.all or args.items:
            keys = _parse_selection(args.items) if args.items else None
            report = service.run_all(progress=progress, keys=keys)
        else:
            item = service.run_item(
                args.item,
                scenario=args.scenario if args.item == "multiturn" else None,
                progress=progress,
            )
            item_status = item.status
            report = service.current_report()
            if report is None:
                raise UCMAcceptError("未生成验收报告")
            print(f"单项结果：{item.name} · {STATUS_LABELS[item.status]}")
    except KeyboardInterrupt:
        service.cancel()
        service.shutdown(timeout=10.0)
        print(YELLOW("\n已中断验收。报告与原始数据已保存到输出目录。"))
        return 130
    except UCMAcceptError as exc:
        print(RED(f"执行失败：{exc}"))
        return 3

    report_path = service.report_path()
    _print_report_summary(report, report_path)
    _save_extra(report_path, args)
    if item_status is not None and not args.all:
        if item_status in {Status.PASSED, Status.WARNING}:
            return 0
        return 2 if item_status == Status.FAILED else 3
    return _exit_code(report)


def _print_menu(service: AcceptanceService, args: argparse.Namespace) -> None:
    mode = "模拟演示（不产生真实请求）" if service.mode == "mock" else "EvalScope 实机"
    scope = "、".join(ITEM_BY_KEY[key].short for key in service.config.run_items())
    print()
    print(BOLD("=" * 52))
    print(BOLD(f" UCM 推理加速验收工具 (CLI)  v{__version__}"))
    print(f" 执行引擎: {CYAN(mode)}")
    print(BOLD("=" * 52))
    print(f"服务地址: {service.config.get('endpoint.base_url')}")
    print(f"模型:     {service.config.get('endpoint.model')}")
    print(f"报告目录: {service.outputs_root}")
    print(f"验收范围: {scope}")
    print(DIM("-" * 52))
    for key in ITEM_KEYS:
        meta = ITEM_BY_KEY[key]
        print(f" [{meta.order}] {meta.name}    {DIM(meta.eta)}")
    print(" [A] 执行所选测试项")
    print(" [T] 选择要执行的测试项")
    print(" [R] 查看最近报告")
    print(" [S] 修改服务地址 / 模型")
    print(" [Q] 退出")
    print(DIM("-" * 52))


def _show_latest(service: AcceptanceService) -> None:
    path = find_latest_report(service.outputs_root)
    if not path:
        print(YELLOW("暂无可查看的报告。"))
        return
    try:
        report = AcceptanceReport.load_json(path)
    except Exception as exc:  # noqa: BLE001
        print(RED(f"读取报告失败：{exc}"))
        return
    _print_report_summary(report, path.with_suffix(".html") if path.suffix == ".json" else path)


def _prompt(text: str, default: str = "") -> str:
    prompt = f"{text}" + (f"（默认 {default}）" if default else "") + "："
    try:
        value = input(prompt).strip()
    except EOFError:
        return default
    return value or default


def _choose_scenario(default: str = "dialogue") -> str:
    mapping = {"1": "dialogue", "2": "code", "3": "office"}
    print("   [1] 对话   [2] 代码   [3] 办公")
    choice = _prompt("请选择场景", default)
    return mapping.get(choice, choice if choice in {"dialogue", "code", "office"} else "dialogue")


def _interactive(service: AcceptanceService, args: argparse.Namespace) -> int:
    progress = _progress_printer()
    while True:
        _print_menu(service, args)
        try:
            choice = input("请选择: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not choice:
            continue
        normalized = choice.upper()
        if normalized == "Q":
            return 0
        looks_like_selection = bool(re.search(r"\d", choice)) or any(
            token in choice.lower() for token in ITEM_KEYS
        )
        if normalized == "A" or looks_like_selection:
            if normalized == "A":
                keys = service.config.run_items()
            else:
                try:
                    keys = _parse_selection(choice)
                except ValueError as exc:
                    print(RED(str(exc)))
                    continue
            scenario = None
            if "multiturn" in keys and len(keys) == 1:
                scenario = _choose_scenario(str(service.config.get("multiturn.scenario", "dialogue")))
            try:
                if len(keys) == 1:
                    service.run_item(keys[0], scenario=scenario, progress=progress)
                    report = service.current_report()
                else:
                    report = service.run_all(keys=keys, progress=progress)
                if report is not None:
                    _print_report_summary(report, service.report_path())
            except KeyboardInterrupt:
                service.cancel()
                service.shutdown(timeout=10.0)
                report = service.current_report()
                _print_report_summary(report, service.report_path())
                print(YELLOW("\n已中断当前验收。报告与原始数据已保存到输出目录。"))
            except UCMAcceptError as exc:
                print(RED(f"执行失败：{exc}"))
                if "EvalScope" in str(exc):
                    print(DIM("提示：可先运行 --doctor；演示可用 --mock --fast。"))
            except Exception as exc:  # noqa: BLE001
                print(RED(f"执行失败：{exc}"))
            try:
                input(DIM("按回车返回菜单…"))
            except EOFError:
                return 0
        elif normalized == "T":
            current = "、".join(str(service.config.run_items()))
            print(DIM(f"当前范围：{current}"))
            text = _prompt(
                "输入要执行的测试项（如 1,3-5 / perf,sla；回车保持不变）", ""
            )
            if not text:
                continue
            try:
                keys = _parse_selection(text)
            except ValueError as exc:
                print(RED(str(exc)))
                continue
            service.config.set("run.items", keys)
            print(GREEN("验收范围已更新：" + "、".join(ITEM_BY_KEY[key].name for key in keys)))
        elif normalized == "R":
            _show_latest(service)
        elif normalized == "S":
            base_url = _prompt("服务地址", str(service.config.get("endpoint.base_url")))
            model = _prompt("模型名称", str(service.config.get("endpoint.model")))
            try:
                service.update_endpoint(base_url=base_url, model=model)
                print(GREEN("服务配置已更新；下一项测试将开启新报告。"))
            except UCMAcceptError as exc:
                print(RED(str(exc)))
        else:
            print(YELLOW("无效选择，请输入 1-5 / 组合（如 1,3）/ A / T / R / S / Q。"))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        config = _apply_args(config, args)
    except ConfigError as exc:
        print(RED(f"配置错误：{exc}"))
        return 1
    except Exception as exc:  # noqa: BLE001
        print(RED(f"加载配置失败：{exc}"))
        return 1

    if args.save_config:
        try:
            target = config.save(args.save_config)
            print(GREEN(f"配置已保存：{target}"))
            print(DIM(f"下次使用：ucm-accept --config {target}"))
            return 0
        except OSError as exc:
            print(RED(f"保存配置失败：{exc}"))
            return 1

    if args.doctor:
        return _doctor(config, args)
    if args.gui:
        from .webapp import main as web_main

        return web_main(
            config=config,
            mode="mock" if args.mock else "live",
            mock_profile=args.profile,
            mock_delay_ms=0 if args.fast else None,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )

    if not (args.all or args.item or args.items):
        if not sys.stdin.isatty():
            parser.print_help()
            print(RED("\n非交互环境请使用 --all、--items 或 --item，或显式 --gui。"))
            return 2

    service = AcceptanceService(
        config,
        mode="mock" if args.mock else "live",
        mock_profile=args.profile,
        mock_delay_ms=0 if args.fast else None,
    )
    # SIGTERM（终端关闭 / kill）：停止当前任务、终止子进程并落盘报告
    def _graceful(signum: int, _frame: object) -> None:
        service.shutdown(timeout=10.0)
        print(YELLOW("\n收到退出信号，验收已中断，报告与原始数据已保存到输出目录。"))
        raise SystemExit(130)

    for _sig in (signal.SIGTERM,):
        try:
            signal.signal(_sig, _graceful)
        except (ValueError, OSError):  # noqa: BLE001 - 非主线程等场景忽略
            pass

    if args.all or args.item or args.items:
        return _run_noninteractive(service, args)
    return _interactive(service, args)


if __name__ == "__main__":
    raise SystemExit(main())
