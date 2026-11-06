<script setup lang="ts">
import { computed } from 'vue'
import type { Scalar } from '../types'
const props = defineProps<{ modelValue?: Scalar; label?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: Scalar | undefined] }>()
const kind = computed(() => props.modelValue === null ? 'null' : typeof props.modelValue === 'number' ? 'number' : typeof props.modelValue === 'boolean' ? 'boolean' : 'string')
function changeKind(value: string) { emit('update:modelValue', value === 'null' ? null : value === 'number' ? 0 : value === 'boolean' ? true : '') }
</script>
<template>
  <div class="scalar-input">
    <el-select :model-value="kind" class="scalar-kind" :aria-label="`${label || '值'}的类型`" @update:model-value="changeKind"><el-option value="string" label="文本" /><el-option value="number" label="数字" /><el-option value="boolean" label="布尔" /><el-option value="null" label="空值" /></el-select>
    <el-input v-if="kind === 'string'" :model-value="String(modelValue ?? '')" :aria-label="label || '文本值'" placeholder="允许空字符串" maxlength="2000" @update:model-value="emit('update:modelValue', $event)" />
    <el-input-number v-else-if="kind === 'number'" :model-value="typeof modelValue === 'number' ? modelValue : undefined" :aria-label="label || '数字值'" :controls="false" @update:model-value="emit('update:modelValue', $event)" />
    <el-select v-else-if="kind === 'boolean'" :model-value="modelValue" :aria-label="label || '布尔值'" @update:model-value="emit('update:modelValue', $event)"><el-option :value="true" label="true" /><el-option :value="false" label="false" /></el-select>
    <span v-else class="null-scalar">null · 缺失值</span>
  </div>
</template>
