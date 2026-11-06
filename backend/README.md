# Java 控制服务

Java 17 + Spring Boot 3.3.5，使用 Spring JDBC、Flyway、Spring Security、Spring AMQP 和 MinIO SDK 8.5.14。依赖的传递版本由 Spring Boot BOM 固定。

推荐从仓库根目录使用 Docker Compose 启动完整系统。单独开发本服务时准备 Java 17 和 Maven 3.9.9，设置根目录 `.env.example` 中的数据库、RabbitMQ、MinIO、内部 token 环境变量，执行：

```sh
mvn verify
mvn spring-boot:run
```

`mvn verify` 当前执行41项测试，覆盖H2 V1–V8迁移与控制服务事务、权限、规则校验HTTP代理、不可变快照、上传完整性、outbox/租约/取消、UUID attempt历史、创建与手动重试幂等、事件去重/乱序隔离、执行额度、分页与保留期续扫、对象租约续期和混合清理范围边界。数据库测试替换外部适配器；完整 MySQL、RabbitMQ、MinIO、Python Worker 联调由根目录真实验收脚本验证。当前契约、运行证据及限制见[V3 完成交付](../docs/V3-COMPLETION.md)、[API](../docs/API.md)和[OpenAPI](../docs/openapi.json)；旧[顺序引擎交付](../docs/DELIVERY-V3.md)保留为历史记录。

服务启动时自动应用 `src/main/resources/db/migration`。后续数据库变更须新增版本迁移，不能修改已在已有实例执行过的迁移文件。健康检查为 `/actuator/health`，检查数据库、RabbitMQ 和 MinIO 桶；桶会自动创建。不会暴露内部错误和数据库异常给浏览器。

上传临时目录须持久化并只由本服务写入。每文件硬上限100 MiB，管理员可下调。每用户最多20个未完成上传且总大小最多512 MiB；全局最多200个未完成上传且总大小最多5 GiB（合并还会写临时副本，物理磁盘峰值需额外留余量）。初始化时通过数据库锁预留额度，24小时未完成的上传会过期。完成时计算完整SHA-256，若init声明了sha256则必须匹配；摘要不匹配时不提交READY。uploadId和文件ID独立，旧文件ID分片API仍作为兼容入口。

完成上传后调用Python尝试解析；失败保留MinIO原文件并返回`status=READY,parseStatus=FAILED,parseError`，可通过`POST /api/files/{id}/parse`重新配置CSV编码/分隔符、工作表及类型解析。只有PARSED能创建任务。解析仅保证有界预览通过，完整执行仍验证全部行、列和单元格资源限制。上传完成的临时文件在数据库提交后清理，定时任务会补清理崩溃残留目录。配额不包含已入MinIO历史对象和导出产物，存储保留由管理员Retention策略与显式计划管理，默认关闭且升级前对象受保护；仍须配置监控和备份。不要另设会绕过业务引用检查的桶前缀批删。

任务创建保存rules/input/limits、schema/engine、logicalFields/fieldBindings等明确快照，重试和自动恢复均复用；后续重新解析文件、修改规则版本或系统配置不会改变已有任务。V1历史任务首次重新入队时从不可变规则版本和当时文件/系统配置回填一次，之后固定。schema3规则默认v3-disk；schema1默认v3-sequential，历史显式v2-phased保持阶段语义。规则保存时Java校验信封并调用Python权威语义校验；任务入队前再预检parseVersion、字段绑定和投影生命周期，避免异构正则和规则集差异。

任务创建/重试和outbox在同一事务提交；消息需publisher confirm且未被return才标记发布。Worker claim检查maxRunningGlobal/maxRunningPerUser后获得租约token，每次回调验证token和attempt。v3-disk事件还需eventId/eventSeq，账本保留重复/乱序判定及payload哈希，状态不能回退。retry操作键按用户+任务隔离保留7天。心跳过期120秒后，未超过冻结执行时限的任务最多自动恢复2次；automatic_retries与人工重试、attempt序号分别计数，执行时限已过则直接TASK_TIMEOUT失败。排队超过1小时失败。成功回调校验`inputRows=outputRows+removedRows`（旧报告回退dirtyRows），标记问题行允许留在结果；另验证任务产物路径和3个对象真实存在且非空。普通用户只能访问自己的资源。管理员可查看所有任务详情、日志、报告并下载产物，但取消/重试仍只允许操作本人任务，文件和规则归属权限没有放宽。

本部署针对单Java节点，Worker可扩容；共享上传卷配置前不支持横向扩容Java。登录/注册按服务观察到的连接IP限制30次/分钟，不自动信任转发IP；单一反向代理下该限制由代理连接共享。任务限制默认每用户10次/分钟、每用户3个、全局20个活跃任务，管理员在硬cap内调整，配置持久化于MySQL。会话token只存SHA-256摘要，有效期24小时，退出或禁用立即撤销。注册始终USER，可用`REGISTRATION_ENABLED=false`关闭。

首次管理员只由`BOOTSTRAP_ADMIN_USERNAME`和`BOOTSTRAP_ADMIN_PASSWORD`创建，无默认值。若用户名已属于普通用户则启动失败，拒绝自动提升；已有管理员不会被重置密码。管理接口动态检查ADMIN角色及enabled，最后一位有效管理员不可被禁用或降级。V2迁移保留已有用户和文件，历史READY文件初始UNPARSED，需要重新解析后新建任务。

新增迁移为 V5（schema3、绑定、事件、操作幂等和分页索引）、V6（清理目录/计划/审计）、V7（清理分页索引）、V8（有界扫描续扫状态和范围索引），既有 V1–V4 未改写。新客户端使用签名游标 `/page` 接口；旧截断数组列表仅作兼容。清理执行先提交 DELETING 意图，再删除单个已登记对象并确认 DELETED；任务引用和持续续期的下载/预览 lease 阻止竞争删除。清理后的对象访问返回410，历史元数据和报告摘要保留。续扫、租约及单节点限制见[保留期清理](../docs/RETENTION.md)，其他参数见[部署](../docs/DEPLOYMENT.md)和[API](../docs/API.md)。
