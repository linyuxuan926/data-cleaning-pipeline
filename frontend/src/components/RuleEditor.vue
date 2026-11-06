<script setup lang="ts">
import { computed } from 'vue'
import type { ColumnProfile, Rule, RuleType } from '../types'
import { dataTypeLabels, executionPlan, newRule, ruleHints, ruleLabels, singleColumnTypes, validationTypes } from '../rules'
import ScalarInput from './ScalarInput.vue'
import ScalarListInput from './ScalarListInput.vue'

const props = defineProps<{ modelValue: Rule[]; columns: ColumnProfile[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: Rule[]] }>()
const plan = computed(() => executionPlan(props.modelValue))
const operators = { eq: '等于（区分类型）', ne: '不等于（区分类型）', gt: '大于', gte: '大于或等于', lt: '小于', lte: '小于或等于', contains: '包含文本', starts_with: '文本开头为', ends_with: '文本结尾为' }
function update(index: number, patch: Partial<Rule>) {
  emit('update:modelValue', props.modelValue.map((rule, i) => i === index ? { ...rule, ...patch } : rule))
}
function changeType(index: number, type: RuleType) { emit('update:modelValue', props.modelValue.map((rule, i) => i === index ? newRule(type) : rule)) }
function move(index: number, direction: number) {
  const values = [...props.modelValue]
  const [item] = values.splice(index, 1)
  if (item) values.splice(index + direction, 0, item)
  emit('update:modelValue', values)
}
function remove(index: number) { emit('update:modelValue', props.modelValue.filter((_, i) => i !== index)) }
function executionNumber(index: number) { return plan.value.findIndex(item => item.index === index) + 1 }
</script>

<template>
  <div class="rule-editor">
    <el-alert title="严格按配置顺序逐条执行。上移或下移规则会改变执行顺序；移除策略在该条规则执行后生效。" type="info" :closable="false" class="panel-alert" />
    <el-collapse v-if="modelValue.length" class="execution-plan"><el-collapse-item title="查看实际执行顺序" name="order"><ol><li v-for="item in plan" :key="item.index">#{{ item.index + 1 }} {{ ruleLabels[item.rule.type] }}</li></ol></el-collapse-item></el-collapse>
    <div v-if="!modelValue.length" class="empty-rules"><span class="empty-symbol">≋</span><strong>从一条规则开始</strong><p>可规范数据、处理缺失与异常、校验并映射字段。空规则集仅转换格式和生成质量报告。</p></div>
    <article v-for="(rule, index) in modelValue" :key="index" class="rule-row">
      <div class="rule-row-heading">
        <span class="rule-number">{{ String(index + 1).padStart(2, '0') }}</span>
        <el-select :model-value="rule.type" class="rule-type" aria-label="规则类型" @update:model-value="changeType(index, $event)"><el-option v-for="(label, type) in ruleLabels" :key="type" :value="type" :label="label" /></el-select>
        <div class="rule-controls"><el-button size="small" :disabled="index === 0" :aria-label="`上移第 ${index + 1} 条规则`" @click="move(index, -1)">↑</el-button><el-button size="small" :disabled="index === modelValue.length - 1" :aria-label="`下移第 ${index + 1} 条规则`" @click="move(index, 1)">↓</el-button><el-button size="small" text type="danger" @click="remove(index)">移除</el-button></div>
      </div>
      <div class="rule-phase"><span>配置顺序</span>实际第 {{ executionNumber(index) }} 步</div>
      <div class="rule-fields v2-rule-fields">
        <label class="field full-field"><span>{{ singleColumnTypes.includes(rule.type) ? '目标字段' : rule.type === 'deduplicate' ? '判重字段（留空 = 整行）' : rule.type === 'select_fields' ? '保留字段（按选择顺序输出）' : '应用字段' }}</span>
          <el-select v-if="!singleColumnTypes.includes(rule.type)" :model-value="rule.columns || []" multiple filterable allow-create default-first-option collapse-tags collapse-tags-tooltip placeholder="选择字段，或输入原始列名后回车" aria-label="应用列" @update:model-value="update(index, { columns: $event })"><el-option v-for="column in columns" :key="column.id || column.name" :value="column.id || column.name" :label="`${column.name}${column.id ? ' · ' + column.id : ''}`" /></el-select>
          <el-select v-else :model-value="rule.column" filterable allow-create default-first-option placeholder="选择字段，或输入原始列名后回车" aria-label="目标列" @update:model-value="update(index, { column: $event })"><el-option v-for="column in columns" :key="column.id || column.name" :value="column.id || column.name" :label="`${column.name}${column.id ? ' · ' + column.id : ''}`" /></el-select>
        </label>
        <label v-if="rule.type === 'case'" class="field"><span>转换方式</span><el-select :model-value="rule.mode" aria-label="转换方式" @update:model-value="update(index, { mode: $event })"><el-option value="lower" label="转为小写" /><el-option value="upper" label="转为大写" /></el-select></label>
        <label v-if="['cast', 'validate_type'].includes(rule.type)" class="field"><span>数据类型</span><el-select :model-value="rule.dtype" aria-label="数据类型" @update:model-value="update(index, { dtype: $event, ...(rule.type === 'cast' ? { precision: undefined, dateFormat: undefined, outputFormat: undefined } : {}) })"><el-option v-for="(label, value) in dataTypeLabels" :key="value" :value="value" :label="label" /></el-select></label>
        <label v-if="rule.type === 'cast' && rule.dtype === 'number'" class="field"><span>小数精度（可选，0–12）</span><el-input-number :model-value="rule.precision" :min="0" :max="12" :precision="0" :controls="false" placeholder="保留原精度" @update:model-value="update(index, { precision: $event })" /></label>
        <template v-if="rule.type === 'cast' && rule.dtype === 'datetime'"><label class="field"><span>输入日期格式（可选）</span><el-input :model-value="rule.dateFormat" placeholder="自动识别，如 %Y-%m-%d" maxlength="100" @update:model-value="update(index, { dateFormat: $event || undefined })" /></label><label class="field"><span>输出日期格式（可选）</span><el-input :model-value="rule.outputFormat" placeholder="如 %Y-%m-%d %H:%M:%S" maxlength="100" @update:model-value="update(index, { outputFormat: $event || undefined })" /></label></template>
        <label v-if="rule.type === 'fill_null' || rule.type === 'compare' || (rule.type === 'fill_stat' && rule.emptyPolicy === 'constant')" class="field full-field"><span>{{ rule.type === 'compare' ? '比较值' : rule.type === 'fill_stat' ? '无有效值时的替换常量' : '替换值' }}</span><ScalarInput :model-value="rule.value" @update:model-value="update(index, { value: $event })" /></label>
        <label v-if="rule.type === 'missing'" class="field full-field"><span>缺失标记（类型敏感）</span><ScalarListInput :model-value="rule.markers || []" label="缺失标记" @update:model-value="update(index, { markers: $event })" /></label>
        <label v-if="rule.type === 'enum'" class="field full-field"><span>允许值列表（类型敏感）</span><ScalarListInput :model-value="rule.values || []" label="枚举值" @update:model-value="update(index, { values: $event })" /></label>
        <label v-if="rule.type === 'fill_stat'" class="field"><span>统计策略</span><el-select :model-value="rule.strategy" @update:model-value="update(index, { strategy: $event })"><el-option value="mean" label="均值（有效数值）" /><el-option value="median" label="中位数（有效数值）" /><el-option value="mode" label="众数（最早值优先）" /></el-select></label>
        <label v-if="['fill_stat', 'iqr'].includes(rule.type)" class="field"><span>没有有效值时</span><el-select :model-value="rule.emptyPolicy || 'fail'" @update:model-value="update(index, { emptyPolicy: $event, value: $event === 'constant' ? '' : undefined })"><el-option value="fail" label="任务失败并说明原因" /><el-option value="leave" label="保留原值" /><el-option v-if="rule.type === 'fill_stat'" value="constant" label="使用指定常量" /></el-select></label>
        <template v-if="['range', 'length'].includes(rule.type)"><label class="field"><span>{{ rule.type === 'range' ? '数值下界（可留空）' : '最少字符数（可留空）' }}</span><el-input-number :model-value="rule.min" :min="rule.type === 'length' ? 0 : undefined" :precision="rule.type === 'length' ? 0 : undefined" :controls="false" @update:model-value="update(index, { min: $event })" /><el-checkbox v-if="rule.type === 'range'" :model-value="rule.inclusiveMin !== false" @update:model-value="update(index, { inclusiveMin: Boolean($event) })">包含下界</el-checkbox></label><label class="field"><span>{{ rule.type === 'range' ? '数值上界（可留空）' : '最多字符数（可留空）' }}</span><el-input-number :model-value="rule.max" :min="rule.type === 'length' ? 0 : undefined" :precision="rule.type === 'length' ? 0 : undefined" :controls="false" @update:model-value="update(index, { max: $event })" /><el-checkbox v-if="rule.type === 'range'" :model-value="rule.inclusiveMax !== false" @update:model-value="update(index, { inclusiveMax: Boolean($event) })">包含上界</el-checkbox></label></template>
        <label v-if="rule.type === 'iqr'" class="field"><span>IQR 系数</span><el-input-number :model-value="rule.factor" :min="0" :step="0.5" :controls="false" @update:model-value="update(index, { factor: $event })" /></label>
        <label v-if="rule.type === 'deduplicate'" class="field"><span>保留方式</span><el-select :model-value="rule.keep || 'first'" @update:model-value="update(index, { keep: $event })"><el-option value="first" label="保留原始顺序第一条" /><el-option value="last" label="保留原始顺序最后一条" /></el-select></label>
        <label v-if="rule.type === 'compare'" class="field"><span>验证条件</span><el-select :model-value="rule.operator" @update:model-value="update(index, { operator: $event })"><el-option v-for="(label, value) in operators" :key="value" :value="value" :label="label" /></el-select></label>
        <label v-if="rule.type === 'regex'" class="field full-field"><span>正则表达式</span><el-input :model-value="rule.pattern" placeholder="例如：^[^@]+@[^@]+$" maxlength="256" @update:model-value="update(index, { pattern: $event })" /></label>
        <label v-if="rule.type === 'rename'" class="field"><span>新的列名</span><el-input :model-value="rule.target" placeholder="输入最终列名" maxlength="128" @update:model-value="update(index, { target: $event })" /></label>
        <label v-if="validationTypes.includes(rule.type)" class="field full-field"><span>不符合规则时</span><el-select :model-value="rule.onError || 'drop'" aria-label="失败处理策略" @update:model-value="update(index, { onError: $event })"><el-option value="drop" label="移除整行，并记录违规明细" /><el-option value="mark" label="保留整行，附带质量标记和全部违规" /><el-option value="fail" label="任务失败，并记录规则、字段和原因" /><el-option v-if="['range', 'iqr'].includes(rule.type)" value="null" label="异常字段置空，记录违规明细" /></el-select></label>
      </div>
      <p class="rule-hint">{{ ruleHints[rule.type] }}</p>
    </article>
    <el-button class="add-rule" :disabled="modelValue.length >= 50" @click="emit('update:modelValue', [...modelValue, newRule()])">＋ 添加清洗规则 <span class="muted">{{ modelValue.length }}/50</span></el-button>
  </div>
</template>
