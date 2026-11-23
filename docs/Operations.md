# Robotic Workshop 初赛 AI Service 运行手册

实施与验收按五步推进：接收官网 PDF 并为 revision 创建作业 → 运行 PaddleOCR 并保存证据 → 由 Deep Agents 判 Checklist → 截稿后按 A1–B3 初评并汇总 → 通过独立 API 返回参赛者和评委结果。公司环境验收按相同顺序执行，最后换入正式规则和真实验收集。

## 1. 在 Windows 本地安装和验证

在项目根目录运行。虚拟环境使用 64 位 Python 3.12。CUDA 12.6 的 Paddle GPU wheel 已在 RTX 4070 上执行矩阵运算。

```powershell
py -3.12 -m venv .venv
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install -r requirements.txt -r requirements-agent.txt -r requirements-test.txt
& $python -m pip install "paddlepaddle-gpu==3.2.1" --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu126/
& $python -m pip install -r requirements-ocr.txt
.\scripts\windows.ps1 download
.\scripts\windows.ps1 smoke -LiveLlm
```

模型目录默认为 `.local/models/layout` 与 `.local/models/vl`。内网部署可从审批后的存储复制权重，并通过 `RW_LAYOUT_DIR` 与 `RW_VL_DIR` 指向只读目录。自带下载脚本按仓库 revision 保存下载清单。测试 PDF 和 Checklist、评审结果 JSON 生成于 `.local/api-*`。只验证 HTTP 契约和规则时，执行 `.\scripts\windows.ps1 native-smoke`。增加 `-LiveLlm` 会把合成提案证据交给配置中的模型处理。

初赛使用 LangChain Deep Agents 执行 Checklist 语义判断及 A1–B3 评分。每次调用仅处理当前提案的规则与证据，使用内存状态，且模型未开放文件、网络、命令执行和子 Agent 工具。结果由服务校验后再写入。

使用 `demo-v1` 模拟规则时，可以从父目录的 `deepseek.txt` 读取本地 DeepSeek 设置，也支持现有文件拼写 `deeepseek.txt`。正式规则要求显式配置全部三项 `RW_LLM_URL`、`RW_LLM_MODEL` 和 `RW_LLM_KEY`。在公司环境通过 Secret 注入并限制评估 Worker 的网络访问，只允许调用公司批准的内网端点。正式提案保存在公司环境。

## 2. 启动和对接 API

在启动 API 的 PowerShell 窗口由平台配置两个不同的服务间凭据 `RW_TOKEN_SUBMIT` 和 `RW_TOKEN_JUDGE`。四个窗口分别运行以下命令，每个窗口使用相同的项目根目录和 `RW_RULES` 配置。Windows 本地默认使用同一 SQLite 与本地文件目录，轮询 Worker 按阶段分开运行。

```powershell
.\scripts\windows.ps1 api
.\scripts\windows.ps1 document
.\scripts\windows.ps1 checklist
.\scripts\windows.ps1 scoring
```

| 操作 | 请求 |
|---|---|
| 上传 | `POST /v1/checklist-jobs`，multipart 包含 `pdf`、`submission_id`、`revision`、`category`、`request_id`，返回 `job_id`。 |
| 等待结果 | `GET /v1/jobs/{job_id}`，轮询到 `completed` 或终态。 |
| 重试失败作业 | `POST /v1/jobs/{job_id}/retry`，使用对应的提交或评委凭据。只允许重排当前 revision 和规则版本的 `failed` 作业，保留原作业 ID。 |
| 读取 Checklist | `GET /v1/submissions/{submission_id}/checklist`。 |
| 截稿后初评 | `POST /v1/scoring-jobs`，JSON 为 `{"submissions":[{"submission_id":"DEMO-001","revision":1}]}`。返回 `job_ids`。每批最多 200 份，证据就绪且版本有效时才接收。 |
| 读取评分 | `GET /v1/submissions/{submission_id}/evaluation`，含六项 `criteria` 及整体 `summary.text`、`strengths`、`gaps` 和 `review_points`。 |
| 看 API 规格 | 浏览器打开 [本地 API 文档](http://127.0.0.1:8000/docs)，或查看 `/openapi.json`。 |

官网后端代用户调用，访问令牌只保存在后端。参赛者用提交权限查看清单，评委初评使用评委权限。结果与当前有效 revision 绑定，重复请求返回已有作业。缺乏可靠判分依据时该项返回 `score: null` 和人工复核标记。任务状态 `completed` 表示计算结束，评估结果可单独携带复核标记。

## 3. 替换成公司七页模板和 A1–B3 评分表

**公司规则文档需逐项转为版本化 JSON。** 将正式原文存放在公司内部，复制 [configs/sample.json](../configs/sample.json) 为正式配置，保存在公司审批的目录。保留原文条目编号及版本关联，由业务或评委复核填写结果。

| 正式材料 | JSON 位置 | 怎么填 |
|---|---|---|
| 七页模板 | `pages` | 填写 1–7 的逻辑页码，并为每页提供有区分度的标题或固定标识 `anchors`。查看真实 PDF 是否能识别这些标识。 |
| 每页必填 Checklist | `checklist` | 每个要求一行配置。设置稳定的 `id`、逻辑 `page`、展示 `title`、判断用 `requirement`，可加 `hint`。原文要求明确填写某个固定字符串时用 `kind: literal` 和 `terms`，其余用 `semantic`。 |
| 错页、加页和调序规则 | `cross_page` | `allow` 接受跨页证据，`require_expected_page` 按指定页判定，`review` 将跨页命中留待人工确认。等待 PM 确认后选择。 |
| A/B/C 分组评分表 | `rubrics.A`、`rubrics.B`、`rubrics.C` | 各填 A1、A2、A3、B1、B2、B3。每项填写 `name`、可核实的 `conditions` 和等级列表 `levels`，等级包含 `id`、`description`、`min`、`max`。区间端点包含在内，区间间避免重叠。平台负责总分与最终排名。 |
| 版本 | `version` | 每次正式发布改用唯一版本，结果同时保存文件 SHA-256。 |

```powershell
$env:RW_RULES = "D:\CompanyApproved\robotics\preliminary-rules.json"
.\.venv\Scripts\python.exe -m scripts.validate_rules $env:RW_RULES
```

排空旧规则任务后一起重启 API、文档 Worker 和两种评估 Worker。相同提交版本可以按新规则重建 Checklist，再重新触发评委初评。评估时用两位评委独立打分，并通过 API 重跑独立测试提交，避免读到幂等缓存。将逐项人工对照、分数误差、证据和 Summary 校验结果纳入验收。

若正式 Rubric 对缺证规定固定分数，在对应评分项加上 `missing_score`，数值须在该项最低档区间内。仅当输入证据未截断、各页可读且所有条件都判为缺证时使用此分数。其余无法评分的情况交人工复核。

评测命令是 `python -m scripts.compare manifest.json`。Manifest 为数组，每个样本包含 `checklist_run`（Checklist JSON 路径）、`checklist_expected`（以 item ID 为键，值为 `{"status":"present","evidence_pages":[2]}`）、`scoring_runs`（多个评分 JSON 路径）和 `human`（A1–B3 每项对应两个 `{"score":75,"level":"approved-level"}` 对象）。

## 4. 公司内网部署

从仓库根目录使用 `deploy/Dockerfile` 的 `runtime` 和 `document` 两个构建目标。部署模板在 `deploy/k8s/workloads.yaml`。将模板中的镜像地址改为已审批的内网镜像，并准备：

- `rw-rules` ConfigMap，键为 `company.json`。
- `rw-config` ConfigMap，设置内网 S3 endpoint、bucket 等非敏感配置。
- `rw-infrastructure` Secret，注入 `RW_DATABASE`（PostgreSQL 包含 SSL 配置）、`RW_BROKER`（Redis）和对象存储凭据。
- `rw-api-tokens` 和 `rw-llm` Secret，分别注入服务间令牌和内网模型配置。
- `rw-models` PVC，预填 `layout`、`vl` 和下载清单。确认 GPU 节点和对应设备插件可用。

内网网关负责 TLS、上传大小上限和调用方流量控制。按公司网络策略限制 API 入站、Worker 出站和模型访问。对象存储的保留生命周期由上线环境配置。Celery Beat 用 Redis 调度三个队列，PostgreSQL 保留待处理、重试和处理租约。生产环境需验证 Redis/PostgreSQL 运行、GPU 时延、并发和正式规则准确性。

## 5. Deep Agents 与后续评委助手

Deep Agents 作为官网评委侧的会话入口，调用初赛服务查询任务、Checklist 和六项初评。先接入只读的证据页定位和评分标准解释，后续将人工备注或复核动作交由平台授权与确认。Agent 侧用经批准的模型和现有平台身份，初赛 API 持有版本、结果和评分规则。接入公司材料时，文件工具和外部网络能力按业务权限配置。产品参考见 [Deep Agents](https://github.com/langchain-ai/deepagents)。
