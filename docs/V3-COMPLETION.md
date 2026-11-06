# V3 增量开发与验收交付

本轮基于已验收代码增量开发，未重建项目，未删除主环境账号、数据卷、对象或原测试证据。保留 `/api` 和 `backend`/`worker` 服务名；历史 `v2-phased`、`v3-sequential` 解释器不改为新引擎。新建 schema3 规则使用独立 `v3-disk`。

## 剩余项与实现位置

| 原缺口 | 当前实现 | 关键验证 |
| --- | --- | --- |
| 完整DataFrame驻留 | `engine/cleaning/disk.py`、`streamio.py`；逐批输入、SQLite记录/问题/索引、逐条规则全存活扫描、增量三格式输出 | 多batch三格式oracle、真实API及两规模资源实测 |
| 全局规则跨块正确性 | SQLite typed键、精确Decimal样本索引、全范围sum/count、线性分位数、first/last winner、mode计数与最早来源平票 | batch1/2/3/1000及独立参考值；新旧真实引擎差分 |
| 列表截断 | `PaginationService/Controller`，HMAC cursor、用户/筛选绑定、createdAt+唯一ID稳定排序、asOf插入上界 | 超200/500条真实MySQL夹具；前端筛选重置、历史可达 |
| 线程无法终止 | `executor.py` spawned子进程，独立watchdog，父进程独占Java终态提交权 | 真正阻塞子进程测试；真实SIGSTOP+TASK_TIMEOUT及后续任务 |
| 历史产物/孤儿保留 | `RetentionService/Controller`，对象目录、计划、两阶段删除意图、审计与重试、下载/预览lease和任务锁 | 默认dry-run、前升级保护、引用竞争、部分失败恢复、410 |
| 规则参数/逻辑字段 | `bindings.py`、`ruleschemas.py`，schema3正式21规则、replace、compare_fields、Decimal；任务前fieldBindings/lifecycle预检 | 原始/显示/逻辑字段分离，投影后引用拒绝，恶意正则、精确数值 |
| 手动重试幂等 | `task_operation_keys`，用户+retry操作+请求哈希，7天内相同响应 | 并发重放同一次attempt，异内容409 |
| 回调重复乱序 | `task_events`，eventId与attempt+seq唯一，payload hash、last seq，保留attempt/lease/state fencing | 重复、异内容、低序、旧attempt、终态与取消/超时竞争 |
| 前端接入 | `DiskRuleEditor`、分页列表/attempt/logs、绑定预检、过期提示、Retention管理页 | 五组真实浏览器请求、下载与截图 |

## 启动、升级与账号

在项目根目录执行，需要Docker Desktop Linux containers / Engine、Compose v2、Python3.11；不需要本机Maven或数据库：

```powershell
python scripts/bootstrap.py
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
```

主入口 http://localhost:8088 。普通账号可自行注册；本地评审账号和密码仍在 `.test-output/local-demo.json`，不是生产初始化账户。首次管理员凭据在已有 `.env`，本轮未改写或打印。全新环境bootstrap产生自己的随机密钥。

升级自动应用新增Flyway V5（schema/绑定/事件/操作幂等/分页索引）、V6（清理目录及审计）、V7（清理分页索引）、V8（清理有界扫描续页）。V1–V4未修改。旧规则版本默认schema1，旧任务快照及引擎版本不变；成功解析文件得到parseVersion，之后每次成功重新解析递增。新旧规则没有原地迁移：页面“另存为磁盘规则”生成新配置，绑定需明确确认，可继续选择原规则/引擎回退。

升级前按部署文档备份MySQL、MinIO和`.env`；本地可使用`python scripts/backup_local.py --project data-cleaning-pipeline`，先确认没有活动上传和任务。脚本只读备份并保存摘要，不打印凭据；备份目录含敏感数据，应按业务文件和密钥保护。新旧应用混跑不是已验证部署方式；回滚应用时保留已迁移数据库，若需撤销数据库结构须恢复匹配备份。普通更新不使用`down -v`。

停止 `docker compose stop`；恢复 `docker compose start`；查看 `docker compose logs --tail=100 backend worker`。独立验收项目使用独立卷和8089，不能把其测试fixture当成主环境的实际清洗历史。

## 引擎、规则与来源语义

schema1请求未指定引擎仍使用`v3-sequential`，历史显式`v2-phased`保持阶段语义；schema3请求默认`v3-disk`。跨schema显式选择不兼容引擎会拒绝。重试沿用提交时完整输入、解析、规则、绑定、资源和引擎快照。

磁盘引擎以原始逻辑数据记录序号`seq`保存来源及存活标志。每条规则只扫描其开始时的存活记录；drop立即移除，mark/null保留问题供后续规则继续处理。全局准备pass完成后再apply，不使用块内均值或块内去重。去重以原始顺序选择first/last，输出按原始顺序排列幸存者；`nullsEqual=true`默认把空值按同一类型空键比较，false时含空值的行互不重复。键为类型化结构编码，布尔与数字、字符串与数字分离，不拼接裸字符串。

Decimal最多38位、scale0–12，默认scale2/HALF_UP；输入及JSON常量保存十进制文本，大整数不用float中转。精确sum/count、median和IQR排序用Decimal及磁盘索引；分位数为`(n−1)q`位置的线性插值，mean保留精确sum/count并按配置scale舍入。众数按全范围计数，平票取最早来源。日期内部UTC、保留类型，配置输出格式时才转导出文本。具体参数、空值/错误动作与适用类型见[RULES.md](RULES.md)及真实`GET /api/rule-schemas/3`。

物理列为解析快照`fN`，显示名可重命名，逻辑ID由规则声明，fieldBindings显式映射logical→physical。同一物理列不可被多逻辑ID重复映射。入队前预检规则参数、映射、parseVersion、投影/删除生命周期与可判断类型；执行中仍验证实际每条记录。运行期失败不能被预检替代。

脏数据每问题一条，包含原件业务值、fileId/sheet、1起始逻辑数据记录序号、ruleId、字段、类型化规则前值、规则动作和最终移除归因。CSV带换行单元格仍只占一条逻辑记录。recordId由文件/解析快照/工作表/逻辑序号哈希形成，重试保持稳定，不把attemptId混入来源身份。普通日志仅记录阶段/规则/身份，不输出整行。

报告分别计算问题条数、唯一问题来源行、有效输出、标记输出和最终移除。实际执行验证`inputRows=outputRows+finalRemovedRows`、`outputRows=validOutputRows+flaggedOutputRows`、`sum(finalRemovalByRule)=finalRemovedRows`。每规则含输入/输出阶段行数和durationMs。

## 分页一致性

新页面使用`/api/.../page`，响应`{items,nextCursor,hasMore,asOf}`，默认20、最大100。支持文件、上传、规则、版本、任务、报告、attempt、日志、管理员用户/任务、清理计划/审计。筛选在SQL中执行并先限定归属；cursor不可跨用户、资源或筛选复用。`createdAt.asc/desc`加唯一ID打破相同时间的平局；版本和日志用数值键作次序。

第一页生成asOf插入上界，后续排除新创建记录；翻页期间状态等可变字段按查询时实时值过滤，不承诺跨页事务快照。刷新重新查询。旧数组接口为兼容保留并附弃用/范围提示，不能据它统计全部历史；前端全部增长列表已转分页。

## 时限、消息和取消

运行时限按每次attempt单独计算，claim后开始，包括读取、清洗、写出、上传及Java接受终态；排队时间另有1小时上限。人工重试新attempt得到独立时限，原已冻结配置不变，不提供跨多次人工重试累计的总deadline。

父Worker保持AMQP及Java心跳，数据处理在独立spawn子进程。watchdog不做网络I/O，Java请求阻塞也不会取消本地deadline。先设共享取消信号，宽限后terminate/kill并join确认；单任务进程退出不停止Worker。Linux子进程设置parent-death SIGKILL并检查父进程，父Worker异常终止后不允许旧子进程继续产出。

人工取消为USER_CANCELED，时限为TASK_TIMEOUT，规则失败RULE_ERROR，进程异常EXECUTOR_CRASHED，失联WORKER_LOST，scratch预算/空间错误单独编码。CANCELING仅在进程确认停止后变CANCELED；迟到终态受lease/attempt/state/事件账本检查。server deadline兜底拒绝超期成功报告；时限已过的失联attempt明确失败，不由租约恢复无限重试。

至少一次投递：Java事务outbox，publisher confirm；Worker仅在Java接受终态后ACK。网络重试保持同eventId/seq/body，重复异内容409、低序忽略、旧attempt不能覆写新attempt。Worker真正失联而未超运行deadline才按有限次数生成恢复attempt。没有宣称恰好一次计算。

## 保留策略及删除安全

默认`enabled=false`，计划只预览。升级时记录protectedBefore；早于该时刻的对象/日志默认保护，管理员须另显式确认`INCLUDE_EXISTING_DATA`才可纳入。启正式清理需要`ENABLE_RETENTION`，不能由新版本自动开启。主环境本轮不对既有文件执行正式删除。

| 对象 | 默认保留期起点与时长 |
| --- | --- |
| raw原件 | 对象创建时起90天 |
| 成功result/dirty/report | 对应attempt完成时起，各30天 |
| 失败/取消部分产物 | attempt完成时起7天 |
| 无引用孤立对象 | 对象创建时起7天，计划需includeOrphans |
| 普通历史任务日志 | 终态任务最后更新时间起90天；任务再次活动时阻止删除 |
| 所有正式清理 | 以上时长另加默认24小时保护宽限 |
| 未完成上传 | 24小时后清理（既有实现） |
| attempt本地scratch | 正常完成/取消/失败确认后立即清理；崩溃残留默认1小时后回收 |

GET/PUT `/api/admin/retention/policy`；POST `/plans`生成只读计划，可指定fileIds/taskIds及孤立对象；GET `/plans/{id}`、`/plans/page`；POST `/plans/{id}/execute`或`/retry`；GET `/runs/page`。发现每次最多扫描1000个MinIO总对象并检查5秒预算；目录和日志也分别按1000条有界检查。返回`scan.nextContinuation`，前端“继续扫描”使用原scope生成下一份dry-run，各页分别审查执行；签名续页绑定管理员/范围并在24小时后过期。范围过滤在SQL LIMIT前执行，避免受保护对象挡住后面的候选。详见[保留策略](RETENTION.md)。当前为管理员显式执行，不后台周期性自动删除业务产物。

计划与执行都检查引用、活动上传/任务/attempt、期限和下载/预览lease。正式删除先在数据库提交DELETING意图，再删单个已登记对象，最后确认DELETED；失败及崩溃可重试，MinIO remove幂等。禁止按前缀批删。提交/重试与tombstone共享锁，不能在删除意图后启动引用此raw的新attempt。下载/预览lease每30秒续期，读取时也续期，关闭流后释放；本Java进程活动引用在续期数据库暂时失败时仍阻止删除。过期lease分批回收；单Java节点是当前部署范围。对象过期返回稳定410 INPUT_EXPIRED/ARTIFACT_EXPIRED，历史元数据和报告摘要保留。

## 配置与资源边界

| 配置 | 默认 / 支持范围 |
| --- | --- |
| EXECUTION_TIMEOUT_SECONDS | 1800；1–86400秒，Java/Worker一致 |
| CANCEL_GRACE_SECONDS | 10；1–60秒 |
| DISK_BATCH_ROWS | 1000；最多10000，受批字节预算共同限制 |
| MAX_SCRATCH_BYTES | 1GiB；最多4GiB，每attempt，提交时冻结 |
| SCRATCH_RETENTION_SECONDS | 3600；60–604800，Worker启动及周期检查，跳过活动目录 |
| maxRecordBytes / maxBatchBytes | 默认4MiB / 32MiB；硬cap16MiB / 64MiB |
| maxRecordIssueBytes | 默认1MiB，硬cap4MiB |
| maxRunningGlobal / maxRunningPerUser | 默认2 / 1，Java claim限制；实际并发仍受Worker副本数限制 |

文件100MiB、50万行、200列、500万单元格和产物256MiB等原保护阈值保留。批大小约束是应用层序列化/扫描预算，scratch是写入前检查、事务检查和父进程采样监督，**不是文件系统硬配额**；预算附近可能存在一个有界写入/底层库阶段的瞬时超量。磁盘不足会明确失败并清理。

XLSX的openpyxl只读仍可能加载工作簿sharedStrings/元数据；未选工作表也可能共用字符串表。Arrow解压页/批次也有底层内存开销。因此不声称绝对恒定RSS或任意大文件能力。子进程隔离保护Worker主循环，但不能替代宿主内存配额/监控。生产多机、TLS入口、漏洞审计及灾备认证不在本次验证承诺内。

## 验收操作与命令

1. 用原账户登录，查看已验收文件/历史任务，下载结果与原SHA256比对。
2. 上传`samples/v3-disk-orders.csv/.xlsx/.parquet`；新建schema3磁盘规则或按文档示例另存；确认logical→fN映射并预检。
3. 按`samples/v3-disk-rules.json`顺序创建任务，期望8输入、4输出、3严格有效、1标记保留、4最终移除；id顺序002/001/006/007，金额185.33/12.00/20.00/40.00。设计目录的旧样例曾把schema3与v3-sequential放在一起；可运行样例明确使用v3-disk，接口拒绝跨schema引擎组合。
4. 查看问题、报告每规则统计、result/dirty预览及下载；修改规则新版本，历史快照不变。
5. 切换列表页/筛选/旧attempt；尝试同键重试及越权ID，确认历史与权限边界。
6. 管理员保留页先看默认dry-run，生成小范围计划。正式删除只对专用验收数据操作；不启用“包含升级前数据”。
7. 在独立验收项目串行运行故障脚本，观察超时、取消、失联恢复、清理部分失败和重试；结束恢复服务/配置。

```powershell
# 纯逻辑/构建
docker compose build backend python-api frontend
cd engine
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m pip check
cd ../frontend
npm test
npm run build
cd ..
python scripts/generate_openapi.py --check

# 独立验收服务使用已有源码、自己的空卷、不同端口/项目名
$env:WEB_PORT='8089'
$env:DISK_BATCH_ROWS='127'
docker compose -p data-cleaning-pipeline-v3-next up -d --no-build --wait
python scripts/smoke_test.py --base-url http://localhost:8089
python scripts/acceptance_v3_disk.py --base-url http://localhost:8089
python scripts/acceptance_v3_admin.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --allow-faults
python scripts/timeout_isolation_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next
python scripts/capacity_disk.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --sizes 10000,50000
```

故障脚本和浏览器验收必须串行执行；容量测试期间没有其他活动任务。分页测试明确插入标记的metadata fixture以验证超过旧边界，不把它们描述为真实执行任务。清理测试只针对本脚本新上传的对象，独立项目拒绝主8088。

## 实际运行证据

本轮日期2026-11-06。以下均为实际运行；上一轮历史记录见[DELIVERY-V3.md](DELIVERY-V3.md)，不可拿旧DataFrame测量替代本轮磁盘引擎实测。

| 实际命令（根目录，目录另注明） | 结果及证据（`.test-output/`） |
| --- | --- |
| `docker compose build backend` | 最新41项全部通过，Platform29/Gateway1/Retention11，Maven verify成功；`v3-next-backend-build-mixed-scope.log` |
| `.venv/Scripts/python.exe -m pytest -q`（engine目录） | 最终301 passed，1条上游Starlette弃用warning；`v3-next-python-mixed-survivors-full.log`；pip check无依赖冲突 |
| `npm test`、`npm run build`（frontend）及`docker compose build frontend` | 14项通过、TypeScript/Vite及镜像通过；`v3-next-frontend-continuation-build.log` |
| `python scripts/generate_openapi.py --check` | 68 operations/65 paths/153 schemas/827引用，路由、TaskMessage和WorkerEvent与源码exact对账；`v3-next-openapi-final.log` |
| `python scripts/smoke_test.py --base-url http://localhost:8089` | 三格式、异步、版本、报告、下载、隔离、多片SHA、失败/重试通过；`v3-next-smoke.log` |
| `python scripts/acceptance_v2.py --base-url http://localhost:8089`（进程环境project=v3-next） | 原有V2角色/解析/全局统计/异常/快照回归通过；`v3-next-acceptance-v2.log` |
| `python scripts/acceptance_v3.py --base-url http://localhost:8089` | 顺序与V2差异、幂等、预览、旧attempt、隔离通过；`v3-next-acceptance-v3.log` |
| `python scripts/acceptance_v3_disk.py --base-url http://localhost:8089` | 三格式输入、CSV/XLSX/Parquet输出、8=4+4、Decimal185.33、脏数据来源、bindings预检、快照和retry-key通过；`v3-next-disk-acceptance-final.log` |
| `python scripts/acceptance_v3_admin.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --allow-faults` | 真实MySQL分页507files/rules/tasks/users/versions/attempts、607logs；干跑/正式小范围删除/410/引用保护，实际MinIO停机PARTIAL_FAILURE及恢复重试通过；`v3-next-admin-final.log` |
| `python scripts/timeout_isolation_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next` | 对真实子进程SIGSTOP，12秒时限+1秒宽限，约14.312秒后完成回收；TASK_TIMEOUT、迟到成功忽略、同Worker新任务成功；`v3-next-timeout.log/json` |
| `python scripts/reliability_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next` | RabbitMQ停机outbox恢复、排队取消和并发限制通过；`v3-next-reliability.log` |
| `python scripts/acceptance_v3_reliability.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next` | 真实重复RabbitMQ投递、重复终态、运行取消、旧attempt和取消/完成竞争通过；`v3-next-reliability-v3.log` |
| `python scripts/event_ledger_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next` | 真实Java内部claim及事件：JSON换序重放一次生效、低seq忽略、payload/seq冲突409、取消/终态围栏；`v3-next-events.log` |
| `python scripts/worker_recovery_test.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --timeout 240` | 20万行/32规则RUNNING时SIGKILL，原120秒租约不改，attempt2成功，165.0秒后恢复并核对下载；`v3-next-worker-recovery.log` |
| `BASE_URL=http://localhost:8089 npm test`（tests/browser） | 前四组真实浏览器通过；disk组初次隐藏switch定位超时后修测试并单独通过。最终新镜像又运行`node tests/browser/disk-v3.mjs`通过；`v3-next-browser-full.log`、`v3-next-browser-disk-final.log` |
| `BASE_URL=http://localhost:8089 node tests/browser/retention-continuation.mjs` | 真实MinIO1001个目标对象，三页UI点击保留原scope和正确签名cursor，全部恰一次可达，末页无续页；policy精确不变且对象字节数不变；`v3-next-browser-retention-continuation-verified.log`和`browser-retention-continuation/evidence.json` |
| `WEB_PORT=8090 docker compose -p data-cleaning-pipeline-v3-fresh up -d --no-build --wait`，然后disk/admin脚本指向8090 | 独立空卷V1–V8真实MySQL初始化、七服务健康，三格式disk闭环和分页/正式隔离对象清理通过；`v3-next-fresh-final-start.log`、`v3-next-fresh-disk.log`、`v3-next-fresh-admin-final.log` |
| `python scripts/smoke_test.py --base-url http://localhost:8088`及`python scripts/verify_upgrade.py` | 主环境最终三格式闭环、鉴权、历史attempt及旧数据报告/下载兼容复核通过；`v3-next-main-smoke.log`、`v3-next-main-compatibility-final.log` |

分页大历史使用明确标记的metadata fixture，不把507条元数据描述为507次已执行清洗。新旧引擎差分使用真实生产代码，不用mock替代计算；301项包含跨batch first/last、全局统计、来源顺序、逐问题明细、最终删除归因、恶意正则及实际子进程终止。最终Parquet导出仅对mixed字段分批复核存活类型，先前失败但已删除的行不再误阻止导出；真正不兼容的混合类型仍明确拒绝。

故障测试只操作独立项目并恢复服务/配置，MinIO正式删除只针对本轮新建的隔离对象。真实故障中曾发现仅检查MinIO live过早重试的问题，脚本现等ready及Java对象存储health后恢复；之前失败证据保留。新增fresh项目的首次admin脚本被保守项目白名单阻止，加入明确fresh白名单后实际重新运行通过；没有绕过主8088保护。

续扫浏览器测试最初错误地把1000个扫描上限当作固定页长、把全桶扫描数当作目标范围匹配数，触发断言；产品5秒预算提前结束和继续扫描其他字典序对象均是约定行为。测试已修为逐页游标推进及目标来源集合完整/唯一验证，原失败日志保留，最终真实运行通过，未通过放宽产品预算或修改清理策略掩盖问题。

### 两种规模资源实测

硬件：AMD Ryzen 9 7945HX，16核/32逻辑处理器，Windows物理内存16,844,877,824字节；Docker可见32逻辑CPU、8,155,533,312字节内存。Python3.11.10、SQLite3.40.1，单Worker、同一时刻1个清洗任务；batchRows127、5列、scratch预算1GiB。命令：

```powershell
python scripts/capacity_disk.py --base-url http://localhost:8089 --project data-cleaning-pipeline-v3-next --sizes 10000,50000
```

数据由脚本确定生成：10%不同ID且跨块重复，11周期缺数值，13周期缺类别，末行含异常值；CSV输入323,800/1,618,911字节。逐行组合为trim→literal replace→Decimal cast→required mark→常量填充；全局组合为前三步→mean→median→mode→IQR mark→keep-last去重。脚本逐条输出对比独立oracle，验证精确统计、顺序、问题计数和守恒，阶段耗时写入JSON。

| 组合 | 输入行 | 含排队总耗时秒 | 父+子RSS采样峰值MiB | 临时盘采样峰值MiB | 输出行 | 问题条数 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 逐行 | 10,000 | 11.266 | 210.76 | 5.81 | 10,000 | 910 |
| 逐行 | 50,000 | 48.953 | 212.79 | 28.95 | 50,000 | 4,546 |
| 全局 | 10,000 | 24.547 | 210.38 | 12.10 | 1,000 | 9,000 |
| 全局 | 50,000 | 110.657 | 211.50 | 60.66 | 5,000 | 45,001 |

最后一组输出214,311字节、脏数据16,510,336字节；问题数包含被重复删除行及另一个IQR问题，不能当作拒收行数。内存为父+子RSS相加（共享页可能重复计数），200ms采样可能漏过更短峰值，磁盘也是目录采样而非内核绝对峰值。这些结果仅适用于实测CSV组合和1并发，不宣称恒定内存、任意大文件或50万行已完成容量认证；XLSX/Parquet完成了功能验证，未单独做大文件容量矩阵。完整原始指标/每阶段耗时见[可移交JSON证据](verification/v3-disk-capacity.json)及`.test-output/v3-next-capacity-final.log`。

### 主环境升级与遗留限制

主环境升级前无活动任务，已保存MySQL逻辑备份、MinIO卷归档及原`.env`，位于`.test-output/v3-next-preupgrade-backup/`（含密钥，不应外传）。`docker compose -p data-cleaning-pipeline up -d --no-build --wait`已实际执行；七服务healthy，入口仍为http://localhost:8088，最终HTTP200。`verify_upgrade.py`确认原账号及3条已验收任务的attemptId、engineVersion、完整报告和结果SHA256完全不变；主环境enabled/includeExisting均false；[对账证据](verification/v3-upgrade-verification.json)。原`.env`与备份逐字节相同，数据卷及既有对象未删除。结束时仅对next/fresh隔离项目执行`down`，未带`-v`，保留其验收卷及证据，主环境保持运行；最终状态见`.test-output/v3-next-main-services-final.log`。

`create_disk_demo.py`已为原本地评审账号增加真实三格式订单任务，各8输入/4输出/3有效/1标记/4移除。保留原`.test-output/local-demo.json`不改写；新任务ID见`.test-output/local-disk-demo.json`。该脚本初次使用设计样例多余属性被严格接口拒绝，已把可执行样例精简为真实API接受字段并重新运行通过，不存在静默忽略参数。

当前限制：单Java节点保留租约保护；业务产物仅管理员显式清理，没有后台自动sweep；旧引擎仍为DataFrame兼容路径；XLSX sharedStrings/Arrow底层分配不是应用批预算可硬限制；不支持旧xls/宏/密码文件、嵌套Parquet或任意表达式代码；不承诺跨状态筛选的事务快照。没有在真实生产流量、多Java节点、断电级数据库/对象存储灾难或外部渗透条件下认证；远端CI配置已更新，本机没有冒称远端CI执行。上述MVP及本轮剩余项均有实际实现，不用这些边界替代功能。

新增/主要修改文件分组：`engine/cleaning/{disk,streamio,bindings,ruleschemas,executor,worker,storage,models,api}.py`与对应tests；`backend/.../{PaginationService,PaginationController,RetentionService,RetentionController,TaskService,FileService,ObjectStorage,ResultPreviewController}.java`及V5–V8/集成测试；`frontend/src/App.vue`、磁盘编辑器/分页/保留策略组件和tests；`compose.yaml`、`.env.example`、`scripts/*v3*`、故障/容量/备份/升级验证脚本、samples；OpenAPI、规则、数据库、部署、测试与本交付文档。无关旧文档与历史证据保留。
