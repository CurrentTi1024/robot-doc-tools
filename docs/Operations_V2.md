# V2 初赛评审接口与运行

V2 应用入口为 `app.v2.api:app`。上传 PPTX 与启动评分均为后台作业。官网使用同一入口提交和查询，每份评级绑定上传任务 ID。

## 两个 HTTP API

| 路径 | 启动作业 | 查询作业 |
|---|---|---|
| `POST /v2/checklist-jobs` | Multipart 表单 `submission_id`、`category`（A/B/C）、`pptx`。返回 `202` 与 `job_id)。 | `POST /v2/checklist-jobs?job_id=<id>`，空请求体。 |
| `POST /v2/scoring-jobs` | JSON `{"upload_job_ids":["<上传任务 ID>"]}`，最多 200 个。返回 `202` 与同序 `job_ids`。 | JSON `{"job_id":"<评分任务 ID>"}`。 |

Checklist 使用 `Authorization: Bearer <RW_TOKEN_SUBMIT>`，评分使用 `Authorization: Bearer <RW_TOKEN_JUDGE>`。两个值必须不同。查询返回 `200`、当前 `status` 和已有 `result`。评分允许上传证据已就绪的提案开始，Checklist 未完成时也可以调度。证据还未就绪返回 `409`。格式损坏返回 `422`。附加页不作为 Checklist 条目或页数错误。

Checklist 条目状态为 `present`、`missing`、`uncertain`。证据包含实际页码、对象 ID 和归一化区域坐标。初评返回六项的 `mean_score`、评委人数、分差、单评结果和摘要。两位评委中有一位无法评分时，该项 `mean_score` 为 `null`。模型汇总重试期间已计算的分数仍可查询。

当前实现每次向 Agent 分配一个 Checklist 条目。Agent 先读首选页，再按需要查其他正文页和局部图片。原生连接线可直接提取端点。曲线、弯折或分支标签归属不明时，可调用裁图工具核对原图。

## Windows 运行

在仓库目录的 Python 3.12 虚拟环境安装。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-agent.txt
```

在四个进程的环境中设置相同的配置。用 `RW_V2_RULES` 指向正式规则 JSON，`RW_V2_TEMPLATE` 指向空白 PPTX，`RW_V2_MODELS` 指向多模型配置。缺省的 [sample.json](../configs/sample.json) 只提供样例规则。部署两位评委默认读取 [models-v2.json](../configs/models-v2.json)，其中两个 URL 使用公司批准的 OpenAI 兼容接口根路径，例如 `https://<内部网关>/v1`。分别设置该配置指定的 URL、实际模型 ID 和密钥。启用读图还需要 `RW_V2_SOFFICE` 指向 LibreOffice 的 `soffice`，以及与提案匹配的字体。正式规则可在现有 Rubric 的条件与等级说明中填写数值公式，评分 Agent 可调用比率计算工具核对证据数值。

在四个独立终端启动以下角色。

```powershell
.\scripts\windows.ps1 v2-api
.\scripts\windows.ps1 v2-document
.\scripts\windows.ps1 v2-checklist
.\scripts\windows.ps1 v2-scoring
```

## 内网部署

用 `docker build -f deploy/Dockerfile --target v2 -t robot-doc-tools:v2 .` 构建镜像。API 使用镜像默认入口。各角色可以使用 `python -m app.v2.worker document`、`python -m app.v2.worker checklist`、`python -m app.v2.worker scoring` 独立运行。使用现有 Celery/Redis 调度时改用 `celery -A app.v2.worker worker -Q v2-document` 等队列，配合一个 `celery -A app.v2.worker beat`。每个 scoring Worker 默认同时执行两位评委。使用 Celery 时建议先用 `--concurrency=1`，再按实测吞吐调整 `RW_V2_SCORING_CONCURRENCY` 和副本数。

通过 `RW_DATABASE` 连接 PostgreSQL，`RW_S3_BUCKET` 与 `RW_S3_ENDPOINT` 配置内网对象存储。单机测试可使用缺省的 SQLite 和本地文件。文档 Worker 负责打开参赛者上传文件，应运行在资源受限的非 root 容器，网络只允许访问数据库和对象存储。用 TCP 探针检查 API 监听。OpenAPI 可离线导出。

```powershell
.\.venv\Scripts\python.exe -c "from app.v2.api import app; import json; print(json.dumps(app.openapi(), ensure_ascii=False))"
```
