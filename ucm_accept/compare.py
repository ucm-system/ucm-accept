"""多结果对比报告：把 1~5 份验收结果（report.json）并排呈现。

- 每份结果一个标签 + 固定配色（Apple 色板）
- 总览表：验收项关键指标逐列对比
- perf 曲线叠加：吞吐 / TTFT avg / TPOT avg（x=并发，多色折线）
- 精度得分对比：按数据集逐行对比
"""

from __future__ import annotations

import html
from typing import Any

PALETTE = ["#0071e3", "#34c759", "#af52de", "#ff9500", "#ff3b30"]


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _svg_lines_chart(series: list[dict], *, title: str, unit: str,
                     width: int = 760, height: int = 260) -> str:
    """多序列折线图：series = [{label, color, points: [(x, y), ...]}]。"""
    left, right, top, bottom = 64, 24, 18, 42
    plot_w, plot_h = width - left - right, height - top - bottom
    all_xy = [(x, y) for s in series for x, y in s["points"]]
    if not all_xy:
        return f'<div class="chart"><h3>{_esc(title)}</h3><p class="muted">无可绘制数据</p></div>'
    xs = [x for x, _ in all_xy]
    ys = [y for _, y in all_xy]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if y_max == y_min:
        y_max = y_min + 1
    if x_max == x_min:
        x_max = x_min + 1

    def sx(x: float) -> float:
        return left + (x - x_min) / (x_max - x_min) * plot_w

    def sy(y: float) -> float:
        return top + (1 - (y - y_min) / (y_max - y_min)) * plot_h

    parts = [f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
             f'font-family="-apple-system,Segoe UI,sans-serif" font-size="11">']
    parts.append(f'<text x="{width / 2}" y="10" text-anchor="middle" font-weight="600">{_esc(title)}</text>')
    for tick in range(5):
        value = y_min + (y_max - y_min) * tick / 4
        y = sy(value)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e6ebf2"/>')
        parts.append(f'<text x="{left - 8}" y="{y + 3.5}" text-anchor="end" fill="#86868b">{value:.2f}</text>')
    for tick in range(4):
        value = x_min + (x_max - x_min) * tick / 3
        x = sx(value)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top+plot_h}" stroke="#f0f3f8"/>')
        parts.append(f'<text x="{x:.1f}" y="{height - 22}" text-anchor="middle" fill="#86868b">{value:.0f}</text>')
    for s in series:
        if len(s["points"]) > 1:
            path = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in s["points"])
            parts.append(f'<polyline points="{path}" fill="none" stroke="{s["color"]}" stroke-width="2.2"/>')
        for x, y in s["points"]:
            parts.append(f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="4" fill="#fff" '
                         f'stroke="{s["color"]}" stroke-width="2"/>')
    legend_x = left
    for s in series:
        parts.append(f'<rect x="{legend_x}" y="{height - 14}" width="10" height="10" rx="2" fill="{s["color"]}"/>')
        parts.append(f'<text x="{legend_x + 14}" y="{height - 5}" fill="#1d1d1f">{_esc(s["label"])}</text>')
        legend_x += 14 + 6 + len(s["label"]) * 11 + 14
    parts.append(f'<text x="{left + plot_w / 2}" y="{height - 4}" text-anchor="middle" fill="#94a3b8">并发</text>')
    parts.append(f'<text x="13" y="{top + plot_h / 2}" fill="#94a3b8" '
                 f'transform="rotate(-90 13 {top + plot_h / 2})">{_esc(unit)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _primary_point(detail: dict | None) -> dict:
    points = (detail or {}).get("points") or []
    if not points:
        return {}
    return max(points, key=lambda p: p.get("concurrency", 0))


def build_compare_html(entries: list[dict]) -> str:
    """entries = [{label, report(AcceptanceReport)}]，1~5 个。"""
    for index, entry in enumerate(entries):
        entry["color"] = PALETTE[index % len(PALETTE)]
    header_cells = "\n".join(
        f'<th><span class="dot" style="background:{entry["color"]}"></span>'
        f'{_esc(entry["label"])}</th>'
        for entry in entries
    )

    def row(label: str, cells: list[str]) -> str:
        return f"<tr><td>{_esc(label)}</td>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>"

    def item_status(entry: dict, key: str) -> str:
        item = entry["report"].items.get(key)
        if item is None:
            return '<span class="muted">—</span>'
        label = {"passed": "通过", "failed": "未通过", "warning": "需关注", "running": "执行中",
                 "pending": "待执行", "skipped": "已跳过", "canceled": "已取消"}.get(
                     item.status.value, item.status.value)
        color = {"passed": "#1d7a3d", "failed": "#c21b17", "warning": "#9a6a00"}.get(
            item.status.value, "#6e6e73")
        return f'<span style="color:{color};font-weight:600">{_esc(label)}</span>'

    def detail_of(entry: dict, key: str) -> dict:
        item = entry["report"].items.get(key)
        return (item.detail or {}) if item else {}

    def fmt(value: Any, digits: int = 2, suffix: str = "") -> str:
        if value in (None, "", 0) and suffix != " tok/s":
            return '<span class="muted">—</span>'
        try:
            return f"{float(value):.{digits}f}{suffix}"
        except (TypeError, ValueError):
            return '<span class="muted">—</span>'

    # ---------- perf ----------
    perf_rows = []
    for label_of_row, key, digits, suffix in (
        ("吞吐（tok/s）", "throughput_tok_s", 0, " tok/s"),
        ("TTFT avg（秒）", "ttft_avg_ms", 2, " s"),
        ("TTFT P99（秒）", "ttft_p99_ms", 2, " s"),
        ("TPOT avg（毫秒）", "tpot_avg_ms", 1, " ms"),
        ("TPOT P99（毫秒）", "tpot_p99_ms", 1, " ms"),
        ("成功率", "success_rate", 1, ""),
    ):
        cells = []
        for entry in entries:
            point = _primary_point(detail_of(entry, "perf"))
            value = point.get(key)
            if key == "success_rate":
                value = float(value) * 100 if value else None
                cells.append(fmt(value, digits, "%"))
            else:
                cells.append(fmt(value, digits, suffix))
        perf_rows.append(row(label_of_row, cells))

    # ---------- multiturn ----------
    mt_rows = []
    for label_of_row, key, digits, suffix in (
        ("完成率", "completion_rate", 1, ""),
        ("平均延迟（秒）", "avg_latency_ms", 2, " s"),
        ("缓存命中率", "cache_hit_rate", 1, ""),
    ):
        cells = []
        for entry in entries:
            detail = detail_of(entry, "multiturn")
            value = detail.get(key)
            if key in ("completion_rate", "cache_hit_rate"):
                value = float(value) * 100 if value else None
                cells.append(fmt(value, digits, "%"))
            else:
                cells.append(fmt(value, digits, suffix))
        mt_rows.append(row(label_of_row, cells))

    # ---------- sla ----------
    sla_rows = []
    thresholds_seen: list[str] = []
    for entry in entries:
        target = (detail_of(entry, "sla").get("target") or {})
        for key in (target.get("thresholds") or {}):
            if key not in thresholds_seen:
                thresholds_seen.append(key)
    label_map = {"avg_ttft_s": "TTFT avg（秒）", "avg_tpot_ms": "TPOT avg（毫秒）",
                 "p99_ttft_s": "TTFT P99（秒）", "p99_tpot_ms": "TPOT P99（毫秒）"}
    if thresholds_seen:
        for key in thresholds_seen:
            cells = []
            for entry in entries:
                detail = detail_of(entry, "sla")
                target = detail.get("target") or {}
                limit = (target.get("thresholds") or {}).get(key)
                point = next((p for p in (detail.get("probes") or [])
                              if p.get("concurrency") == target.get("concurrency")),
                             detail.get("best") or (detail.get("probes") or [{}])[0])
                scale = 1000.0 if key.endswith("_s") else 1.0
                field = {"avg_ttft_s": "ttft_avg_ms", "avg_tpot_ms": "tpot_avg_ms",
                         "p99_ttft_s": "ttft_p99_ms", "p99_tpot_ms": "tpot_p99_ms"}.get(key)
                actual = (point.get(field) or 0) / scale if point and point.get(field) else None
                target_text = fmt(limit, 2, "") if limit else "—"
                cells.append(
                    f"{fmt(actual, 2, '')} / 目标 ≤ {target_text} · "
                    f"{_esc(detail.get('recommended_concurrency') or '—')}"
                )
            sla_rows.append(row(label_map.get(key, key), cells))
        sla_rows.append(row("建议并发", [
            _esc((detail_of(entry, "sla").get("recommended_concurrency")) or "—")
            for entry in entries
        ]))
        sla_rows.append(row("结论", [
            _esc(detail_of(entry, "sla").get("status", {}).get("value")
                 or ("达标" if detail_of(entry, "sla").get("target_met") else "未达标"))
            for entry in entries
        ]))

    # ---------- accuracy ----------
    dataset_names: list[str] = []
    for entry in entries:
        for ds in (detail_of(entry, "accuracy").get("datasets") or []):
            name = str(ds.get("label") or ds.get("key"))
            if name not in dataset_names:
                dataset_names.append(name)
    acc_rows = []
    for name in dataset_names:
        cells = []
        for entry in entries:
            score = None
            for ds in (detail_of(entry, "accuracy").get("datasets") or []):
                if str(ds.get("label") or ds.get("key")) == name:
                    scores = [r.get("score") for r in (ds.get("runs") or []) if r.get("score") is not None]
                    score = sum(scores) / len(scores) if scores else None
            cells.append(fmt(score, 3, ""))
        acc_rows.append(row(name, cells))

    # ---------- perf 叠加曲线 ----------
    def perf_series(field: str, scale: float) -> list[dict]:
        out = []
        for entry in entries:
            points = (detail_of(entry, "perf").get("points") or [])
            pts = [(float(p.get("concurrency", 0)), float(p.get(field, 0) or 0) / scale)
                   for p in points if p.get("concurrency") is not None]
            pts.sort()
            if pts:
                out.append({"label": entry["label"], "color": entry["color"], "points": pts})
        return out

    charts = (
        '<div class="charts-2">'
        + _svg_lines_chart(perf_series("throughput_tok_s", 1.0), title="输出吞吐（tok/s）", unit="tok/s")
        + _svg_lines_chart(perf_series("ttft_avg_ms", 1000.0), title="TTFT avg（秒）", unit="秒")
        + "</div>"
        '<div class="charts-2">'
        + _svg_lines_chart(perf_series("ttft_p99_ms", 1000.0), title="TTFT P99（秒）", unit="秒")
        + _svg_lines_chart(perf_series("tpot_avg_ms", 1.0), title="TPOT avg（毫秒）", unit="毫秒")
        + "</div>"
    )

    header_cells = "\n".join(
        f'<th><span class="dot" style="background:{entry["color"]}"></span>{_esc(entry["label"])}</th>'
        for entry in entries
    )
    meta_cells = "\n".join(
        f"<td class='mono'>{_esc(entry['report'].run_id)}<br>"
        f"<span class='muted'>{_esc(entry['report'].mode)} · {_esc(entry['report'].started_at)}</span></td>"
        for entry in entries
    )
    conclusion_cells = "\n".join(
        f"<td style='font-weight:700;color:{entry['color']}'>"
        f"{_esc(entry['report'].conclusion)}</td>"
        for entry in entries
    )
    status_cells = "\n".join(
        "<td>" + " · ".join(
            item_status(entry, key) for key in ("perf", "multiturn", "sla", "accuracy", "agent")
        ) + "</td>"
        for entry in entries
    )

    def two_col_table(title: str, rows_html: str) -> str:
        return (
            f"<h2>{_esc(title)}</h2>"
            "<div class='table-wrap'><table>"
            f"<thead><tr><th style='width:220px'>指标</th>{header_cells}</tr></thead>"
            f"<tbody>{rows_html}</tbody></table></div>"
        )

    legend = " ".join(
        f"<span class='legend' style='color:{entry['color']}'>■ {_esc(entry['label'])}</span>"
        for entry in entries
    )

    return f"""<!DOCTYPE html>
<html lang="zh_CN">
<head>
<meta charset="utf-8">
<title>验收结果对比</title>
<style>
:root{{--ink:#1d1d1f;--muted:#86868b;--line:#e8e8ed;--card:#fff}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f5f5f7;color:var(--ink);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased}}
main{{max-width:1180px;margin:0 auto;padding:0 24px 60px}}
.hero{{padding:40px 4px 8px}}
.hero h1{{margin:8px 0 4px;font-size:28px;font-weight:700;letter-spacing:-.02em}}
.hero p{{margin:0;color:var(--muted);font-size:13.5px}}
.legend-bar{{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 6px;font-size:14px;font-weight:600}}
h2{{font-size:19px;margin:34px 4px 12px}}
.card{{background:var(--card);border-radius:20px;box-shadow:0 1px 2px rgba(0,0,0,.03),0 4px 12px rgba(0,0,0,.04);
  padding:22px 26px;margin-bottom:18px}}
.table-wrap{{overflow-x:auto}}
table{{width:100%;border-collapse:separate;border-spacing:0;font-size:13px;
  border:1px solid var(--line);border-radius:12px;overflow:hidden}}
th{{background:#f5f5f7;color:#6e6e73;font-weight:600;text-align:left;padding:9px 12px;white-space:nowrap}}
th .dot{{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:6px}}
td{{padding:9px 12px;border-top:1px solid var(--line);vertical-align:top}}
.muted{{color:#aeaeb2}}
.mono{{font-family:"SF Mono",ui-monospace,Consolas,monospace;font-size:11.5px}}
.charts-2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}
.chart{{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px}}
.chart svg{{width:100%;height:auto;display:block}}
@media print {{ body{{background:#fff}} .card{{box-shadow:none;border:1px solid var(--line)}} }}
</style>
</head>
<body>
<main>
  <div class="hero">
    <h1>验收结果对比</h1>
    <p>{len(entries)} 份结果并排对比 · 生成自 UCM 推理加速验收工具</p>
    <div class="legend-bar">{legend}</div>
  </div>

  <div class="card">
    <h2 style="margin-top:0">结果元信息</h2>
    <div class="table-wrap"><table>
      <thead><tr><th style="width:220px"></th>{header_cells}</tr></thead>
      <tbody>
        <tr><td>任务编号</td>{meta_cells}</tr>
        <tr><td>验收结论</td>{conclusion_cells}</tr>
        <tr><td>各项状态（性能 · 多轮 · SLA · 精度 · Agent）</td>{status_cells}</tr>
      </tbody></table></div>
  </div>

  {two_col_table("基础性能（最大并发点）", "".join(perf_rows))}
  <div class="card">{charts}</div>
  {two_col_table("多轮对话", "".join(mt_rows))}
  {two_col_table("SLA 自动调优", "".join(sla_rows))}
  {two_col_table("精度得分（两轮均值）", "".join(acc_rows) or "<tr><td class='muted'>无精度数据</td></tr>")}

  <p class="muted" style="font-size:11.5px">各结果的完整数据请下载对应 run 目录（打包下载），或查看单份验收报告。</p>
</main>
</body>
</html>"""
