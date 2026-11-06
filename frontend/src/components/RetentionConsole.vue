<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { date } from '../format'
import { useCursorPage } from '../pagination'
import CursorPager from './CursorPager.vue'
interface Policy { enabled: boolean; includeExisting: boolean; rawDays: number; resultDays: number; dirtyDays: number; reportDays: number; failedDays: number; logDays: number; orphanDays: number; graceHours: number; protectedBefore?: string }
interface Item { id: string; objectKey: string; taskId?: string; kind: string; state: string; reason: string; attempts: number }
interface Scope { fileIds?: string[]; taskIds?: string[]; includeOrphans?: boolean; afterObjectKey?: string; continuation?: string }
interface Scan { scannedObjects: number; matchedObjects: number; catalogInspected: number; logTasksInspected: number; nextContinuation: string | null; afterObjectKey: string; warnings: string[] }
interface Plan { id: string; state: string; createdAt: string; truncated: boolean; items: Item[]; policy: Policy; scope: Scope; scan?: Scan | null }
interface Run { id: number; planId: string; itemId: string; action: string; message: string; requestId: string; createdAt: string }
interface PlanSummary { id: string; state: string; truncated: boolean; createdBy: string; createdAt: string; updatedAt: string }
const policy = ref<Policy | null>(null)
const baseline = ref('')
const plan = ref<Plan | null>(null)
const busy = ref('')
const error = ref('')
const taskIds = ref('')
const fileIds = ref('')
const lookupId = ref('')
const includeOrphans = ref(false)
const planStatus = ref('ALL')
const runPlanId = ref('')
const runStatus = ref('ALL')
const plans = useCursorPage<PlanSummary>(() => '/admin/retention/plans/page', () => ({ status: planStatus.value }))
const runs = useCursorPage<Run>(() => '/admin/retention/runs/page', () => ({ planId: runPlanId.value.trim(), status: runStatus.value }))
watch(planStatus, () => { void plans.fetch(true) })
watch([runPlanId, runStatus], () => { void runs.fetch(true) })
const fields: { key: keyof Policy; label: string }[] = [{ key: 'rawDays', label: '原始文件保留天数' }, { key: 'resultDays', label: '结果保留天数' }, { key: 'dirtyDays', label: '脏数据保留天数' }, { key: 'reportDays', label: '报告产物保留天数' }, { key: 'failedDays', label: '失败产物保留天数' }, { key: 'logDays', label: '日志保留天数' }, { key: 'orphanDays', label: '孤立对象保留天数' }, { key: 'graceHours', label: '清理宽限小时' }]
async function perform(name: string, action: () => Promise<void>) { busy.value = name; error.value = ''; try { await action() } catch (e) { error.value = e instanceof Error ? e.message : '保留期操作失败。' } finally { busy.value = '' } }
async function refreshPolicy() { await perform('policy', async () => { policy.value = await api<Policy>('/admin/retention/policy'); baseline.value = JSON.stringify(policy.value) }) }
async function savePolicy() {
  if (!policy.value) return
  const old = JSON.parse(baseline.value) as Policy
  let confirmation: string | undefined
  if (!old.enabled && policy.value.enabled) {
    try { await ElMessageBox.confirm('启用后，管理员可正式执行经预览的清理计划。当前受保护的既有数据不会自动纳入。确定启用？', '启用保留期清理', { type: 'warning', confirmButtonText: '启用', cancelButtonText: '返回' }) } catch { policy.value.enabled = false; return }
    confirmation = 'ENABLE_RETENTION'
  }
  await perform('save', async () => { const { protectedBefore, ...config } = policy.value!; policy.value = await api<Policy>('/admin/retention/policy', 'PUT', { ...config, confirmation }); baseline.value = JSON.stringify(policy.value); ElMessage.success('保留期策略已保存。') })
}
function ids(value: string) { const result = value.split(/[\s,，]+/).filter(Boolean); if (result.length > 100) throw new Error('文件与任务各最多指定100个ID。'); return result }
async function dryRun() { await perform('plan', async () => { plan.value = await api<Plan>('/admin/retention/plans', 'POST', { taskIds: ids(taskIds.value), fileIds: ids(fileIds.value), includeOrphans: includeOrphans.value }); lookupId.value = plan.value.id; await plans.fetch(true) }) }
async function continueScan() {
  if (!plan.value?.scan?.nextContinuation) return
  const { fileIds, taskIds, includeOrphans, afterObjectKey } = plan.value.scope
  const continuation = plan.value.scan.nextContinuation
  await perform('continue', async () => {
    plan.value = await api<Plan>('/admin/retention/plans', 'POST', { fileIds, taskIds, includeOrphans, afterObjectKey, continuation })
    lookupId.value = plan.value.id; await plans.fetch(true)
  })
}
async function readPlan() { if (lookupId.value.trim()) await perform('read', async () => { plan.value = await api<Plan>(`/admin/retention/plans/${encodeURIComponent(lookupId.value.trim())}`) }) }
async function execute(retry: boolean) {
  if (!plan.value || !policy.value?.enabled) return
  try { await ElMessageBox.confirm(`计划 ${plan.value.id} 将按服务端实时引用与保护检查删除到期对象。删除后无法恢复，请核对计划。`, retry ? '重试失败清理项' : '执行清理计划', { type: 'warning', confirmButtonText: retry ? '重试失败项' : '执行计划', cancelButtonText: '返回检查' }) } catch { return }
  await perform('execute', async () => { plan.value = await api<Plan>(`/admin/retention/plans/${plan.value!.id}/${retry ? 'retry' : 'execute'}`, 'POST'); await Promise.all([runs.fetch(true), plans.fetch(true)]) })
}
function selectPlan(id: string) { lookupId.value = id; void readPlan() }
onMounted(() => { void refreshPolicy(); void plans.fetch(); void runs.fetch() })
</script>
<template><div class="retention-console"><el-alert title="先生成预览计划，再核对候选对象。默认关闭正式清理；既有数据受保护，执行时服务端会重新检查引用和宽限期。" type="warning" :closable="false" class="panel-alert" /><el-alert v-if="error" :title="error" type="error" :closable="false" class="panel-alert" />
  <form v-if="policy" class="settings-form" @submit.prevent="savePolicy"><label class="field"><span>允许正式执行清理</span><el-switch v-model="policy.enabled" aria-label="启用保留期清理" /></label><label v-for="field in fields" :key="field.key" class="field"><span>{{ field.label }}</span><el-input-number v-model="(policy[field.key] as number)" :min="0" :max="36500" :precision="0" :controls="false" :aria-label="field.label" /></label><div class="settings-save"><p class="form-note">既有数据：{{ policy.includeExisting ? '已纳入（由服务端策略控制）' : '受保护' }} · 保护截止 {{ date(policy.protectedBefore || '') }}</p><el-button native-type="submit" type="primary" :loading="busy === 'save'" :disabled="!!busy">保存保留期策略</el-button></div></form>
  <h3 class="section-heading">预览清理计划</h3><div class="rule-fields"><label class="field"><span>任务ID（空 = 按策略扫描）</span><el-input v-model="taskIds" type="textarea" :rows="2" aria-label="清理任务ID" placeholder="多个ID以逗号或空格分隔，最多100个" /></label><label class="field"><span>文件ID（空 = 按策略扫描）</span><el-input v-model="fileIds" type="textarea" :rows="2" aria-label="清理文件ID" /></label></div><div class="editor-tools"><el-checkbox v-model="includeOrphans">扫描孤立对象</el-checkbox><el-button type="primary" :loading="busy === 'plan'" :disabled="!!busy" @click="dryRun">生成预览计划</el-button></div><div class="admin-toolbar"><el-input v-model="lookupId" placeholder="输入已有计划ID" aria-label="清理计划ID" /><el-button :loading="busy === 'read'" :disabled="!!busy || !lookupId" @click="readPlan">读取计划状态</el-button></div>
  <template v-if="plan"><div class="selected-attempt-context"><span class="mono">{{ plan.id }}</span><el-tag>{{ plan.state }}</el-tag><span>{{ date(plan.createdAt) }}</span></div><el-alert v-if="plan.truncated" title="本次扫描尚未覆盖全部范围。可继续扫描生成后续预览计划，或缩小文件/任务范围重新预览。" type="warning" :closable="false" class="panel-alert" /><div v-if="plan.scan" class="scan-summary"><p class="form-note">本次对象扫描 {{ plan.scan.scannedObjects }} 个 · 匹配 {{ plan.scan.matchedObjects }} 个 · 目录检查 {{ plan.scan.catalogInspected }} 个 · 日志任务检查 {{ plan.scan.logTasksInspected }} 个</p><el-alert v-for="warning in plan.scan.warnings || []" :key="warning" :title="warning" type="warning" :closable="false" class="panel-alert" /><div v-if="plan.scan.nextContinuation" class="editor-tools"><el-button :disabled="!!busy" :loading="busy === 'continue'" @click="continueScan">继续扫描并生成新预览计划</el-button><span class="form-note">保持原筛选范围，读取后续对象；游标24小时内有效。每次仍只生成预览计划。</span></div></div><el-table :data="plan.items" stripe max-height="480"><el-table-column prop="kind" label="产物类型" width="100" /><el-table-column prop="objectKey" label="对象引用" min-width="220" show-overflow-tooltip /><el-table-column prop="state" label="状态" min-width="130" /><el-table-column prop="reason" label="原因" min-width="200" show-overflow-tooltip /><el-table-column prop="attempts" label="执行次数" width="100" /></el-table><div class="editor-tools"><el-button type="danger" plain :disabled="!!busy || !policy?.enabled || plan.truncated || plan.state !== 'DRY_RUN'" :loading="busy === 'execute'" @click="execute(false)">正式执行计划</el-button><el-button :disabled="!!busy || !policy?.enabled || plan.state !== 'PARTIAL_FAILURE'" @click="execute(true)">重试失败项</el-button></div></template>
  <h3 class="section-heading">历史清理计划</h3><div class="admin-toolbar"><el-select v-model="planStatus" aria-label="清理计划状态"><el-option value="ALL" label="全部状态" /><el-option v-for="state in ['DRY_RUN', 'COMPLETED', 'PARTIAL_FAILURE']" :key="state" :value="state" :label="state" /></el-select></div><el-table :data="plans.items.value" stripe @row-click="(row: PlanSummary) => selectPlan(row.id)"><el-table-column prop="id" label="计划ID" min-width="200" show-overflow-tooltip /><el-table-column prop="state" label="状态" width="180" /><el-table-column label="创建时间" min-width="170"><template #default="{ row }">{{ date(row.createdAt) }}</template></el-table-column><el-table-column label="操作" width="110"><template #default="{ row }"><el-button link type="primary" @click.stop="selectPlan(row.id)">查看状态</el-button></template></el-table-column></el-table><CursorPager :page="plans.page.value" :can-previous="plans.canPrevious.value" :has-more="plans.hasMore.value" :loading="plans.loading.value" :error="plans.error.value" :count="plans.items.value.length" @previous="plans.previous" @next="plans.next" @refresh="plans.fetch()" />
  <h3 class="section-heading">清理执行记录</h3><div class="admin-toolbar"><el-input v-model="runPlanId" clearable placeholder="按计划ID筛选执行记录" aria-label="执行记录计划ID" /><el-select v-model="runStatus" aria-label="清理执行动作"><el-option value="ALL" label="全部动作" /><el-option v-for="state in ['DELETED', 'FAILED', 'PROTECTED']" :key="state" :value="state" :label="state" /></el-select></div><el-table :data="runs.items.value" stripe><el-table-column prop="planId" label="计划ID" min-width="180" show-overflow-tooltip /><el-table-column prop="action" label="动作" width="110" /><el-table-column prop="message" label="说明" min-width="230" /><el-table-column label="时间" min-width="170"><template #default="{ row }">{{ date(row.createdAt) }}</template></el-table-column></el-table><CursorPager :page="runs.page.value" :can-previous="runs.canPrevious.value" :has-more="runs.hasMore.value" :loading="runs.loading.value" :error="runs.error.value" :count="runs.items.value.length" @previous="runs.previous" @next="runs.next" @refresh="runs.fetch()" />
</div></template>
