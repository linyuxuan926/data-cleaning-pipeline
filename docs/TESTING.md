# 测试与验证

当前能力和最终运行证据以[V3 完成交付](V3-COMPLETION.md)为准。本文保留 V2 和上一轮 V3 的历史测量，不能用其 DataFrame 数据推断 v3-disk 的吞吐或资源消耗。

## 分层验证

后端测试使用 Spring Boot Test / MockMvc、H2 MySQL mode 验证业务事务、权限和状态机，只在该层替换对象存储适配器。Python 使用 pytest 验证真实 CSV / XLSX / Parquet 读写及规则语义，在确定性故障测试中替换 HTTP / 队列适配器。生产代码没有测试桩开关。

端到端脚本通过真实 Nginx、Java、MySQL、RabbitMQ、Python Worker 和 MinIO，所有文件、任务、日志与产物均真实读写。浏览器测试不拦截业务请求。这样既可定位单个规则错误，也能发现跨语言协议、Linux 依赖、对象存储和消息拓扑的集成问题。

## 可重复命令

项目根目录启动 Compose 后：

```sh
python scripts/smoke_test.py
python scripts/acceptance_v3_disk.py
python scripts/acceptance_v3.py
```

以下脚本会改变系统设置或停止服务；只在为其准备的独立本机验收部署串行运行。旧脚本默认选择项目 `data-cleaning-pipeline`，不能直接指向保留业务数据的主部署：

```powershell
# acceptance_v2没有--project，Compose操作通过环境变量选择隔离项目
$env:COMPOSE_PROJECT_NAME='data-cleaning-pipeline-v3-next'
$env:WEB_PORT='8089'
python scripts/acceptance_v2.py --base-url http://localhost:8089
python scripts/reliability_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next
python scripts/acceptance_v3_reliability.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next
# 额外约 3 分钟：真正 SIGKILL 执行中的 Worker，等待原定租约恢复
python scripts/worker_recovery_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next
```

smoke 支持 `--base-url http://localhost:8088 --timeout 180`。每次创建独立账号，避免共享固定账号和资源；数据留在服务中用于追踪。acceptance_v2 从本地 `.env` 读取初始管理员凭据，验证角色、系统设置、独立上传 ID、SHA-256、CSV 手动参数、工作表和字段覆盖、多原因和失败策略、统计和转换边界以及任务快照；会暂时修改系统设置并停止 Worker，finally 恢复。reliability 会临时停止本项目 RabbitMQ 和 Worker，只在独立本机验收部署运行，并在 finally 恢复服务。worker_recovery 使用 20 万行 / 32 条规则的真实任务，观察到 RUNNING 后 SIGKILL Worker，保留 120 秒租约及正常恢复调度，不改数据库。以上脚本和浏览器验收必须串行运行，且仅用于独立的本机验收部署。

后端，在 backend 目录使用 Java 17 / Maven 3.9.9：

```sh
mvn verify
```

Python，在 engine 目录使用 Python 3.11：

```sh
python -m venv .venv
# Linux/macOS
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip check
.venv/bin/python -m pytest -q
# Windows 的解释器路径为 .venv/Scripts/python.exe
```

前端，在 frontend 目录使用 Node 24.11.0+：

```sh
npm ci
npm test
npm run build
```

真实浏览器，在 tests/browser 目录：

```sh
npm ci
npx playwright install chromium
npm test
```

浏览器测试默认访问 8088，可设置 `BASE_URL`。使用 Playwright 1.56.1 / Chromium；`npm test` 依次执行 smoke.mjs、expanded.mjs、browser-v3.mjs、upload-resume.mjs 和 disk-v3.mjs。expanded/disk-v3 需要本地 `.env` 的初始管理员凭据；disk-v3 要求清理策略保持默认关闭，只生成预览计划。截图和下载证据位于 `.test-output/browser/`、`browser-v2/`、`browser-v3/`、`browser-v3-upload-resume/`、`browser-disk-v3/`，覆盖登录、真实上传与断网恢复、工作表/字段覆盖、规则编辑、报告/产物预览、历史 attempt、管理员只读边界、手机布局和实际下载。新增用例验证 Decimal HALF_UP、replace、compare_fields、DISABLED、冻结绑定，以及翻页间新增记录时 asOf 稳定和筛选重置游标。

隔离项目的当前 V3 验收命令如下；先按[交付说明](V3-COMPLETION.md)启动独立项目和空卷，避免与其他测试并行：

```sh
python scripts/acceptance_v3_disk.py --base-url http://localhost:8089
python scripts/acceptance_v3_admin.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --allow-faults
python scripts/timeout_isolation_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next
python scripts/capacity_disk.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --sizes 10000,50000
```

admin 脚本插入明确标记的分页元数据 fixture，并仅对自己新建对象验收清理；`--allow-faults` 会短暂停止该隔离项目的 MinIO。timeout 脚本检验执行子进程失联后的超时隔离。容量命令是复现入口，是否已运行及测量值以最新交付的实际证据为准。

## 验收覆盖

| 层次 | 主要覆盖 |
| --- | --- |
| Java | 会话认证 / 撤销、内部密钥、所有权和管理员隔离、禁用及最后管理员保护、SHA-256、独立上传 ID、解析失败恢复、配置硬上限和不可变任务快照、规则版本、事务 outbox、限流、claim、旧回调拒绝、取消 / 重试、租约恢复、报告计数及产物验证 |
| Python | schema3 的 21 类规则和历史 19 类规则；磁盘批处理/全局统计/跨块去重、Decimal、逻辑绑定和投影生命周期、严格配置顺序/V2阶段兼容、多原因与 drop / mark / fail / null、来源与计数守恒、三格式、预算、子进程超时/取消及 Worker 回调路径 |
| API smoke | 三种输出再导入为输入、真实异步完成、6→3 清洗报告、逐行脏数据、版本不变、下载权限、跨用户拒绝、明确失败及重试、5 万行多分片合并、登出失效 |
| 扩展 API | 管理员权限 / 禁用、SHA-256 和重传、伪造格式拒绝、GB18030 / 分号、Excel 选表 / 字符串覆盖、多原因、均值填充、类型 / 日期格式、非法 NaN / inf / NaT 显式诊断、去重 / IQR、提交后修改规则 / 解析 / 限额仍保持快照 |
| 浏览器 | 注册、文件选择、上传、预览、实际规则编辑、自动保存与提交、报告、日志、鉴权下载；工作表切换、保留邮编前导零、稳定字段改名；管理员账户 / 全部任务 / 限额与只读边界；390px 手机布局和退出、无 JS 异常 |
| 故障注入 | RabbitMQ 停机期间提交、outbox 恢复投递、取消排队任务、活跃任务上限、服务恢复后取消状态不回退；另提供真实 Worker SIGKILL / 新 attempt 恢复验收 |

## 依赖与兼容性

| 层 | 固定版本及锁定来源 |
| --- | --- |
| Java | Java 17；Spring Boot 3.3.5 BOM 固定 Spring Framework 6.1.14、Security 6.3.4、Flyway 10.10.0、MySQL Connector/J 8.3.0；MinIO Java 8.5.14；pom.xml |
| Python | Python 3.11.10；FastAPI 0.115.4、pandas 2.2.3、NumPy 2.1.3、PyArrow 18.0.0、openpyxl 3.1.5、pika 1.3.2、MinIO 7.2.12；requirements.in + 全量精确 requirements.txt |
| 前端 | Vue 3.5.45、TypeScript 5.9.3、Vite 8.3.6、Element Plus 2.14.7；package.json + package-lock.json |
| 基础设施 | MySQL 8.0.40、RabbitMQ 3.13.7、Nginx 1.27.5；MinIO 固定 release / commit / 源码校验和构建 |

依赖修改必须重新解析锁文件并执行对应测试，Python 镜像在安装后运行 pip check，Java 镜像构建包含 mvn verify，前端镜像包含 vue-tsc / vite build。GitHub Actions 配置在 `.github/workflows/ci.yml`，包含三个模块检查、磁盘规则单测及独立 Compose 的正向 API / 五组浏览器验收。故障注入按上述独立部署命令执行；本次尚未执行远端 CI，不能把已配置工作流描述为已通过托管 CI。

## V2基线实测（历史记录）

验证日期：2026-11-05 至 2026-11-06。环境：Windows Docker Desktop Linux containers；本机 Java 17.0.12、Python 3.11.9、Node 24.18.0，容器使用各 Dockerfile 固定版本。

当前通过 Java 16 项测试（15 项控制服务集成及 1 项真实 HTTP 规则校验代理）、Python 152 项 pytest、前端 3 项规则参数 / 顺序测试和 TypeScript / Vite 构建。已完成全部镜像构建、真实 MySQL Flyway 初始化及 V2 升级、三格式 API 往返、多分片任务和扩展 API 验收。浏览器验证真实请求和下载，覆盖普通用户完整流程与管理员页面。Python 锁定依赖在本机及 Linux 镜像 pip check 无冲突；初版前端官方 registry npm audit 为 0 漏洞，此后未改变依赖锁文件。

最终镜像的队列停机 / outbox 恢复、排队取消和并发限制验收通过。真实 Worker 故障恢复也通过：20 万行、32 条规则的任务在 attempt=1 RUNNING 时被 SIGKILL，等待原定租约自然过期后自动以 attempt=2 执行成功，最终版本从强制终止到完成共 164.9 秒。逐行校验 20 万行结果，确认 MySQL 报告与 MinIO 下载报告完全一致、当前下载只选择新 attempt，且存在持久化的心跳超时恢复日志。测试结束后已恢复 Worker。

验证证据保存在 `.test-output/`：初版 `build-verified.log`、`reliability.log`、`worker-recovery.log`；当前 `v2-backend-build.log`、`v2-engine-final-build.log`、`v2-app-build.log`、`v2-engine-tests-final.log`、`v2-smoke-final.log`、`v2-acceptance-final.log`、`v2-browser-final.log`、`v2-reliability-final.log`、`v2-worker-recovery-final.log` 及两组浏览器截图。该目录为本地验证产物，不提交测试账号凭据或部署密钥。

MVP 验证以功能、隔离和故障恢复为主，未作生产并发压测或多节点容灾认证。容量边界有自动化测试，但容量上限不等于已测得的吞吐承诺；较宽、较长文本的数据应根据实际内存与处理耗时选取 Worker 数量。

## 上一轮 V3 顺序引擎实测（历史记录，2026-11-06）

该轮Java为21项、Python为181项、前端为9项，均通过；TypeScript/Vite及最终应用镜像构建通过。真实三格式、配置顺序/旧引擎兼容、任务创建幂等、历史attempt、续传、权限、重复消息/终态回调、排队/运行取消和迟到回调均通过。Worker真实SIGKILL后的恢复用原120秒租约，attempt=2成功，耗时145.0秒。另用独立空数据库/对象卷完成Flyway V1–V4及全套smoke。详见[交付说明的命令、证据和限制](DELIVERY-V3.md#本轮实测记录)。

该历史小样容量实测：Docker 32逻辑CPU/8155533312字节内存，CSV 1,236,405字节、50,001×3、并发1，全局统计/IQR/keep-last组合含排队7.188秒，Worker峰值RSS255,940KiB，逐行验证5000条输出。当时采用完整 DataFrame，不能据此声称本轮磁盘引擎容量已验收。历史复现命令为 `python scripts/capacity_probe.py`（会重启所选项目的Worker，仅限本机独立验收部署）。

该轮 OpenAPI 校验曾为45操作、43路径、92模型、512引用、19类规则；当前数量以 `python scripts/generate_openapi.py --check` 输出为准。本地历史日志与截图在 `.test-output/v3-*`。当时 Python 有1条上游Starlette弃用警告，没有测试失败。

## 本轮 V3 磁盘引擎实测（2026-11-06）

最终通过 Java 41 项、Python 301 项、前端 14 项测试，及 TypeScript/Vite 与应用镜像构建；日志分别为 `.test-output/v3-next-backend-build-mixed-scope.log`、`v3-next-python-mixed-survivors-full.log`、`v3-next-frontend-continuation-build.log`。独立项目 `data-cleaning-pipeline-v3-next` 的 8089 入口已通过五组真实浏览器验收：前四组日志为 `.test-output/v3-next-browser-full.log`，disk-v3 最后一组修正隐藏 switch 的测试定位后重跑通过，最终新镜像再次通过，日志为 `.test-output/v3-next-browser-disk-final.log`。前四组运行证据和初次测试超时均保留，未改写为整次命令成功。

浏览器真实验证了 Decimal HALF_UP、replace、compare_fields、DISABLED、快照、历史游标稳定和筛选重置；该组保留期仅生成并读取 dry-run 计划，执行前后策略一致且 `enabled=false`，没有 execute/retry 删除请求。另以真实API在隔离项目验证正式删除与MinIO停机恢复。两规模容量、运行子进程阻塞超时、事件/取消竞争、Worker SIGKILL恢复、新空卷V1–V8和主库升级复核均已运行，逐项命令与边界见[本轮交付的实际证据](V3-COMPLETION.md#实际运行证据)。

新增真实浏览器续扫脚本`tests/browser/retention-continuation.mjs`也已通过：三页共1001个目标对象恰一次可达，每次提交保留scope及正确token，末页无续页；未改策略、未删除对象、未创建清洗任务。日志`.test-output/v3-next-browser-retention-continuation-verified.log`、截图与全部请求/计划/策略证据`.test-output/browser-retention-continuation/`。默认五组`npm test`与此独立脚本的执行范围分别记录，不冒称默认命令覆盖了额外一组。
