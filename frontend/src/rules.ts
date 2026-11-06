import type { DataType, Rule, RuleType, Scalar } from './types'

export const dataTypeLabels: Record<DataType, string> = { integer: '整数', number: '数值', string: '文本', datetime: '日期时间', boolean: '布尔值' }
export const ruleLabels: Record<RuleType, string> = {
  trim: '去除首尾空格', case: '统一大小写', missing: '规范缺失标记', fill_null: '填充空值', fill_stat: '统计值填充', drop_null: '过滤空值行',
  deduplicate: '删除重复行', cast: '转换数据类型', range: '数值范围校验', iqr: 'IQR 异常值检测', required: '必填校验', enum: '枚举值校验',
  length: '文本长度校验', regex: '正则格式校验', validate_type: '数据类型校验', compare: '条件校验', select_fields: '选择保留字段', drop_fields: '删除字段', rename: '重命名列',
}
export const ruleHints: Record<RuleType, string> = {
  trim: '仅处理字符串；保留中间空格。', case: '仅处理字符串；数字和空值保持原值。',
  missing: '将指定标记规范为缺失。可将去除空格后产生的空字符串继续转为空值。',
  fill_null: '将缺失值替换为指定值。CSV 原始空字段按解析设置处理；去除空格后形成的空字符串不会自动视为缺失。',
  fill_stat: '使用当前仍保留的有效值。均值/中位数忽略非数值；众数并列时采用原始顺序最早的值。',
  drop_null: '任一选中列为空时触发失败处理策略。', required: '选中列缺失时触发失败处理策略；空字符串是否缺失取决于解析设置与缺失标记规则。',
  deduplicate: '不选字段时按整行全局判重。首条/末条由原始输入顺序确定，质量标记不参与判重。',
  cast: '转换失败会记录字段和原因；整数严格为 int64。日期默认 UTC；格式可使用 %Y-%m-%d。',
  range: '至少填写一侧边界，支持含/不含边界。非空非数值也触发失败策略。',
  iqr: '使用当前有效数值计算 Q1、Q3 与异常范围；零 IQR 仍执行边界校验。阈值和样本数写入报告。',
  enum: '非空值必须属于允许列表，类型参与比较。缺失值跳过，可搭配必填规则。',
  length: '按 Unicode 字符数校验文本长度；非文本违规，缺失值跳过。',
  regex: '对非空值完整匹配。最多 256 字符，服务端限制单元格和整条规则匹配时间。',
  validate_type: '检查能否转换为目标类型，保留原值；缺失值跳过。',
  compare: '数值目标的大小比较按数值执行；相等比较区分类型。不兼容类型记为违规，缺失值跳过。',
  select_fields: '在当前步骤仅保留所选字段，按选择顺序排列。至少保留一列。', drop_fields: '在当前步骤删除所选字段，不能删除所有列；后续规则不能引用已删除字段。',
  rename: '在当前步骤修改列名。字段仍可通过稳定 ID 引用，目标列名不能重复。',
}
export const phaseNames: Record<number, string> = { 10: '规范化', 20: '类型转换', 30: '缺失值处理', 40: '异常值处理', 50: '数据校验', 60: '全局去重', 70: '最终字段映射' }
export function phaseOf(type: RuleType): number {
  if (['trim', 'case'].includes(type)) return 10
  if (type === 'cast') return 20
  if (['missing', 'fill_null', 'fill_stat', 'drop_null'].includes(type)) return 30
  if (['range', 'iqr'].includes(type)) return 40
  if (type === 'deduplicate') return 60
  if (['select_fields', 'drop_fields', 'rename'].includes(type)) return 70
  return 50
}
export function executionPlan<T extends { type: string }>(rules: T[], engineVersion = 'v3-sequential') {
  const plan = rules.map((rule, index) => ({ rule, index, phase: phaseOf(rule.type as RuleType) }))
  return engineVersion === 'v2-phased' ? plan.sort((a, b) => a.phase - b.phase || a.index - b.index) : plan
}
export const singleColumnTypes: RuleType[] = ['range', 'iqr', 'enum', 'length', 'regex', 'compare', 'rename']
export const validationTypes: RuleType[] = ['cast', 'drop_null', 'required', 'range', 'iqr', 'enum', 'length', 'regex', 'validate_type', 'compare', 'deduplicate']
export function newRule(type: RuleType = 'trim'): Rule {
  if (type === 'case') return { type, columns: [], mode: 'lower' }
  if (type === 'fill_null') return { type, columns: [], value: '' }
  if (type === 'fill_stat') return { type, columns: [], strategy: 'mean', emptyPolicy: 'fail' }
  if (type === 'missing') return { type, columns: [], markers: [''] }
  if (type === 'cast' || type === 'validate_type') return { type, columns: [], dtype: 'string', onError: 'drop' }
  if (type === 'range') return { type, column: '', min: 0, max: 100, inclusiveMin: true, inclusiveMax: true, onError: 'drop' }
  if (type === 'iqr') return { type, column: '', factor: 1.5, emptyPolicy: 'fail', onError: 'drop' }
  if (type === 'regex') return { type, column: '', pattern: '', onError: 'drop' }
  if (type === 'enum') return { type, column: '', values: [], onError: 'drop' }
  if (type === 'length') return { type, column: '', min: 0, onError: 'drop' }
  if (type === 'compare') return { type, column: '', operator: 'eq', value: '', onError: 'drop' }
  if (type === 'rename') return { type, column: '', target: '' }
  if (type === 'deduplicate') return { type, columns: [], keep: 'first', onError: 'drop' }
  return { type, columns: [], ...(validationTypes.includes(type) ? { onError: 'drop' as const } : {}) }
}
function isScalar(value: unknown): value is Scalar { return value === null || ['string', 'boolean'].includes(typeof value) || (typeof value === 'number' && Number.isFinite(value)) }
export function validateRules(rules: Rule[]) {
  if (rules.length > 50) throw new Error('一个规则集最多包含 50 条规则。')
  rules.forEach((rule, i) => {
    const prefix = `第 ${i + 1} 条规则：`
    if (singleColumnTypes.includes(rule.type)) {
      if (!rule.column?.trim()) throw new Error(prefix + '请选择或输入字段。')
    } else if (rule.type !== 'deduplicate' && (!rule.columns?.length || rule.columns.some(c => !c.trim()))) throw new Error(prefix + '请至少选择一个字段。')
    if (rule.type === 'rename' && !rule.target?.trim()) throw new Error(prefix + '请输入目标列名。')
    if (rule.type === 'regex' && (!rule.pattern || rule.pattern.length > 256)) throw new Error(prefix + '正则表达式长度需为 1–256 个字符。')
    if (rule.type === 'range' || rule.type === 'length') {
      if ((rule.min === undefined && rule.max === undefined) || (rule.min !== undefined && !Number.isFinite(rule.min)) || (rule.max !== undefined && !Number.isFinite(rule.max)) || (rule.min !== undefined && rule.max !== undefined && rule.min > rule.max)) throw new Error(prefix + '请至少填写一侧有效边界，且下界不大于上界。')
      if (rule.type === 'length' && [rule.min, rule.max].some(n => n !== undefined && (!Number.isInteger(n) || n < 0))) throw new Error(prefix + '长度边界必须是非负整数。')
    }
    if (rule.type === 'iqr' && (rule.factor === undefined || !Number.isFinite(rule.factor) || rule.factor < 0)) throw new Error(prefix + 'IQR 系数必须为非负数。')
    if ((rule.type === 'fill_null' || rule.type === 'compare' || (rule.type === 'fill_stat' && rule.emptyPolicy === 'constant')) && !isScalar(rule.value)) throw new Error(prefix + '请输入有效的文本、数字、布尔值或空值。')
    if ((rule.type === 'missing' || rule.type === 'enum') && (!(rule.type === 'missing' ? rule.markers : rule.values)?.length || !(rule.type === 'missing' ? rule.markers : rule.values)!.every(isScalar))) throw new Error(prefix + '值列表至少需要一个有效值。')
    if (rule.precision !== undefined && (!Number.isInteger(rule.precision) || rule.precision < 0 || rule.precision > 12)) throw new Error(prefix + '小数精度需为 0–12 的整数。')
  })
}
