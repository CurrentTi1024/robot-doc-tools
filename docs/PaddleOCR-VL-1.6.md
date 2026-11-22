# PaddleOCR-VL-1.6 下载、校验与私有部署

## 1. 准备模型和依赖

本服务使用 PaddleOCR 3.6.0、PaddleX 3.6.1 和 PaddlePaddle GPU 3.2.1。完整 Pipeline 由 PP-DocLayoutV3 做版面检测，由 PaddleOCR-VL-1.6 做区域识别。Windows 本地已用 RTX 4070 和 CUDA 12.6 跑过真实样本。建议准备 10 GB 以上可用磁盘空间用于 Python 依赖、临时下载、权重及样本结果。

| 目录 | 官方权重 | 主文件 |
|---|---|---|
| `.local/models/layout` | [PP-DocLayoutV3](https://huggingface.co/PaddlePaddle/PP-DocLayoutV3) | `inference.pdiparams`、`inference.json`、`inference.yml` |
| `.local/models/vl` | [PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6) | `model.safetensors`、`config.json`、tokenizer 与推理配置 |

Windows 需要 64 位 Python 3.12、NVIDIA 驱动与 `curl.exe`。在项目根目录运行：

```powershell
nvidia-smi
py -3.12 -m venv .venv
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install -r requirements.txt -r requirements-agent.txt -r requirements-test.txt
& $python -m pip install "paddlepaddle-gpu==3.2.1" --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
& $python -m pip install -r requirements-ocr.txt
& $python -c "import paddle; assert paddle.is_compiled_with_cuda(); print(paddle.device.cuda.device_count())"
```

## 2. 下载并离线校验

```powershell
.\scripts\windows.ps1 download
& $python -m scripts.download_models --verify-only
```

下载脚本在开始时解析两个官方仓库的仓库提交 SHA，按固定 SHA 拉取文件。大权重使用分段下载，写入前核对大小和发布方的 LFS SHA-256。下载后保存 `.local/models/manifest.json`。要复现同一版本，从上次清单读取两个 SHA 并传入 `--layout-revision` 和 `--vl-revision`。重试前保留当前下载目录可复用完整文件与已完成分段。

本地验证使用 PP-DocLayoutV3 `241f8bdfc77a7c7bee915a5057aaee58c235a8d3`、PaddleOCR-VL-1.6 `132c45f52c0a26ed5a6b2f6bd8e3985794cc27dd`。两个主权重文件分别为 130,806,572 和 1,917,255,968 字节。

内网环境没有外网时，在允许访问官方仓库的准备机执行下载。把 `layout`、`vl` 和 `manifest.json` 一并通过审批渠道导入内网，在目标环境离线校验：

```powershell
& $python -m scripts.download_models --verify-only --output "D:\ApprovedModels\PaddleOCR-VL-1.6"
```

归档时同时保留官方许可、版本清单和公司审批记录。Git 只保存下载脚本和配置，模型权重由内网存储分发。

## 3. 本地启动和验证

模型放在默认目录时自动加载。若使用独立路径，在启动 Document Worker 的窗口设置：

```powershell
$env:RW_LAYOUT_DIR = "D:\ApprovedModels\PaddleOCR-VL-1.6\layout"
$env:RW_VL_DIR = "D:\ApprovedModels\PaddleOCR-VL-1.6\vl"
$env:RW_OCR_DEVICE = "gpu:0"
.\scripts\windows.ps1 document
```

Document Worker 单进程处理 PDF，首次任务加载两个模型。API、Checklist Worker、Scoring Worker 的启动与调用见[运行手册](Operations.md#2-启动和对接-api)。快速验证可省略启动常驻进程，直接执行下面的命令，三个测试分别保存 Checklist 和初评输出：

```powershell
.\scripts\windows.ps1 smoke -LiveLlm -Repeat 3
```

`-LiveLlm` 让 Deep Agents 通过父目录配置调用真实 DeepSeek，仅发送合成提案的证据。省略 `-LiveLlm` 时，使用同一 Deep Agents 编排和可重复的测试模型返回值。

## 4. 内网 Kubernetes 私有部署

在获准联网的构建机准备两种镜像：

```powershell
docker build -f deploy/Dockerfile --target runtime -t registry.internal/robot-doc-tools:runtime .
docker build -f deploy/Dockerfile --target document -t registry.internal/robot-doc-tools:document .
```

将镜像导入公司镜像仓库。根据 [K8s 模板](../deploy/k8s/workloads.yaml) 准备 GPU 节点、内网 Redis/PostgreSQL/S3 配置和 `rw-models` PVC。PVC 以只读方式挂载至 `/models`，Document Worker 使用 `/models/layout` 和 `/models/vl`。Pod 初始申请一张 GPU，单进程加载模型。限制 Document Worker 的网络访问，使其只连接所需的内网服务。公司 LLM 配置由评估 Worker 的 Secret 注入。

上线前使用批准的 GPU/CUDA/cuDNN 组合验证容器，压测扫描 PDF、复杂图表、并发与最长页数。Windows 测试环境曾输出 cuDNN 编译版本和运行时版本不一致的警告，合成样本推理完成。目标集群仍需按公司镜像基线核对。

官方安装与配置详见 [PaddleOCR-VL 指南](https://github.com/PaddlePaddle/PaddleOCR/blob/main/docs/version3.x/pipeline_usage/PaddleOCR-VL.en.md)。
