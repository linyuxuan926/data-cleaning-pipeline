<script setup lang="ts">
import type { Scalar } from '../types'
import ScalarInput from './ScalarInput.vue'
const props = defineProps<{ modelValue: Scalar[]; label?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: Scalar[]] }>()
function update(index: number, value: Scalar | undefined) { emit('update:modelValue', props.modelValue.map((item, i) => i === index ? value ?? null : item)) }
</script>
<template>
  <div class="scalar-list">
    <div v-for="(value, index) in modelValue" :key="index" class="scalar-list-row"><ScalarInput :model-value="value" :label="`${label || '列表值'} ${index + 1}`" @update:model-value="update(index, $event)" /><el-button text type="danger" :aria-label="`删除第 ${index + 1} 个值`" @click="emit('update:modelValue', modelValue.filter((_, i) => i !== index))">×</el-button></div>
    <el-button size="small" :disabled="modelValue.length >= 100" @click="emit('update:modelValue', [...modelValue, ''])">＋ 添加值</el-button>
  </div>
</template>
