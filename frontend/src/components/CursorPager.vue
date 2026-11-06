<script setup lang="ts">
defineProps<{ page: number; canPrevious: boolean; hasMore: boolean; loading?: boolean; count: number; error?: string }>()
defineEmits<{ previous: []; next: []; refresh: [] }>()
</script>
<template><div class="cursor-pager"><span>第 {{ page }} 页 · 本页 {{ count }} 条</span><el-button size="small" :disabled="!canPrevious || loading" @click="$emit('previous')">上一页</el-button><el-button size="small" :disabled="!hasMore || loading" @click="$emit('next')">下一页</el-button><el-button size="small" :loading="loading" @click="$emit('refresh')">刷新本页</el-button></div><el-alert v-if="error" :title="error" type="error" :closable="false" class="panel-alert" /></template>
