# 部署与运维

本轮新增Flyway V5–V8，原V1–V4不变。schema3新任务启用磁盘引擎，子进程deadline、scratch和保留策略配置见[最新交付](V3-COMPLETION.md)。清理默认关闭，升级前对象默认保护；当前由管理员明确执行已预览的清理计划，不后台周期性删除业务产物。有界续扫和对象租约说明见[保留期清理](RETENTION.md)。

升级保留已有账号、文件、规则和任务快照。schema3 默认 v3-disk，schema1 默认 v3-sequential，历史显式 v2-phased 保持阶段顺序；不原地改写旧任务引擎。运行取消先进入 CANCELING，Worker 父进程确认执行子进程停止后才 CANCELED。完整启动、兼容和当前限制请先读[当前交付](V3-COMPLETION.md)；[上一轮交付](DELIVERY-V3.md)仅作历史证据。

## 单机 Compose

最低建议可用 4 CPU、8 GiB 内存、20 GiB 空闲磁盘，64 位 Linux 或 Docker Desktop Linux containers。运行时 Java 17、Python 3.11、Node 24（仅构建），镜像定义固定版本。Windows 在 PowerShell 中运行命令即可，不需要安装本机 Maven。

```sh
python scripts/bootstrap.py
docker compose config --quiet
docker compose up -d --build --wait --wait-timeout 180
docker compose ps
python scripts/smoke_test.py
```

首次构建需要下载Maven、npm、pip、Go依赖；网络受限时配置可信镜像仓库或预先构建并转移镜像。拉取网络错误不应通过改用`latest`解决，预先`docker pull` Dockerfile中的固定基础镜像后可重试构建。成功启动后页面为[http://localhost:8088](http://localhost:8088)。没有固定默认密码或预设测试账号；管理员由生成的.env凭据初始化，普通账号可在页面注册。

### MinIO 构建来源

本次验证发现 `minio/minio` 旧公开 Docker Hub 镜像已不可拉取，官方旧二进制下载返回 410。因此项目提供 `infra/minio/Dockerfile`，从官方 [MinIO 源码](https://github.com/minio/minio/tree/d10bb7e1b667c2df72c394ef1fa52ab4a6802d0f) 构建未修改的服务，固定 release `RELEASE.2024-10-13T13-34-11Z`、commit `d10bb7e1b667c2df72c394ef1fa52ab4a6802d0f`，并校验源码压缩包 SHA-256。构建用 Go 1.23.2，依赖由上游 go.mod / go.sum 锁定，运行镜像包含上游 AGPL LICENSE。团队分发镜像时应保留相应许可证及源码来源。该服务是真实 MinIO，对象持久化在 Compose 卷中。

## 配置

`.env` 是部署秘密，生成脚本只补缺失键，不修改已有凭据；`.env.example` 仅列出字段，不能直接用占位密码部署。不要把展开后的 `docker compose config` 输出提交到代码库。无须把密码放在启动命令参数里。初始管理员使用该文件内 BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD 登录；脚本生成随机值且不打印密码。

| 变量 | 作用 |
| --- | --- |
| `DB_PASSWORD` / `DB_ROOT_PASSWORD` | 应用账号和数据库 root 的独立密码 |
| `RABBITMQ_USER` / `RABBITMQ_PASSWORD` | 消息队列应用账号 |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | 单机 MinIO 及服务端访问凭据 |
| `INTERNAL_API_TOKEN` | Java ↔ Python 内部调用密钥，至少 32 字符 |
| `WEB_PORT` | 本机页面入口，默认 8088 |
| `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD` | 首次启动创建管理员，已有同名普通账号不会被提升；修改变量不重置已有管理员密码 |

Compose 内部额外指定 `DB_HOST/PORT/NAME/USER`、`MINIO_ENDPOINT/BUCKET`、`RABBITMQ_HOST`、`JAVA_API_URL`、`PYTHON_API_URL`、`UPLOAD_DIR`。Python API 和 Worker 用同一镜像、不同命令启动。Java 在启动时执行 Flyway，调度器创建 MinIO bucket，健康检查确认依赖可用。

默认仅 frontend 绑定 `127.0.0.1`，其余服务只有内网端口。远程演示需要显式修改入口绑定并在可信反向代理终止 HTTPS（浏览器 SHA-256 需要安全上下文）。Nginx 不代理 `/internal`，Python API、MinIO、RabbitMQ 控制台和 MySQL 不应直接开放到公网。首次创建业务账号后，可在 backend environment 设置 `REGISTRATION_ENABLED: "false"` 关闭公开注册并重建容器。普通注册只能获得 USER，不能通过请求参数自行设置 ADMIN。

当前认证限流根据 Java 看到的来源地址；Nginx 后面的客户端共享保守额度。对外多用户部署前应配置受信任的代理地址并按可信来源限流，不应无条件信任用户传入的转发头。

## 本机开发

基础设施可按需开放本机端口：

```sh
docker compose -f compose.yaml -f compose.dev.yaml up -d mysql minio rabbitmq
```

开发人员设置 `.env` 对应变量后，把 Java 的 DB_HOST / RABBITMQ_HOST 改为 localhost，MINIO_ENDPOINT 改为 `http://localhost:9000`；PYTHON_API_URL 为 `http://localhost:8000`。Python 设置 JAVA_API_URL 为 `http://localhost:8080`，MINIO_ENDPOINT 与 RABBITMQ_HOST 同上。注意 `.env` 是 Compose 输入文件，本机进程需要自行加载变量。

后端：在 `backend` 使用 Maven 3.9.9 执行 `mvn verify`、`mvn spring-boot:run`。清洗引擎：在 `engine` 创建 Python 3.11 venv，安装 `requirements-dev.txt`，执行 `python -m pytest -q`；API 命令 `uvicorn cleaning.api:app --host 0.0.0.0 --port 8000`，独立进程命令 `python -m cleaning.worker`。前端：在 `frontend` 执行 `npm ci`、`npm run dev`，Vite `/api` 代理至本机 Java 8080。

## 运行与容量

```sh
docker compose logs --tail=100 backend worker
docker compose exec rabbitmq rabbitmqctl list_queues name messages_ready messages_unacknowledged consumers
docker compose up -d --scale worker=2
docker compose stop
docker compose start
```

单 Worker 一次处理一个任务。默认全局活动任务上限 20、每用户 3、每用户每分钟 10 次提交；claim 执行槽默认全局2、每用户1。管理员可在页面持久化调整（硬上限全局100、每用户20、每分钟120）；这限制排队和运行总数，不代表推荐 Worker 数量。文件 / 行 / 列 / 单元格 / Excel 展开体积 / 预览数量也由系统设置控制，硬上限为 100 MiB、500,000 行、200 列、5,000,000 单元格、512 MiB、1000 行预览。默认预览100行，每个任务固定提交时的限制。

v3-disk 分批读取，使用 SQLite 保存记录、问题与全局统计索引，并增量写出三格式结果；v2-phased/v3-sequential 兼容路径仍有完整 DataFrame 内存开销。磁盘引擎同样保留逻辑文本、记录/批次、scratch、问题和产物预算。单个导出最多256MiB；脏数据最多100,000条问题及5,000,000个展开单元格，超限明确失败。建议起始1–2 Worker，再按实际峰值内存和磁盘调整；XLSX sharedStrings 和 Arrow 页缓冲仍有底层内存开销，不能把批大小当作总RSS承诺。完整配置见[当前交付的资源边界](V3-COMPLETION.md#配置与资源边界)。

上传分片最大8MiB；用户暂存预留最多512MiB/20个未完成上传，全局5GiB/200个。5GiB是原文件预留总量，合并时还生成副本，暂存卷峰值可接近10GiB，另需为数据库和持续保留的MinIO产物留足空间。超过24小时的未完成上传每小时清理。Worker父进程持续回报心跳并检查取消，执行在独立子进程；超过取消宽限期可终止子进程，独立deadline保护阻塞的底层调用。租约120秒过期，调度器每30秒扫描；未超过冻结执行时限时最多自动恢复2次，automatic_retries与人工重试、attempt序号分别计数。执行时限已过的失联任务直接以TASK_TIMEOUT失败，不自动重试；超过时限的成功回调也不接收报告和产物。排队超过1小时失败。

管理员调整资源限制不会改写已提交任务的执行快照，重试也沿用原解析、规则与资源设置；但重试申请必须通过当前并发和提交频率额度。降低额度不会强制中断正在运行的任务。修改.env中的bootstrap管理员密码不会重置数据库已有管理员，若配置用户名已属于普通账号，服务启动会明确失败并拒绝自动提升。

## 故障定位

| 现象 | 检查 / 处理 |
| --- | --- |
| 容器构建失败 | 检查日志中的镜像 / 仓库网络；保持版本，重试构建 |
| 页面无法连接 | `docker compose ps`；确认 frontend 已启动及 WEB_PORT 未冲突 |
| backend unhealthy | 检查 MySQL 迁移、MinIO bucket、RabbitMQ 凭据和服务日志 |
| 上传失败 | 检查分片大小、剩余磁盘及未完成上传额度；不要手动改元数据为 READY |
| 预览失败 | 检查CSV自动识别结果或显式编码/分隔符、表头唯一、所选XLSX工作表、Parquet扁平标量及当前资源限制；修正参数后调用重新解析 |
| 任务停在排队 | 确认 Worker consumers>0、RabbitMQ 可用、outbox 发布日志；队列恢复后会继续投递 |
| 任务执行中断 | 等待租约恢复；不可直接复写已成功状态。查看具体 attempt 日志 |
| 格式转换失败 | Parquet 每列需一致类型，可配置 cast；XLSX 单元格最多 32767 字符 |
| 死信队列非空 | 排查不符合契约的消息来源，修复生产者后审查恢复，避免盲目无限重投 |

容器日志不包含密码；用户失败信息避免泄露对象存储凭据。任务错误日志保存在 MySQL，运行异常堆栈在容器日志。

## 数据、备份与升级

持久卷为 `mysql-data`、`minio-data`、`rabbit-data`、`uploads`，Compose 项目前缀为 `data-cleaning-pipeline`。`docker compose down` 保留卷；使用 `-v` 会永久删除业务数据。普通更新不应删除卷。

一致备份流程：暂停业务入口和新任务提交 → 等待活动任务完成 → 停止 Worker 与 Java → 使用 mysqldump 备份 cleaning 数据库，并备份 MinIO 数据卷及 `.env`；如需保留未完成上传还要备份 uploads，消息恢复可依据 MySQL outbox 和任务状态设计处理。备份应加密、控制访问并定期在独立项目和端口恢复演练。单独复制数据库而丢失对象卷，报告中的下载引用将不可用。

升级前先备份，再更新代码和固定依赖版本、跑测试、构建新镜像并执行 `docker compose up -d --build`。Flyway 自动应用新增迁移；如迁移不向后兼容，回滚需要恢复匹配数据库快照。业务产物和历史日志的策略默认关闭；管理员先生成dry-run计划，再按显式启用的策略执行。升级前对象默认受保护，不自动纳入删除。运维仍需监控存储并备份；保留期策略不替代备份。

清理每页最多扫描1000个MinIO总对象、检查1000条目录记录及1000个日志任务。计划返回truncated和scan.nextContinuation时，保留原范围字段生成下一份dry-run计划，每份分别审查和执行。扫描在对象间检查5秒预算，单次存储网络调用仍可能超出该间隔。下载和预览租约每30秒续期；仍打开的本地引用在单Java节点内继续阻止删除。完整续扫请求、游标限制及租约回收见[保留期清理](RETENTION.md)。

## 隔离验收部署

不要在主项目运行故障注入或清理验收。使用独立项目名、端口和新卷，例如 PowerShell：

```powershell
$env:WEB_PORT='8089'
docker compose -p data-cleaning-pipeline-v3-next up -d --build --wait --wait-timeout 180
python scripts/acceptance_v3_disk.py --base-url http://localhost:8089
$env:BASE_URL='http://localhost:8089'
cd tests/browser
npm test
```

上述独立项目与默认主项目的命名卷隔离，但使用同一份本地 `.env` 中的初始化凭据。首次启动使用空数据库时自动执行 V1–V8；升级实例只执行未应用的新增迁移。浏览器和故障脚本应串行，详细命令及实际结果见[测试文档](TESTING.md)和[本轮交付](V3-COMPLETION.md)。
