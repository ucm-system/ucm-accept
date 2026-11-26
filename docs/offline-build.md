# 离线构建与交付手册

本文面向内部 CI / 交付构建人员。目标：产出**客户现网可直接使用、无需联网**的物料，并确保客户能用报告里的命令逐条复现。

> 工具本身不包含 EvalScope、数据集或 tokenizer。离线包由构建流水线把下列物料固化到镜像/安装包中。

---

## 一、物料清单

| 类别 | 建议路径 | 说明 |
|---|---|---|
| 工具源码 / wheel | `/opt/ucm-accept` | `pip install --no-index` 安装 |
| Python 运行时 | 镜像或 PyInstaller 内置 | 建议 Python 3.11（AgentX 要求 3.11–3.13） |
| EvalScope | venv | `evalscope[perf,service]>=1.15,<2`，Agent 场景加 `[agentx]` |
| Python wheels | `/opt/ucm-accept/wheelhouse` | `pip download` 产物，供离线重装/修复 |
| **统一资产包** | `/opt/ucm-accept/assets` | 结构见下节；deb/rpm/容器内置，exe/tar 同级，wheel 用 `UCM_ASSETS_DIR` |
| 配置 | `/etc/ucm-accept/config.yaml` | 所有参数外置 |

---

## 一（续）、统一资产包（ucm-assets）：一次构建，处处接入

**只构建一个资产包**，所有交付形态（Docker / deb / rpm / Windows exe / portable tar / wheel）共用；工具通过 AssetHub 自动发现，无需按形态改配置。

### 标准目录结构

```text
ucm-assets/
├── manifest.json                 # SHA256 清单（scripts/prepare_assets.py 生成）
├── README.txt                    # --init 自动生成
├── tokenizers/
│   └── Qwen2.5-0.5B-Instruct/    # tokenizer 本体（目录名 = 模型名）
├── datasets/
│   └── multiturn/*.jsonl         # 多轮预设（覆盖包内置同名文件）
├── evalscope-cache/              # EvalScope 缓存（GPQA/GSM8K/LongBench-v2/BFCL-v3/AgentX）
└── modelscope-cache/             # ModelScope 缓存
```

### 构建（联网构建机执行一次）

```bash
# 1) 建骨架
python scripts/prepare_assets.py --assets-dir /opt/ucm-build/ucm-assets --init --seed-bundled

# 2) tokenizer 下载到 tokenizers/<模型名>/
modelscope download --model Qwen/Qwen2.5-0.5B-Instruct \
  --local_dir /opt/ucm-build/ucm-assets/tokenizers/Qwen2.5-0.5B-Instruct

# 3) 数据集预热（缓存落到包内，见下文第 2 步的预热命令）
export EVALSCOPE_CACHE=/opt/ucm-build/ucm-assets/evalscope-cache
export MODELSCOPE_CACHE=/opt/ucm-build/ucm-assets/modelscope-cache

# 4) 固化清单
python scripts/prepare_assets.py --assets-dir /opt/ucm-build/ucm-assets
```

### 各交付形态的接入方式（同一个包）

| 形态 | 接入 |
|---|---|
| Docker | 构建时 `COPY ucm-assets/ /opt/ucm-accept/assets/`；或运行时 `-v /data/ucm-assets:/opt/ucm-accept/assets:ro`（升级资产不用换镜像） |
| deb/rpm | 安装到 `/opt/ucm-accept/assets`（固定路径，自动发现）；或把包解压到该路径 |
| Windows exe / portable tar | 把 `ucm-assets` 目录改名为 `assets/`，与 `ucm-accept.exe` 放同级（自动发现） |
| wheel / 手工部署 | `export UCM_ASSETS_DIR=/path/ucm-assets`，或 config.yaml：`app.assets_dir: /path/ucm-assets` |

### 数据集注册表：纯配置扩展数据集与多轮场景

在资产包放一个 `datasets/registry.yaml`，即可声明新的精度数据集 / 多轮场景（含描述与样本数），
GUI 数据集一览表、勾选芯片、CLI、报告与 `--doctor` 自动生效，**无需改代码**：

```yaml
accuracy:
  datasets:
    customer_qa:                     # 现场自有数据集
      label: 客户问答集
      dataset: customer_qa           # EvalScope 数据集名（缓存需提前预热进 evalscope-cache）
      limit: 50
      enabled: true
      description: 客户自有业务问答集
multiturn:
  scenarios:
    telecom:
      label: 电信客服
      description: 电信多轮工单场景
      dataset_path: datasets/multiturn/telecom.jsonl
```

> 多轮 JSONL 格式：每行一个 **OpenAI 消息数组**（`[{"role":"user","content":…},{"role":"assistant","content":…},…]`），
> 与 EvalScope 1.15 `custom_multi_turn` 插件一致；assistant 内容仅作轮次边界，实际由被测模型生成。

优先级：内置默认 < 注册表 < 用户 `--config`（可在用户配置中按 key 覆盖 `enabled`/`limit`）。
GUI 重启后生效。

### 离线实测清单（W2P 环境实测沉淀）

以下问题在 `docker run --network=none` 纯离线容器实测中逐一暴露并已验证解决：

| 问题 | 处理 |
|---|---|
| llama.cpp 等推理二进制缺 `libgomp.so.1`，且 host 版本依赖 GLIBC 2.38 与目标环境不匹配 | 从**目标镜像/系统同版本**提取 libgomp1 随包携带（`dpkg -x libgomp1*.deb`），LD_LIBRARY_PATH 指向 |
| 新模型 tokenizer 目录缺 chat template（transformers 新惯例拆分为独立 `chat_template.jinja`） | 下载模型时把 `chat_template.jinja` 一并放入 `tokenizers/<模型名>/` |
| ModelScope SDK 离线加载数据集仍先探测端点（即使缓存已存在），15s 预算耗尽抛 E1001（其提示的 `=0` 会被 `_env_int` 拒绝） | 见下方「已知限制」 |
| 缓存/输出目录属主与容器运行用户不一致导致 Permission denied | 挂载目录预先 `chown -R 999:999`（容器 ucm 用户）或 `chmod -R a+rwX` |

**离线精度（精度数据集）——已在真机离线环境跑通**，需按以下顺序固化资产包（工具已自动处理加粗步骤）：

1. 联网预热 gsm8k 到 SDK snapshot 布局（ModelScope 原始文件，可移植）：
   `modelscope download --dataset AI-ModelScope/gsm8k`（`MODELSCOPE_CACHE=<资产包>/modelscope-cache`）；
2. 联网预热 datasets arrow 缓存到 `modelscope-cache/datasets/datasets/`（跑一次 `evalscope eval --limit 1` 即产出）；
3. **把 arrow 目录移植到 `evalscope-cache/datasets/`**（`cp -r modelscope-cache/datasets/datasets/AI-ModelScope_gsm8k-* evalscope-cache/datasets/`）——
   现场离线时 datasets 库直接复用该缓存（环境相关哈希在容器内一致，因为 MODELSCOPE_CACHE 指向资产包）；
4. **工具自动完成**：运行时把 ModelScope SDK snapshot 布置到 evalscope 工作目录
   （evalscope 以相对路径 `os.path.exists(dataset_id)` 检查本地数据集，命中走 LOCAL 分支完全离线），
   并强制 `MODELSCOPE_CACHE`/`EVALSCOPE_CACHE` 指向资产包（覆盖镜像内置只读路径）；
5. ModelScope 离线变量（见上表）压制联网重试为快速失败。

Agent 场景数据集 `evalscope/cc-traces-weka-062126-256k` 已在 ModelScope **下线**（API 返回 record not found），
Agent 项当前无法在任何环境准备数据集；工具会正确报告失败。数据源恢复后无需改代码，直接预热即可。

**离线环境变量清单**（config.yaml `evalscope.env`，实测必要项）：

```yaml
evalscope:
  env:
    EVALSCOPE_LANGUAGE: zh
    HF_DATASETS_OFFLINE: "1"      # datasets 库离线模式（命中本地缓存不联网）
    HF_HUB_OFFLINE: "1"
    # 以下将 ModelScope 联网重试压制为快速失败，避免 15s 探测拖慢失败路径
    MODELSCOPE_API_CONNECT_TIMEOUT: "1"
    MODELSCOPE_API_TIMEOUT: "1"
    MODELSCOPE_DOWNLOAD_TOTAL_TIMEOUT: "1"
    MODELSCOPE_DOWNLOAD_REGION_PROBE_TIMEOUT_SECONDS: "0"
    MODELSCOPE_REPO_TREE_PAGE_TIMEOUT_SECONDS: "0"
    MODELSCOPE_REPO_TREE_LIST_TIMEOUT_SECONDS: "0"
```

### 自动生效的三件事

1. **tokenizer**：`perf.tokenizer_path` 可只写模型名（如 `Qwen2.5-0.5B-Instruct`），工具在包内 `tokenizers/` 下自动补全；写模型 ID（`Qwen/Qwen2.5-…`）也按名称尝试解析。
2. **数据集缓存**：包内 `evalscope-cache/`、`modelscope-cache/` 自动注入 EvalScope 子进程环境变量（显式设置的环境变量优先）。
3. **校验**：`--doctor` 自动发现资产包并按 `manifest.json` 逐文件校验 SHA256，报告来源与缺失项。

> 优先级：`UCM_ASSETS_DIR` > `app.assets_dir` > exe 同级 `assets/` > `/opt/ucm-accept/assets` > 包内置 `resources/assets`。

---

### 数据集来源（EvalScope 1.15 内置）

| 验收项 | EvalScope 数据集名 | ModelScope ID |
|---|---|---|
| GPQA Diamond | `gpqa_diamond` | `AI-ModelScope/gpqa_diamond` |
| GSM8K | `gsm8k` | `AI-ModelScope/gsm8k` |
| LongBench-v2 | `longbench_v2` | `ZhipuAI/LongBench-v2` |
| BFCL-v3 | `bfcl_v3` | `AI-ModelScope/bfcl_v3` |
| AgentX 256K | `agentx` | `evalscope/cc-traces-weka-062126-256k` |

> 数据集加载由 EvalScope 负责。不要在工具层复制/改写数据格式；离线构建只负责把 EvalScope 实际使用的缓存目录完整打包，并记录哈希。

---

## 二、构建机（联网）流程

### 1. 准备 Python 与 wheelhouse

```bash
python3.11 -m venv /opt/ucm-build/venv
source /opt/ucm-build/venv/bin/activate
pip install --upgrade pip wheel

mkdir -p /opt/ucm-build/wheelhouse
pip download -d /opt/ucm-build/wheelhouse \
  "ucm-accept==1.2.0" \
  "evalscope[perf,service,agentx]>=1.15,<2" \
  "wheel" "setuptools"   # sdist 构建依赖，离线镜像安装必需
```

如果从源码构建：

```bash
pip wheel -w /opt/ucm-build/wheelhouse .
```

### 2. 预热 EvalScope 数据与 tokenizer

在构建机上用 EvalScope 原生命令预热。**这一步必须使用与现场一致的 EvalScope 版本**，避免缓存格式差异。

```bash
export EVALSCOPE_CACHE=/opt/ucm-build/assets/evalscope-cache
export MODELSCOPE_CACHE=/opt/ucm-build/assets/modelscope-cache

# 数据集预热：指向任意可达的 OpenAI 兼容服务；limit 1 即可触发下载，不要求跑完。
evalscope eval \
  --model Qwen2.5-0.5B-Instruct \
  --api-url http://127.0.0.1:8801/v1 \
  --datasets gpqa_diamond gsm8k longbench_v2 bfcl_v3 \
  --limit 1 \
  --work-dir /tmp/warmup

# AgentX 256K 数据集预热（需要 aiperf 与 256K 服务；也可只下载数据集）
evalscope perf \
  --scenario '{"name":"agentx","mode":"smoke"}' \
  --model Qwen2.5-0.5B-Instruct \
  --tokenizer-path /opt/ucm-build/assets/tokenizers/Qwen2.5-0.5B-Instruct \
  --url http://127.0.0.1:8801/v1/chat/completions \
  --parallel 1
```

> 若构建机没有模型服务，可在内网部署一个最小 OpenAI 兼容 stub，仅用于触发 EvalScope 的数据集下载；**不要**把 stub 或预热结果作为验收数据。

### 3. 准备 tokenizer

```bash
mkdir -p /opt/ucm-build/assets/tokenizers
modelscope download --model Qwen/Qwen2.5-0.5B-Instruct \
  --local_dir /opt/ucm-build/assets/tokenizers/Qwen2.5-0.5B-Instruct
```

### 4. 生成资产清单

```bash
python scripts/prepare_assets.py \
  --assets-dir /opt/ucm-build/assets \
  --output /opt/ucm-build/assets/manifest.json
```

脚本会：

- 遍历 `assets/` 下文件，写 SHA256 与大小；
- 校验内置多轮 JSONL 可被 JSON 解析；
- 输出 `manifest.json`，供现场 `--doctor` 或安装脚本校验。

### 5. 虚跑校验

在构建机上先不接客户模型，用模拟引擎验证工具链：

```bash
ucm-accept --mock --fast --all --outputs /tmp/ucm-smoke
ucm-accept --doctor --mock
python scripts/verify_report.py /tmp/ucm-smoke/runs/*/report.json
```

这一步必须检查：

- 五项全部完成；
- `report.html` 存在、无外网 URL；
- 每项 `detail.command` 或 accuracy `commands` 非空；
- `report.json` 可被 `AcceptanceReport.load_json` 加载。

### 6. 打包

#### 6.1 Linux GUI / deb / rpm

```bash
bash scripts/build_linux.sh 1.2.0 amd64
bash scripts/build_linux.sh 1.2.0 arm64
```

依赖：

- `pyinstaller`
- `nfpm`（生成 deb/rpm）
- 对应架构的构建机（PyInstaller 不能交叉编译）

产物：

```text
dist/
├── ucm-accept-linux-amd64/          # PyInstaller onedir
├── ucm-accept_1.2.0_amd64.deb
└── ucm-accept-1.2.0-1.aarch64.rpm
```

deb 安装后：

```bash
sudo dpkg -i ucm-accept_1.2.0_amd64.deb
ucm-accept --doctor
ucm-accept --gui
```

桌面快捷方式见 `packaging/ucm-accept.desktop`。

#### 6.2 Windows exe

在 `windows-latest` 上：

```powershell
pip install -e ".[build]"
pyinstaller packaging/ucm-accept.spec
Compress-Archive -Path dist/ucm-accept -DestinationPath dist/ucm-accept-windows-x64.zip
```

产物为 `ucm-accept.exe` + `_internal` 目录。分发时整目录打包，不要只发单个 exe。

#### 6.3 双架构 Docker 镜像

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  -t registry.example.com/ucm/ucm-accept:1.2.0 \
  --build-arg INSTALL_AGENTX=0 \
  --push .
```

离线构建时，把 wheelhouse 与 assets 放到构建上下文，使用：

```bash
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --build-arg OFFLINE=1 \
  --build-arg INSTALL_AGENTX=0 \
  -t registry.example.com/ucm/ucm-accept:1.2.0 \
  .
```

`Dockerfile` 在 `OFFLINE=1` 时只从 `wheelhouse/` 安装，不访问 PyPI。

运行：

```bash
docker run --rm -it \
  -v /data/ucm:/data \
  -e UCM_BASE_URL=http://172.17.0.1:8801/v1 \
  registry.example.com/ucm/ucm-accept:1.2.0 \
  --config /etc/ucm-accept/config.yaml --outputs /data/outputs
```

---

## 三、现场离线安装

### 3.1 容器（推荐，屏蔽麒麟服务器 openEuler 差异）

```bash
docker load -i ucm-accept-1.2.0-arm64.tar
docker run --rm -it \
  -v /data/ucm:/data \
  ucm-accept:1.2.0 \
  --config /etc/ucm-accept/config.yaml --outputs /data/outputs
```

> **挂载目录权限**：容器内以 `ucm` 用户（uid 999）运行，宿主机挂载目录需预先授权：
> `sudo chown -R 999:999 /data/ucm` 或 `chmod 777 /data/ucm`，否则报告目录不可写。

若现场禁止 Docker，可使用 PyInstaller onedir + systemd/ssh 直接运行。

### 3.2 验证物料

```bash
ucm-accept --doctor --probe
```

应全部通过；若有 `EvalScope 版本`、`AgentX 依赖`、`Tokenizer` 警告，按提示补齐。

### 3.3 真实验收

```bash
ucm-accept --all --report /data/ucm-accept-report.html
```

或 GUI：

```bash
ucm-accept --gui --no-browser
```

---

## 四、构建校验清单

- [ ] `pip install --no-index --find-links wheelhouse` 可在无网环境完成
- [ ] EvalScope 版本 >= 1.15，`--doctor` 无失败项
- [ ] 四个精度数据集缓存齐全，`evalscope eval` 预热命令不触发下载
- [ ] AgentX 数据集缓存齐全，`--scenario agentx` 可离线启动
- [ ] tokenizer 路径存在且与 `config.yaml` 一致
- [ ] 内置多轮 JSONL 哈希与 `assets_manifest.json` 一致
- [ ] `--mock --fast --all` 虚跑通过，报告无外网依赖
- [ ] 双架构镜像均能 `--doctor`
- [ ] deb/rpm 在对应麒麟版本可安装、可启动 GUI/CLI
- [ ] Windows exe 在 Windows 10/11 可启动并打开浏览器
- [ ] 报告中的 `command.txt` 可在现场原样复现

---

## 五、常见问题

**Q：为什么报告里 Agent 没有“任务完成率”？**
A：EvalScope AgentX 是服务级长会话压测，不输出任务质量分。工具不自行计算；需要任务级指标请另配 Agent 评测任务集。

**Q：为什么多轮对话没有回放内容？**
A：EvalScope 多轮压测只输出统计指标。工具不生成伪造记录，保留原始 EvalScope 报告供人工复核。

**Q：SLA 的 avg / P99 参数在旧版 EvalScope 不存在怎么办？**
A：升级到 >= 1.15（`--sla-auto-tune` 支持 avg / p99 的 latency / ttft / tpot 指标）。

**Q：能否只用 CLI，不安装 Web 前端？**
A：可以。GUI 依赖是可选 extra（fastapi + uvicorn，体积远小于原 Gradio），CLI 与报告不依赖它。

**Q：报告能直接发邮件吗？**
A：可以。`report.html` 自包含，无外网依赖；图表为内嵌 SVG。原始数据目录较大，按需单独打包。
