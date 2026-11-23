# Robotic Workshop 初赛 AI Service 验证记录

## 1. Windows 本地端到端

用七页合成 PDF，经 FastAPI 上传与查询，Windows RTX 4070 上的 PaddleOCR-VL-1.6 完成版面和内容解析。Deep Agents 使用真实 DeepSeek 完成 Checklist、A1–B3 初评和整体评语。三次测试各用独立的提交记录。

| 测试 | Checklist | 评委初评 | 全链耗时 |
|---|---|---|---:|
| 第 1 次 | 13 已提供、1 缺失，跨页证据定位到第 7 页 | 6 项分数、证据与 Summary | 208.9 秒 |
| 第 2 次 | 同上 | 6 项分数、证据与 Summary | 216.4 秒 |
| 第 3 次 | 同上 | 6 项分数、证据与 Summary | 214.5 秒 |
| 审查修复后复测 | 同上 | 6 项分数、证据与 Summary | 213.9 秒 |

同一合成提案的六项分数最大跨度为 10 分。A3 和 B3 三次相同。该结果用于观察重复运行的波动，正式业务验收仍需用双评委标签衡量分数误差、等级一致率及评语依据。详见[规则与评分评测说明](Operations.md#3-替换成公司七页模板和-a1b3-评分表)。

## 2. 审查与修复

- PDF 上传在进入 multipart 解析前校验服务凭据和请求大小。更新 FastAPI、Starlette、Uvicorn 与 python-multipart，避开已公开的上传处理问题。升级后的 API 回归连续三次通过。 [Starlette 公告](https://github.com/advisories/GHSA-2c2j-9gv5-cj73) · [python-multipart 公告](https://github.com/advisories/GHSA-pp6c-gr5w-3c5g)
- 长任务续租，提交结果前在事务中核对作业状态、租约和最新 revision。回归覆盖并发重复提交、租约接管和旧版任务迟到。
- 正式规则要求显式配置模型地址、模型 ID 和密钥，本地文件仅供模拟规则使用。评分校验会拒绝非法分数区间和无效证据引用。
- 6 项针对性回归、Windows Uvicorn 本地回环健康检查、规则校验、OpenAPI 生成、Python 编译和 K8s YAML 语法检查通过。

## 3. 部署验收范围

RTX 4070 上已验证 GPU 运算、PP-DocLayoutV3 和 PaddleOCR-VL-1.6 实际推理，以及两套权重的离线 SHA-256 校验。本地使用 SQLite 和文件存储，仅合成样本调用外部测试模型。公司 K8s、PostgreSQL/Redis/S3、正式规则、真实提案以及目标环境的吞吐和 P95 仍待验收。Windows CUDA 运行时的 cuDNN 版本警告和处理步骤见 [模型部署手册](PaddleOCR-VL-1.6.md)。
