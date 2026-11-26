"""统一离线资产包（AssetHub）的发现与解析。

一个资产包 = 固定目录结构的文件夹 + manifest.json（SHA256 清单）：

    ucm-assets/
    ├── manifest.json                 # scripts/prepare_assets.py --assets-dir <包> 生成
    ├── tokenizers/<model>/           # tokenizer 本体（目录名 = config 里的模型名）
    ├── datasets/
    │   ├── multiturn/*.jsonl         # 多轮预设（覆盖包内置同名文件）
    │   └── accuracy/…                # 可选：自定义精度数据集
    ├── evalscope-cache/              # EvalScope 缓存（GPQA/GSM8K/LongBench/BFCL/AgentX）
    └── modelscope-cache/             # ModelScope 缓存（与 EvalScope 配套）

不同交付形态共用同一个包，接入方式见 docs/offline-build.md：

- Docker：构建时 COPY 进镜像，或运行时 ``-v <包>:/opt/ucm-accept/assets:ro``
- deb/rpm：安装到 /opt/ucm-accept/assets（固定路径，自动发现）
- Windows exe / portable tar：把包目录改名为 assets/ 放在 exe 同级（自动发现）
- wheel / 手工部署：设置环境变量 UCM_ASSETS_DIR，或配置 ``app.assets_dir``

本模块只依赖标准库，供 config / engine / doctor 复用。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

ENV_VAR = "UCM_ASSETS_DIR"
DEFAULT_LINUX_ROOT = Path("/opt/ucm-accept/assets")
PACK_HINT_DIRS = ("tokenizers", "datasets", "evalscope-cache", "modelscope-cache")


def candidate_roots(configured: str | None = None) -> list[Path]:
    """资产包的可能位置，按优先级排列。"""
    roots: list[Path] = []
    env_value = os.environ.get(ENV_VAR, "").strip()
    if env_value:
        roots.append(Path(env_value).expanduser())
    if configured and str(configured).strip():
        roots.append(Path(str(configured)).expanduser())
    exe_dir = Path(sys.executable).resolve().parent
    roots.append(exe_dir / "assets")
    if getattr(sys, "frozen", False):
        # PyInstaller onefile 解包目录与 onedir 的 _internal 上级
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "assets")
        roots.append(exe_dir)
    roots.append(DEFAULT_LINUX_ROOT)
    roots.append(Path(__file__).resolve().parent / "resources" / "assets")
    return roots


def is_pack(root: Path) -> bool:
    """判定一个目录是否像资产包：有 manifest 或包含标准子目录。"""
    if not root.is_dir():
        return False
    if (root / "manifest.json").exists():
        return True
    return any((root / name).is_dir() for name in PACK_HINT_DIRS)


def find_assets_root(configured: str | None = None) -> Path | None:
    """返回第一个实际存在的资产包目录；找不到返回 None。"""
    for root in candidate_roots(configured):
        try:
            if is_pack(root):
                return root.resolve()
        except OSError:  # noqa: PERF203 - 路径不可访问时尝试下一个候选
            continue
    return None


def resolve_in_hub(root: Path, relative: str) -> Path | None:
    """在资产包内解析相对路径；允许只写名字，自动补标准子目录。

    绝对路径返回 None（由调用方自行处理）。
    """
    rel = Path(relative)
    if rel.is_absolute():
        return None
    candidates = [
        root / rel,
        root / "tokenizers" / rel,
        root / "datasets" / rel,
        root / "datasets" / "multiturn" / rel,
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate.resolve()
        except OSError:
            continue
    return None


def cache_env(root: Path) -> dict[str, str]:
    """资产包内的数据集缓存 → EvalScope 环境变量。"""
    env: dict[str, str] = {}
    mapping = (
        ("evalscope-cache", "EVALSCOPE_CACHE"),
        ("modelscope-cache", "MODELSCOPE_CACHE"),
    )
    for cache_dir, var in mapping:
        candidate = root / cache_dir
        if candidate.is_dir():
            env[var] = str(candidate.resolve())
    return env


def evalscope_dataset_dir(root: Path) -> str:
    """资产包内 EvalScope snapshot 数据集缓存根（evalscope --dataset-dir 直读，离线不访问 hub）。

    evalscope loader 的 cache_root = <dataset_dir>/datasets，snapshot 由联网预热发布。
    """
    candidate = root / "evalscope-cache"
    if candidate.is_dir():
        return str(candidate.resolve())
    return ""


def verify_manifest(root: Path, *, limit_problems: int = 5) -> tuple[int, list[str]]:
    """按 manifest.json 校验资产包；返回 (文件总数, 问题列表)。"""
    manifest_path = root / "manifest.json"
    if not manifest_path.exists():
        return 0, ["缺少 manifest.json（用 scripts/prepare_assets.py 生成）"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return 0, [f"manifest.json 无法读取：{exc}"]
    assets = manifest.get("assets", [])
    if not isinstance(assets, list):
        return 0, ["manifest.json 格式异常：assets 不是列表"]
    problems: list[str] = []
    for asset in assets:
        path = root / str(asset.get("path", ""))
        if not path.exists():
            problems.append(f"缺失：{asset.get('path')}")
            continue
        expected = asset.get("sha256")
        if expected:
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                problems.append(f"哈希不一致：{asset.get('path')}")
        if len(problems) >= limit_problems:
            break
    return len(assets), problems


def describe(root: Path | None) -> str:
    """资产包来源说明（doctor / 日志用）。"""
    if root is None:
        return "未发现资产包"
    return str(root)


def find_sdk_snapshot(root: Path, dataset_id: str) -> str:
    """定位资产包内 ModelScope SDK snapshot（``modelscope download --dataset`` 的产物）。

    dataset_id 形如 ``AI-ModelScope/gsm8k``，SDK 布局为
    ``<MODELSCOPE_CACHE>/datasets/AI-ModelScope--gsm8k/snapshots/<revision>``。
    root 可传资产包根（自动下钻 modelscope-cache）或 MODELSCOPE_CACHE 本身。
    返回 snapshot 目录（仓库根内容），未找到返回空串。
    """
    repo_dir_name = dataset_id.replace("/", "--")
    candidates = [root / "modelscope-cache", root]
    for base in candidates:
        snapshots = base / "datasets" / repo_dir_name / "snapshots"
        if not snapshots.is_dir():
            continue
        revisions = sorted((p for p in snapshots.iterdir() if p.is_dir()), reverse=True)
        for revision_dir in revisions:
            if any(revision_dir.iterdir()):
                return str(revision_dir.resolve())
    return ""


def builtin_tokenizers(root: Path) -> list[str]:
    """资产包内可用的内置 tokenizer 名称列表（tokenizers/ 下的一级目录）。"""
    base = root / "tokenizers"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if p.is_dir())

