# 清洗规则参考

规则保存为 JSON 数组。参数用白名单校验，Java 在保存前调用受保护的 Python 校验接口；执行时再次校验。最多 50 条、规则 JSON 最多 100,000 UTF-8 字节。规则版本和任务冻结 schemaVersion、engineVersion，版本间不隐式转换。

| 契约 | 引擎 | 规则与字段 | 执行方式 |
| --- | --- | --- | --- |
| 历史 schema1 | `v2-phased` | 19 类；`column` / `columns`，稳定 fN 优先、兼容原始列名 | 历史阶段排序；校验阶段可延迟删除 |
| 历史 schema1 | `v3-sequential` | 同一套 19 类，执行快照可带 id/enabled | 配置数组顺序；每条规则立即处置删除 |
| schema3 | `v3-disk` | 21 类；声明 logicalFields，规则引用逻辑 fieldId / fieldIds，任务显式绑定到 fN | 配置数组顺序；SQLite 磁盘保存中间状态与全局索引 |

旧客户端省略 schemaVersion 仍按 schema1 保存，旧任务默认 `v3-sequential`；schema3 规则保存需显式声明 `schemaVersion:3`、`engineVersion:"v3-disk"` 和 logicalFields。不能给旧规则直接选择新引擎，也不能在原规则集中改变 schema；应另存为新的规则集。以下先保留历史规则说明，后文给出 schema3 的实际规格。

## 历史 schema1：顺序与字段

schema1 任务默认 `v3-sequential`，严格按配置数组顺序执行，每条规则结束立即处置drop行。只有显式或迁移的 `v2-phased` 仍按下表阶段稳定排序。报告保留原数组index、V3规则ID和实际executionOrder。changedCells累加各规则结束时仍保留的已变更单元格，后续删除不倒扣；字段选择/改名不算内容变更。第一条/最后一条按原始输入数据顺序定义。

| 阶段 | 规则 | 类型和参数 | 空值及失败语义 |
| --- | --- | --- | --- |
| 10 字符串 | trim | columns，字符串首尾去空白 | 非字符串保留，缺失保留 |
| 10 字符串 | case | columns, mode=lower/upper | 非字符串保留，缺失保留 |
| 20 转换 | cast | columns, dtype；integer/number/string/datetime/boolean；可选 dateFormat、precision 0–12、outputFormat | 缺失保留；非空转换失败记录问题并按 onError 处理 |
| 30 缺失 | missing | columns, markers，有限标量数组 | 明确将自定义标记转 null；原 null 始终缺失 |
| 30 缺失 | fill_null | columns, value，非 null 有限标量，文本≤10000字符 | 仅替换缺失，已有有效值保留 |
| 30 缺失 | fill_stat | columns, strategy=mean/median/mode, emptyPolicy=fail/leave/constant, value? | 基于当前有效存活值计算；无统计量按显式策略处理 |
| 30 缺失 | drop_null | columns, onError? | 任一选中列缺失算失败 |
| 40 异常 | range | column, min?/max?, inclusiveMin?/inclusiveMax?；至少一个有限边界 | 缺失跳过；非数值或超界记录问题；默认包含边界 |
| 40 异常 | iqr | column, factor=1.5, emptyPolicy=fail/leave | 当前有效数值的 Q1/Q3，界限 Q1−factor×IQR 至 Q3+factor×IQR，包含边界；报告阈值和样本数 |
| 50 校验 | required | columns | 缺失算失败 |
| 50 校验 | enum | column, values，有限标量集合 | 缺失跳过，用 required 组合非空约束 |
| 50 校验 | length | column, min?/max?，0–1000000整数，至少一个边界 | Unicode 字符数；非字符串失败，缺失跳过 |
| 50 校验 | regex | column, pattern，1–256 字符 | 完整匹配；缺失跳过；受限语法，每单元格10ms、每规则10秒预算 |
| 50 校验 | validate_type | columns, dtype，同 cast 类型 | 仅验证可转换性，不修改值，缺失跳过 |
| 50 校验 | compare | column, operator, value | eq/ne/gt/gte/lt/lte/contains/starts_with/ends_with；缺失跳过，类型不兼容记录失败 |
| 60 去重 | deduplicate | columns?（空或省略为整行）, keep=first/last | 全任务存活行去重，质量元数据不算入键 |
| 70 输出 | select_fields | 非空 columns | 按声明顺序输出；缺列使任务失败 |
| 70 输出 | drop_fields | columns | 不允许删除全部字段 |
| 70 输出 | rename | column, target | 最终改显示名；禁止目标冲突和保留元数据名 |

## 历史 schema1：失败动作和多条原因

校验、转换和去重接受onError=drop/mark/fail，默认drop。range/iqr另外接受null。drop将行移出后续处理；mark保留违规单元格当前值和该记录，并添加`_quality_valid`/`_quality_errors`，其他正常转换继续执行；fail使任务失败，日志指出规则和字段；null只把异常单元格替换成缺失，但保留问题明细。

仅V2校验阶段延迟删除以收集多规则原因；V3每条规则立即删除，若需要后续继续收集应选择mark/null。同一规则的多个失败字段仍全部记录。脏数据包含原始值、`_record_id`、`_source_row`、`_field`、`_rule_index`、`_error_code`、`_reason`；V3另外包含文件/工作表、规则ID、该规则前值、动作与最终去向，见[交付说明](DELIVERY-V3.md)。

## 历史 schema1：数值、日期和统计约定

整数要求精确int64范围和无小数，数值拒绝非有限值。precision使用十进制ROUND_HALF_UP舍入。range及有序compare在比较前保留整数和十进制精度，避免超过2^53后错误判定；均值、中位数和IQR统计使用浮点数。eq/ne使用类型感知的等值判断：数字之间按数值相等，布尔值独立于数字，字符串不隐式转数字；有序和文本比较遇到不兼容类型按失败策略处理。布尔转换接受true/false、1/0、yes/no（字符串忽略大小写），其他值按失败策略处理。

日期使用 Python strptime / strftime 格式（如 `%Y/%m/%d`）。dateFormat 控制解析，UTC 为内部时区，outputFormat 控制导出文本；未配置时使用 ISO-8601。数值型 Unix 时间戳不自动猜测单位。

均值 / 中位数仅用当前仍参与处理的非空有效数值，报告有效样本及排除情况；众数按出现次数，平局取原始顺序先出现的值。全空或没有有效数值时：fail 明确失败，leave 保留缺失，constant 使用声明的固定值。统计是在对应步骤的全任务存活集合上计算，不按读取分块分别计算；去重位于统计之后。

IQR使用pandas线性分位数，factor必须是0–100有限数。零IQR仍使用Q1/Q3界限，全部缺失遵循emptyPolicy。CSV/XLSX导出会转义可能被解释为公式的字符串，报告给出提示。

## 历史 schema1：声明式示例

```json
[
  {"type":"rename","column":"f1","target":"customer_name"},
  {"type":"trim","columns":["f1","f2"]},
  {"type":"cast","columns":["f3"],"dtype":"number","precision":2,"onError":"drop"},
  {"type":"fill_stat","columns":["f3"],"strategy":"median","emptyPolicy":"fail"},
  {"type":"regex","column":"f2","pattern":"[^@]+@[^@]+","onError":"mark"},
  {"type":"compare","column":"f3","operator":"gte","value":0,"onError":"mark"},
  {"type":"deduplicate","columns":["f0"],"keep":"last"}
]
```

显式 `v2-phased` 执行此例时按阶段先规范化、转换及填充，再校验、去重，最后改名；`v3-sequential` 则先执行第一条 rename。字段 ID f1 始终引用原第二列。参数不经过 eval、SQL 拼接或 shell 执行。

## Schema3：规则声明与绑定

每条规则必须有唯一 `id`（1–128 字符）、`type`、布尔 `enabled`；停用的规则仍校验参数，但不参与执行或改变字段生命周期。逻辑字段 ID 匹配 `[A-Za-z][A-Za-z0-9_]{0,63}`，label 为 1–128 字符。规则保存时校验声明和引用；创建任务时要求用户确认 `fieldBindings`，每个引用必须绑定，禁止两个逻辑字段绑定同一个 fN。未引用的原字段默认保留。

```json
{
  "name": "金额校验",
  "schemaVersion": 3,
  "engineVersion": "v3-disk",
  "logicalFields": [{"id":"amount","label":"金额"}],
  "rules": [
    {"id":"cast-amount","type":"cast","enabled":true,
     "fieldIds":["amount"],"dtype":"decimal","precision":38,
     "scale":2,"rounding":"HALF_UP","onError":"null"},
    {"id":"positive","type":"range","enabled":true,
     "fieldId":"amount","min":"0.00","onError":"mark"}
  ]
}
```

任务创建另传 `fieldBindings:{"amount":"f2"}`，可同时传当前 `parseVersion`。Java 读取拥有权限的文件解析快照，调用 `/internal/preflight`；公开入口为 `POST /api/rules/preflight`。预检返回 `normalizedRules`、`outputFields`、warnings，任务冻结实际规则、字段映射、解析版本和限额。`GET /api/rule-schemas/3` 返回 21 类正式 JSON Schema；接口请求和响应见 [OpenAPI](openapi.json)。

fN 身份始终对应原解析位置。rename 只改显示名；select/drop 后再引用已删除字段会在预检失败。投影不得删掉全部字段，select_fields 按给定顺序输出。rename 禁止当前字段名冲突及质量/来源元数据保留名。每次 cast 都替换该字段之前的类型与导出格式声明，避免旧日期格式影响后续类型。

## Schema3：精确类型与常量

支持 `string`、`integer`、`decimal`、`datetime`、`boolean`，不接受历史 dtype `number`。integer 为精确 signed int64。decimal 的 precision 为总位数 1–38，scale 为小数位 0–12 且不得超过 precision，默认 precision38 / scale2；只接受 HALF_UP。舍入后再检查域，例如 `-1.235 → -1.24`，`precision:2,scale:2` 可表示 `0.01`；超过声明域按失败动作处理。

| 常量类型 | JSON 示例 | 约束 |
| --- | --- | --- |
| string | `{"type":"string","value":"OK"}` | 最多10000字符 |
| integer | `{"type":"integer","value":"9223372036854775807"}` | 可用 JSON 整数或精确整数字符串；浏览器优先字符串以保留超过2^53的值 |
| decimal | `{"type":"decimal","value":"123.45","scale":2}` | value 必须精确十进制文本；可指定 precision |
| boolean | `{"type":"boolean","value":true}` | value 必须 JSON 布尔值 |
| datetime | `{"type":"datetime","value":"2026-01-02T00:00:00Z"}` | ISO-8601，内部统一 UTC |
| null | `{"type":"null","value":null}` | 仅允许 missing/enum 等允许空常量的参数；填充值与 compare 常量不得 null |

上述常量用于 markers、values、fill_null.value、fill_stat 的空集合回退值和 compare.value；range 的 min/max、IQR factor 接受十进制文本或有限 JSON 数值。需要精确保留边界时使用文本。日期默认接受严格 ISO 日期/时间，或显式 dateFormat 的受支持 strptime 指令；不猜测数值 Unix 时间戳。outputFormat 仅适用于 datetime，生成文本；未配置时 Parquet 保留 timestamp，CSV/XLSX 导出 ISO 文本。

数字等值键使用精确数值，`1`、decimal `1.00` 等值，`-0` 与 `0` 等值；字符串 `"1"`、布尔 true 与数值1不同。数值排序、去重、众数、范围比较和统计使用 Decimal，避免38位数经默认28位上下文或浮点数丢失精度。compare 的有序比较要求实际数值或相同兼容类型，不隐式解析文本数值。

原 Parquet 的 float32/float64 在未改变为其它数值类型时保留原物理类型和值，普通 `1.2345` 不会被默认两位小数误拒。显式 cast 为 decimal 后始终输出 decimal128；统计填充或精确整数常量引入 decimal 值时，写出前逐批观测当前数值列所需 scale，保留尚未修改原值的精度，整数常量超过2^53也不会转回浮点。需要 decimal 物理列而实际值超出38位/12位小数域时明确失败，提示显式设置 cast scale 或转字符串；原生浮点值可原样往返，精确十进制规则仍按声明域校验。

## Schema3：21 类实际规则

下表所有字段都指逻辑 ID；`onError` 未声明时默认 drop。普通校验跳过缺失，用 required/drop_null 组合非空约束。配置/绑定错误、预算超限、正则超时和取消使任务停止，不作为可 mark 的普通质量问题。

| type | 必需业务参数 | 可选参数与实际语义 |
| --- | --- | --- |
| trim | fieldIds | 只对字符串去首尾空白；非字符串与缺失不变 |
| case | fieldIds, mode | mode=lower/upper；只处理字符串 |
| cast | fieldIds, dtype | precision/scale/rounding 仅用于 decimal；dateFormat/outputFormat 仅用于 datetime；onError=drop/mark/null/fail |
| validate_type | fieldIds, dtype | 检查可转换性，不修改业务值；允许 decimal 参数和 dateFormat；onError=drop/mark/fail |
| missing | fieldIds, markers | 1–100 个类型常量；匹配项置 null；原 null 保持缺失 |
| fill_null | fieldIds, value | 非空类型常量，仅填缺失；已知字段类型必须兼容 |
| fill_stat | fieldIds, strategy, emptyPolicy | strategy=mean/median/mode；emptyPolicy=fail/leave/constant；constant 才能且必须声明 value；invalidPolicy 固定 exclude；mean/median 可声明 precision/scale/rounding |
| drop_null | fieldIds | 缺失字段逐个产生问题；onError=drop/mark/fail |
| required | fieldIds | 同 drop_null 的非空判断；不隐式 trim；onError=drop/mark/fail |
| range | fieldId | min/max 至少一个且 min≤max；inclusiveMin/inclusiveMax 默认 true；实际非数值为违规；onError 可含 null |
| iqr | fieldId, emptyPolicy | emptyPolicy=fail/leave；factor 默认精确1.5、范围0–100；包含上下界；onError 可含 null |
| enum | fieldId, values | 1–1000 个类型常量；按类型感知的精确等值判断；onError=drop/mark/fail |
| length | fieldId | min/max 至少一个，0–1000000整数；Unicode 字符数；非字符串违规；onError=drop/mark/fail |
| regex | fieldId, pattern | 1–256字符、完整匹配字符串；不把数值转成文本；受限语法；onError=drop/mark/fail |
| compare | fieldId, operator, value | operator=eq/ne/gt/gte/lt/lte/contains/starts_with/ends_with；value 为非空类型常量；字符串谓词要求两侧字符串 |
| deduplicate | 无 | fieldIds 省略/空表示所有当前字段；keep=first/last 默认 first；nullsEqual 默认 true；onError=drop/mark/fail |
| select_fields | fieldIds | 非空且不重复；保持声明顺序；不修改字段身份 |
| drop_fields | fieldIds | 删除指定当前字段；不得删除全部字段 |
| rename | fieldId, target | target 为1–128字符非保留显示名，禁止冲突 |
| replace | fieldIds, search, replacement | 新增；mode 仅 literal；search 非空≤10000字符，replacement≤10000字符；count 默认0表示全部，范围0–100000；只处理字符串 |
| compare_fields | leftFieldId, rightFieldId, operator, valueType, nullPolicy | 新增；operator=eq/ne/gt/gte/lt/lte；valueType 为五种类型；nullPolicy=skip/violation；onError=drop/mark/fail |

replace 不执行正则表达式，`.`、`$` 按字面量处理。例如 `search:".", replacement:"$", count:1` 把 `a.b.c` 变成 `a$b.c`。每单元格匹配次数最多100000，替换前预测增长，替换后再次检查整条记录字节预算；不会执行代码。

compare_fields 的 decimal 允许实际 integer/decimal 两侧，integer 则要求两侧都是整数；datetime 比较 UTC 时间点，string 按 Unicode 顺序，boolean 只允许 eq/ne。任一侧缺失时 skip 不产生问题，violation 记录一次含两字段前值的问题。类型已知不兼容时预检拒绝，类型未知时执行中按 onError 处置，不自动 cast。例如先 cast 两个日期字段，再声明 `leftFieldId:"start_at",operator:"lte",rightFieldId:"end_at",valueType:"datetime",nullPolicy:"violation"`。

## Schema3：跨批次的全局算法

每条启用规则在当前存活集合上完成一次操作后，下一条才开始。CSV 按逻辑记录流式读取，带引号的换行不增加来源序号；XLSX 用 read-only 遍历选定工作表；Parquet 用 Arrow batches，跨 row group 的序号连续。SQLite 保存原始记录、当前记录、存活标记和问题事件，结果与脏数据逐批写出。

均值/中位数要求已知文本字段先显式数值 cast；统计有效样本并报告排除的非数值/缺失数。均值使用精确 sum/count，在写入填充值时按声明 scale 做 HALF_UP。中位数使用全局索引的线性分位数：`h=(n−1)×q`，在相邻样本间插值。IQR 同样取 q=0.25/0.75，`lower=Q1−factor×(Q3−Q1)`、`upper=Q3+factor×(Q3−Q1)`；零 IQR 仍校验阈值；无有效数值时 fail 失败、leave 不操作。均值/中位数无有效样本时 constant 仅接受 integer/decimal 回退值。

众数按全局类型键计数，平票取原始来源序号最早的值。去重先全局确定每个键的 first/last 来源记录，再在第二遍处置其它记录；nullsEqual=false 时含缺失键的每条记录独立，不能互相合并。二者均使用磁盘索引，批次切分不改变统计结果和赢家。

正则每单元格10ms、整条规则10秒；规则时钟跨所有批次连续，不在换批时重置。受限语法拒绝 lookaround、特殊分组、反向引用和重复分组等；超时直接使任务失败。整任务另外受父进程截止时间限制。

## Schema3：问题、删除归因和产物

drop 将来源记录立即移出后续统计/去重；mark 保留违规当前值；null 仅允许 cast/range/iqr，将对应单元格置空并保留问题；fail 停止任务。同一规则内多个违规字段均记录。一次问题是事件，早期 mark/null 后来被删除仍保留早期事件；恢复填充值也不抹掉此前质量标记。

报告核对 `inputRows = outputRows + finalRemovedRows`、`outputRows = validOutputRows + flaggedOutputRows`；finalRemovalByRule 每条来源最多归因一个删除规则。issueRows 是所有曾有问题的唯一来源记录数，issueCount 为问题事件数；changedCells/missingFilledCells/typeConversionFailedCells 为逐规则事件计数，可在同一字段重复累计。逐规则报告有 stageRows.input/output、durationMs 及动作计数；统计值以类型常量或精确十进制文本报告。

脏数据保留完整原始值、文件/工作表、1-based逻辑来源序号、稳定 recordId、ruleId、规则前类型值、动作和最终去向；最终去向为 REMOVED_BY_THIS_RULE、REMOVED_BY_LATER_RULE 或 OUTPUT_FLAGGED。recordId 由文件身份、解析版本/快照、解析选项、工作表和来源序号生成，同一冻结输入跨 attempt 保持一致。结果仍有问题时附加 `_quality_errors` JSON 和 `_quality_valid`。CSV/XLSX 对公式前导字符串加保护前缀；Parquet 保留类型，混合字段必须末尾显式 cast 或改用 CSV/XLSX；不会为了完成导出静默丢失精度。

磁盘默认 batchRows1000、单记录4MiB、批字节32MiB、scratch1GiB、单记录累计问题1MiB；任务可在硬上限内降低或调整。source/current/original 编码、变更放大和脏数据都受预算约束，整任务另有文件/行/列/单元格/文本/问题数量/输出文件限制。scratch 统计包括下载、SQLite、可见 journal、Excel 临时文件及结果；下载按剩余额度限写，事务提交前后检查，父进程独立采样并停止超限任务。它是应用层检测与有界批次控制，不能视为文件系统硬配额；检查间仍可能有有界增长，原生库解压、XLSX 共享字符串及操作系统缓存也不等同于 batch 字节数。部署需给容器内存/磁盘留出余量，实际容量以本轮测量为准。

完整八行规则和输入见 [标准案例](v3/examples/ordered-rules.json)、[输入](v3/examples/input.csv)；执行 schema3 时选择 `v3-disk`，逻辑字段和规则无需转换。该案例预期输入8、输出4、有效3、标记1、最终删除4、问题记录5、问题事件5、changedCells22，均值填充值185.33。
