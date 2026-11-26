#!/usr/bin/env python3
"""启动 mock GUI 并用 Playwright 截图，产出 GitHub Pages 预览站点。

产物结构（--out 指定目录）：
    index.html            截图索引页
    screenshots/*.png     界面截图（首页 / 项内配置展开 / 报告）

用法：
    python scripts/capture_gui.py --out site
CI 中配合 actions/deploy-pages 发布为 GitHub Pages。
"""

from __future__ import annotations

import argparse
import http.client
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = 8321
BASE_URL = f"http://127.0.0.1:{PORT}"


def wait_ready(seconds: int = 60) -> bool:
    for _ in range(seconds // 2):
        try:
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3)
            conn.request("GET", "/")
            conn.getresponse().read()
            conn.close()
            return True
        except OSError:
            time.sleep(2)
    return False


def _open_accordion(page, scope: str) -> None:
    """展开某测试项行内的「配置」面板。"""
    page.locator(scope).first.click()
    page.wait_for_timeout(800)


def capture(out_dir: Path) -> list[dict]:
    from playwright.sync_api import sync_playwright

    shots = [
        ("home", "首页 — 服务地址、模型路径与五项验收测试列表", None),
        ("item-config", "测试项配置 — 展开项内「配置」调整该项参数", "#item-multiturn button.link"),
        ("report", "验收报告 — 自包含 HTML，离线归档与分发", None),
    ]
    pages = []
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1440, "height": 1050}, device_scale_factor=1.5)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        page.wait_for_selector(".hero", timeout=30000)
        page.wait_for_timeout(2500)
        for name, caption, click_sel in shots:
            if click_sel:
                _open_accordion(page, click_sel)
            path = out_dir / "screenshots" / f"{name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=(name == "report"))
            pages.append({"file": f"screenshots/{name}.png", "caption": caption})
        browser.close()
    return pages


def write_index(out_dir: Path, pages: list[dict]) -> None:
    cards = "\n".join(
        f"""
        <figure>
          <img src="{page['file']}" alt="{page['caption']}" loading="lazy">
          <figcaption>{page['caption']}</figcaption>
        </figure>"""
        for page in pages
    )
    (out_dir / "index.html").write_text(
        f"""<!DOCTYPE html>
<html lang="zh_CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>UCM 验收工具 · 界面预览</title>
<style>
  body {{ margin: 0; background: #f5f5f7; color: #1d1d1f;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
         -webkit-font-smoothing: antialiased; }}
  header {{ max-width: 1080px; margin: 0 auto; padding: 48px 16px 8px; }}
  header .eyebrow {{ font-size: 12px; letter-spacing: .18em; color: #86868b; font-weight: 600;
                     text-transform: uppercase; }}
  header h1 {{ margin: 10px 0 6px; font-size: 30px; font-weight: 700; letter-spacing: -.02em; }}
  header p {{ margin: 0; color: #86868b; font-size: 14px; }}
  main {{ max-width: 1080px; margin: 24px auto; padding: 0 16px; }}
  figure {{ margin: 0 0 28px; background: #fff; border-radius: 24px; overflow: hidden;
           box-shadow: 0 4px 12px rgba(0, 0, 0, .04); }}
  figure img {{ width: 100%; display: block; }}
  figcaption {{ padding: 12px 20px; font-size: 13px; color: #6e6e73; border-top: 1px solid #e8e8ed; }}
  footer {{ text-align: center; color: #86868b; font-size: 12px; padding: 20px 16px 32px; }}
</style>
</head>
<body>
<header>
  <div class="eyebrow">UCM Inference Acceleration · Acceptance</div>
  <h1>UCM 推理加速验收工具 · 界面预览</h1>
  <p>以下截图由 CI 在每次 main 分支更新时自动生成（mock 模式，不产生真实请求）。</p>
</header>
<main>{cards}</main>
<footer>ucm-accept · GitHub Pages 自动发布</footer>
</body>
</html>
""",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub Pages 界面预览站点")
    parser.add_argument("--out", default="site", help="站点输出目录")
    args = parser.parse_args()
    out_dir = Path(args.out).resolve()
    if shutil.which("uvloop") is None and sys.platform == "win32":
        pass

    gui = subprocess.Popen(
        [sys.executable, "-m", "ucm_accept", "--gui", "--mock", "--fast", "--no-browser",
         "--port", str(PORT), "--outputs", str(out_dir / "outputs")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(ROOT),
    )
    try:
        if not wait_ready():
            print(f"GUI 未在 {BASE_URL} 就绪", file=sys.stderr)
            return 1
        try:
            pages = capture(out_dir)
        finally:
            subprocess.run(["taskkill", "/F", "/PID", str(gui.pid)] if sys.platform == "win32"
                           else ["kill", str(gui.pid)], capture_output=True)
    finally:
        if gui.poll() is None:
            gui.terminate()
    write_index(out_dir, pages)
    print(f"站点已生成：{out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
