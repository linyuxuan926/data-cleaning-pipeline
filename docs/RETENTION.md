# 保留期清理与对象租约

保留策略默认 `enabled=false`，生成计划只登记候选与保护原因。正式删除每个对象前重新检查任务引用、下载租约、当前策略，先提交 `DELETING` 再幂等删除 MinIO 对象；失败保持隔离状态，使用计划的 `/retry` 恢复。旧对象默认受升级边界保护。

`POST /api/admin/retention/plans` 接收 `fileIds`、`taskIds`、`includeOrphans`，以及可选 `afterObjectKey` 和 `continuation`。文件/任务范围在目录与日志 SQL 的 `LIMIT` 前生效。每页最多检查 1000 个对象目录条目及 1000 个日志任务。MinIO 发现最多扫描 1000 个总对象，并在对象间检查 5 秒时间预算；一次对象存储网络调用仍可能超过该检查间隔。

计划详情的 `scan` 返回 `scannedObjects`、`matchedObjects`、`catalogInspected`、`logTasksInspected`、`warnings` 和 `nextContinuation`。`truncated=true` 时，复制原计划的全部范围字段并把 `continuation` 替换为 `scan.nextContinuation`，创建下一份只读计划；每份计划都要分别审查和显式执行。该签名游标绑定管理员、协议版本、原范围和 24 小时有效期，具有分别记录发现、目录及日志位置的游标，受保护的前 1000 个对象不会阻挡后方对象。续扫期间新增到已扫描字典序之前的对象，需要重新开始一次扫描才能发现。管理员也可在首次请求显式使用排他的字典序起点 `afterObjectKey`。

下载和有界 Python 预览持有对象租约。服务每 30 秒为本进程仍打开的租约续期，下载读取时也检查续期；关闭与续期互斥，关闭后不会重新创建租约。单 Java 节点中，仍打开的本地引用即使遇到续期数据库故障也阻止清理。过期且没有本地活动引用的租约每次计划、执行、开流及续期维护时按最多 1000 条回收；崩溃后遗留租约最多保留至原租约到期。当前仍是单 Java 节点方案，不据此宣称支持多 Java 节点的本地引用保护。
