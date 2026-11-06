<script setup lang="ts">
import type { Settings } from '../types'
import { bytes, number } from '../format'
defineProps<{ settings: Settings | null; compact?: boolean }>()
</script>
<template>
  <div v-if="settings" class="limits-display" :class="{ compact }"><strong>当前处理限额</strong><p>文件 ≤ {{ bytes(settings.maxFileBytes) }} · {{ number(settings.maxRows) }} 行 · {{ number(settings.maxColumns) }} 列 · {{ number(settings.maxCells) }} 单元格。Excel 解压 ≤ {{ bytes(settings.maxExcelExpandedBytes) }}。</p><p>预览上限 {{ number(settings.previewRows) }} 行 · 每人 {{ settings.maxActivePerUser }} 个活跃任务 · 平台 {{ settings.maxActiveGlobal }} 个活跃任务 · 每人每分钟提交 {{ settings.submissionsPerMinute }} 次。</p></div>
  <el-alert v-else title="尚未读取到平台限额，请刷新后上传或创建任务。" type="warning" :closable="false" show-icon />
</template>
