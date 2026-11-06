import type { DiskRule, LogicalField, Rule, TypedScalar } from './types'
import { ruleLabels } from './rules.ts'

export const diskRuleLabels = { ...ruleLabels, replace: '替换文本', compare_fields: '跨字段比较' }
export const diskTypeLabels = { integer: '整数 int64', decimal: '精确小数 Decimal', string: '文本', datetime: '日期时间', boolean: '布尔值' }
export const roundings = ['HALF_UP']
export const diskSingleTypes = ['range', 'iqr', 'enum', 'length', 'regex', 'compare', 'rename']
export function diskRule(type: DiskRule['type'] = 'trim'): DiskRule {
  const rule: DiskRule = { id: crypto.randomUUID(), enabled: true, type }
  if (type === 'compare_fields') return { ...rule, leftFieldId: '', rightFieldId: '', operator: 'lte', valueType: 'decimal', nullPolicy: 'violation', onError: 'drop' }
  if (diskSingleTypes.includes(type)) rule.fieldId = ''
  else rule.fieldIds = []
  if (type === 'case') rule.mode = 'lower'
  if (type === 'replace') Object.assign(rule, { search: '', replacement: '', mode: 'literal', count: 0 })
  if (type === 'cast' || type === 'validate_type') Object.assign(rule, { dtype: 'string', onError: 'drop' })
  if (type === 'fill_null' || type === 'compare') rule.value = { type: 'string', value: '' }
  if (type === 'fill_stat') Object.assign(rule, { strategy: 'mean', emptyPolicy: 'fail', scale: 2, rounding: 'HALF_UP' })
  if (type === 'missing') rule.markers = [{ type: 'string', value: '' }]
  if (type === 'enum') rule.values = [{ type: 'string', value: '' }]
  if (type === 'range') Object.assign(rule, { min: '0', max: '100', inclusiveMin: true, inclusiveMax: true })
  if (type === 'iqr') Object.assign(rule, { factor: '1.5', emptyPolicy: 'fail' })
  if (type === 'regex') rule.pattern = ''
  if (type === 'length') rule.min = 0
  if (type === 'rename') rule.target = ''
  if (type === 'deduplicate') rule.keep = 'first'
  if (type === 'compare') rule.operator = 'eq'
  if (['cast', 'validate_type', 'drop_null', 'required', 'range', 'iqr', 'enum', 'length', 'regex', 'compare', 'deduplicate'].includes(type)) rule.onError = 'drop'
  return rule
}
export function validateDiskRules(rules: DiskRule[], fields: LogicalField[], bindings?: Record<string, string>) {
  if (rules.length > 50) throw new Error('一个规则集最多包含 50 条规则。')
  if (!fields.length || fields.length > 200) throw new Error('请定义 1–200 个逻辑字段。')
  const fieldIds = new Set(fields.map(field => field.id))
  if (fields.some(field => !/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(field.id) || !field.label.trim() || field.label.length > 128) || fieldIds.size !== fields.length) throw new Error('逻辑字段 ID 需以英文字母开头，仅含字母、数字和下划线，最多64位；ID 不能重复，名称不能为空且最多128字。')
  if (bindings) {
    const values = Object.values(bindings).filter(Boolean)
    if (new Set(values).size !== values.length) throw new Error('逻辑字段与输入列需一一对应，不能重复绑定同一列。')
  }
  const ids = new Set<string>()
  for (const [index, rule] of rules.entries()) {
    const prefix = `第 ${index + 1} 条规则：`
    if (!rule.id || ids.has(rule.id)) throw new Error(prefix + '规则 ID 不能为空或重复。')
    ids.add(rule.id)
    if (!Object.hasOwn(diskRuleLabels, rule.type)) throw new Error(prefix + '规则类型不受支持。')
    const refs = rule.type === 'compare_fields' ? [rule.leftFieldId, rule.rightFieldId] : diskSingleTypes.includes(rule.type) ? [rule.fieldId] : rule.fieldIds || []
    if ((!refs.length && rule.type !== 'deduplicate') || refs.some(field => !field || !fieldIds.has(field))) throw new Error(prefix + '请选择有效的逻辑字段。')
    if (bindings && refs.some(field => !bindings[field!])) throw new Error(prefix + '逻辑字段尚未绑定输入列。')
    if (rule.dtype === 'decimal' || rule.type === 'fill_stat') {
      if (!Number.isInteger(rule.scale ?? 2) || (rule.scale ?? 2) < 0 || (rule.scale ?? 2) > 12) throw new Error(prefix + 'Decimal 小数位需为 0–12 的整数。')
      if (!Number.isInteger(rule.precision ?? 38) || (rule.precision ?? 38) < Math.max(1, rule.scale ?? 2) || (rule.precision ?? 38) > 38 || (rule.rounding ?? 'HALF_UP') !== 'HALF_UP') throw new Error(prefix + 'Decimal precision 需为 1–38 且不小于 scale，舍入方式需为 HALF_UP。')
    }
    if (rule.type === 'replace' && (!rule.search || !Number.isInteger(rule.count ?? 0) || (rule.count ?? 0) < 0 || (rule.count ?? 0) > 100000)) throw new Error(prefix + '替换查找文本不能为空，次数需为 0–100000 的整数。')
    if (rule.type === 'compare_fields' && rule.valueType === 'boolean' && !['eq', 'ne'].includes(rule.operator || '')) throw new Error(prefix + '布尔跨字段比较仅支持等于或不等于。')
  }
}
export function migrateLegacyRules(rules: Rule[], fields: { id?: string; name: string }[]) {
  const logicalFields = fields.map((field, index) => ({ id: `field_${field.id || `f${index}`}`, label: field.name }))
  const resolve = (ref: string) => {
    const index = fields.findIndex(field => field.id === ref || field.name === ref)
    if (index < 0) throw new Error(`旧规则字段 ${ref} 不在当前输入中，请先选择相应文件。`)
    return logicalFields[index]!.id
  }
  const typed = (value: unknown): TypedScalar => value === null ? { type: 'null', value: null } : typeof value === 'number'
    ? { type: Number.isInteger(value) ? 'integer' : 'decimal', value: String(value), ...(Number.isInteger(value) ? {} : { scale: Math.min(12, String(value).split('.')[1]?.length || 0) }) }
    : typeof value === 'boolean' ? { type: 'boolean', value } : { type: 'string', value: String(value ?? '') }
  const nextRules = rules.map((source, index) => {
    const { columns, column, ...config } = source
    const rule = { ...diskRule(source.type), ...config, id: `rule-${index + 1}`, enabled: true } as unknown as DiskRule
    if (columns) rule.fieldIds = columns.map(resolve)
    if (column) rule.fieldId = resolve(column)
    if (rule.dtype === ('number' as string)) { rule.dtype = 'decimal'; rule.scale = source.precision ?? 2; rule.precision = 38; rule.rounding = 'HALF_UP' }
    else delete rule.precision
    if ('value' in source) rule.value = typed(source.value)
    if (source.values) rule.values = source.values.map(typed)
    if (source.markers) rule.markers = source.markers.map(typed)
    for (const key of ['min', 'max', 'factor'] as const) if (rule[key] !== undefined && rule.type !== 'length') rule[key] = String(rule[key])
    return rule
  })
  return { logicalFields, rules: nextRules, fieldBindings: Object.fromEntries(fields.map((field, index) => [logicalFields[index]!.id, field.id || `f${index}`])) }
}
