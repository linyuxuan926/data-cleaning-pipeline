# 可运行交付说明

> 后续增量已继续开发：磁盘引擎、分页、子进程时限、保留策略、schema3字段绑定和操作/事件幂等的实际状态见 [V3补齐交付](V3-COMPLETION.md)。以下保留上一轮已验收交付及其当时缺口和测试证据，不能把历史缺口表当成当前实现状态。当前新规则schema3默认v3-disk，旧规则/历史任务仍保留原引擎；迁移已扩展至V7。

本文件描述当前实际代码。`docs/v3/` 保留完整目标设计，**不是所有设计项均已完成**。现有 V2 原件和版本保留；新增默认使用 `v3-sequential` 顺序引擎与独立执行历史。测试记录见文末，不把旧测试结果代替本轮验证。

## 启动和账户

在仓库根目录执行，需要 Docker Desktop Linux containers / Docker Engine、Compose v2，以及 Python 3.11：

```powershell
python scripts/bootstrap.py
docker compose config --quiet
docker compose up -d --build
docker compose ps
```

访问 http://localhost:8088 。注册即为普通用户。已有本地管理员用户名/密码在 `.env` 的 `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`，不写入交付文档。初次启动自动建立数据库、Flyway V1–V4、私有桶、队列和管理员；不需要手工建表。已有库升级只应用新增迁移，不重置用户、规则或文件。

本次留在主环境的评审账户为 本地随机生成的验收账户，密码及三个真实样例任务的 ID 见 [本地评审账户文件](../.test-output/local-demo.json)。这是仅供本机验收的普通账户，不是生产初始化账户。也可运行 `python scripts/create_demo.py` 创建或复用本地评审账户：它真实上传三种格式、保存规则并等待任务成功，不向页面注入静态数据。全新环境中脚本会生成新的随机账户。

```powershell
docker compose stop
docker compose start
docker compose logs --tail=100 backend worker
```

`docker compose down` 保留持久卷；清空所有本地业务数据才使用 `docker compose down -v`，这不是升级步骤。复制到全新环境时复制源码和 `.env.example`，再执行 bootstrap；不要复制旧 `.env` 或隐藏本机配置。

服务名保留 `backend` 和 `worker`，分别对应设计中的 Java API 和 Python Worker，避免破坏已有内部地址与运维命令。MySQL、MinIO、RabbitMQ、Python 均在内网，只发布本机 8088。七个服务均有健康检查，Worker 的健康文件由 AMQP 主循环更新，不能代表每个规则都能即时中断。

## 已有及本轮实现

| 页面 / 能力 | 实际入口与行为 |
| --- | --- |
| 登录、注册、当前用户、退出 | `/api/auth/*`；USER/ADMIN，禁用即时撤销会话 |
| 文件和预览 | 工作台；CSV/XLSX/Parquet，真实分片、SHA-256、MinIO 原件、CSV 编码/分隔符、Excel 单表、字段建议/覆盖 |
| 上传恢复 | `/api/uploads/{uploadId}` 返回实际已落盘分片；同一浏览器会话刷新后重选同名/同大小/同 SHA-256 文件继续上传；单片最多尝试3次（首次加两次重试） |
| 文件详情 | `/api/files/{id}`；原件保存成功和解析成功分别显示 |
| 规则库、编辑、复制、版本 | `/api/rule-sets` 及 `/{id}/versions`；另存为新规则集；Python 权威校验；快照冻结 |
| 异步任务 | `/api/tasks`；MySQL/outbox → RabbitMQ → 独立 Worker → MinIO → Java；新任务默认严格数组顺序 |
| 防重复提交 | `POST /api/tasks` 可传 `Idempotency-Key`；7天内同键同请求返回原创建响应，异内容409；前端未确认请求复用原键 |
| 取消、重试、历史 | QUEUED → CANCELED；RUNNING → CANCELING → Worker确认后CANCELED；重试新建 UUID attemptId，旧状态/日志/报告/产物不覆盖 |
| 任务详情、日志 | 实时查询；“执行尝试”→“查看”切换对应日志；成功尝试另提供报告、预览和下载；取消/重试只操作当前尝试 |
| 报告、结果与脏数据 | `/api/tasks/{id}/report`、`/preview?kind=result\|dirty`、`/artifacts/{kind}`；历史位于 `/attempts/{attemptId}` 下 |
| 平台管理 | 用户角色/禁用、全平台任务只读、系统限额；普通用户无法调用管理接口 |

所有上述文件、任务、规则、日志、报告、预览、下载均在 Java 校验归属；管理员可读取全平台任务，但执行控制只对本人资源有效。下载通过鉴权代理，桶非公开。Python 只接受平台对象引用和内部 token，不写业务数据库。

## 验收操作

1. 注册普通用户，上传 `samples/customers.csv`，查看原件成功/解析成功与字段预览。邮编等字段默认保留文本，推断不自动转换。
2. 在工作台按 `samples/rules.json` 手工逐条配置，保存规则并创建任务；当前没有 JSON 文件导入按钮。也可登录上述评审账户直接选取已保存的“客户清洗验收规则”。规则列表顺序就是新任务执行顺序；历史 V2 标为旧阶段顺序。
3. 在任务中心查看排队/执行/完成、日志和创建快照。报告应为6输入、3输出、3最终移除，输出 id 为1、2、5。
4. 进入“结果预览”“违规明细”，下载结果、脏数据、JSON报告；比对报告与实际文件。XLSX/Parquet 分别上传 `samples/customers.xlsx` / `.parquet` 重复操作。可选择输出三种格式。
5. 上传 `samples/multi-sheet.xlsx`，切换“客户”工作表，手动将邮编保留为文本；预览中应保留00101等前导零。
6. 保存规则新版本，历史任务快照和结果不变。构造不存在的字段会明确失败，不会静默忽略。失败后点击重试，看到新 UUID 和两个独立失败尝试，分别查看日志。
7. 通过故障脚本检查排队/运行取消、重复消息、迟到回调和 Worker 重启。它们仅用于独立本机环境并须串行运行。
8. 注册第二个普通账户，将文件/任务/attempt ID 替换为第一个用户的值；API 应拒绝预览、报告、日志和下载。管理员的查看权限另有明确边界。

自动化验收命令：

```powershell
python scripts/smoke_test.py
python scripts/acceptance_v3.py
python scripts/acceptance_v2.py
python scripts/reliability_test.py
python scripts/acceptance_v3_reliability.py
python scripts/worker_recovery_test.py
cd tests/browser
npm ci
npx playwright install chromium
npm test
```

`acceptance_v2` 明确选择旧引擎以验证原有语义，且临时调整/恢复系统设置和 Worker；故障脚本会临时停止服务并在 finally 恢复。不要并行执行这些脚本。纯规则测试：`.venv/Scripts/python.exe -m pytest -q`（工作目录engine，先按[测试说明](TESTING.md)安装requirements-dev.txt），前端 `npm test` 和 `npm run build`（frontend），Java由 `docker compose build backend` 执行固定 Maven/JDK 的 `mvn verify`。

实际路由的完整机器可读协议见 [OpenAPI](openapi.json)，`python scripts/generate_openapi.py --check` 校验文档与源代码路由、规则参数及引用的一致性。内部接口不向浏览器发布；OpenAPI 文件本身不是在线 Swagger 服务。

## 规则、计数与故障语义

当前公共规则形状沿用 `columns`/`column`，引用稳定 f0、f1 或原始名称；不是目标设计中的 logicalFieldId/fieldBindings。支持19类：trim、case、missing、fill_null、fill_stat、drop_null、cast、range、iqr、required、enum、length、regex、validate_type、compare、deduplicate、select_fields、drop_fields、rename。参数见 `docs/RULES.md`；新引擎仅改变顺序与即时处置，不把尚未实现的21类设计配置当成可执行配置。

`v3-sequential` 在每条规则结束后立即删除drop行；删除后不参加后续统计。mark/null保留并收集问题。当前缺失识别、全空统计策略、众数平局取最早来源、全任务first/last去重、IQR全局分位数均有测试。全局操作对完整存活 DataFrame 计算，**没有声称已分块执行**。转换失败有错误动作，int64不经过float；`number`不带precision仍为浮点，带precision用Decimal舍入；目标设计的decimal/scale精确域尚未发布。

V3报告明确满足 `inputRows = outputRows + finalRemovedRows`、`outputRows = validOutputRows + flaggedOutputRows`、`sum(finalRemovalByRule) = finalRemovedRows`。Java校验这些关系。issueCount是问题明细条数，issueRows是有问题的唯一来源记录；同一记录多个问题不会重复计为移除。missingFilledCells/typeConversionFailedCells为单元格事件；outlierHandledRows为至少一次异常命中的唯一来源记录。

V3脏数据包含原始记录、`_source_row`（从1开始的逻辑数据记录，不是CSV物理行号）、`_file_id`、`_sheet`、`_rule_id`、`_before_value`（该规则执行前的类型化JSON）、`_action` 和 `_final_action`。同一行可有多条问题；早期标记后被后续规则删除的去向可追溯。CSV/XLSX输出对公式前缀加单引号并在报告告知；Parquet不做文本转义。

RabbitMQ至少一次投递，outbox与任务同事务，发布确认后标已发送。Java验证UUID attempt、数字序号、lease及状态；旧/终态回调不会改变新结果，进度单调。失联120秒后按30秒调度扫描，新attempt恢复最多至第3次；旧attempt先明确FAILED。取消停止未确认不会谎报CANCELED，而以CANCEL_UNCONFIRMED失败。运行中的底层库调用可能等待当前调用返回；不承诺即时取消。HTTP响应和执行日志提供requestId，日志还带attemptId；Worker生命周期日志含taskId/attemptId/requestId而不输出整行数据。

## 与完整V3设计的差距及限制

代码与设计冲突时的取舍：保留 V2 的 `/api`、`backend`/`worker` 服务名和已有规则参数形状，避免破坏旧客户端；通过显式引擎版本隔离新旧执行语义，通过新增 V3/V4 迁移保存今后的尝试历史。完整 V3 文档中的 `/api/v3`、逻辑字段绑定、SQLite scratch 和事件账本是后续目标，不能作为当前接口调用。原 V2 已被覆盖的早期尝试无法重建，迁移仅回填实际仍保存的当前尝试，无法归属的旧日志保留空 attemptId。

- 仍为受资源预算限制的完整DataFrame执行，尚未实现SQLite scratch、多遍磁盘引擎和跨分块执行，因此不承诺流式容量。全局正确性已测，跨分块算法尚无可验收实现。
- 保留既有 `/api`，没有宣称实现全部 `/api/v3`、cursor分页、规则删除、逻辑字段绑定或21类完整schema。列表仍为有界最近记录（文件/规则/任务200、管理员500），前端任务只提供状态筛选；大历史库需要后续分页。
- `compare_fields`、字符串替换及新decimal/scale配置尚不支持，非法类型/额外参数明确拒绝。缺字段/投影后引用在Worker全量预检或执行阶段失败，并未全部提前到任务提交。
- 仅创建任务有幂等键；手动重试尚无操作幂等键。事件没有V3设计中的eventId/seq去重账本；终态、UUID/lease和单调进度负责结果正确性，重复运行心跳日志可能重复。
- 上传记录复用既有files表和持久上传卷；未拆分完整V3 upload_sessions/chunks数据模型。恢复依赖原账号、同源标签页的sessionStorage，仍需重选同一文件；关闭标签页、清理会话或上传过期后不能保证续传。没有独立暂停/恢复页面。
- 未完成上传24小时清理已实现。成功结果、原件、失败孤立对象及历史日志的自动保留期清理尚未实现，当前保留，需监控磁盘；不能声称30/90天自动销毁已启用。
- 暂无任务最长运行时限或强制隔离子进程；心跳正常但底层长期阻塞时需人工干预。并发限制是活动任务总数，实际全局执行并发由Worker副本数控制。
- 仅单Java节点，本地上传卷未设计多Java共享写。未作生产并发、TLS入口、漏洞审计或多机灾备认证。依赖版本仍沿用已锁定基线，没有顺手升级框架。

硬上限是保护阈值而非性能承诺：100MiB文件、500000行、200列、5000000单元格、Excel展开512MiB、逻辑文本256MiB、单DataFrame估算512MiB、单产物256MiB、问题100000条。多个副本与脏数据可能共存，512MiB不是整个Worker峰值；预览只保证样本可读，完整文件仍可能在后续行明确失败。

## 本轮实测记录

验证日期：2026-11-06。运行环境为Windows Docker Desktop Linux containers；Docker报告32个逻辑CPU、8155533312字节可用内存。以下是本地实际执行记录，未声称托管 CI 已运行。完整结果和命令记录写入 `.test-output/v3-*`；该目录不属于部署必需配置。

| 实际命令 / 检查 | 结果 | 本地证据 |
| --- | --- | --- |
| `docker compose build backend python-api frontend` | 镜像构建成功；Java `mvn verify` 21项，0失败/错误；前端类型检查和生产构建通过 | `v3-final-build.log` |
| engine内 `.venv/Scripts/python.exe -m pytest -q`、`-m pip check` | 181通过，1条上游弃用警告；依赖无冲突 | `v3-engine-tests-final.log`、`v3-pip-check.log` |
| frontend内 `npm test` | 9项通过，覆盖规则参数、报告、提交幂等辅助逻辑 | `v3-frontend-tests-final.log` |
| `python scripts/smoke_test.py` | 三格式输入/输出及再次导入、报告/下载/权限/失败重试、5万行多分片通过 | `v3-smoke-final.log` |
| `python scripts/acceptance_v3.py` | 缺片拒绝、真实续传、摘要、新旧顺序差异、幂等创建、历史预览和隔离通过 | `v3-acceptance-final.log` |
| `python scripts/acceptance_v2.py` | 原有角色、解析、规则与快照回归通过 | `v3-v2-regression.log` |
| 浏览器 `npm test` 前三组及 `npm run test:resume` | 四组均通过；真实页面操作、报告/历史、鉴权下载、断网刷新后续传 | `v3-browser-full.log`、`v3-browser-resume.log`及四组截图目录 |
| `python scripts/reliability_test.py` | RabbitMQ停机后outbox恢复、排队取消、活动任务限额通过 | `v3-reliability.log` |
| `python scripts/acceptance_v3_reliability.py` | 重复真实消息、重复终态回调、运行协作取消、迟到成功和旧attempt回调通过 | `v3-cancel-duplicates.log` |
| `python scripts/worker_recovery_test.py` | 20万行/32规则真实RUNNING时SIGKILL，原120秒租约自然过期后attempt=2成功；逐行验证20万行 | `v3-worker-recovery.log`；从kill到完成145.0秒 |
| 独立空卷项目 `data-cleaning-pipeline-v3-clean` 启动并执行smoke | V1→V4自动迁移，7服务healthy，三格式完整闭环通过 | `v3-clean-start.log`、`v3-clean-services.log`、`v3-clean-smoke.log` |
| `python scripts/generate_openapi.py --check` | 45操作/43路径/92模型/512引用及19类规则参数校验通过 | `v3-openapi-check.log` |

容量小样实测命令：`python scripts/capacity_probe.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-clean`。CSV为1,236,405字节、50,001行×3列，并发1；执行trim、数值cast、全局mean填充、median统计、IQR、保留最后一条去重。含排队7.188秒，引擎报告5.04秒；重启Worker后采集进程RSS高水位255,940KiB（约249.94MiB）。5,000条输出全部逐行校验，全局mean/median/Q1/Q3独立oracle、45,001条重复移除、1条标记保留及报告守恒均通过。详细配置见 `v3-capacity.json`。median统计是在mean填充后计算；没有将该组合描述为另一次median填充压测。**这是小样观测，不是容量上限或流式性能承诺。**

交付结束时保留主项目8088七服务运行；独立空卷验收项目已停止，卷保留。`v3-services-final.log`记录最终服务状态；`v3-handover-check.json`记录评审账户的真实三格式任务、日志关联和下载复核结果，均不包含密码或令牌。

## 修改模块与后续工作

- Java：TaskService/API/Messaging及上传、产物预览、请求关联；新增V3独立尝试/幂等和V4日志关联迁移；集成测试。
- Python：规则顺序与计数/脏数据、Worker取消和租约、产物预览、表头校验及安全日志；相应单元测试。
- Vue：上传恢复、规则顺序、新旧引擎提示、任务幂等提交、独立尝试/日志/报告和产物预览；单元及浏览器测试。
- 工程：Compose Worker健康检查、样例账户脚本、V3/故障/容量验收脚本、OpenAPI生成校验、CI配置及启动/部署/接口/规则说明。

本目录没有Git元数据，无法提供Git差异或提交号；未执行reset，也未删除原有数据卷、`.env`或旧测试证据。下一步优先实现有界SQLite多遍执行、服务端分页、提交前字段生命周期校验、任务deadline和产物引用清理，再扩展逻辑字段绑定/精确decimal及新规则类型；这些均没有计入当前完成项。
