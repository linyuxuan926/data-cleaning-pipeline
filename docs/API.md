# 接口设计

当前新增磁盘引擎/schema3、fieldBindings与预检、签名游标分页、重试幂等、事件账本、清理计划/审计及410状态，完整实际路由以[OpenAPI](openapi.json)为准；接口行为和旧数组过渡见[最新交付](V3-COMPLETION.md)。旧接口继续兼容，不强制/api/v3。

基址 `/api`，JSON camelCase，分片 / 下载除外。Bearer会话保护业务接口（注册、登录除外），错误含message及可选code，控制器错误另含requestId，响应头提供X-Request-ID。新建通常201，其余200；当前契约见[V3 完成交付](V3-COMPLETION.md)，规则见[RULES.md](RULES.md)。普通用户只能操作自己的资源；ADMIN可读取全部任务，取消/重试仍限本人。

GET `/uploads/{uploadId}` 查询 receivedChunks，GET `/files/{id}` 查询文件。POST `/tasks` 支持 Idempotency-Key；schema1 默认 v3-sequential，schema3 默认 v3-disk，显式 v2-phased 保留历史阶段顺序。schema/engine 不兼容组合拒绝。任务及每个 attempt 提供详情、分页日志、报告、有限 preview 和鉴权下载。新建幂等重放200，异内容409；运行取消先 CANCELING，执行进程停止确认后才 CANCELED。

## 游标列表

新客户端使用下列 `/page` 路由：`/files/page`、`/uploads/page`、`/rule-sets/page`、`/rule-sets/{id}/versions/page`、`/tasks/page`、`/tasks/{id}/attempts/page`、`/tasks/{id}/logs/page`、`/tasks/{id}/attempts/{attemptId}/logs/page`、`/reports/page`、`/admin/users/page`、`/admin/tasks/page`、`/admin/retention/plans/page`、`/admin/retention/runs/page`。

统一返回 `{items,nextCursor,hasMore,asOf}`。`pageSize` 默认20、最大100；`sort=createdAt.desc|createdAt.asc` 默认倒序，`cursor` 原样传回。支持资源适用的 `q` 字面片段、`status` 逗号集合、`format`、`createdFrom/createdTo` ISO UTC，管理员任务可用 `ownerId`，用户列表另有 `role/enabled`，清理记录可用 `planId`。不适用的筛选会拒绝，详见 OpenAPI。游标签名绑定用户、资源、全部筛选、排序及首次 asOf；改变筛选应不带旧 cursor 从第一页开始。asOf 固定新增记录上界，任务状态等可变字段仍反映当前值，并非数据库历史快照。旧数组列表保留为 deprecated 兼容入口，其截断上限不适用于新分页列表。

## 认证和角色

| 方法 / 路径 | 请求 | 返回 |
| --- | --- | --- |
| POST /auth/register | `{username,password}` | `{token,user:{id,username,role,enabled}}`，role 固定USER |
| POST /auth/login | 同上 | 同上，禁用账户拒绝 |
| GET /auth/me | 无 | 当前用户 |
| POST /auth/logout | 无 | 当前会话立即撤销 |

用户名3–64位字母 / 数字 / 下划线 / 连字符；密码至少10字符且UTF-8不超过72字节。会话24小时，数据库只存token摘要。管理员由 BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD 初始化，无默认密码、不自动提升已有普通用户。禁用用户立即撤销已有会话。

## 系统与管理员

| 方法 / 路径 | 请求或返回 | 权限 |
| --- | --- | --- |
| GET /settings | 当前完整限制对象 | 已登录 |
| GET /admin/users/page | 分页用户，含role/enabled | ADMIN |
| PUT /admin/users/{id} | `{role,enabled}` | ADMIN；最后有效管理员不可降级 / 禁用 |
| GET /admin/tasks/page | 分页全平台任务，含ownerId、username | ADMIN |
| PUT /admin/settings | 完整限制对象 | ADMIN，白名单 / 整数 / 硬上限验证 |

设置键：maxFileBytes、maxRows、maxColumns、maxCells、maxExcelExpandedBytes、previewRows、maxActivePerUser、maxActiveGlobal、submissionsPerMinute、maxRunningGlobal、maxRunningPerUser。运行额度默认全局2、每用户1，在 claim 时检查；无可用槽的消息延迟重投。活动额度限制排队与执行总数。默认值及上限见交付说明，所有值须为至少1的整数，用户并发不得大于全局并发。保存至MySQL后，资源配置用于后续上传、解析和新任务；已有任务执行及重试沿用处理资源快照，但用户每次提交或重试仍需通过当前并发和频率额度。普通用户请求管理员接口返回403，不依赖页面隐藏。

## 上传、解析和预览

| 方法 / 路径 | 请求或参数 | 返回 |
| --- | --- | --- |
| GET /files/page | 统一分页参数 | 当前用户文件页 |
| POST /files/init | `{name,size,chunkSize?,sha256?,parseOptions?}` | `{id,uploadId,chunkSize,totalChunks}` |
| PUT /uploads/{uploadId}/chunks/{index} | 原始字节，index从0计 | 分片写入，可重复传输 |
| POST /uploads/{uploadId}/complete | 无 | 合并 / 摘要校验 / 尝试解析后的文件 |
| POST /files/{id}/parse | 解析选项对象 | 更新后的文件，解析失败仍保留raw |
| GET /files/{id}/preview | `limit` 可选，默认系统previewRows | 预览 |

fileId 与 uploadId 是不同UUID；新客户端使用 uploads 路径，旧 `/files/{id}/chunks/{index}` 及 `/files/{id}/complete` 兼容。chunkSize 为256KiB–8MiB，默认4MiB；每片精确长度验证。服务端计算完整SHA-256，传入期望摘要时必须匹配。最大文件大小由系统限制控制；未完成上传单用户≤20个 / 512MiB，全局≤200个 / 5GiB，24小时过期清理。

文件字段：id、uploadId、name、format、size、status、sha256、parseStatus、parseError、parseOptions、fields、uploader、objectKey、createdAt、parseVersion、objectStatus。objectStatus 为 AVAILABLE/DELETING/EXPIRED；status=READY仅代表原始上传完成，parseStatus=UNPARSED/PARSED/FAILED。创建任务要求 READY、PARSED 且原始对象可用；成功重新解析时 parseVersion 递增。SHA-256不匹配时返回400并保留UPLOADING，不提交READY。完整性通过后解析内容失败，complete返回200及READY/FAILED/parseError，原始对象保留；手动parse也以文件状态表示失败。重复complete返回现有状态，不重新解析，修复解析请使用parse接口。MySQL仅存元数据。

解析选项：encoding=auto/utf-8-sig/utf-8/gb18030/utf-16/latin-1；delimiter=auto/逗号/分号/Tab/竖线；sheetName指定Excel表；missingValues为有限标量数组（默认[""]）；fieldTypes映射稳定ID到string/integer/number/datetime/boolean。CSV自动识别后返回解析到的选项，也可手动调整重试。XLSX只处理选中工作表，Parquet仅扁平标量列。

预览结构：`{columns:[{id,name,dtype,inferredType,nullCount}],rows,rowCount,previewRowCount,parseOptions,sheets,warnings}`。稳定ID按原始位置生成f0/f1，样例行以原名为键；空值JSON null。推断类型和缺失数仅基于样本，CSV默认保留文本和前导零，fieldTypes是显式覆盖。默认前100行，实际默认及上限均为当前previewRows（管理员最大1000）。未扫描全文件时rowCount=null，预览不承诺完整文件通过执行容量验证。

## 规则

| 方法 / 路径 | 请求 | 返回 |
| --- | --- | --- |
| GET /rule-sets/page | 统一分页参数 | 当前用户规则集最新版本页 |
| POST /rule-sets | `{name,schemaVersion?,engineVersion?,logicalFields?,rules}` | 新规则集及版本、schema/engine、逻辑字段 |
| PUT /rule-sets/{id} | 同上 | 新版本，原版本不变 |
| GET /rule-sets/{id}/versions/page | 统一分页参数 | 不可变历史版本页 |
| GET /rule-schemas/3 | 无 | Python 权威的 21 类规则定义与 JSON Schema |
| POST /rules/preflight | fileId、parseVersion、schema/engine、logicalFields/rules/fieldBindings，或指定规则版本 | valid、normalizedRules、outputFields、warnings |

最多50条、序列化JSON≤100000 UTF-8字节。空数组合法，用于基线分析 / 格式转换。Java检查消息结构和大小，Python权威检查规则与参数；Python不可用返回502，不保存虚假成功版本。schema3 需声明 logicalFields，规则以 fieldId/fieldIds 引用逻辑 ID；fieldBindings 属于任务，显式映射到输入解析快照 fN，且一一对应。v3-disk 的21类规则按启用配置顺序执行，停用规则在报告记为 DISABLED；schema1 的 v3-sequential 同样按数组顺序，仅显式 v2-phased 保留阶段顺序。参数、精确 Decimal、空值及失败行为见 RULES.md。无任何脚本或SQL执行接口。

## 任务与产物

| 方法 / 路径 | 请求或参数 | 返回 |
| --- | --- | --- |
| POST /tasks | `{fileId,ruleSetId,ruleVersion,outputFormat,schemaVersion?,engineVersion?,parseVersion?,fieldBindings?}` + 可选 Idempotency-Key | QUEUED任务；csv/xlsx/parquet输出 |
| GET /tasks/page | 统一分页参数 | 本人任务页 |
| GET /tasks/{id} | 无 | 详情及rulesSnapshot/inputSnapshot/limitsSnapshot/ownerId |
| POST /tasks/{id}/cancel | 无 | 取消QUEUED/RUNNING，终态不回退 |
| POST /tasks/{id}/retry | 可选 `{expectedAttemptId}` + Idempotency-Key | FAILED/CANCELED 新 attempt，复用完整原快照；同键重放同一次结果，异内容409 |
| GET /tasks/{id}/logs/page | 统一分页参数 | 稳定排序日志页 |
| GET /tasks/{id}/attempts/page | 统一分页参数 | 独立 attempt 历史页 |
| GET /tasks/{id}/attempts/{attemptId} | 无 | 所选执行详情、状态和报告摘要 |
| GET /tasks/{id}/report | 无 | 完整报告；未成功409 |
| GET /tasks/{id}/artifacts/{kind} | result/dirty/report | 权限核验后流式下载，需Bearer |

ADMIN可读所有任务的上述详情、日志、报告、下载，写操作仍限资源本人，文件和规则归属没有放宽。任务创建时保存完整规则、解析选项 / 字段、限制、引擎、schema 和逻辑绑定快照，每次重试沿用；修改规则、重新解析文件或变更系统资源设置不改变已有任务的处理快照。重试申请仍校验当前并发和频率。完整消息快照超过128KiB会在创建/重试时返回400；V1历史任务首次重新入队会补齐当时可获得的快照，见数据库文档。手动重试键按用户和 retry:taskId 隔离，保留7天，超时后客户端应复用同一键；已确认成功后才换键。成功回调须三个MinIO对象存在且非空，进度100%；旧attempt和旧租约不能覆盖新状态。

报告包括inputRows、outputRows、removedRows、dirtyRows、flaggedRows、issueCount、changedCells、逐规则计数 / 统计量、executionOrder和ruleSnapshot。守恒为inputRows=outputRows+removedRows；dirtyRows为有问题的独立记录数，issueCount为问题条数。脏数据CSV每个问题一行，可多条对应同一原始记录；保留原始值、来源标识、字段、规则编号、错误码及原因。标记行在结果保留，质量元数据见规则文档。V1历史报告兼容读取。

当前限流 / 活动任务额度以settings为准，默认每用户3、全局20，每用户每分钟10次提交 / 重试；超限429。活动状态每2–3秒轮询，空闲降低频率，日志游标增量读取。

运行心跳租约120秒，调度器每30秒检查：超时且attempt<3时增加attempt重新排队，attempt≥3时失败，用户可再手动重试。手动重试也增加同一attempt计数，因此达到第3次及之后的尝试不再自动恢复。排队超过1小时失败。已取消任务重复cancel仍返回当前CANCELLED，其他终态不能取消；取消由父 Worker 通知执行子进程停止，宽限期后可强制终止；独立 deadline 超时失败为 TASK_TIMEOUT。取消或超时后的迟到产物不会变为可下载成功结果。阶段以 stage/stageProcessedRows/totalRows 展示，多遍扫描的阶段处理行数不能相加为总进度。

## 状态和错误

| HTTP | 含义 |
| --- | --- |
| 400 | 参数 / 格式 / 规则 / 内容无效 |
| 401 | 未登录、会话错误 / 过期 / 用户停用 |
| 403 | 缺少管理员权限 |
| 404 | 资源不存在或归属不符 |
| 409 | 状态冲突、分片缺失、未完成、用户名冲突、最后管理员保护 |
| 410 | INPUT_EXPIRED / ARTIFACT_EXPIRED；对象已清理或正在删除，历史状态与报告摘要保留 |
| 413 | 请求体超过入口限制 |
| 429 | 认证 / 上传 / 任务额度超限 |
| 502 | Python内部服务不可用 |
| 500 | 未预期错误 / 对象存储IO错误，详见服务日志 |

## 管理员保留期与清理

GET/PUT `/admin/retention/policy` 读取或局部保存策略，默认 enabled=false、includeExisting=false。启用需 confirmation=ENABLE_RETENTION；纳入升级前数据需独立明确确认 INCLUDE_EXISTING_DATA。protectedBefore 由服务端维护，不能由客户端改写。

POST `/admin/retention/plans` 生成只读计划，可传 fileIds/taskIds（各最多100个）及 includeOrphans；GET `/plans/{id}` 读取状态。计划最多1000个候选并明确 truncated。detail.scan 返回扫描/匹配/目录/日志计数、warnings 与 nextContinuation；有后续游标时，复用原 scope 的 fileIds/taskIds/includeOrphans/afterObjectKey，将 continuation 替换为 nextContinuation 再 POST plans，生成新的 dry-run 计划。签名游标24小时有效，绑定管理员及原范围，变更范围或无效游标返回400 RETENTION_CURSOR_INVALID；也可缩小范围重新预览。POST `/plans/{id}/execute` 正式执行，POST `/plans/{id}/retry` 重试失败项，均要求策略启用及管理员权限。plans/page 和 runs/page 返回历史计划与逐项审计。正式删除对每个对象重新检查期限、引用、任务活动和读取 lease，使用 DELETING → DELETED 两阶段状态；失败可重试。当前没有后台周期性自动删除业务产物。

## 内部协议

只在 Compose 内网访问，携带 X-Internal-Token，不能用普通用户会话替代。Python GET /health 供探针；POST /internal/preview 接收对象引用和有界预览选项；POST /internal/validate-rules 作权威规则校验，另有 `/internal/rule-schemas/3` 和 `/internal/preflight`。Python 不写业务数据库，耗时清洗由独立 Worker 的执行子进程承担。

RabbitMQ 持久化消息固定 taskId/attemptId、input、rules、limits、outputFormat、engineVersion、schemaVersion、logicalFields、fieldBindings。Java claim/control/events 使用 attemptId 和 leaseToken 隔离执行；v3-disk 回调还要求 eventId UUID 和递增 eventSeq，重复相同事件幂等、同键异内容409、低序事件记录但不回退状态，旧 attempt/lease 或终态事件不能覆盖当前任务。消息发件箱、事件账本、报告摘要及任务状态由 Java 事务管理，完整字段见 OpenAPI 与[V3交付](V3-COMPLETION.md)。
