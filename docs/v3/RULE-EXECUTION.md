# V3 规则执行方案：评审草案

本文是 **V3 评审草案**，描述拟实现的契约、算法和验收口径，不表示这些行为已经上线，也不改变当前 V2 的代码、规则顺序、依赖或已验证容量。所有默认值在创建规则版本时解析并冻结，任务保存实际采用的引擎、规则、绑定、解析选项和限额快照。

## 1. 执行版本、顺序与历史任务

- 新方案的版本标识为 `engineVersion: "v3-sequential"`。严格按配置数组从第一条执行到最后一条，不按规则种类、阶段或是否需要全局统计调整顺序。
- 历史任务使用 `v2-phased`，继续执行已冻结的 V2 阶段排序、规则和错误策略。没有版本字段的旧任务在迁移边界明确归为 V2；不能根据当前默认配置推断成 V3。历史重试继续使用其引擎版本和全部快照。
- 每条规则使用必填的 `id`（规则身份）、`type`（规则种类）和 `enabled`（boolean）。修改参数保留 `id`；新增规则生成新 ID；数组重排只改变执行位置。`id` 在同一规则版本内唯一。`enabled:false` 保留在配置快照中、跳过执行，报告该规则的 `status:"DISABLED"`，不计运行时间和行命中。报告规则条目保存 `ruleId`（来自输入 `id`）和原数组 1-based `index`；任务级 `executionOrder` 按执行顺序列出已启用规则 ID，与 canonical report 一致。
- 用户界面可提示“通常先转换再填充”，但只能由用户明确调整数组，不能自动排序。Planner 可以改变分块和落盘方式，不能改变可观察到的规则先后关系。
- 每条规则接收上一条完成后的存活记录、当前值和字段集合；只有当前存活记录参与该条规则的全局统计。原始输入序号不会因删除、分块或投影而重新编号。

### 错误策略

规则的 `onError` 默认 `drop`。支持范围见后文各规则。

| 策略 | 当前规则完成时的行为 | 后续规则 | 问题记录 |
| --- | --- | --- | --- |
| `drop` | 删除违规记录；同一规则内先收集该记录所有选中字段的问题 | 不再处理该记录 | 保留问题和唯一删除归因 |
| `mark` | 保留违规字段处理前值；同规则其他正常转换可继续 | 继续处理，并持续追加问题 | 记录规则、字段、处理前值及原因 |
| `null` | 将失败字段置为缺失，保留记录；仅适用于 `cast`、`range`、`iqr` | 后续规则看到缺失值 | 原失败值仍保存在问题中 |
| `fail` | 终止任务，给出首个确定性失败的规则、字段、来源序号和原因 | 不再执行 | 失败诊断可查询，没有成功结果下载 |

严格顺序下，`drop` 不会为了收集后面的原因而延迟删除。例如“邮箱校验 drop → 枚举校验”不会再对已删除记录执行枚举。需要收集多条原因时，应使用连续的 `mark`，再显式决定最终处置；不能恢复 V2 的隐式校验阶段聚合。若产品需要“全部标记后统一删除”，应另行定义、评审显式质量过滤规则，而不是隐藏在上述 21 类规则中。

配置错误、字段绑定缺失、磁盘不足、预算超限和取消属于任务控制错误，不应伪装成可跳过的单元格质量问题。正则超时默认使任务失败，不能无限重试。

## 2. 可复用规则集：逻辑字段与显式绑定

规则引用规则集的 `logicalFieldId`，任务创建时明确绑定到某个文件解析快照中的 `fN`。不按同名列、位置相似、推断类型或历史绑定自动猜测。

```json
{
  "logicalFields": [
    {"id": "order_id", "label": "订单号"},
    {"id": "amount", "label": "金额"},
    {"id": "customer_name", "label": "客户名"},
    {"id": "start_at", "label": "开始时间"},
    {"id": "end_at", "label": "结束时间"},
    {"id": "state", "label": "状态"}
  ],
  "fieldBindings": {
    "order_id": "f0",
    "customer_name": "f1",
    "amount": "f2",
    "start_at": "f3",
    "end_at": "f4",
    "state": "f5"
  }
}
```

逻辑字段 ID 建议使用 1–64 位字母开头的字母、数字、下划线，禁止元数据保留名。所有被规则引用的逻辑字段必须绑定；默认要求一一绑定，禁止两个逻辑字段悄悄映射同一个物理字段。确需重复映射应作为单独产品能力评审。未被规则引用的文件字段默认保留，直到显式投影。

同一规则集应用到另一文件时，用户重新确认绑定；原任务的绑定不受重解析、字段改名或新规则版本影响。开始执行前校验解析快照中的字段 ID、原名和顺序。`rename` 只改变最终显示名，不改变逻辑字段身份；被 `drop_fields` 删除的字段如果又被后续规则引用，任务预检失败，不重新绑定到其他同名列。

## 3. 公共类型、空值、指标和边界

### 类型与精度

- 支持 `string`、`integer`、`decimal`、`datetime`、`boolean`。`integer` 为精确有符号 int64。V3 `decimal` 使用 Decimal，不走 SQLite REAL 或 Python float 比较路径。V2 `number` 是旧类型名，仅在 V2 引擎中有效。
- Decimal 采用精度最多 38 位、小数位最多 12 位的拟定逻辑域；`scale` 是小数位数，默认 2，范围 0–12；舍入默认 `ROUND_HALF_UP`。V2 的 `precision` 含义和行为冻结；V3 正式输入只接受 `scale`；从旧版本导入必须通过显式迁移器把 `precision` 转成 `scale`，用户确认后保存新版本；不能静默兼容接受两个名称。
- 原始文本必须先精确解析，再按声明 scale 舍入和检查总位数，不能先转 float。Parquet 原生浮点数已经丢失的十进制信息无法恢复；如用户转换为 Decimal，应记录来源浮点类型及转换方式，不宣称恢复原始精度。
- JSON 中计数为整数、布尔为布尔。精确小数值使用规范十进制字符串，并同时声明逻辑类型和 scale，例如 `{"type":"decimal","value":"185.33","scale":2}`；不把 Decimal 强制转为 JSON double，也不使用 `default=str` 混淆计数类型。
- 日期默认 UTC；输入 `dateFormat` 可省略，省略时只接受文档明确列出的 ISO-8601 形式，不猜测数字型 Unix 时间戳或日/月顺序。`outputFormat` 只接受受限 strftime 格式或 `iso8601`。非空输入转换成 NaT 必须算失败。
- 显式 null 始终是缺失。空字符串是否缺失由解析选项或此前的 `missing` 决定。非字符串不会被 `trim` 自动变成字符串；字符串 `"1"` 与数字 `1` 在等值比较中不同，布尔值与数字分开。

### 每条规则的公共指标

报告遵循 schema version 3。每条启用规则输出 `ruleId`、`inputRows`、`outputRows`、`hitRows`、`issueCount`、`changedCells`、`removedRows`、`markedRows`、`durationMs`；按需另含 `hitCells`、`nulledCells`、规则专属指标和 statistics。不适用的计数明确为 0，不省略、不借用其他指标。任务汇总使用 `validOutputRows`（严格有效输出，不含 mark/null 保留的问题记录）、`flaggedOutputRows`、`finalRemovedRows`、`issueRows`（至少有一条问题的唯一来源记录）、`issueCount` 等字段。

- `hitRows`：本规则命中条件或产生质量失败的唯一记录数；`hitCells`：命中的字段事件数。多列规则中一行可命中多个字段。
- `changedCells`：本规则结束时仍保留的记录中，实际改变逻辑类型或内容的单元格数；输出文本格式或字段显示名变化另记，不算内容清洗。后续删除不倒扣此前计数。
- `removedRows`：本规则首次实际删除的唯一记录数。禁止记录复活，所以所有规则的该项之和等于最终删除数。
- 正常的“去空白后仍相同”等无变化不记 changed；缺失填成非缺失才记填充；失败后 `mark` 保留值不记成功转换。
- 校验/转换问题按“规则 × 来源记录 × 字段”记录；整行去重使用明确的键字段集合，不为每个键字段重复生成相同的去重问题。

### 共同资源约束

保留当前已评审的文件、行、列、单元格、逻辑文本、输出字节和脏数据预算作为初始安全上限，不因“流式”自动提高。另为单记录、分块字节、临时磁盘和每记录问题 JSON 设置明确硬限额；具体默认值在容量试验后定稿。预算不足应明确失败并清理，不截断、不返回部分成功。

## 4. 21 类规则规格

以下示例中的 `fieldIds`/`fieldId` 是规则集逻辑字段 ID，不是文件物理 `fN`；`null` 表示显式缺失。每条存储规则包含 `id`、`type`、`enabled`，配置快照完整保存默认值。下列局部示例为了可读性省略这三个公共成员中的 `id`/`enabled` 时，只是参数片段，不是可直接提交的完整规则。唯一完整八行示例使用 [ordered-rules.json](examples/ordered-rules.json)，其输入、字段、参数和报告分别见同目录设计样例；本文不再维护第二套完整配置。

### 4.1 trim：去除首尾空白

- **参数、默认、类型**：`fieldIds: logicalFieldId[]` 必填、非空、去重；没有隐式全列默认。
- **前置**：字段已绑定且当前仍存在；只处理字符串，不隐式 cast。
- **错误和边界**：null、数字、布尔、日期保持原值；只去 Unicode 首尾空白，不改中间空格。空白串可变成 `""`，但不会自动变 null；需要随后显式 `missing`。
- **指标**：matched 为存在可移除首尾空白的记录/字段，changed 为实际变化字段。
- **例子**：`{"type":"trim","fieldIds":["name"]}`：`[" Alice ","A B",null,2] → ["Alice","A B",null,2]`。

### 4.2 case：大小写转换

- **参数、默认、类型**：`fieldIds: logicalFieldId[]` 必填；`mode: "lower"|"upper"` 必填。
- **前置**：引用字段存在；只处理字符串。
- **错误和边界**：null 和非字符串保留；使用固定 Unicode 规则，不依赖服务器 locale。大小写可能增加字符数，转换过程中检查增长预算，不先构造整列再检查。
- **指标**：matched/changed 计实际发生变化的字段；预算超限使任务失败。
- **例子**：`{"type":"case","fieldIds":["name"],"mode":"upper"}`：`["Alice",null,3] → ["ALICE",null,3]`。

### 4.3 cast：类型转换

- **参数、默认、类型**：`fieldIds` 必填；`dtype` 必填，为五种逻辑类型之一；`onError` 默认 `drop`，支持 `drop/mark/null/fail`；decimal 的 `scale` 默认 2、0–12 整数，`rounding` 默认且首版只允许 `HALF_UP`；datetime 的 `dateFormat?`、`outputFormat?` 为受限格式字符串或显式 `"iso8601"`。其他 dtype 带 scale 或日期参数应拒绝。
- **前置**：字段存在；输出逻辑类型变化记录到当前 schema，不能依靠下游统计偷偷完成转换。
- **错误和边界**：缺失保留。integer 拒绝小数和 int64 溢出；decimal 拒绝非有限值和精度溢出；boolean 接受大小写无关 true/false、yes/no 和 1/0；datetime 拒绝不能解析或解析成 NaT 的非空值。mark 必须保留原失败值，不能被后续 nullable dtype 整理再变 null。
- **指标**：`typeConversionAttemptedCells`、`typeConversionSucceededCells`、`typeConversionFailedCells`，以及公共动作计数。失败置 null 不记为成功转换。
- **例子**：`{"type":"cast","fieldIds":["amount"],"dtype":"decimal","scale":2,"onError":"null"}`：`["1.235","bad",null] → [1.24,null,null]`，失败 1、置空 1，问题保留 `"bad"`。

### 4.4 missing：自定义缺失标记

- **参数、默认、类型**：`fieldIds` 必填；`markers: typedScalar[]` 必填、1–100 项，可包含 null；字符串长度受配置预算限制。
- **前置**：字段存在；使用类型感知等值，字符串 `"0"` 不匹配数字 `0`。
- **错误和边界**：已有 null 保持缺失；仅将命中标记归一为 null，不填充、不删行。标记错误为配置错误，不接受任意表达式。
- **指标**：`normalizedMissingCells` 只计算非缺失变缺失；已有 null 不算变化。
- **例子**：`{"type":"missing","fieldIds":["city"],"markers":[{"type":"string","value":""},{"type":"string","value":"NA"}]}`：`["", "NA", "Nanjing", null] → [null,null,"Nanjing",null]`，归一 2。

### 4.5 fill_null：常量填充

- **参数、默认、类型**：`fieldIds`、非 null 有限 typed scalar `value` 必填；无默认填充值。
- **前置**：字段存在；value 与当前明确类型兼容，否则配置/预检失败。未知/混合类型必须先明确转换或显式声明输出为 string，不悄悄提升整列。
- **错误和边界**：只填当前缺失；空串若未归一则不填。先预测“缺失数 × 填充值字节/内存”再分配，避免短文件放大成超大结果。
- **指标**：`missingFilledCells`、matched/changed；不产生质量问题，不删行。
- **例子**：`{"type":"fill_null","fieldIds":["city"],"value":{"type":"string","value":"Unknown"}}`：`[null,"Shanghai",""] → ["Unknown","Shanghai",""]`，填充 1。

### 4.6 fill_stat：全局统计填充

- **参数、默认、类型**：`fieldIds` 必填；`strategy: mean|median|mode` 必填；`invalidPolicy: exclude` 默认且首版只支持 exclude；`emptyPolicy: fail|leave|constant` 必填；constant 时必填兼容的非 null typed scalar `value`，其他策略不能带 value；mean/median 的 `scale` 默认 2，0–12；`rounding` 默认且首版仅支持 `HALF_UP`。
- **前置**：在规则当前位置的全部存活记录上计算；mean/median 使用有效、非缺失的当前数值，也允许在已声明 decimal 的字段精确解析数值字符串，全部使用 Decimal，禁止先转 float。不能按 chunk 分别统计。
- **错误和边界**：无有效样本严格执行 emptyPolicy，禁止静默回退到 0。invalidPolicy=exclude 将数值无效样本排除并计入 excludedInvalidValueCount，不在 fill_stat 中自动删除。mode 用类型感知规范键，平局取最早原始来源序号。填充不改变已有非缺失无效值。
- **指标**：每字段 `validValueCount`、`excludedInvalidValueCount`、`missingValueCount`、类型化 `fillValue`、`emptyPolicy`、`scale`、`rounding`、`missingFilledCells`；mean 另外记录精确 sum 和 unroundedMean 分子/分母，统计溢出使任务失败。
- **例子**：decimal 当前值 `[10,30,null]`，mean/scale2 得 `20.00` 并只填第 3 项。`[null,null]` 配 emptyPolicy=leave 则不填，validValueCount=0；constant/value=7 则填 2 个。

### 4.7 drop_null：缺失处置

- **参数、默认、类型**：`fieldIds` 必填；`onError` 默认 drop，支持 drop/mark/fail。
- **前置**：字段存在；基于当前缺失状态。
- **错误和边界**：任一选中字段缺失即该记录违规；同一规则先记录所有缺失字段，再按策略处置。空字符串不等于缺失，除非此前配置了归一。
- **指标**：`missingViolationCells`、hitRows、issueCount、各动作计数；一行多列缺失只删除一次。
- **例子**：`{"type":"drop_null","fieldIds":["id","name"]}`：`[{id:null,name:null},{id:"2",name:"B"}] → [{id:"2",name:"B"}]`，问题 2、删除 1。

### 4.8 range：数值范围

- **参数、默认、类型**：`fieldId` 必填；`min?`、`max?` 至少一个，有限精确数值；`inclusiveMin/Max` 默认 true；`onError` 默认 drop，支持 drop/mark/null/fail。
- **前置**：字段存在，当前值须是明确数值类型；V3 不把未 cast 的数字字符串悄悄当数值。边界按 Decimal 精确比较。
- **错误和边界**：缺失跳过；非数值、非有限数或越界为问题；min≤max。等边界且两边包含时只接受该值；排除边界可以形成空允许区间，但 UI 应明确提示。
- **指标**：`outlierMatchedRows/Cells`、低于/高于界限及类型失败计数、动作计数；阈值写入报告。
- **例子**：`{"type":"range","fieldId":"amount","min":"0.00","max":"200.00","onError":"null"}`：`[10,1000,null] → [10,null,null]`，异常处理 1、置空 1。

### 4.9 iqr：全局四分位异常检测

- **参数、默认、类型**：`fieldId` 必填；`factor` 默认十进制 1.5，有限且 0–100；`emptyPolicy: fail|leave` 必填；`onError` 默认 drop，支持 drop/mark/null/fail。
- **前置**：当前全部存活的有效数值样本；使用全局排序及线性分位数位置 `h=(n−1)q`，q=0.25/0.75，不能对各 chunk 的四分位数求平均。
- **错误和边界**：缺失跳过；非缺失非数值记录类型问题。没有有效样本按 emptyPolicy；leave 不制造不存在的阈值。零 IQR 仍使用闭区间 Q1..Q3；样本 1 个时上下界相等。插值及 factor 运算采用足够的 Decimal 工作精度，不能先转 float。
- **指标**：validValueCount、excludedInvalidValueCount、Q1、Q3、IQR、lower、upper、outlierHandledRows/Cells 和动作计数。
- **例子**：`[2,2,2,2,100]`，factor1.5 得 Q1=Q3=2；onError=null 后 `[2,2,2,2,null]`，异常处理 1。

### 4.10 required：必填校验

- **参数、默认、类型**：`fieldIds` 必填；`onError` 默认 drop，支持 drop/mark/fail。
- **前置**：当前字段存在；不隐式 trim，不把 0 或 false 视为缺失。
- **错误和边界**：每个缺失字段各记一条问题；空串是否违规取决于此前 missing 归一，而不是隐藏的 required 默认。
- **指标**：missingViolationCells、issueCount 和动作计数。
- **例子**：`{"type":"required","fieldIds":["name"],"onError":"mark"}`：`[null,"", "A"]` 全保留，只标记第 1 条。

### 4.11 enum：枚举校验

- **参数、默认、类型**：`fieldId` 必填；`values: typedScalar[]` 必填、1–1000 项；`onError` 默认 drop，支持 drop/mark/fail。
- **前置**：字段存在；枚举与当前逻辑类型兼容。数字间按精确数值相等，boolean 不与 1/0 混同。
- **错误和边界**：缺失跳过，即使 values 包含 null 也不替代 required；不自动忽略大小写或 trim。无效集合为配置错误。
- **指标**：notInEnumCells、hitRows、issueCount、动作计数。
- **例子**：`{"type":"enum","fieldId":"state","values":[{"type":"string","value":"A"},{"type":"string","value":"B"}],"onError":"mark"}`：`["A","BAD",null]` 保留 3 条，仅标记 BAD。

### 4.12 length：字符串长度

- **参数、默认、类型**：`fieldId` 必填；min?/max? 至少一项，0–1000000 整数；`onError` 默认 drop，支持 drop/mark/fail。
- **前置**：字段存在；只接受字符串，不把数值格式化后计长。
- **错误和边界**：缺失跳过；非字符串违规；长度按 Unicode code point 计数，不按 UTF-8 字节或视觉字形簇。边界包含，min≤max。
- **指标**：tooShortCells、tooLongCells、typeMismatchCells、动作计数。
- **例子**：`{"type":"length","fieldId":"name","min":1,"max":2}`：`["张三","Alice",null] → ["张三",null]`，删除 1。

### 4.13 regex：受保护的正则校验

- **参数、默认、类型**：`fieldId`、1–256 字符 pattern 必填；`onError` 默认 drop，支持 drop/mark/fail；默认大小写敏感、完整匹配。
- **前置**：字段存在且为字符串；模式保存前由 Python 权威校验，不接受任意代码或动态 flag 表达式。
- **错误和边界**：缺失跳过；非字符串违规；拒绝危险/不支持的扩展组、反向引用和重复分组；每单元格 10ms、每规则 10 秒初始安全预算。预算超时为任务失败，不无限重试、不悄悄略过该记录。
- **指标**：patternMismatchCells、typeMismatchCells、regexEvaluationMs、动作计数。
- **例子**：`{"type":"regex","fieldId":"email","pattern":"[^@]+@[^@]+"}`：`["a@b","bad",null] → ["a@b",null]`。

### 4.14 validate_type：无副作用类型检查

- **参数、默认、类型**：`fieldIds`、dtype 必填；datetime 可有 dateFormat；decimal 可有 scale；`onError` 默认 drop，支持 drop/mark/fail。
- **前置**：字段存在；使用与 cast 一致的转换判定，但不提交转换结果、不修改 schema。
- **错误和边界**：缺失跳过；非缺失转成 NaT、非有限数、小数转整数和超范围均违规。即使验证字符串 `"10"` 可转整数，通过后仍为原字符串；后续数值规则仍需显式 cast。
- **指标**：typeCheckedCells、typeViolationCells、issueCount、动作计数；changedCells 恒为 0。
- **例子**：`{"type":"validate_type","fieldIds":["age"],"dtype":"integer","onError":"mark"}`：`["10","1.5",null]` 原值全保留，只标记 1.5。

### 4.15 compare：字段与常量的声明式比较

- **参数、默认、类型**：`fieldId`、operator、带类型的有限 value 必填；operator 为 eq/ne/gt/gte/lt/lte/contains/starts_with/ends_with；onError 默认 drop，支持 drop/mark/fail。
- **前置**：字段与目标类型兼容；数值用 Decimal，日期用 UTC 时间点；不接受表达式、脚本、函数名或 SQL。
- **错误和边界**：缺失默认跳过。eq/ne 类型感知，不把字符串 `"2"` 当数字 2；有序/文本比较不兼容是问题。文本操作仅字符串，默认区分大小写；不自动 trim。
- **指标**：predicateViolationCells、typeMismatchCells、动作计数。
- **例子**：`{"type":"compare","fieldId":"amount","operator":"gt","value":{"type":"decimal","value":"0.00"}}`：`[10,0,null] → [10,null]`。

### 4.16 deduplicate：全任务去重

- **参数、默认、类型**：`fieldIds?: logicalFieldId[]`，省略/空数组表示全部当前业务字段；`keep: first|last` 默认 first；`nullsEqual: boolean` 默认 true；onError 默认 drop，支持 drop/mark/fail。
- **前置**：使用当前存活记录、当前字段和值；元数据和问题列表不参加键。键类型/字段顺序规范化并冻结，不能因读取分块变化。
- **错误和边界**：first/last 按原始来源序号；完整扫描后才能确定 last。nullsEqual=true 时缺失在同一键位视为相等；false 时含缺失的键按来源身份视为不同；string/decimal/bool 分开，数值按规范精确值相等。mark 保留所有记录，仅对非保留代表记录记重复问题。
- **指标**：duplicateMatchedRows、duplicateRemovedRows、distinctKeys、keepPolicy、动作计数；每个重复记录一条去重问题，不按键字段数倍增。
- **例子**：`[{id:"1",v:"first"},{id:"2",v:"middle"},{id:"1",v:"last"}]`，fieldIds=[id]、keep=last 后保留 `[middle,last]`，删除 1。

### 4.17 select_fields：选择和重排字段

- **参数、默认、类型**：非空、无重复 fieldIds 必填，无隐式全选。
- **前置**：所有逻辑字段当前存在；配置顺序就是输出字段顺序。
- **错误和边界**：缺字段立即预检失败；不会按同名寻找替代字段。追溯/质量元数据属于独立系统字段，不得通过业务字段投影静默移除。
- **指标**：fieldsBefore、fieldsAfter、projectedFields、reorderedFields；行数不变，changedCells=0。
- **例子**：业务字段 `[id,name,amount]`，fieldIds=[amount,id] 后为 `[amount,id]`，记录数不变。

### 4.18 drop_fields：删除字段

- **参数、默认、类型**：非空、无重复 fieldIds 必填。
- **前置**：字段存在；删除后至少保留一个业务字段。完整计划预检后续引用，不允许删除后再隐式恢复。
- **错误和边界**：不能删除保留系统字段；重复或不存在字段拒绝，而不是忽略。原始记录仍保存在追溯存储中，字段删除不影响问题导出的原始值。
- **指标**：droppedFields、fieldsBefore/After；行数不变，changedCells=0。
- **例子**：`[id,name,temp]` 删除 temp 后 `[id,name]`；只剩 id 时再次删除 id 属配置错误。

### 4.19 rename：改输出显示名

- **参数、默认、类型**：fieldId、1–128 字符 target 必填；没有自动冲突覆盖。
- **前置**：当前逻辑字段存在；目标不与其他当前输出字段名或系统保留名冲突。
- **错误和边界**：不改变内部 logicalFieldId、文件 fN、原始字段名和既有问题。后续规则仍引用逻辑 ID，不引用 target。改成自身现有显示名为无操作。
- **指标**：renamedFields、noOpFields；行数不变，changedCells=0。
- **例子**：rename amount→金额 后，后续 range(fieldId=amount) 仍引用同一逻辑字段；最终表头为金额。

### 4.20 replace：字符串字面量替换

- **参数、默认、类型**：fieldIds、非空 search 字符串、replacement 字符串必填；`mode` 默认且首版仅允许 literal；`count` 默认 0 表示全部，其他为正整数且受单元格替换次数预算限制；默认区分大小写。
- **前置**：字段存在；只处理字符串。首版不新增正则替换或分组展开，正则能力仅用于 regex 校验。
- **错误和边界**：null 和非字符串保留；replacement 可为空以删除片段。拒绝空 search，避免隐式在每个字符间插入。按不重叠、从左到右替换；先限制增长量，不能用循环替换直到无匹配。配置 mode=regex 明确拒绝，不能假装按 literal 执行。
- **指标**：replacementOccurrences、hitRows/Cells、changedCells、outputGrowthBytes；命中但替换前后相同则 changed=0。
- **例子**：`{"type":"replace","fieldIds":["phone"],"search":"-","replacement":"","count":0}`：`["021-123-456",null] → ["021123456",null]`，替换 2 次、变更 1 字段。

### 4.21 compare_fields：字段间比较

- **参数、默认、类型**：`leftFieldId`、`rightFieldId` 和 operator 必填；operator 为 eq/ne/gt/gte/lt/lte；`valueType: decimal|integer|string|datetime|boolean` 必填；`nullPolicy: skip|violation` 默认 skip；onError 默认 drop，支持 drop/mark/fail。
- **前置**：两字段已显式绑定且当前存在；逻辑类型兼容。数值以精确 Decimal 比较，日期均为 UTC 时间点，字符串有序比较按 Unicode code point；布尔只支持 eq/ne。
- **错误和边界**：任一缺失遵循 nullPolicy，不把 null 当 0、空串或最早日期。默认不把两 null 判为已满足业务等式；需要非空应先 required。类型不兼容为问题；失败事件同时记录两字段及各自处理前值。比较同一字段可接受但应在 UI 提示冗余/恒真或恒假。
- **指标**：comparisonCheckedRows、comparisonViolationRows、skippedMissingRows、typeMismatchRows、动作计数；每次两字段约束失败是一条问题，而不是两条。
- **例子**：`{"id":"r-period","type":"compare_fields","enabled":true,"leftFieldId":"start_at","rightFieldId":"end_at","valueType":"datetime","operator":"lte","nullPolicy":"violation","onError":"drop"}`：开始 2026-11-03、结束 2026-11-02 违规并删除；相等通过；任一缺失也违规并按 drop 处置。

## 5. Handler Registry 与执行计划

处理器使用受控注册表，例如 `registry[(engineVersion, ruleType)]`；元信息包含 JSON 参数 schema、默认值、引用字段、允许错误策略、类型前置、输出类型变化、指标 schema 和执行特征。

执行特征至少区分 `row_local`、`global_aggregate`、`global_rank`、`global_key_selection`、`projection`。接口分为 `validate`、`plan`、`execute_batches`、`finalize_metrics`；全局算子另外声明 prepare/scan/finalize/replay，不在逐行回调里偷偷维护跨 chunk 不完整统计。

注册表只能包含随项目发布、经测试的处理器，不接受用户模块路径、动态 Python、SQL 或 shell。规则 schema 和文档可以来自同一元数据源，避免默认值、参数白名单和页面提示分叉。V2 处理器与 V3 分开版本路由，不在一份函数中依赖不断增加的兼容分支。

基线执行器按规则边界落盘/分段。后续 Planner 只有在证明不改变失败定位与统计口径时，才连接连续的 row_local 规则以减少落盘，但必须对每条记录保持用户指定顺序，对每条规则保持完整指标/错误语义。遇到需要全局当前集合的规则，先完成前面所有规则，再物化该边界的存活集合，统计后继续，禁止跨越该边界重排。

## 6. 有界流式与 SQLite 本地磁盘算法

### 读取和保存当前集合

CSV 使用保持解码器和解析器状态的记录迭代器；带换行的引号字段只能算一条记录。Parquet 使用 Arrow RecordBatch，不先转换整文件 pandas。分块同时限制行数和字节数，超大单记录有单独上限。XLSX 继续受文件/展开字节/行列/共享字符串预算限制，不宣传无限流式。

每个 attempt 使用独立临时目录和 SQLite 文件；它是本机中间数据，不是共享业务库。保存带类型的当前值、不可变原始记录、来源序号、问题 ledger、最终 disposition 和全局键/排序索引。批量事务、游标读取；禁止 `fetchall` 和把完整问题集保存在 Python 列表中。

临时磁盘配额必须包括数据库、索引、事务日志、输入缓存、中间 Parquet、dirty 和最终输出。分配/写入前预测，批次后复核，拒绝超过配额；不以“文件上传只有 100MiB”推断中间数据也小。SQLite 运行时版本、journal/cache/temp 配置属于测量记录，不先宣称任意版本都兼容。

### Decimal 排序键

不使用 SQLite REAL/NUMERIC 亲和性承载 Decimal。对满足 p≤38、s≤12 的数值，以足够 Decimal 工作精度精确放大到公共 `10^12` 得到整数，规范化负零，再生成有序定长 TEXT/BLOB 键。例如将该有符号整数加上固定偏移 `10^50` 并编码为 51 位零填充十进制字符串；数值比较等价于二进制字典序比较。转换前严格检查域，不能截断溢出或默默舍入排序键。

为 `(sortKey,sourceRecordNumber)` 建立磁盘索引。分位数需要的秩通过全局有序游标一次扫描取得，不为每个 chunk 分别计算、不采用近似分位数。对于整数/Decimal 混合，先规范到同一逻辑数值域；字符串和 boolean 不进入这个数值索引。

### 全局统计及去重

- **mean**：对当前有效存活数值，Decimal 累加 sum/count；累加工作精度覆盖输入精度和计数增长位数；最后按声明 scale/HALF_UP 舍入。不是 chunk 均值的简单平均。
- **median**：全局有序值的中间秩，偶数样本取两中间值的精确平均，再按声明 scale 舍入。
- **IQR**：按全局线性分位数公式取相邻秩并用 Decimal 插值；中间位数、factor 和阈值采用足够精度，报告实际阈值，不将它们压回输入 scale 后再比较。
- **mode**：按类型化规范值建键并累计 count、min(sourceRecordNumber)，按 count 降序、minSourceRecordNumber 升序取一个；null 不参加。规范键与其他等值语义一致，数字 1 与 1.00 相等，字符串 "1" 独立。
- **keep-first/last**：全局规范键映射到 min/max(sourceRecordNumber)，第二遍按原始来源序号输出保留记录。索引用摘要缩短键时，必须同时核对完整规范键，不能把摘要碰撞当成相等。

SQLite 的代价是索引构建、可能多次全局扫描和单写连接吞吐；这些是可测量的性能边界。只有基准证明其成为主要瓶颈时，再评审 DuckDB 等新依赖及兼容性，不把候选版本当成已验证依赖。

## 7. 追溯、问题事件与最终删除口径

每条来源记录保存 `fileId`、解析快照 ID、sheet（CSV/Parquet 为 null）、`sourceRecordNumber` 和稳定 recordId。CSV 的序号是逻辑记录序号，不是物理文本行；XLSX 可另保存真实工作表行号；Parquet 序号跨 row group 连续。重试沿用来源身份，不把 attempt 或当前剩余行数作为来源序号。

每条问题事件保存至少：`ruleId`、index、字段逻辑 ID、原始字段名、fileId、sheet、sourceRecordNumber、带类型的处理前值、errorCode、reason、requestedAction。跨字段比较的处理前值是左右字段的明确结构。问题导出同时能取得不可变原始记录，不能把“前面已 trim/cast 的值”误称原始值。

最终 disposition 每个 recordId 最多一条删除决定，保存 `removedByRuleId`、删除类别和首次删除原因。问题的 `finalAction` 在最终落定后明确为保留、保留但标记、置空后保留、被本规则删除、被后续规则删除或任务失败。先 mark 后被去重删除的记录，不计入最终 flagged 输出；其早期问题仍可追溯，不能消失。

### 可对账的报告

- `inputRows = outputRows + finalRemovedRows`；outputRows 是全部输出，包含 mark 保留的行。
- `validOutputRows + flaggedOutputRows = outputRows`；validOutputRows 是最终输出中没有任何质量问题的记录，flaggedOutputRows 是最终输出中至少有一条问题的记录。置空后保留的违规行也属于 flagged，而不是严格有效。
- `finalRemovedRows = Σ rule.removedRows`；首次删除拥有单一归因，禁止跨规则重复计入最终删除。
- `duplicateRemovedRows` 是最终删除中归因去重的子集，`outlierRemovedRows` 是归因 range/IQR 的子集；其他类别共同构成 finalRemovedRows。
- `outlierHandledRows` 是至少命中过一个异常规则并采取动作的唯一来源记录数，可能与其他事件指标重叠，不与 duplicateRemovedRows 直接相加对账。
- `missingFilledCells` 是各规则将缺失变为非缺失的字段事件数；同一字段之后再次置空又填充会再记一次，属于处理量，不是最终唯一单元格数。
- `typeConversionFailedCells` 是转换失败事件数；同一字段经两条 cast 失败属于两次事件。各条规则的 failed 数之和可对账到总值。
- `issueCount` 是问题事件数，`issueRows` 是至少有一个问题的唯一来源记录数，均可能大于 finalRemovedRows；不能再使用 V1 的 `input=output+dirty`。

如果继续提供旧字段名 `removedRows`、`flaggedRows`、`dirtyRows`，它们必须分别是 finalRemovedRows、flaggedOutputRows、issueRows 的精确别名，并在报告 schemaVersion 中标明，不能同时出现不同定义。

## 8. 八行完整推演与验收样例

### 唯一规范样例及执行顺序

完整输入与配置以 [ordered-rules.json](examples/ordered-rules.json) 及其配套八行输入为唯一 canonical 示例，不在本文件复制第二套字段名、规则参数或规则 ID。规则集字段声明使用 `logicalFields:[{id,label,...}]`；任务绑定使用 logicalFieldId → 文件 fN，不能按字段名猜测。输入规则 ID 使用 `id`；报告/问题事件中的引用字段仍叫 `ruleId`。

canonical 八行来源记录的 sourceRecordNumber 保持 1..8。金额原始文本依次为 10、缺失、bad、1000、12、30、20、40。第 1、5 条的 order_id 都为 001；第 6 条 start_at 晚于 end_at；第 7 条 state=BAD，其他状态满足 canonical 的 OK 枚举。金额空白按解析 missingValues=[""] 读为 null。CSV 序号按逻辑记录，不能因物理换行或删除重排。

配置严格执行以下八条，ID/参数以 JSON 文件为准：

| 规则 ID | 输入行 | 输出行 | 关键结果 | 新增问题 | 本规则删除 |
| --- | --- | --- | --- | --- | --- |
| r01-trim | 8 | 8 | 仅规范化 customer_name；不影响金额或来源序号 | 0 | 0 |
| r02-cast-amount | 8 | 7 | fieldIds=[amount]、dtype=decimal、scale=2、rounding=HALF_UP、onError=drop；第 3 条 bad 删除，第 2 条仍缺失 | 1 | 1 |
| r03-fill-mean | 7 | 7 | 当前有效金额 10/1000/12/30/20/40，sum=1112.00、validValueCount=6；1112/6 按 HALF_UP scale2 为 185.33，填第 2 条 | 0 | 0 |
| r04-range | 7 | 6 | fieldId=amount，min/max 为精确十进制字符串 0.00/200.00；此时才删除第 4 条 1000 | 1 | 1 |
| r05-cast-dates | 6 | 6 | fieldIds=[start_at,end_at]、dtype=datetime、dateFormat/outputFormat=iso8601；类型有效不等于先后关系有效 | 0 | 0 |
| r06-dates-order | 6 | 5 | leftFieldId=start_at、operator=lte、rightFieldId=end_at、valueType=datetime、nullPolicy=violation、onError=drop；删除第 6 条 | 1 | 1 |
| r07-enum | 5 | 5 | fieldId=state，values=[{type:string,value:OK}]，onError=mark；第 7 条 BAD 标记保留 | 1 | 0 |
| r08-dedup | 5 | 4 | fieldIds=[order_id]、keep=last、nullsEqual=true、onError=drop；001 的存活记录为 1 和 5，删除 1、保留 5 | 1 | 1 |

不能先 range 再 mean；那会改变统计集合并错误得到其他填充值。不能把“开始晚于结束”当成日期类型转换错误；它由下一条 compare_fields 处理。nullPolicy=violation 表示缺失是比较失败并按 onError 处置，而不是无条件使任务 fail。

### 最终输出和计数

最终按原始来源序号输出 **[2,5,7,8]**：

| 来源记录序号 | order_id | amount | state | 最终质量状态 |
| --- | --- | --- | --- | --- |
| 2 | 002 | 185.33 | OK | 严格有效 |
| 5 | 001 | 12.00 | OK | 严格有效 |
| 7 | 006 | 20.00 | BAD | 标记保留，1 条枚举问题 |
| 8 | 007 | 40.00 | OK | 严格有效 |

日期仍在输出中，以上表格仅省略重复列。最终报告的 metrics 摘要应与 [report.json](examples/report.json) 一致：

```json
{
  "inputRows": 8,
  "outputRows": 4,
  "finalRemovedRows": 4,
  "validOutputRows": 3,
  "flaggedOutputRows": 1,
  "issueRows": 5,
  "issueCount": 5,
  "missingFilledCells": 1,
  "typeConversionFailedCells": 1,
  "duplicateRemovedRows": 1,
  "outlierHandledRows": 1
}
```

删除集合为 **[1,3,4,6]**，分别归因 deduplicate、cast、range、compare_fields；标记保留集合为 [7]。8=4+4，4=3+1。输出数 4 **包含**标记保留；不能将 outputRows 写成 3，也不能把严格有效数 3 解释成“所有成功导出的记录数”。五条问题对应来源 [1,3,4,6,7]，没有把一个删除记录重复算两次最终删除。

## 9. 取消、失败恢复与容量验证

读取每批、逐行规则每批、全局聚合/排序游标、写每批输出和上传前后均检查取消；长期 native 调用设置可解释的上限或在 Worker 子进程边界终止。HTTP/MinIO/队列网络错误使用有限次数、有限总耗时的退避重试；永久解析/规则错误不重试。任务级 attempt/lease 与幂等产物路径继续隔离旧 Worker。

临时目录在成功、失败、取消时清理；进程 SIGKILL 产生的遗留目录由启动/定时回收按任务 attempt 和存活 lease 清理，不能删除仍在运行的目录。磁盘满、预算超限或取消不得留下可下载的“成功”部分结果。

容量报告必须公布 CPU、RAM、容器资源限制、Python/SQLite/Arrow 版本、输入格式/字节/行列/单字段宽度、规则数组、分块设置、并发、耗时、峰值 RSS、临时磁盘峰值及正确性断言。至少测试跨 chunk 重复、keep-last、全局均值/median/mode/IQR、Decimal 边界、多问题、取消、网络失败和磁盘不足。现有 V2 测试不能证明 V3 容量；未测规模明确标为未验证，配置安全上限不能当成实测承诺。
