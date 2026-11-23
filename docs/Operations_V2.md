# V2 启动、联调与 API

V2 入口为 `app.v2.api:app`。官网后端上传 PPTX 后查询 Checklist，确定正式参评的那次上传后，再用它的任务 ID 发起 A1–B3 初评。先运行 [离线验证](#2-离线验证)，再按 [Windows 联调](#3-windows-联调) 配置真实模型。部署拓扑和代码路径见 [V2 架构与 Pipeline](Robotic_Workshop_Pipeline_V2.md)。

## 1. 环境与依赖

| 组件 | 使用场景 |
|---|---|
| Python 3.12、PowerShell 7.2+ | 本文的 Windows 安装和接口示例。 |
| `requirements.txt`、`requirements-agent.txt` | FastAPI、PPTX 与 PDF 裁图组件、Deep Agents 和模型客户端。 |
| `requirements-test.txt` | 离线测试以及生成合成演示用的 PPTX。 |
| LibreOffice Impress 与相应字体 | 图片、复杂图表和流程图需要视觉核对时，把 PPTX 转为供内部取证的 PDF。本地通读渲染 PDF 时也可核对字形和布局。 |
| OpenAI 兼容模型 API | 真实联调时，评审模型需支持工具调用，视觉模型需收取 Base64 图片，汇总模型需返回 JSON 文本。接口通用性以公司网关的实测为准。 |

在项目根目录执行。使用虚拟环境里的 Python 路径可以直接运行，无需激活虚拟环境。

```powershell
Set-Location -LiteralPath 'D:\Work\llxie\robotic-workshop-tools\robot-doc-judge-tools'
py -3.12 --version
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-agent.txt
```

如果 `py -3.12` 报找不到解释器，先用 `py -0p` 核对安装位置，或者以已安装的 Python 3.12 可执行文件全路径执行 `-m venv .venv`。公司内网环境可通过内部 Python 包源安装同一组依赖。

## 2. 离线验证

在尚未设置正式规则和模型配置的终端里运行。测试使用临时数据库、合成 PPTX、模拟模型网关和渲染样本。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-test.txt
.\.venv\Scripts\python.exe -m unittest scripts.test_v2 -v
```

预期三项通过，分别覆盖 PPTX 页映射与流程线、两个 API 与评分续跑、Deep Agents 读取 Skill 并发出带图片的模型请求。实际 LibreOffice 的渲染保真和内网模型的判定质量要用目标环境验证。

## 3. Windows 联调

### 3.1 在一个 PowerShell 会话设置参数

本地采用 SQLite 和项目内的文件目录。示例给 API 生成两枚本次联调专用 Bearer token，并从交互输入读取模型密钥。四个子进程都继承这组参数。

```powershell
$env:RW_DATA = Join-Path $PWD.Path '.local'
New-Item -ItemType Directory -Path $env:RW_DATA -Force | Out-Null
$env:RW_DATABASE = Join-Path $env:RW_DATA 'service.db'
$env:RW_S3_BUCKET = ''
$env:RW_TOKEN_SUBMIT = [Guid]::NewGuid().ToString('N')
$env:RW_TOKEN_JUDGE = [Guid]::NewGuid().ToString('N')

$env:RW_V2_QWEN_URL = Read-Host '主模型 API 根路径，例如 https://内网网关/v1'
$env:RW_V2_QWEN_MODEL = Read-Host '主模型 ID'
$env:RW_V2_QWEN_KEY = Read-Host '主模型 API key' -MaskInput
$env:RW_V2_JUDGE_B_URL = Read-Host '第二评委 API 根路径'
$env:RW_V2_JUDGE_B_MODEL = Read-Host '第二评委模型 ID'
$env:RW_V2_JUDGE_B_KEY = Read-Host '第二评委 API key' -MaskInput

$env:RW_V2_SOFFICE = 'C:\Program Files\LibreOffice\program\soffice.exe'
Test-Path -LiteralPath $env:RW_V2_SOFFICE
```

最后一行预期是 `True`。请按实际 LibreOffice 路径调整，或在 `PATH` 已配置时使用默认的 `soffice`。模型 URL 填 API 根路径，例如 `https://<内网网关>/v1`，由客户端拼接请求路由。

默认 [models-v2.json](../configs/models-v2.json) 使用 `qwen` 做 Checklist、视觉和汇总，评分使用 `qwen` 与 `judge_b`。第二评委通过 `vision: "qwen"` 调用同一个视觉端点，两位评委的评分会话和取证记录独立。不同公司的模型 ID 由对应网关提供。以下是联调时常用的配置项。

| 参数 | 默认值与用途 |
|---|---|
| `RW_TOKEN_SUBMIT` / `RW_TOKEN_JUDGE` | 必填且两值不同，对应提交端与评委端。由官网后端持有。 |
| `RW_V2_MODELS` | `configs/models-v2.json`。`models` 定义 profile 与环境变量名，`checklist`、`judges`、`summary` 选择角色，`vision` 指向读图 profile。评委支持配置 1–3 个不同的 profile。只有一位评委时，可复制模型 JSON 到 `.local` 并改为 `"judges": ["qwen"]`，再指定该副本。正式双评委使用两个不同的模型 ID。 |
| `RW_V2_RULES` | `configs/sample.json`。默认规则版本为 `demo-v2`，作为联调示例。正式环境指向实际规则 JSON。 |
| `RW_V2_TEMPLATE` | 可选的正式空白 PPTX 路径。配置后，提取时比较同一逻辑页的模板对象文字，过滤原样保留的说明文字。 |
| `RW_V2_SOFFICE` / `RW_V2_SCORING_CONCURRENCY` | 渲染程序默认 `soffice`。评分 Worker 的默认并发是 2，可按模型配额调整。 |
| `RW_DATA` / `RW_DATABASE` | 默认使用项目根目录 `.local` 和其中的 `service.db`。可为 `RW_DATABASE` 提供 SQLite 文件路径或 PostgreSQL DSN。 |
| `RW_S3_BUCKET` / `RW_S3_ENDPOINT` | 可选的共享对象存储。配置 bucket 后由 boto3 按环境凭据读取和写入，未配置时文件保存在 `RW_DATA/objects`。 |
| `RW_MAX_BYTES` / `RW_BROKER` | PPTX 上传文件默认上限 30 MiB。Redis 地址用于 Celery 运行方式，本地 Worker 直接从数据库领任务。 |

连接自定义模型配置时，确保这里设置的环境变量名与该 JSON 一致。生产环境通过部署系统注入密钥，源码里只放环境变量名。

### 3.2 切换正式模板与规则

将以下 Windows 路径替换为实际文件位置。空白模板可与业务规则一同验证。

```powershell
$env:RW_V2_RULES = 'D:\review-config\rules-v2.json'
$env:RW_V2_TEMPLATE = 'D:\review-config\blank-template.pptx'
.\.venv\Scripts\python.exe -m scripts.validate_rules $env:RW_V2_RULES
```

以 [sample.json](../configs/sample.json) 的结构为起点。联调阶段可以沿用默认配置，正式接入时填写这些字段。

| 区段 | 填写要求 |
|---|---|
| `version` | 使用本次发布的规则版本。V2 的通过口径允许跨已映射的正文页。 |
| `pages` | 定义模板逻辑页 1–7，以及每页独特的 `anchors`。用于识别章节和定位首选页。 |
| `checklist` | 每项填写唯一 `id`、所属逻辑 `page`、`title`、`requirement` 和 `kind`，可补 `hint`。所有逻辑页需要有条目。沿用 `literal` 的条目要提供 `terms`，V2 仍通过 Agent 评估输入的条目规则。 |
| `rubrics` | A/B/C 分组，每组恰含 A1、A2、A3、B1、B2、B3。每项提供 `name`、`conditions`、`levels`。档位有 `id`、`min`、`max`、`description`，分数范围在 0–100 之间且各档不重叠。可按正式规则指定最低档内的 `missing_score`。 |

规则文件、空白模板、Skill 或模型配置变动时，先让上一版实跑任务结束，然后在同一版本上重启四个进程。改变规则或模板后需要重新上传，让新规则生成证据。

### 3.3 启动四个角色

在设好参数的同一个 PowerShell 里启动后台任务，便于共享环境变量。也可在四个终端里配置相同参数后，分别运行 `.\scripts\windows.ps1 v2-api`、`v2-document`、`v2-checklist`、`v2-scoring`。

```powershell
$project = $PWD.Path
$rwJobs = foreach ($role in @('api', 'document', 'checklist', 'scoring')) {
    Start-Job -Name "rw-v2-$role" -ArgumentList $project, $role -ScriptBlock {
        param($root, $role)
        Set-Location -LiteralPath $root
        & .\scripts\windows.ps1 -Action "v2-$role"
    }
}
$rwJobs | Format-Table Id, Name, State
Receive-Job -Job $rwJobs -Keep
```

| 角色 | 启动的模块 | 职责 |
|---|---|---|
| API | `uvicorn app.v2.api:app` | 监听 `127.0.0.1:8000`，收件、创建任务、查询进度和结果。 |
| Document Worker | `python -m app.v2.worker document` | 打开 PPTX、提取与保存证据，有视觉对象时尝试准备渲染 PDF。 |
| Checklist Worker | `python -m app.v2.worker checklist` | 逐条执行 Checklist Skill 和证据校验。 |
| Scoring Worker | `python -m app.v2.worker scoring` | 并行领模型评委任务，逐项保存分数，再执行汇总任务。 |

HTTP 监听验证可使用不带凭据的请求，预期返回 `401`。这一步只验证监听和鉴权。完整联调继续执行下一节。

```powershell
curl.exe -sS --max-time 5 -o NUL -w "%{http_code}" -X POST http://127.0.0.1:8000/v2/checklist-jobs
```

结束本次后台联调时执行 `Stop-Job -Job $rwJobs`。单个后台角色异常时用 `Receive-Job -Job $rwJobs -Keep` 查看日志。

## 4. API 巡检与完整调用

### 4.1 两条路由

| 路径 | 身份 | 创建任务 | 查询进度或结果 |
|---|---|---|---|
| `POST /v2/checklist-jobs` | Bearer `RW_TOKEN_SUBMIT` | Multipart 表单 `submission_id`、`category`、`pptx`。HTTP `202` 返回 `job_id` 与 `status: "queued"`。 | 同一路径加 `?job_id=<上传任务 ID>`，空请求体。 |
| `POST /v2/scoring-jobs` | Bearer `RW_TOKEN_JUDGE` | JSON `{"upload_job_ids":["<上传任务 ID>"]}`，每批 1–200 个。HTTP `202` 返回同序的 `job_ids`。 | 同一路径发 JSON `{"job_id":"<评分任务 ID>"}`。 |

`submission_id` 使用 1–100 个英文字母、数字、连字符或下划线。`category` 取 `A`、`B` 或 `C`。服务发出的 `job_id` 为 32 位小写十六进制字符串。每次上传都会得到新 ID，官网用该 ID 锁定某次 PPTX。相同上传、规则摘要和模型配置重复发起评分，会得到同一个评分任务 ID。

### 4.2 PowerShell 示例

在已设置 Bearer token 的 PowerShell 7.2+ 会话执行。示例用一份经授权的联调 PPTX。若已安装测试依赖，也可用文件生成示例中的 `proposal(1, 3, flow=True)` 快速创建测试稿。当前规则为样例时，输出只用于验证流转与结果结构。

```powershell
# 可选。在文件路径示例不存在时生成 .local\demo-v2.pptx
.\.venv\Scripts\python.exe -c "from pathlib import Path; from scripts.test_v2 import proposal; Path('.local/demo-v2.pptx').write_bytes(proposal(1, 3, flow=True))"

$base = 'http://127.0.0.1:8000'
$submitHeaders = @{ Authorization = "Bearer $env:RW_TOKEN_SUBMIT" }
$judgeHeaders = @{ Authorization = "Bearer $env:RW_TOKEN_JUDGE" }
$pptx = Get-Item -LiteralPath '.\.local\demo-v2.pptx'

$upload = Invoke-RestMethod -Method Post -Uri "$base/v2/checklist-jobs" -Headers $submitHeaders -Form @{
    submission_id = 'demo-001'
    category = 'A'
    pptx = $pptx
}
$uploadId = $upload.job_id
$upload

function Wait-RwJob {
    param([scriptblock]$Query, [int]$TimeoutSeconds = 600)
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $current = & $Query
        if ($current.status -eq 'failed') {
            throw "Job $($current.job_id) failed at $($current.stage): $($current.error)"
        }
        if ($current.status -eq 'completed') { return $current }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Polling timed out. Reuse job_id $($current.job_id) to inspect progress."
}

$checklist = Wait-RwJob {
    Invoke-RestMethod -Method Post -Uri "$base/v2/checklist-jobs?job_id=$uploadId" -Headers $submitHeaders
}
$checklist.result | ConvertTo-Json -Depth 8

$scoreRequest = @{ upload_job_ids = @($uploadId) } | ConvertTo-Json -Compress
$submission = Invoke-RestMethod -Method Post -Uri "$base/v2/scoring-jobs" -Headers $judgeHeaders -ContentType 'application/json' -Body $scoreRequest
$scoreJobId = $submission.job_ids[0]
$scoreQuery = @{ job_id = $scoreJobId } | ConvertTo-Json -Compress
$score = Wait-RwJob {
    Invoke-RestMethod -Method Post -Uri "$base/v2/scoring-jobs" -Headers $judgeHeaders -ContentType 'application/json' -Body $scoreQuery
}
$score.result | ConvertTo-Json -Depth 10
```

示例在 Checklist 完成后发起评分。接口实际只要求文档证据已建立，Checklist 还在运行时也能提交评分。批次里任何上传缺证据或规则不匹配，整个创建请求返回 `409`。

### 4.3 返回字段和排查

| 场景 | 官网关注的字段 |
|---|---|
| 查询 Checklist | HTTP `200`，见 `status`、`stage`、`result`。完成结果有 `rules_version`、`items`、`summary`。每项有 `item_id`、`template_page`、`status`、`evidence`、`hint`，可能带 `reason`。 |
| 查询初评进度 | `upload_job_id` 关联原 PPTX；汇总写入结果前可以看 `judges[].status`、`judges[].criteria_done`。父任务的 `waiting` 表示仍在等评委，正常情况下其 `stage` 为 `summary`。 |
| 查询初评结果 | `result.criteria` 恰含六项，包含 `mean_score`、`spread`、`valid_judges`、`expected_judges`、`needs_review`、各模型 `judges[].result` 及分项摘要。用 `summary_status` 识别摘要 `pending`、`completed` 或 `failed`。 |
| 显示证据 | `evidence[].page` 是原文件物理页，从 1 开始。`id` 标识对象或裁图区域。`bbox` 为 `[左, 上, 右, 下]`，相对整页归一化为 0–1000。可按页面实际宽高将它换算成像素。 |

Checklist 的 `present` 表示已提供有效内容，可显示 OK。`missing` 表示完成证据覆盖后判缺项。`uncertain` 表示需人工确认。评分项只有在预期评委都给出有效分数时才有均分，其他情况下 `mean_score` 为 `null`。摘要重试期间已计算的评分可查询；摘要耗尽重试时，任务可同时返回 `status: "completed"`、`summary_status: "failed"` 和保留的评分明细。

| 现象 | 从哪里查 |
|---|---|
| API 进程退出 | 核对两枚 token 非空且不同。再看 JSON 文件路径、模板文件路径和端口占用。 |
| HTTP `401` / `404` / `405` | 分别核对 Bearer token、任务 ID 是否属于该接口、是否使用 `POST`。 |
| HTTP `409` | 确认 Document Worker 已解析、来源是上传任务 ID，且上传索引与当前规则一致。 |
| HTTP `413` / `422` | 检查文件与请求体大小、PPTX 格式和必填参数。评分 JSON 请求体上限是 64 KiB。 |
| Checklist 长时间 `queued` | 看 `stage`，检查对应 Worker 是否运行，以及 API 和 Worker 是否共用同一个数据库和对象存储。 |
| `model_unavailable` / `configuration_changed` | 核对各进程的模型 ID、端点、凭据、工具调用能力以及规则和模板文件，尤其是部署更新后的配置一致性。 |
| 文档能解析，图片相关项常为 `uncertain` | 检查 Document Worker 的 `slide rendering unavailable` 告警、LibreOffice 路径和字体，以及视觉模型的图文输入契约。 |

需要给官网导入 API 定义时，可从代码离线导出 OpenAPI。

```powershell
.\.venv\Scripts\python.exe -c "from app.v2.api import app; import json; print(json.dumps(app.openapi(), ensure_ascii=False))" | Set-Content -Encoding utf8 .\.local\openapi-v2.json
```

## 5. 内网部署

从仓库根目录构建 `v2` 镜像阶段。镜像包含 LibreOffice Impress、CJK 字体和两份 Skill。按正式模板补齐字体后检查实际渲染效果。

```powershell
docker build -f deploy/Dockerfile --target v2 -t robot-doc-tools:v2 .
```

API 使用镜像默认入口 `python -m uvicorn app.v2.api:app --host 0.0.0.0 --port 8000`。文档、Checklist、评分分别运行 `python -m app.v2.worker document`、`python -m app.v2.worker checklist`、`python -m app.v2.worker scoring`。这四个角色共用数据库与对象存储，多副本使用 PostgreSQL 和共享对象存储。将非敏感配置、模板和规则挂载到各角色，凭据通过 Secret 注入。

如果部署采用 Celery 调度，三个 Worker 改用下列命令，并单独运行一个 Beat。将 `RW_BROKER` 指向部署环境 Redis。Celery 每约两秒调度各角色轮询，评分线程数仍由 `RW_V2_SCORING_CONCURRENCY` 控制。每个角色选用一种启动方式即可。

```text
celery -A app.v2.worker worker -Q v2-document --concurrency=1
celery -A app.v2.worker worker -Q v2-checklist --concurrency=1
celery -A app.v2.worker worker -Q v2-scoring --concurrency=1
celery -A app.v2.worker beat
```

在 Kubernetes 中用上述镜像为四个角色分别指定启动命令。API 监听可用 TCP 探针检查。文档 Worker 运行在资源受限的非 root 容器，处理文件需要访问数据库和对象存储。内网模型调用由 Checklist 和评分 Worker 发出。
