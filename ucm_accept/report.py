from __future__ import annotations

import html
import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .errors import PDFExportError
from .models import (
    ITEM_KEYS,
    STATUS_LABELS,
    AcceptanceReport,
    ItemResult,
    Status,
    human_duration,
    percent,
)

CSS = r"""
:root{
  --ink:#1d1d1f; --muted:#86868b; --line:#e8e8ed; --bg:#f5f5f7; --card:#ffffff;
  --brand:#1d1d1f; --brand2:#0071e3; --accent:#0071e3; --accent-soft:#e5f0ff;
  --amber:#9a6a00; --amber-soft:#fff6e0; --red:#c21b17; --red-soft:#ffecec;
  --green:#1d7a3d; --green-soft:#e8f9ee;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","SF Pro Text","Segoe UI","Microsoft YaHei",
  "Noto Sans CJK SC","PingFang SC","Hiragino Sans GB",sans-serif;font-size:14px;line-height:1.65;
  -webkit-font-smoothing:antialiased}
a{color:var(--brand2)}
code,pre{font-family:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace}
/* 长命令自动换行：避免单行命令把报告撑出横向滚动条 */
pre.code{white-space:pre-wrap;word-break:break-all;overflow-wrap:anywhere}
.hero{background:var(--card);color:var(--ink);
  padding:36px 40px 32px;position:relative;overflow:hidden;border-bottom:1px solid var(--line)}
.hero .wrap{max-width:1180px;margin:0 auto;position:relative;z-index:1}
.eyebrow{font-size:12px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);font-weight:600}
.hero h1{margin:10px 0 10px;font-size:32px;line-height:1.15;font-weight:700;letter-spacing:-.02em}
.hero .sub{color:var(--muted);max-width:820px;margin:0;font-size:14px}
.hero-meta{display:flex;flex-wrap:wrap;gap:8px 22px;margin-top:18px;color:var(--muted);font-size:12.5px}
.hero-meta b{color:var(--ink);font-weight:600}
.mode-pill{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);
  background:var(--accent-soft);border-radius:999px;padding:3px 11px;font-size:12px;color:#0062c4}
main{max-width:1180px;margin:0 auto;padding:24px 28px 56px}
.banner{border-radius:12px;padding:14px 18px;margin:18px 0;border:1px solid;font-size:13.5px}
.banner.warn{background:var(--amber-soft);border-color:#f3d19b;color:#7c4a03}
.banner.info{background:#eff6ff;border-color:#bfdbfe;color:#1e3a8a}
.banner strong{font-weight:700}
.verdict{display:grid;grid-template-columns:176px 1fr;gap:24px;align-items:center;
  background:var(--card);border:none;border-radius:24px;padding:24px 28px;
  margin-top:0;position:relative;box-shadow:0 4px 12px rgba(0,0,0,.04)}
.verdict-badge{border-radius:16px;padding:18px 12px;text-align:center;color:#fff;
  background:var(--accent);box-shadow:0 4px 14px rgba(0,113,227,.30)}
.verdict-badge .label{font-size:11px;letter-spacing:.16em;opacity:.85}
.verdict-badge .value{font-size:27px;font-weight:800;line-height:1.25;margin-top:3px}
.verdict h2{font-size:17px;margin:0 0 6px}
.verdict ul{margin:4px 0 0;padding-left:20px;color:var(--muted)}
.verdict li{margin:2px 0}
.section{background:var(--card);border:none;border-radius:24px;padding:24px 28px;
  margin-top:20px;box-shadow:0 4px 12px rgba(0,0,0,.04);page-break-inside:avoid}
.section>h2{font-size:19px;margin:0 0 4px;display:flex;align-items:center;gap:9px}
.section>h2 .num{font-size:12px;color:#fff;background:var(--blue,#0071e3);border-radius:999px;
  min-width:24px;height:24px;display:inline-flex;align-items:center;justify-content:center;font-weight:700}
.section .lead{color:var(--muted);margin:0 0 16px;font-size:13px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}
.kv{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--bg)}
.kv .k{color:var(--muted);font-size:11.5px;letter-spacing:.03em}
.kv .v{font-weight:650;font-size:15px;margin-top:2px;overflow-wrap:anywhere}
.kv.endpoint{grid-column:span 2}
.kv.endpoint .v{font-size:13px}
.metric{background:var(--bg);border:1px solid var(--line);
  border-radius:12px;padding:14px 16px}
.metric .k{font-size:12px;color:var(--muted)}
.metric .v{font-size:24px;font-weight:750;color:var(--brand2);margin-top:2px;line-height:1.2}
.metric .s{font-size:11.5px;color:var(--muted);margin-top:3px}
table{width:100%;border-collapse:collapse;margin:10px 0 2px;font-size:12.8px}
th,td{border-bottom:1px solid var(--line);padding:8px 9px;text-align:left;vertical-align:top}
th{background:var(--bg);color:#6e6e73;font-weight:600;white-space:nowrap}
tbody tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.table-wrap{overflow-x:auto}
.pill{display:inline-flex;align-items:center;gap:5px;border-radius:999px;padding:2px 9px;
  font-size:11.5px;font-weight:650;border:1px solid}
.pill.passed{color:var(--green);background:var(--green-soft);border-color:#b7e4c3}
.pill.warning{color:var(--amber);background:var(--amber-soft);border-color:#f2d19a}
.pill.failed{color:var(--red);background:var(--red-soft);border-color:#f2c0c0}
.pill.pending,.pill.skipped{color:#475569;background:#f1f5f9;border-color:#dbe3ec}
.pill.running{color:#1d4ed8;background:#eff6ff;border-color:#bfdbfe}
.chart{margin:14px 0 4px;background:#fbfcfe;border:1px solid var(--line);border-radius:12px;padding:12px}
.chart h3{margin:0 0 4px;font-size:13.5px;color:#334155}
.chart p{margin:0 0 4px;color:var(--muted);font-size:11.5px}
.chart svg{width:100%;height:auto;display:block}
.charts-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.callout{border-left:4px solid var(--amber);background:var(--amber-soft);padding:10px 14px;
  border-radius:0 9px 9px 0;margin:12px 0;color:#6b4103;font-size:13px}
.callout.bad{border-color:var(--red);background:var(--red-soft);color:#7f1d1d}
.callout.good{border-color:var(--green);background:var(--green-soft);color:#14532d}
.callout.info{border-color:#0071e3;background:#e5f0ff;color:#0062c4}
.details{border:1px solid var(--line);border-radius:10px;margin:10px 0;background:#fcfdff}
.details summary{cursor:pointer;padding:10px 14px;font-weight:600;color:#334155}
.details .body{padding:2px 14px 14px;border-top:1px solid var(--line)}
.raw-list{list-style:none;margin:8px 0 0;padding:0}
.raw-list li{display:flex;gap:10px;justify-content:space-between;border-bottom:1px dashed var(--line);
  padding:7px 0;font-size:12.5px;flex-wrap:wrap}
.raw-list li:last-child{border-bottom:none}
.raw-list .path{color:#334155;word-break:break-all}
.muted{color:var(--muted)}
.small{font-size:12px}
.right{text-align:right}
footer{max-width:1180px;margin:0 auto;padding:0 28px 40px;color:var(--muted);font-size:12px}
.print-btn{border:1px solid var(--line);background:#fff;color:var(--brand2);border-radius:9px;
  padding:7px 13px;font-size:12.5px;cursor:pointer}
.print-btn:hover{background:#f5f9ff}
.header-actions{display:flex;justify-content:flex-end;margin:14px 0 0}
.empty{padding:16px;border:1px dashed var(--line);border-radius:10px;color:var(--muted);background:#fbfcfe}
@media (max-width:800px){
  .hero{padding:26px 20px}
  main{padding:18px 14px 40px}
  .verdict{grid-template-columns:1fr;margin-top:0}
  .charts-2{grid-template-columns:1fr}
  .kv.endpoint{grid-column:span 1}
  .section{padding:18px 15px}
}
@media print{
  body{background:#fff;font-size:11.5px}
  .hero{padding:24px 26px}
  .hero:after{display:none}
  main{padding:16px 0 0;max-width:none}
  .verdict{margin-top:0;box-shadow:none}
  .section{box-shadow:none;break-inside:avoid;margin-top:14px;border-color:#cbd5e1}
  .print-btn,.no-print{display:none!important}
  .details .body{display:block!important}
  footer{max-width:none}
  *{-webkit-print-color-adjust:exact;print-color-adjust:exact}
}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt(value: float | None, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:.{digits}f}{suffix}"


def _fmt_int(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return esc(value)


def _status_pill(status: Status | str) -> str:
    if isinstance(status, str):
        try:
            status = Status(status)
        except ValueError:
            status = Status.PENDING
    return f'<span class="pill {status.value}">{esc(STATUS_LABELS[status])}</span>'


def _parse_dt(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _total_duration(report: AcceptanceReport) -> str:
    start = _parse_dt(report.started_at)
    end = _parse_dt(report.finished_at)
    if start and end:
        return human_duration((end - start).total_seconds())
    return "—"


def _time_text(value: str) -> str:
    dt = _parse_dt(value)
    if not dt:
        return esc(value or "—")
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------- charts
def _svg_text(x: float, y: float, text: str, **attrs: Any) -> str:
    options = {"font-size": "11", "fill": "#64748b", "text-anchor": "middle"}
    options.update(attrs)
    attr_text = " ".join(f'{key}="{esc(value)}"' for key, value in options.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" {attr_text}>{esc(text)}</text>'


def _axis_label(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value / 1000:g}k"
    if abs(value) >= 100:
        return f"{value:.0f}"
    if abs(value) >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def svg_line_chart(
    points: Sequence[Any],
    value_fn,
    *,
    title: str,
    unit: str,
    color: str = "#2563eb",
    threshold: float | None = None,
    threshold_label: str = "",
    width: int = 760,
    height: int = 250,
) -> str:
    """单指标折线图；单点时退化为点图，避免出现误导性的斜率。"""
    values = []
    for point in points:
        value = value_fn(point)
        if value is not None:
            concurrency = point.get("concurrency") if isinstance(point, dict) else getattr(point, "concurrency", None)
            if concurrency is not None:
                values.append((float(concurrency), float(value)))
    if not values:
        return f'<div class="empty">暂无可绘制的{esc(title)}数据</div>'
    values.sort(key=lambda item: item[0])
    left, right, top, bottom = 58, 22, 18, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    xs = [item[0] for item in values]
    ys = [item[1] for item in values]
    x_min, x_max = min(xs), max(xs)
    if x_max == x_min:
        x_min, x_max = max(0, x_min * 0.8), x_max * 1.2 or 1
    y_max = max(ys) * 1.12 or 1
    if threshold is not None:
        y_max = max(y_max, threshold * 1.18)
    y_min = 0.0

    def sx(value: float) -> float:
        return left + (value - x_min) / (x_max - x_min) * plot_w

    def sy(value: float) -> float:
        return top + plot_h - (value - y_min) / (y_max - y_min) * plot_h

    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{esc(title)}">',
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="#ffffff" rx="6"/>',
    ]
    for tick in range(5):
        value = y_min + (y_max - y_min) * tick / 4
        y = sy(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e6ebf2"/>')
        parts.append(_svg_text(left - 8, y + 3.5, _axis_label(value), **{"text-anchor": "end"}))
    for tick in range(5):
        value = x_min + (x_max - x_min) * tick / 4
        x = sx(value)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_h}" stroke="#f0f3f8"/>')
        parts.append(_svg_text(x, height - 22, f"{value:.0f}"))
    if threshold is not None:
        y = sy(threshold)
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" '
            f'stroke="#d97706" stroke-width="1.4" stroke-dasharray="6 4"/>'
        )
        label = threshold_label or f"目标 {threshold:g}{unit}"
        parts.append(_svg_text(width - right - 4, y - 7, label, **{"text-anchor": "end", "fill": "#b45309"}))
    if len(values) > 1:
        path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in values)
        parts.append(f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2.2"/>')
    for x, y in values:
        parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4.2" fill="#fff" stroke="{color}" stroke-width="2"/>')
        label = f"{y:.2f}" if abs(y) < 10 else f"{y:.1f}"
        parts.append(_svg_text(sx(x), sy(y) - 9, label, fill=color, **{"font-weight": "600"}))
    parts.append(_svg_text(left + plot_w / 2, height - 4, "并发", fill="#94a3b8"))
    parts.append(_svg_text(13, top + plot_h / 2, unit, fill="#94a3b8", transform=f"rotate(-90 13 {top + plot_h / 2})"))
    parts.append("</svg>")
    return "".join(parts)


def svg_accuracy_bars(
    labels: Sequence[str],
    run1: Sequence[float | None],
    run2: Sequence[float | None],
    *,
    width: int = 760,
    height: int = 250,
) -> str:
    if not labels:
        return '<div class="empty">暂无精度数据</div>'
    left, right, top, bottom = 58, 22, 18, 46
    plot_w, plot_h = width - left - right, height - top - bottom
    colors = ["#0071e3", "#ff9500"]
    groups = len(labels)
    group_w = plot_w / max(1, groups)
    bar_w = min(30, group_w * 0.26)
    parts = [
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="精度两次结果对比">',
        '<rect width="100%" height="100%" fill="#ffffff" rx="6"/>',
    ]
    for tick in range(5):
        value = tick / 4
        y = top + plot_h - value * plot_h
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e6ebf2"/>')
        parts.append(_svg_text(left - 8, y + 3.5, f"{value*100:.0f}%", **{"text-anchor": "end"}))
    for index, label in enumerate(labels):
        center = left + group_w * (index + 0.5)
        parts.append(_svg_text(center, height - 20, label))
        for series_index, series in enumerate((run1, run2)):
            value = series[index] if index < len(series) else None
            if value is None:
                continue
            value = max(0.0, min(1.0, float(value)))
            x = center - bar_w - 2 + series_index * (bar_w + 4)
            y = top + plot_h - value * plot_h
            parts.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{top+plot_h-y:.1f}" '
                f'rx="3" fill="{colors[series_index]}" opacity="0.88"/>'
            )
            parts.append(_svg_text(x + bar_w / 2, y - 5, f"{value*100:.1f}", fill=colors[series_index]))
    legend_y = height - 4
    for index, name in enumerate(("第 1 遍", "第 2 遍")):
        x = left + index * 86
        parts.append(f'<rect x="{x}" y="{legend_y-9}" width="9" height="9" rx="2" fill="{colors[index]}"/>')
        parts.append(_svg_text(x + 13, legend_y, name, **{"text-anchor": "start"}))
    parts.append("</svg>")
    return "".join(parts)


# -------------------------------------------------------------------- sections
def _overview(report: AcceptanceReport) -> str:
    items = []
    for key in ITEM_KEYS:
        item = report.items.get(key)
        if item:
            items.append(
                "<tr>"
                f"<td>{esc(item.meta.name)}</td>"
                f"<td>{_status_pill(item.status)}</td>"
                f"<td class='num'>{esc(human_duration(item.duration_s))}</td>"
                f"<td>{esc(item.summary or item.error or '—')}</td>"
                "</tr>"
            )
        else:
            items.append(
                "<tr>"
                f"<td>{esc(ITEM_KEYS.index(key) + 1)}. {esc(_item_name(key))}</td>"
                "<td><span class='pill pending'>待执行</span></td>"
                "<td class='num'>—</td><td class='muted'>尚未执行</td></tr>"
            )
    endpoint = report.endpoint or {}
    mode_text = "实机验收" if report.mode == "live" else "模拟演示"
    scope = report.selected_keys()
    scope_text = "全部五项" if len(scope) == len(ITEM_KEYS) else "、".join(_item_name(key) for key in scope)
    scope_cell = "" if len(scope) == len(ITEM_KEYS) else (
        f'<div class="kv"><div class="k">执行范围</div>'
        f'<div class="v">{esc(scope_text)}（部分执行，{len(scope)}/{len(ITEM_KEYS)} 项）</div></div>'
    )
    return f"""
<section class="section" id="overview">
  <h2><span class="num">01</span>验收概览</h2>
  <p class="lead">被测服务信息、执行方式与各项测试结论。单项详情见后续章节。</p>
  <div class="grid">
    <div class="kv endpoint"><div class="k">服务地址</div><div class="v">{esc(endpoint.get('base_url', '—'))}</div></div>
    <div class="kv"><div class="k">模型名称</div><div class="v">{esc(endpoint.get('model', '—'))}</div></div>
    <div class="kv"><div class="k">执行方式</div><div class="v">{esc(mode_text)}</div></div>
    {scope_cell}
    <div class="kv"><div class="k">开始时间</div><div class="v">{_time_text(report.started_at)}</div></div>
    <div class="kv"><div class="k">结束时间</div><div class="v">{_time_text(report.finished_at)}</div></div>
    <div class="kv"><div class="k">总耗时</div><div class="v">{esc(_total_duration(report))}</div></div>
  </div>
  <div class="table-wrap">
  <table>
    <thead><tr><th style="width:26%">测试项</th><th style="width:12%">状态</th><th class="num" style="width:13%">耗时</th><th>结论摘要</th></tr></thead>
    <tbody>{''.join(items)}</tbody>
  </table>
  </div>
</section>
"""


def _item_name(key: str) -> str:
    from .models import ITEM_BY_KEY

    return ITEM_BY_KEY[key].name if key in ITEM_BY_KEY else key


def _empty_item(item: ItemResult | None) -> str:
    if item is None:
        return '<div class="empty">该项尚未执行。</div>'
    if item.status == Status.SKIPPED:
        return f'<div class="empty">{esc(item.summary or "该项已取消")}</div>'
    if item.error and not item.detail:
        return f'<div class="callout bad">{esc(item.error)}</div>'
    return '<div class="empty">该项暂无详细结果。</div>'


def _command_block(item: ItemResult | None) -> str:
    if not item:
        return ""
    command = (item.detail or {}).get("command")
    if not command:
        return ""
    return (
        '<details class="details"><summary>EvalScope 执行命令（可原样复现）</summary>'
        f'<div class="body"><pre class="code">{esc(command)}</pre>'
        '<p class="small muted">命令中的 API Key 已脱敏；原始输出目录见报告末尾“原始数据与复核”。</p>'
        "</div></details>"
    )


def _accuracy_commands(item: ItemResult | None) -> str:
    if not item:
        return ""
    commands = (item.detail or {}).get("commands") or []
    if not commands:
        return ""
    blocks = "".join(
        f"<div class='small muted' style='margin-top:10px'>{esc(entry.get('label', ''))} · "
        f"第 {esc(entry.get('run', ''))} 遍</div><pre class='code'>{esc(entry.get('command', ''))}</pre>"
        for entry in commands
    )
    return (
        '<details class="details"><summary>EvalScope 执行命令（逐数据集、逐遍可复现）</summary>'
        f'<div class="body">{blocks}<p class="small muted">API Key 已脱敏。</p></div></details>'
    )


def _artifact_link(report: AcceptanceReport, label: str, path: str) -> str:
    from urllib.parse import quote

    target = Path(report.output_dir) / path if report.output_dir and not Path(path).is_absolute() else Path(path)
    suffix = target.suffix.lower()
    if target.is_file() and suffix in {".html", ".json", ".txt", ".log", ".md"}:
        href = quote(path.replace(os.sep, "/"), safe="/._-")
        return f'<a href="{esc(href)}" target="_blank" rel="noopener">{esc(label)}</a>'
    return esc(label)


def _perf_section(report: AcceptanceReport, item: ItemResult | None) -> str:
    body = _empty_item(item)
    if item and item.detail and item.detail.get("points"):
        points = item.detail["points"]
        _sla_cfg = (report.config_snapshot.get("sla", {}) or {})
        _thr = (_sla_cfg.get("thresholds") or {})
        ttft_target = _thr.get("avg_ttft_s")  # 秒
        tpot_target = _thr.get("avg_tpot_ms")  # 毫秒
        rows = []
        for point in points:
            rows.append(
                "<tr>"
                f"<td class='num'>{_fmt_int(point.get('concurrency'))}</td>"
                f"<td class='num'>{_fmt_int(point.get('success_requests'))}/{_fmt_int(point.get('requests'))}</td>"
                f"<td class='num'>{_fmt(point.get('throughput_tok_s'), 1)}</td>"
                f"<td class='num'>{_fmt(point.get('ttft_avg_ms'), 1)}</td>"
                f"<td class='num'>{_fmt(point.get('ttft_p90_ms'), 1)}</td>"
                f"<td class='num'>{_fmt(point.get('ttft_p99_ms'), 1)}</td>"
                f"<td class='num'>{_fmt(point.get('tpot_avg_ms'), 2)}</td>"
                f"<td class='num'>{_fmt(point.get('tpot_p90_ms'), 2)}</td>"
                f"<td class='num'>{_fmt(point.get('tpot_p99_ms'), 2)}</td>"
                f"<td class='num'>{_fmt(point.get('latency_p90_s'), 2)}</td>"
                f"<td class='num'>{percent(point.get('success_requests', 0) / point.get('requests', 1) if point.get('requests') else 1)}</td>"
                "</tr>"
            )
        charts = (
            '<div class="charts-2">'
            f'<div class="chart"><h3>TTFT.P90（秒）</h3><p>首字延迟 P90 随并发变化；虚线为 SLA 的 TTFT avg 目标（如配置）</p>'
            f'{svg_line_chart(points, lambda p: (p.get("ttft_p90_ms") or 0) / 1000, title="TTFT.P90", unit="秒", threshold=(ttft_target / 1000 if ttft_target else None), threshold_label=f"目标 {ttft_target/1000:.1f}s" if ttft_target else "", color="#0071e3")}</div>'
            f'<div class="chart"><h3>TPOT.P90（毫秒）</h3><p>逐字延迟 P90 随并发变化；虚线为 SLA 的 TPOT avg 目标（如配置）</p>'
            f'{svg_line_chart(points, lambda p: p.get("tpot_p90_ms"), title="TPOT.P90", unit="毫秒", threshold=tpot_target, threshold_label=f"目标 {tpot_target:.0f}ms" if tpot_target else "", color="#ff9500")}</div>'
            "</div>"
            f'<div class="chart"><h3>输出吞吐（tok/s）</h3><p>不同并发下的稳态输出吞吐</p>'
            f'{svg_line_chart(points, lambda p: p.get("throughput_tok_s"), title="输出吞吐", unit="tok/s", color="#7c3aed")}</div>'
        )
        body = f"""
        <div class="grid">
          <div class="metric"><div class="k">验收并发点</div><div class="v">{_fmt_int(max(p.get('concurrency', 0) for p in points))}</div><div class="s">并发数</div></div>
          <div class="metric"><div class="k">输出吞吐</div><div class="v">{_fmt(max(p.get('throughput_tok_s', 0) for p in points), 0)}</div><div class="s">tok/s</div></div>
          <div class="metric"><div class="k">TTFT.P90</div><div class="v">{_fmt(max(p.get('ttft_p90_ms', 0) for p in points) / 1000, 2)}</div><div class="s">秒</div></div>
          <div class="metric"><div class="k">TPOT.P90</div><div class="v">{_fmt(max(p.get('tpot_p90_ms', 0) for p in points), 1)}</div><div class="s">毫秒</div></div>
        </div>
        <div class="table-wrap">
        <table>
          <thead><tr><th class="num">并发</th><th class="num">成功/请求</th><th class="num">吞吐 tok/s</th>
          <th class="num">TTFT 均值</th><th class="num">TTFT P90</th><th class="num">TTFT P99</th>
          <th class="num">TPOT 均值</th><th class="num">TPOT P90</th><th class="num">TPOT P99</th>
          <th class="num">总延迟 P90(s)</th><th class="num">成功率</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table></div>
        {charts}
        {_command_block(item)}
        """
    return f"""
<section class="section" id="perf">
  <h2><span class="num">02</span>基础性能 <span class="small muted" style="font-weight:400">EvalScope perf</span></h2>
  <p class="lead">多组配置逐组执行 EvalScope perf 并聚合；表格中 TTFT/TPOT 单位为毫秒。</p>
  {body}
</section>
"""


def _multiturn_section(report: AcceptanceReport, item: ItemResult | None) -> str:
    body = _empty_item(item)
    if item and item.detail:
        detail = item.detail
        completion = (
            detail.get("load_success_conversations", 0) / detail.get("load_total_conversations", 1)
            if detail.get("load_total_conversations")
            else 0
        )
        body = f"""
        <div class="grid">
          <div class="metric"><div class="k">场景</div><div class="v" style="font-size:20px">{esc(detail.get('label', '—'))}</div><div class="s">{esc(detail.get('scenario', ''))}</div></div>
          <div class="metric"><div class="k">会话完成率</div><div class="v">{percent(completion)}</div><div class="s">{esc(detail.get('load_success_conversations', 0))}/{esc(detail.get('load_total_conversations', 0))} 组</div></div>
          <div class="metric"><div class="k">总轮次</div><div class="v">{_fmt_int(detail.get('total_turns'))}</div><div class="s">{esc(detail.get('parallel', 0))} 并发</div></div>
          <div class="metric"><div class="k">平均请求延迟</div><div class="v">{_fmt((detail.get('avg_latency_ms') or 0) / 1000, 2)}</div><div class="s">秒</div></div>
          <div class="metric"><div class="k">缓存命中</div><div class="v">{percent(detail.get('cache_hit_rate'))}</div><div class="s">历史 token 占比</div></div>
        </div>
        <div class="callout info">{esc(detail.get('note') or '')}</div>
        {_command_block(item)}
        """
    return f"""
<section class="section" id="multiturn">
  <h2><span class="num">03</span>多轮对话 <span class="small muted" style="font-weight:400">EvalScope multi-turn</span></h2>
  <p class="lead">由 EvalScope <code>perf --multi-turn</code> 直接执行“对话 / 代码 / 办公”预设，统计上下文增长下的会话完成率、延迟与缓存命中。</p>
  {body}
</section>
"""


def _sla_target_text(target: dict) -> str:
    """SLA 目标摘要（avg / P99 任意档位组合）。"""
    labels = {
        "avg_ttft_s": ("TTFT avg", 1000.0, "s"),
        "avg_tpot_ms": ("TPOT avg", 1.0, "ms"),
        "p99_ttft_s": ("TTFT P99", 1000.0, "s"),
        "p99_tpot_ms": ("TPOT P99", 1.0, "ms"),
    }
    thresholds = target.get("thresholds") or {}
    parts = [
        f"{label} ≤ {_fmt(thresholds.get(key), 2)}{unit}"
        for key, (label, scale, unit) in labels.items()
        if thresholds.get(key)
    ]
    return "；".join(parts) or "未配置"


def _sla_section(report: AcceptanceReport, item: ItemResult | None) -> str:
    body = _empty_item(item)
    strategy_hint = "EvalScope 原生 · avg / P99"
    mode_note = (
        "调用 EvalScope 原生 <code>--sla-auto-tune</code>，按 avg / P99 阈值搜索满足 SLA 的最高并发。"
    )
    if item and item.detail:
        detail = item.detail
        target = detail.get("target") or {}
        probes = detail.get("probes") or []
        strategy = detail.get("strategy", "native")
        target_point = next((p for p in probes if p.get("concurrency") == target.get("concurrency")), None)
        if target_point is None and strategy == "auto_tune" and detail.get("recommended_concurrency") == target.get("concurrency"):
            target_point = detail.get("best")
        if detail.get("target_met"):
            status, cls, color = "达标", "good", "#15803d"
        elif detail.get("satisfiable"):
            status, cls, color = "有条件", "info", "#b45309"
        else:
            status, cls, color = "不达标", "bad", "#b91c1c"
        # SLA 阈值 → (实测字段, 换算到显示单位, 单位名)；与 models.PerfPoint 一致
        _metrics = [
            ("avg_ttft_s", "TTFT avg", "ttft_avg_ms", 1000.0, "s"),
            ("avg_tpot_ms", "TPOT avg", "tpot_avg_ms", 1.0, "ms"),
            ("p99_ttft_s", "TTFT P99", "ttft_p99_ms", 1000.0, "s"),
            ("p99_tpot_ms", "TPOT P99", "tpot_p99_ms", 1.0, "ms"),
        ]
        thresholds = target.get("thresholds") or {}
        active = [(key, label, field, scale, unit) for key, label, field, scale, unit in _metrics
                  if thresholds.get(key)]

        def verdict(value: float | None, limit: float | None) -> str:
            if value is None or limit is None:
                return '<span class="pill pending">—</span>'
            return ('<span class="pill passed">通过</span>'
                    if value <= limit else '<span class="pill failed">未通过</span>')

        compare_rows = "".join(
            "<tr>"
            f"<td>{label}</td>"
            f"<td class='num'>≤ {_fmt(thresholds.get(key), 2)} {unit}</td>"
            f"<td class='num'>{_fmt((target_point.get(field) if target_point else None) / scale, 2) if target_point and target_point.get(field) is not None else '—'} {unit}</td>"
            f"<td>{verdict((target_point.get(field) / scale) if target_point and target_point.get(field) is not None else None, thresholds.get(key))}</td>"
            "</tr>"
            for key, label, field, scale, unit in active
        )
        gap_html = ""
        if detail.get("gaps"):
            gap_html = f'<div class="callout bad"><strong>差距：</strong>{esc("；".join(detail["gaps"]))}</div>'
        probe_rows = []
        for point in probes:
            ok = all(
                (point.get(field) or 0) / scale <= thresholds.get(key)
                for key, _label, field, scale, _unit in active
                if thresholds.get(key)
            )
            verdict_html = (
                '<span class="pill passed">满足</span>' if ok else '<span class="pill failed">不满足</span>'
            )
            probe_rows.append(
                "<tr>"
                f"<td class='num'>{_fmt_int(point.get('concurrency'))}</td>"
                f"<td class='num'>{_fmt(point.get('ttft_avg_ms'), 1)} / {_fmt(point.get('ttft_p99_ms'), 1)}</td>"
                f"<td class='num'>{_fmt(point.get('tpot_avg_ms'), 1)} / {_fmt(point.get('tpot_p99_ms'), 1)}</td>"
                f"<td class='num'>{_fmt(point.get('throughput_tok_s'), 0)}</td>"
                f"<td>{verdict_html}</td>"
                "</tr>"
            )
        charts = ""
        if probes and active:
            cells = ""
            for (key, label, field, scale, unit), color in zip(
                active, ["#0071e3", "#ff9500", "#af52de", "#30b0c7"], strict=False
            ):
                cells += (
                    f'<div class="chart"><h3>{label}（{unit}）</h3>'
                    f'{svg_line_chart(probes, lambda p, f=field, s=scale: (p.get(f) or 0)/s, title=label, unit=unit, threshold=(thresholds.get(key) or 0), threshold_label="目标", color=color)}</div>'
                )
            charts = '<div class="charts-2">' + cells + "</div>"
        recommended = detail.get("recommended_concurrency")
        body = f"""
        <div class="grid">
          <div class="metric"><div class="k">结论</div><div class="v" style="font-size:21px;color:{color}">{esc(status)}</div><div class="s">{'目标并发下满足' if detail.get('target_met') else '目标并发下未满足'}</div></div>
          <div class="metric"><div class="k">目标并发</div><div class="v">{_fmt_int(target.get('concurrency'))}</div><div class="s">{esc(target.get('input_length'))} 输入 · {esc(target.get('output_tokens'))} 输出</div></div>
          <div class="metric"><div class="k">建议并发</div><div class="v">{_fmt_int(recommended)}</div><div class="s">EvalScope 原生搜索结果</div></div>
        </div>
        <div class="table-wrap">
        <table><thead><tr><th>指标</th><th class="num">目标（≤）</th><th class="num">实测（{_fmt_int(target.get('concurrency'))} 并发）</th><th>判定</th></tr></thead>
        <tbody>{compare_rows or '<tr><td colspan="4" class="muted">未配置阈值</td></tr>'}</tbody></table></div>
        <div class="callout {cls}">{esc(detail.get('note') or '')}</div>
        {gap_html}
        {_command_block(item)}
        <div class="table-wrap">
        <table><thead><tr><th class="num">并发</th><th class="num">TTFT avg/P99 (ms)</th><th class="num">TPOT avg/P99 (ms)</th><th class="num">吞吐 (tok/s)</th><th>判定</th></tr></thead>
        <tbody>{''.join(probe_rows) or '<tr><td colspan="5" class="muted">无有效探测点</td></tr>'}</tbody></table></div>
        {charts}
        """
    return f"""
<section class="section" id="sla">
  <h2><span class="num">04</span>SLA 自动调优 <span class="small muted" style="font-weight:400">{strategy_hint}</span></h2>
  <p class="lead">{mode_note}</p>
  {body}
</section>
"""


def _accuracy_section(report: AcceptanceReport, item: ItemResult | None) -> str:
    body = _empty_item(item)
    if item and item.detail and item.detail.get("datasets"):
        datasets = item.detail["datasets"]
        rows = []
        for dataset in datasets:
            runs = dataset.get("runs") or []
            run_cells = []
            for index in range(2):
                run = runs[index] if index < len(runs) else None
                if run is None:
                    run_cells.append("<td class='num'>—</td>")
                elif run.get("score") is None:
                    run_cells.append("<td class='num' title='执行失败'>—</td>")
                else:
                    run_cells.append(f"<td class='num'>{_fmt(run.get('score') * 100, 2)}%</td>")
            delta = dataset.get("delta")
            stable = dataset.get("stable")
            verdict = (
                '<span class="pill passed">稳定</span>'
                if stable
                else '<span class="pill warning">超差</span>'
            )
            rows.append(
                "<tr>"
                f"<td><strong>{esc(dataset.get('label'))}</strong><div class='small muted'>{esc(dataset.get('dataset'))}"
                f"{(' · ' + esc(dataset.get('subset'))) if dataset.get('subset') else ''}</div></td>"
                f"{''.join(run_cells)}"
                f"<td class='num'>{_fmt((dataset.get('mean') or 0) * 100, 2)}%</td>"
                f"<td class='num'>{_fmt((dataset.get('std') or 0) * 100, 2)}</td>"
                f"<td class='num'>{_fmt((delta or 0) * 100, 2)} pp</td>"
                f"<td>{verdict}</td>"
                "</tr>"
            )
        labels = [str(dataset.get("label")) for dataset in datasets]
        run1 = [((dataset.get("runs") or [{}])[0].get("score") if dataset.get("runs") else None) for dataset in datasets]
        run2 = [
            ((dataset.get("runs") or [None, None])[1].get("score") if len(dataset.get("runs") or []) > 1 else None)
            for dataset in datasets
        ]
        body = f"""
        <div class="grid">
          <div class="metric"><div class="k">数据集</div><div class="v">{len(datasets)}</div><div class="s">GPQA / GSM8K / LongBench / BFCL</div></div>
          <div class="metric"><div class="k">执行遍数</div><div class="v">{esc(item.detail.get('runs_per_dataset', 2))}</div><div class="s">每项独立执行</div></div>
          <div class="metric"><div class="k">一致性</div><div class="v" style="font-size:20px;color:{'#15803d' if item.detail.get('all_stable') else '#b45309'}">{'稳定' if item.detail.get('all_stable') else '需关注'}</div><div class="s">阈值 {_fmt((item.detail.get('stability_threshold') or 0)*100, 1)} pp</div></div>
          <div class="metric"><div class="k">最大偏差</div><div class="v">{_fmt(max(((d.get('delta') or 0) for d in datasets), default=0)*100, 2)}</div><div class="s">百分点</div></div>
        </div>
        <div class="table-wrap">
        <table><thead><tr><th>数据集</th><th class="num">第 1 遍</th><th class="num">第 2 遍</th><th class="num">均值</th><th class="num">标准差</th><th class="num">偏差</th><th>判定</th></tr></thead>
        <tbody>{''.join(rows)}</tbody></table></div>
        <div class="chart"><h3>两次结果对比</h3><p>重点观察同一数据集两遍得分的差异，以验证 KVCache 落盘/读取一致性。</p>{svg_accuracy_bars(labels, run1, run2)}</div>
        <div class="callout info">{esc(item.detail.get('note') or '')}</div>
        {_accuracy_commands(item)}
        """
    return f"""
<section class="section" id="accuracy">
  <h2><span class="num">05</span>精度测试 <span class="small muted" style="font-weight:400">每项两遍</span></h2>
  <p class="lead">GPQA、GSM8K、LongBench-v2（short）、BFCL-v3 各执行两遍；两遍差异用于核验 KVCache 落盘一致性。</p>
  {body}
</section>
"""


def _agent_section(report: AcceptanceReport, item: ItemResult | None) -> str:
    body = _empty_item(item)
    if item and item.detail:
        detail = item.detail
        degraded = detail.get("degraded")
        degraded_html = (
            '<div class="callout"><strong>降级执行：</strong>被测服务可用上下文小于 256K，'
            "已使用 EvalScope AgentX smoke 模式；smoke 指标不可与标准 256K 结果直接比较。</div>"
            if degraded
            else '<div class="callout good">按标准 256K 上下文执行，未触发降级。</div>'
        )
        notes = detail.get("notes") or []
        notes_html = "".join(f"<li>{esc(note)}</li>" for note in notes)
        valid = detail.get("submission_valid")
        valid_text = "有效" if valid is True else ("无效/降级" if valid is False else "未提供")
        body = f"""
        <div class="grid">
          <div class="metric"><div class="k">服务级成功率</div><div class="v">{percent(detail.get('completion_rate'))}</div><div class="s">AgentX 请求成功率</div></div>
          <div class="metric"><div class="k">每会话请求数</div><div class="v">{_fmt(detail.get('avg_steps'), 1)}</div><div class="s">不是任务步数</div></div>
          <div class="metric"><div class="k">会话数</div><div class="v">{_fmt_int(detail.get('sessions'))}</div><div class="s">{_fmt_int(detail.get('requests'))} 次请求</div></div>
          <div class="metric"><div class="k">可用上下文</div><div class="v">{esc(detail.get('context_label') or (str(detail.get('context_detected') or '未知')))}</div><div class="s">目标 {_fmt_int((detail.get('context_required') or 0)/1024)}K</div></div>
          <div class="metric"><div class="k">提交有效性</div><div class="v" style="font-size:19px">{esc(valid_text)}</div><div class="s">EvalScope submission_valid</div></div>
        </div>
        {degraded_html}
        <div class="callout info"><strong>口径说明：</strong>EvalScope AgentX 是 256K 长会话的服务级压测，不是模型任务质量评测。本工具不自行计算任务完成率、平均步数或工具调用成功率；如需这些指标，请使用独立的 Agent 评测任务集。</div>
        <div class="details"><summary>执行说明与原始口径</summary><div class="body"><ul>{notes_html or '<li>无</li>'}</ul></div></details>
        {_command_block(item)}
        """
    return f"""
<section class="section" id="agent">
  <h2><span class="num">06</span>Agent 场景 <span class="small muted" style="font-weight:400">AgentX-claudecode</span></h2>
  <p class="lead">由 EvalScope <code>perf --scenario agentx</code> 执行 256K 长会话负载；上下文不足时降级为 smoke 并明确标注。任务质量指标不在本工具范围。</p>
  {body}
</section>
"""


def _criteria_section(report: AcceptanceReport) -> str:
    config = report.config_snapshot or {}
    sla = config.get("sla", {}) or {}
    accuracy = config.get("accuracy", {}) or {}
    agent = config.get("agent", {}) or {}
    perf = config.get("perf", {}) or {}
    parallel = perf.get("parallel") or [20]
    perf_text = (
        f"{_fmt_int(perf.get('max_prompt_length') or 65536)} 上下文 / "
        f"{esc(','.join(str(item) for item in parallel))} 并发 / "
        f"{_fmt_int(perf.get('number') or 200)} 请求"
    )
    return f"""
<section class="section" id="criteria">
  <h2><span class="num">07</span>验收口径</h2>
  <p class="lead">本次执行采用的默认预设；所有参数均外置于 config.yaml，可在现场按合同口径调整。</p>
  <div class="grid">
    <div class="kv"><div class="k">性能默认</div><div class="v">{perf_text}</div></div>
    <div class="kv"><div class="k">SLA 目标</div><div class="v">{_sla_target_text({"thresholds": sla.get("thresholds")})}</div></div>
    <div class="kv"><div class="k">SLA 口径</div><div class="v">{_fmt_int(sla.get("auto_tune", {}).get("upper") or sla.get("target", {}).get("concurrency") or 256)} 并发上限 · {_fmt_int(sla.get("input_length") or 65536)} 输入 · {_fmt_int(sla.get("output_tokens") or 512)} 输出</div></div>
    <div class="kv"><div class="k">SLA 模式</div><div class="v">'EvalScope 原生 auto_tune（avg / P99 口径）'</div></div>
    <div class="kv"><div class="k">精度</div><div class="v">每项 {esc(accuracy.get('runs', 2))} 遍 · 稳定性阈值 {_fmt((accuracy.get('stability_threshold') or 0.01)*100, 1)}pp</div></div>
    <div class="kv"><div class="k">Agent</div><div class="v">{_fmt_int((agent.get('context_required') or 262144)/1024)}K · 服务级成功率阈值 {percent(agent.get('success_threshold'))} · {esc(agent.get('duration_s', 900))}s</div></div>
    <div class="kv"><div class="k">报告模式</div><div class="v">{'实机' if report.mode == 'live' else '模拟（不作验收依据）'}</div></div>
  </div>
</section>
"""


def _raw_section(report: AcceptanceReport) -> str:
    blocks = []
    for item in report.ordered_items():
        if not item.artifacts:
            continue
        rows = "".join(
            f"<li><span>{_artifact_link(report, label, path)}</span>"
            f"<span class='path'>{esc(path)}</span></li>"
            for label, path in item.artifacts.items()
        )
        blocks.append(f"<h3 class='small'>{esc(item.name)}</h3><ul class='raw-list'>{rows}</ul>")
    items = "".join(
        f"<li><span>{esc(item.name)}</span><span class='path'>{esc(item.status.value)} · {esc(item.summary or item.error or '—')}</span></li>"
        for item in report.ordered_items()
    )
    return f"""
<section class="section" id="raw">
  <h2><span class="num">08</span>原始数据与复核</h2>
  <p class="lead">EvalScope 原始输出保留在验收输出目录，供交付复核与问题定位。</p>
  <div class="kv"><div class="k">本次输出目录</div><div class="v">{esc(report.output_dir)}</div></div>
  <ul class="raw-list">
    <li><span>验收报告 JSON</span><span class="path">report.json</span></li>
    <li><span>执行日志</span><span class="path">run.log</span></li>
    <li><span>逐项结果</span><span class="path">items/</span></li>
    <li><span>EvalScope 原始输出</span><span class="path">evalscope/</span></li>
  </ul>
  {''.join(blocks) if blocks else '<div class="empty">暂无原始数据索引。</div>'}
  <details class="details"><summary>查看逐项状态快照</summary><div class="body"><ul class="raw-list">{items}</ul></div></details>
</section>
"""


def render_report(report: AcceptanceReport, *, report_dir: str | Path | None = None) -> str:
    """渲染完全自包含的 HTML（内嵌 CSS/SVG，无外网依赖）。"""
    report.finalize()
    verdict_color = report.conclusion_color
    mode_text = "实机验收" if report.mode == "live" else "模拟演示"
    reasons = "".join(f"<li>{esc(reason)}</li>" for reason in report.reasons)
    mock_banner = (
        '<div class="banner warn"><strong>模拟数据声明：</strong>本报告由内置 Mock 引擎生成，'
        "仅用于界面走查、交付演示与 CI 虚跑，<strong>不能作为客户现网验收结论</strong>。"
        "实机验收请在配置中关闭 mock 并使用 EvalScope 引擎。</div>"
        if report.mode != "live"
        else ""
    )
    endpoint = report.endpoint or {}
    sections = [
        _overview(report),
        _perf_section(report, report.items.get("perf")),
        _multiturn_section(report, report.items.get("multiturn")),
        _sla_section(report, report.items.get("sla")),
        _accuracy_section(report, report.items.get("accuracy")),
        _agent_section(report, report.items.get("agent")),
        _criteria_section(report),
        _raw_section(report),
    ]
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(report.title)} · {esc(report.run_id)}</title>
<style>{CSS}</style>
</head>
<body>
<header class="hero">
  <div class="wrap">
    <div class="eyebrow">UCM INFERENCE ACCELERATION · ACCEPTANCE</div>
    <h1>{esc(report.title)}</h1>
    <p class="sub">面向客户现网的推理加速效果与服务质量验收 · 统一由 EvalScope 承载性能、SLA、精度与 Agent 测试</p>
    <div class="hero-meta">
      <span>任务编号 <b>{esc(report.run_id)}</b></span>
      <span>被测模型 <b>{esc(endpoint.get('model', '—'))}</b></span>
      <span>生成时间 <b>{_time_text(report.finished_at or report.started_at)}</b></span>
      <span class="mode-pill">{esc(mode_text)}</span>
    </div>
  </div>
</header>
<main>
  {mock_banner}
  <section class="verdict">
    <div class="verdict-badge" style="background:{verdict_color}">
      <div class="label">验收结论</div>
      <div class="value">{esc(report.conclusion)}</div>
    </div>
    <div>
      <h2>总体结论与依据</h2>
      <ul>{reasons or '<li>暂无结论。</li>'}</ul>
    </div>
  </section>
  {''.join(sections)}
  <div class="header-actions no-print">
    <button class="print-btn" onclick="window.print()">打印 / 另存为 PDF</button>
  </div>
</main>
<footer>
  <p>{esc(report.title)} · 版本 {esc(report.version)} · 任务 {esc(report.run_id)}</p>
  <p>本报告为离线自包含 HTML，可直接归档。若需 PDF，请使用浏览器打印功能，或在安装 WeasyPrint/Chromium 后使用工具的导出功能。</p>
</footer>
</body>
</html>
"""


def save_report(report: AcceptanceReport, path: str | Path, *, report_dir: str | Path | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(report, report_dir=report_dir), encoding="utf-8")
    return target


def export_pdf(html_path: str | Path, pdf_path: str | Path | None = None) -> Path:
    """可选 PDF 导出：优先 WeasyPrint，其次无头 Chromium。"""
    source = Path(html_path).resolve()
    if not source.exists():
        raise PDFExportError(f"HTML 报告不存在：{source}")
    target = Path(pdf_path) if pdf_path else source.with_suffix(".pdf")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        from weasyprint import HTML  # type: ignore

        HTML(string=source.read_text(encoding="utf-8"), base_url=str(source.parent)).write_pdf(str(target))
        return target
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise PDFExportError(f"WeasyPrint 导出失败：{exc}") from exc

    candidates = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "microsoft-edge"]
    for name in candidates:
        executable = shutil.which(name)
        if not executable:
            continue
        with tempfile.TemporaryDirectory():
            try:
                subprocess.run(
                    [
                        executable,
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--no-pdf-header-footer",
                        f"--print-to-pdf={target}",
                        source.as_uri(),
                    ],
                    check=True,
                    capture_output=True,
                    timeout=120,
                )
                if target.exists():
                    return target
            except (subprocess.SubprocessError, OSError):
                continue
    raise PDFExportError(
        "未找到可用的 PDF 导出组件。请使用浏览器打开 HTML 后打印为 PDF，"
        "或安装 weasyprint / chromium 后重试。"
    )
