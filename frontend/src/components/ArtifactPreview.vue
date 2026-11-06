<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import { number } from '../format'
import type { Preview } from '../types'

const props = defineProps<{ path: string; kind: 'result' | 'dirty'; enabled: boolean }>()
const preview = ref<Preview | null>(null)
const busy = ref(false)
const error = ref('')
let generation = 0
async function refresh() {
  const sequence = ++generation
  if (!props.path || !props.enabled) return
  busy.value = true; error.value = ''
  try {
    const value = await api<Preview>(`${props.path}?kind=${props.kind}&limit=100`)
    if (sequence === generation) preview.value = value
  } catch (e) { if (sequence === generation) error.value = e instanceof Error ? e.message : '读取产物预览失败。' }
  finally { if (sequence === generation) busy.value = false }
}
watch(() => [props.path, props.kind, props.enabled], () => {
  generation++; preview.value = null; error.value = ''; busy.value = false
  if (props.enabled) void refresh()
}, { immediate: true })
onBeforeUnmount(() => { generation++ })
function cell(value: unknown) { return value === null || value === undefined ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : String(value) }
</script>

<template>
  <section class="artifact-preview">
    <div class="inline-heading"><h3>{{ kind === 'result' ? '清洗结果预览' : '违规明细预览' }}</h3><el-button size="small" :loading="busy" :disabled="!enabled" @click="refresh">刷新预览</el-button></div>
    <p class="form-note">读取实际产物前 100 行。{{ kind === 'result' ? '输出可能包含保留并标记的记录，请检查质量字段。' : '同一条原始记录可能对应多条违规明细。' }}完整内容可从上方下载。</p>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="panel-alert" />
    <el-skeleton v-if="busy && !preview" :rows="4" animated />
    <template v-if="preview"><el-alert v-for="(warning, index) in preview.warnings || []" :key="index" :title="warning" type="warning" :closable="false" class="panel-alert" /><el-table :data="preview.rows" stripe max-height="480" empty-text="该产物没有数据行。"><el-table-column v-for="column in preview.columns" :key="column.id || column.name" min-width="175"><template #header><span>{{ column.name }}</span><small class="column-dtype">{{ column.dtype }}</small></template><template #default="{ row }"><el-popover v-if="column.name === '_quality_errors' || column.name === '_reason'" trigger="click" width="360" placement="top"><pre class="quality-error-detail">{{ cell(row[column.name]) }}</pre><template #reference><el-button text size="small" type="primary">查看明细</el-button></template></el-popover><span v-else :class="{ 'null-cell': row[column.name] == null }">{{ cell(row[column.name]) }}</span></template></el-table-column></el-table><p class="form-note">当前显示 {{ number(preview.previewRowCount) }} 行{{ preview.rowCount == null ? '；预览未统计完整产物行数。' : ` / 产物 ${number(preview.rowCount)} 行。` }}</p></template>
    <el-empty v-else-if="!busy && !error" description="所选尝试成功完成后可读取产物预览。" />
  </section>
</template>
