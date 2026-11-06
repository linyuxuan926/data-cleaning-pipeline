<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, downloadArtifact, HttpError } from '../api'
import { clearSubmitKey, getOrCreateSubmitKey } from '../idempotency'
import { useCursorPage, pageQuery } from '../pagination'
import CursorPager from './CursorPager.vue'
import type { CursorPage, LogItem, Report, Task, TaskAttempt } from '../types'
import { date, isActive, number, statusLabel, statusLabels, statusType, statusTypes } from '../format'
import { dataTypeLabels, executionPlan, phaseNames, ruleLabels } from '../rules'
import { executionStep, reportCounts } from '../reports'
import { diskRuleLabels } from '../diskRules'
import LimitsDisplay from './LimitsDisplay.vue'
import ArtifactPreview from './ArtifactPreview.vue'

const props = defineProps<{ taskId: string | null; currentUserId: string }>()
const emit = defineEmits<{ close: []; changed: [] }>()
const task = ref<Task | null>(null)
const logs = ref<LogItem[]>([])
const attempts = ref<TaskAttempt[]>([])
const attemptsError = ref('')
const attemptsLoading = ref(false)
const attemptList = useCursorPage<TaskAttempt>(() => `/tasks/${encodeURIComponent(props.taskId || '')}/attempts/page`)
const report = ref<Report | null>(null)
const counts = computed(() => report.value ? reportCounts(report.value) : null)
const finalRemovals = computed(() => Object.entries(report.value?.finalRemovalByRule || {}).map(([ruleId, removedRows]) => {
  const rule = report.value?.rules.find(item => item.ruleId === ruleId)
  return { ruleId, removedRows, index: rule?.index, type: rule ? diskRuleLabels[rule.type as keyof typeof diskRuleLabels] || rule.type : '—' }
}))
const selectedAttemptId = ref<string | null>(null)
const followsCurrentAttempt = ref(true)
const attemptDetail = ref<TaskAttempt | null>(null)
const attemptDetailError = ref('')
const busy = ref(false)
const error = ref('')
const downloading = ref('')
const acting = ref(false)
const tab = ref('logs')
const refreshing = ref(false)
const logsHaveMore = ref(false)
const fetchedLogCount = ref(0)
const LOG_PAGE_SIZE = 100
const LOG_PAGES_PER_REFRESH = 5
const MAX_VISIBLE_LOGS = 2000
const owned = computed(() => Boolean(task.value && (!task.value.ownerId || task.value.ownerId === props.currentUserId)))
const snapshotRules = computed(() => task.value?.rulesSnapshot || report.value?.ruleSnapshot || [])
const plan = computed(() => executionPlan(snapshotRules.value, task.value?.engineVersion))
const currentAttempt = computed(() => attempts.value.find(item => (item.attemptId || item.id) === (task.value?.currentAttemptId || task.value?.attemptId))
  || attempts.value.find(item => (item.attemptNo ?? item.attempt) === task.value?.attempt))
const currentAttemptId = computed(() => task.value?.currentAttemptId || task.value?.attemptId || currentAttempt.value?.attemptId || currentAttempt.value?.id || null)
const selectedAttempt = computed(() => selectedAttemptId.value
  ? attempts.value.find(item => (item.attemptId || item.id) === selectedAttemptId.value)
    || ((attemptDetail.value?.attemptId || attemptDetail.value?.id) === selectedAttemptId.value ? attemptDetail.value : null)
  : currentAttempt.value)
const selectedAttemptNumber = computed(() => selectedAttempt.value?.attemptNo ?? selectedAttempt.value?.attempt ?? task.value?.attempt ?? 1)
const selectedIsCurrent = computed(() => !selectedAttemptId.value || selectedAttemptId.value === currentAttemptId.value
  || (!currentAttemptId.value && followsCurrentAttempt.value))
const selectedStatus = computed(() => selectedIsCurrent.value ? task.value?.status || selectedAttempt.value?.status : selectedAttempt.value?.status)
const artifactsExpired = computed(() => Object.values((selectedIsCurrent.value ? task.value?.artifactStatus : attemptDetail.value?.artifactStatus) || {}).some(state => state === 'EXPIRED' || state === 'DELETING'))
const selectedRouteAttemptId = computed(() => selectedAttemptId.value)
const selectedAttemptPath = computed(() => {
  const base = `/tasks/${encodeURIComponent(task.value?.id || '')}`
  return selectedRouteAttemptId.value ? `${base}/attempts/${encodeURIComponent(selectedRouteAttemptId.value)}` : base
})
let generation = 0
let logCursor = 0
let refreshingSequence: number | null = null
let actionToken = 0
let timer: ReturnType<typeof setTimeout> | undefined
let currentReportKey = ''
let olderLogCursor: string | null = null
function metricNumber(value: number | undefined) { return value === undefined ? '—' : number(value) }

async function loadLogs(id: string, sequence: number, attemptId: string | null) {
    const base = attemptId ? `/tasks/${id}/attempts/${encodeURIComponent(attemptId)}` : `/tasks/${id}`
    const result = await api<CursorPage<LogItem>>(`${base}/logs/page?${pageQuery({ sort: 'createdAt.desc' }, null, LOG_PAGE_SIZE)}`)
    const batch = result.items
    if (sequence !== generation) return
    const incoming = [...new Map(batch.map(log => [log.id, log])).values()]
      .sort((a, b) => a.id - b.id)
    const merged = new Map(logs.value.map(log => [log.id, log]))
    incoming.forEach(log => merged.set(log.id, log))
    logs.value = [...merged.values()].sort((a, b) => a.id - b.id).slice(-MAX_VISIBLE_LOGS)
    fetchedLogCount.value = merged.size
    if (!olderLogCursor) { olderLogCursor = result.nextCursor; logsHaveMore.value = result.hasMore }
}
async function loadOlderLogs() {
  if (!task.value || !olderLogCursor || refreshing.value) return
  const sequence = generation
  refreshing.value = true
  try {
    const value = await api<CursorPage<LogItem>>(`${selectedAttemptPath.value}/logs/page?${pageQuery({ sort: 'createdAt.desc' }, olderLogCursor, LOG_PAGE_SIZE)}`)
    if (sequence !== generation) return
    const merged = new Map(logs.value.map(item => [item.id, item])); value.items.forEach(item => merged.set(item.id, item))
    logs.value = [...merged.values()].sort((a, b) => a.id - b.id).slice(0, MAX_VISIBLE_LOGS)
    fetchedLogCount.value = merged.size; olderLogCursor = value.nextCursor; logsHaveMore.value = value.hasMore && logs.value.length < MAX_VISIBLE_LOGS
  } catch (e) { if (sequence === generation) error.value = e instanceof Error ? e.message : '读取历史日志失败。' }
  finally { if (sequence === generation) refreshing.value = false }
}

async function refresh(id: string, sequence: number) {
  if (sequence !== generation || refreshingSequence === sequence) return
  if (timer) clearTimeout(timer)
  refreshingSequence = sequence
  refreshing.value = true
  attemptsLoading.value = true
  try {
    const [taskResult, attemptsResult] = await Promise.allSettled([
      api<Task>(`/tasks/${id}`), attemptList.fetch(),
    ])
    if (sequence !== generation) return
    if (taskResult.status === 'rejected') throw taskResult.reason
    const nextTask = taskResult.value
    task.value = nextTask
    const previousAttemptId = selectedAttemptId.value
    if (attemptsResult.status === 'fulfilled') {
      attempts.value = attemptList.items.value
      attemptsError.value = attemptList.error.value
    } else attemptsError.value = attemptsResult.reason instanceof Error ? attemptsResult.reason.message : '读取执行尝试失败。'
    if (followsCurrentAttempt.value) selectedAttemptId.value = nextTask.currentAttemptId || nextTask.attemptId || currentAttempt.value?.attemptId || currentAttempt.value?.id || null
    if (previousAttemptId !== selectedAttemptId.value) resetAttemptView()
    attemptsLoading.value = false
    const attemptId = selectedRouteAttemptId.value
    const shouldReadLogs = Boolean(attemptId || followsCurrentAttempt.value)
    const selectedKey = attemptId || `${id}:${nextTask.attempt}`
    const selectedStatusNow = selectedStatus.value
    const [logsResult, detailResult] = await Promise.allSettled([
      shouldReadLogs ? loadLogs(id, sequence, attemptId) : Promise.resolve(),
      attemptId && (!attemptDetail.value || (attemptDetail.value.attemptId || attemptDetail.value.id) !== attemptId || (selectedStatusNow === 'SUCCEEDED' && !attemptDetail.value.report))
        ? api<TaskAttempt>(`/tasks/${id}/attempts/${encodeURIComponent(attemptId)}`)
        : Promise.resolve(null),
    ])
    if (sequence !== generation) return
    if (logsResult.status === 'rejected') {
      if (attemptId && selectedIsCurrent.value) {
        logCursor = 0; fetchedLogCount.value = 0; logs.value = []; logsHaveMore.value = false
        await loadLogs(id, sequence, null)
      } else if (attemptId) throw logsResult.reason
      else throw logsResult.reason
    }
    if (detailResult.status === 'fulfilled' && detailResult.value) {
      attemptDetail.value = detailResult.value
      attemptDetailError.value = ''
      if (detailResult.value.report) {
        report.value = detailResult.value.report
        currentReportKey = attemptId!
      }
    } else if (detailResult.status === 'rejected') attemptDetailError.value = detailResult.reason instanceof Error ? detailResult.reason.message : '读取尝试详情失败。'
    error.value = ''
    if (selectedStatus.value === 'SUCCEEDED' && currentReportKey !== selectedKey && !report.value) {
      try {
        const nextReport = await api<Report>(attemptId ? `${selectedAttemptPath.value}/report` : `/tasks/${id}/report`)
        if (sequence !== generation) return
        report.value = nextReport
        currentReportKey = selectedKey
        attemptDetailError.value = ''
      } catch (e) {
        if (attemptId && selectedIsCurrent.value) {
          try {
            const nextReport = await api<Report>(`/tasks/${id}/report`)
            if (sequence !== generation) return
            report.value = nextReport
            currentReportKey = selectedKey
            attemptDetailError.value = ''
          } catch (fallbackError) {
            attemptDetailError.value = fallbackError instanceof Error ? fallbackError.message : '读取清洗报告失败。'
          }
        } else attemptDetailError.value = e instanceof Error ? e.message : '读取清洗报告失败。'
      }
      if (report.value && selectedIsCurrent.value && (tab.value === 'logs' || tab.value === 'attempts')) {
        tab.value = 'report'
      }
    } else if (selectedStatus.value !== 'SUCCEEDED' && report.value && currentReportKey !== selectedKey) { report.value = null; currentReportKey = '' }
  } catch (e) { if (sequence === generation) error.value = e instanceof Error ? e.message : '加载任务失败。' }
  finally {
    if (sequence === generation) {
      busy.value = false
      refreshing.value = false
      attemptsLoading.value = false
      refreshingSequence = null
      if (task.value && isActive(task.value.status)) timer = setTimeout(() => void refresh(id, sequence), 2500)
    }
  }
}
function resetAttemptView() {
  attemptDetail.value = null; attemptDetailError.value = ''; report.value = null; currentReportKey = ''
  logs.value = []; logCursor = 0; fetchedLogCount.value = 0; logsHaveMore.value = false
  olderLogCursor = null
}
watch(() => props.taskId, id => {
  generation++
  actionToken++
  attemptList.clear(); olderLogCursor = null
  if (timer) clearTimeout(timer)
  task.value = null; logs.value = []; attempts.value = []; attemptsError.value = ''; report.value = null; error.value = ''; currentReportKey = ''; tab.value = 'logs'
  selectedAttemptId.value = null; followsCurrentAttempt.value = true; attemptDetail.value = null; attemptDetailError.value = ''
  logCursor = 0; fetchedLogCount.value = 0; logsHaveMore.value = false; refreshing.value = false; acting.value = false
  if (id) { busy.value = true; void refresh(id, generation) }
})
onBeforeUnmount(() => { generation++; actionToken++; if (timer) clearTimeout(timer) })

async function act(action: 'cancel' | 'retry') {
  if (!task.value || !owned.value) return
  const id = task.value.id
  const sequence = generation
  if (action === 'cancel') {
    try { await ElMessageBox.confirm('取消后将停止排队或通知执行进程停止。确定取消此任务？', '取消任务', { confirmButtonText: '确定取消', cancelButtonText: '继续执行', type: 'warning' }) }
    catch { return }
  }
  if (sequence !== generation || props.taskId !== id) return
  const currentAction = ++actionToken
  const namespace = `${props.currentUserId}.retry:${id}`
  const retryKey = action === 'retry' ? getOrCreateSubmitKey(sessionStorage, namespace, { taskId: id }) : null
  acting.value = true
  try {
    await api<Task>(`/tasks/${id}/${action}`, 'POST', undefined, retryKey ? { headers: { 'Idempotency-Key': retryKey } } : {})
    if (retryKey) clearSubmitKey(sessionStorage, namespace, retryKey)
    if (currentAction !== actionToken || props.taskId !== id) return
    if (timer) clearTimeout(timer)
    generation++
    followsCurrentAttempt.value = true
    selectedAttemptId.value = null; resetAttemptView(); attemptList.clear()
    await refresh(id, generation)
    if (currentAction !== actionToken || props.taskId !== id) return
    emit('changed')
    ElMessage.success(action === 'cancel' ? '已提交取消请求。' : '任务已重新排队。')
  } catch (e) { if (retryKey && e instanceof HttpError && e.status < 500 && ![408, 409, 429].includes(e.status)) clearSubmitKey(sessionStorage, namespace, retryKey); ElMessage.error(e instanceof Error ? e.message : '操作失败。') }
  finally { if (currentAction === actionToken) acting.value = false }
}
function selectAttempt(attempt: TaskAttempt) {
  const attemptId = attempt.attemptId || attempt.id
  if (!attemptId || !task.value) return
  selectedAttemptId.value = attemptId
  followsCurrentAttempt.value = attemptId === currentAttemptId.value || (attempt.attemptNo ?? attempt.attempt) === task.value.attempt
  resetAttemptView(); error.value = ''
  if (timer) clearTimeout(timer)
  generation++; void refresh(task.value.id, generation)
}
async function pageAttempts(direction: 'next' | 'previous' | 'refresh') {
  if (direction === 'next') await attemptList.next()
  else if (direction === 'previous') await attemptList.previous()
  else await attemptList.fetch()
  attempts.value = attemptList.items.value
  attemptsError.value = attemptList.error.value
}
async function download(kind: 'result' | 'dirty' | 'report') {
  if (!task.value || selectedStatus.value !== 'SUCCEEDED') return
  downloading.value = kind
  try {
    const ext = kind === 'report' ? 'json' : kind === 'dirty' ? 'csv' : task.value.outputFormat
    await downloadArtifact(task.value.id, kind, `${task.value.id}-attempt-${selectedAttemptNumber.value}-${kind}.${ext}`, selectedRouteAttemptId.value || undefined)
  } catch (e) { ElMessage.error(e instanceof Error ? e.message : '下载失败。') }
  finally { downloading.value = '' }
}
</script>

<template>
  <el-drawer :model-value="Boolean(taskId)" title="任务详情" size="min(94vw, 960px)" destroy-on-close @close="emit('close')">
    <el-skeleton v-if="busy && !task" :rows="8" animated />
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="panel-alert" />
    <el-button v-if="error && taskId" class="retry-button" :loading="busy" @click="busy = true; refresh(taskId, generation)">重新加载</el-button>
    <template v-if="task">
      <div class="task-detail-heading"><div><h2>{{ task.fileName }}</h2><p class="mono muted">{{ task.id }}</p></div><el-tag :type="statusTypes[task.status]" effect="light">{{ statusLabels[task.status] }}</el-tag></div>
      <el-progress :percentage="task.progress" :status="task.status === 'SUCCEEDED' ? 'success' : task.status === 'FAILED' ? 'exception' : undefined" :stroke-width="8" />
      <p v-if="task.stage" class="form-note">当前阶段：{{ task.stage }}{{ task.stageProcessedRows == null ? '' : ` · 本阶段处理 ${number(task.stageProcessedRows)} 行` }}{{ task.totalRows == null ? '' : ` / 输入 ${number(task.totalRows)} 行` }}。进度为服务端阶段估计。</p>
      <dl class="task-metadata"><div><dt>创建时间</dt><dd>{{ date(task.createdAt) }}</dd></div><div><dt>规则版本</dt><dd>v{{ task.ruleVersion }}</dd></div><div><dt>执行次数</dt><dd>{{ task.attempt }}</dd></div><div><dt>输出格式</dt><dd>{{ task.outputFormat.toUpperCase() }}</dd></div></dl>
      <div class="selected-attempt-context"><span>正在查看：第 {{ selectedAttemptNumber }} 次执行</span><el-tag v-if="selectedStatus" :type="statusType(selectedStatus)">{{ statusLabel(selectedStatus) }}</el-tag><el-tag v-if="selectedIsCurrent" type="success" effect="plain">当前执行</el-tag><el-tag v-else type="info" effect="plain">历史执行</el-tag></div>
      <el-alert v-if="!owned" title="管理员查看：该任务属于其他用户，可查看和下载，执行控制由所属用户操作。" type="info" :closable="false" class="panel-alert" />
      <el-alert v-if="task.error && selectedIsCurrent" :title="task.error" type="error" show-icon :closable="false" class="panel-alert" />
      <el-alert v-if="selectedAttempt?.error && !selectedIsCurrent" :title="selectedAttempt.error" type="error" show-icon :closable="false" class="panel-alert" />
      <el-alert v-if="attemptDetailError" :title="`所选执行的详情或报告暂不可用：${attemptDetailError}`" type="warning" show-icon :closable="false" class="panel-alert" />
      <el-alert v-if="artifactsExpired" title="所选执行部分产物已过期或正在清理，相关预览和下载可能返回已过期；历史状态和报告摘要继续保留。" type="warning" :closable="false" class="panel-alert" />
      <div class="detail-actions">
        <el-button v-if="owned && selectedIsCurrent && ['QUEUED', 'RUNNING'].includes(task.status)" type="danger" plain :loading="acting" @click="act('cancel')">取消任务</el-button>
        <el-button v-if="owned && selectedIsCurrent && ['FAILED', 'CANCELED', 'CANCELLED'].includes(task.status)" type="primary" :loading="acting" @click="act('retry')">重新执行</el-button>
        <el-tag v-if="selectedIsCurrent && task.status === 'CANCELING'" type="warning">取消请求已提交，等待执行进程确认</el-tag>
        <template v-if="selectedStatus === 'SUCCEEDED'">
          <el-button type="primary" :loading="downloading === 'result'" :disabled="!!downloading" @click="download('result')">↓ 下载清洗结果</el-button>
          <el-button :loading="downloading === 'dirty'" :disabled="!!downloading" @click="download('dirty')">脏数据明细</el-button>
          <el-button :loading="downloading === 'report'" :disabled="!!downloading" @click="download('report')">JSON 报告</el-button>
        </template>
      </div>
      <el-tabs v-model="tab">
        <el-tab-pane label="结果预览" name="result-preview" :disabled="selectedStatus !== 'SUCCEEDED'"><ArtifactPreview :path="`${selectedAttemptPath}/preview`" kind="result" :enabled="tab === 'result-preview' && selectedStatus === 'SUCCEEDED'" /></el-tab-pane>
        <el-tab-pane label="违规明细" name="dirty-preview" :disabled="selectedStatus !== 'SUCCEEDED'"><ArtifactPreview :path="`${selectedAttemptPath}/preview`" kind="dirty" :enabled="tab === 'dirty-preview' && selectedStatus === 'SUCCEEDED'" /></el-tab-pane>
        <el-tab-pane label="执行尝试" name="attempts">
          <el-alert v-if="attemptsError" :title="`执行历史暂不可用：${attemptsError}`" type="warning" show-icon :closable="false" class="panel-alert" />
          <el-button v-if="attemptsError" size="small" :loading="attemptsLoading" @click="refresh(task.id, generation)">重新读取尝试历史</el-button>
          <el-table v-if="attempts.length" :data="attempts" stripe :row-class-name="({ row }: { row: TaskAttempt }) => [((row.attemptId || row.id) === currentAttemptId ? 'current-attempt-row' : ''), ((row.attemptId || row.id) === selectedAttemptId ? 'selected-attempt-row' : '')].filter(Boolean).join(' ')">
            <el-table-column label="尝试" width="85"><template #default="{ row, $index }">第 {{ row.attemptNo ?? row.attempt ?? (attempts.length - $index) }} 次</template></el-table-column>
            <el-table-column label="状态" width="115"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
            <el-table-column label="触发原因" min-width="145"><template #default="{ row }">{{ row.triggerReason || row.trigger || '初次提交' }}</template></el-table-column>
            <el-table-column label="开始时间" min-width="170"><template #default="{ row }">{{ date(row.startedAt || row.createdAt || '') }}</template></el-table-column>
            <el-table-column label="耗时" min-width="95"><template #default="{ row }">{{ row.durationMs == null ? '—' : `${(row.durationMs / 1000).toFixed(1)} 秒` }}</template></el-table-column>
            <el-table-column label="执行标识" min-width="155"><template #default="{ row }"><span class="mono tiny">{{ row.attemptId || row.id || '—' }}</span></template></el-table-column>
            <el-table-column label="错误摘要" min-width="190" show-overflow-tooltip><template #default="{ row }">{{ row.error || '—' }}</template></el-table-column>
            <el-table-column label="详情" width="90" fixed="right"><template #default="{ row }"><el-button text type="primary" :disabled="(row.attemptId || row.id) === selectedAttemptId" @click="selectAttempt(row)">{{ (row.attemptId || row.id) === selectedAttemptId ? '查看中' : '查看' }}</el-button></template></el-table-column>
          </el-table>
          <CursorPager :page="attemptList.page.value" :can-previous="attemptList.canPrevious.value" :has-more="attemptList.hasMore.value" :loading="attemptList.loading.value" :error="attemptList.error.value" :count="attempts.length" @previous="pageAttempts('previous')" @next="pageAttempts('next')" @refresh="pageAttempts('refresh')" />
          <el-empty v-if="!attempts.length && !attemptsError && !attemptsLoading" description="暂无执行尝试历史。" />
          <p v-if="attempts.length" class="form-note">绿色行标识当前执行，蓝色边线标识正在查看的执行。选择不同记录后，报告、日志、预览和下载都会切换到该次执行；取消与重试始终作用于当前任务。</p>
        </el-tab-pane>
        <el-tab-pane label="执行快照" name="snapshot">
          <p class="form-note">任务创建时固定以下输入解析、字段、规则与限额；重新解析、修改规则或调整平台限额不会影响此任务的后续重试。</p>
          <el-tag>执行引擎：{{ task.engineVersion || 'v2-phased' }} · schema {{ task.schemaVersion || 1 }}</el-tag><template v-if="task.logicalFieldsSnapshot?.length"><h3 class="section-heading">逻辑字段与冻结绑定</h3><el-table :data="task.logicalFieldsSnapshot" stripe><el-table-column prop="id" label="逻辑字段ID" min-width="150" /><el-table-column prop="label" label="字段名称" min-width="130" /><el-table-column label="输入列ID"><template #default="{ row }">{{ task.fieldBindingsSnapshot?.[row.id] || '—' }}</template></el-table-column></el-table></template>
          <h3 class="section-heading">输入解析与字段</h3>
          <template v-if="task.inputSnapshot"><dl class="snapshot-info"><div><dt>输入文件</dt><dd>{{ task.inputSnapshot.name || task.fileName }}</dd></div><div><dt>字符编码 / 分隔符</dt><dd>{{ task.inputSnapshot.parseOptions?.encoding || '格式默认' }} / {{ task.inputSnapshot.parseOptions?.delimiter === '\t' || task.inputSnapshot.parseOptions?.delimiter === '\\t' ? 'Tab' : task.inputSnapshot.parseOptions?.delimiter || '格式默认' }}</dd></div><div><dt>工作表</dt><dd>{{ task.inputSnapshot.parseOptions?.sheetName || '不适用 / 第一张工作表' }}</dd></div><div><dt>缺失标记</dt><dd class="mono">{{ JSON.stringify(task.inputSnapshot.parseOptions?.missingValues || []) }}</dd></div></dl><el-table :data="task.inputSnapshot.fields || []" stripe><el-table-column prop="id" label="稳定 ID" width="100" /><el-table-column prop="name" label="原始列名" min-width="140" /><el-table-column prop="dtype" label="读取类型" min-width="100" /><el-table-column label="类型覆盖" min-width="120"><template #default="{ row }">{{ task.inputSnapshot?.parseOptions?.fieldTypes?.[row.id] ? dataTypeLabels[task.inputSnapshot.parseOptions.fieldTypes[row.id]!] : '保持读取类型' }}</template></el-table-column></el-table></template>
          <el-empty v-else description="历史任务没有显式输入快照；服务端使用其不可变版本信息。" />
          <h3 class="section-heading">实际执行顺序{{ task.engineVersion === 'v2-phased' ? '（V2 历史阶段顺序）' : '（严格按配置顺序）' }}</h3><p v-if="task.engineVersion === 'v2-phased'" class="form-note">此历史任务保留 V2 阶段顺序，同一阶段内按原配置顺序执行。</p><ol class="snapshot-plan"><li v-for="item in plan" :key="item.index"><strong>#{{ item.index + 1 }} {{ diskRuleLabels[item.rule.type as keyof typeof diskRuleLabels] || item.rule.type }}</strong><span>{{ 'enabled' in item.rule && !item.rule.enabled ? 'DISABLED · 已停用' : task.engineVersion === 'v2-phased' ? phaseNames[item.phase] : '配置顺序' }}</span><pre>{{ JSON.stringify(item.rule, null, 2) }}</pre></li></ol><p v-if="!plan.length" class="form-note">无清洗规则，仅转换格式并生成质量报告。</p>
          <h3 class="section-heading">创建时的处理限额</h3><LimitsDisplay :settings="task.limitsSnapshot || null" />
        </el-tab-pane>
        <el-tab-pane label="执行日志" name="logs">
          <div class="log-panel" role="log" aria-label="任务执行日志" aria-live="polite">
            <div v-if="!logs.length" class="log-empty">暂无执行日志。任务出队后将在此显示执行过程。</div>
            <div v-for="log in logs" :key="log.id" class="log-line"><time>{{ date(log.createdAt) }}</time><span class="log-level" :class="log.level.toLowerCase()">{{ log.level }}</span><span>{{ log.message }}</span></div>
          </div>
          <div class="logs-footer"><p class="form-note">执行期间每 2.5 秒更新；当前显示 {{ number(logs.length) }} 条（最多 {{ number(MAX_VISIBLE_LOGS) }} 条）。历史日志按服务端游标读取。</p><el-button v-if="logsHaveMore" size="small" :loading="refreshing" @click="loadOlderLogs">加载更早日志</el-button><el-button size="small" :loading="refreshing" @click="refresh(task.id, generation)">刷新日志</el-button></div>
        </el-tab-pane>
        <el-tab-pane label="清洗报告" name="report" :disabled="!report">
          <template v-if="report">
            <div class="report-metrics">
              <div><span>输入行数</span><strong>{{ number(report.inputRows) }}</strong></div>
              <div><span>输出行数</span><strong class="positive">{{ number(report.outputRows) }}</strong></div>
              <div><span>有效输出行数</span><strong class="positive">{{ metricNumber(counts?.validOutputRows) }}</strong></div>
              <div><span>标记输出行数</span><strong class="warning-text">{{ metricNumber(counts?.flaggedOutputRows) }}</strong></div>
              <div><span>最终移除行数</span><strong class="warning-text">{{ metricNumber(counts?.finalRemovedRows) }}</strong></div>
              <div><span>含违规的唯一行数</span><strong class="warning-text">{{ metricNumber(counts?.issueRows) }}</strong></div>
              <div><span>违规明细条数</span><strong>{{ number(report.issueCount ?? report.dirtyRows) }}</strong></div>
              <div><span>重复移除行数</span><strong>{{ metricNumber(counts?.duplicateRemovedRows) }}</strong></div>
              <div><span>异常处理行数</span><strong>{{ metricNumber(counts?.outlierHandledRows) }}</strong></div>
              <div><span>缺失填充单元格</span><strong>{{ metricNumber(counts?.missingFilledCells) }}</strong></div>
              <div><span>类型转换失败单元格</span><strong>{{ metricNumber(counts?.typeConversionFailedCells) }}</strong></div>
              <div><span>变更单元格</span><strong>{{ number(report.changedCells) }}</strong></div>
            </div>
            <p class="muted">执行耗时 {{ report.durationSeconds.toFixed(2) }} 秒 · 输出保留率 {{ report.inputRows ? ((report.outputRows / report.inputRows) * 100).toFixed(1) : '0.0' }}%</p>
            <el-alert :title="`行数核对：${number(report.inputRows)} 输入 = ${number(report.outputRows)} 输出 + ${metricNumber(counts?.finalRemovedRows)} 最终移除。${counts?.validOutputRows !== undefined && counts?.flaggedOutputRows !== undefined ? `${number(report.outputRows)} 输出 = ${number(counts.validOutputRows)} 有效输出 + ${number(counts.flaggedOutputRows)} 标记输出。` : ''}违规明细每次违规占一条，同一行可有多条。`" type="info" :closable="false" class="panel-alert" />
            <p class="form-note">异常处理行数按原始记录去重；填充与转换失败的单元格按每条规则的处理事件累计。含违规的行可能随后被移除，标记输出只统计最终保留的行。</p>
            <p v-if="report.finalRemovedRows === undefined" class="form-note">历史 V2 报告继续使用原有行数口径；未记录的处理计数以“—”显示。</p>
            <el-alert v-for="(warning, index) in report.warnings" :key="index" :title="warning" type="warning" :closable="false" class="panel-alert" />
            <h3 class="section-heading">规则执行明细</h3>
            <el-table :data="report.rules" stripe empty-text="未配置清洗规则；本次执行仅生成报告或转换格式。">
              <el-table-column type="expand"><template #default="{ row }"><pre class="rule-statistics">{{ JSON.stringify(row.statistics || {}, null, 2) }}</pre></template></el-table-column>
              <el-table-column prop="index" label="规则 #" width="75" />
              <el-table-column label="执行步" width="85"><template #default="{ row }">{{ executionStep(row, report.executionOrder) }}</template></el-table-column>
              <el-table-column label="规则" min-width="150"><template #default="{ row }">{{ diskRuleLabels[row.type as keyof typeof diskRuleLabels] || row.type }}</template></el-table-column>
              <el-table-column prop="inputRows" label="输入" min-width="85" /><el-table-column prop="outputRows" label="输出" min-width="85" />
              <el-table-column prop="rejectedRows" label="移除行" min-width="85" /><el-table-column label="标记行" min-width="85"><template #default="{ row }">{{ row.flaggedRows || 0 }}</template></el-table-column><el-table-column label="违规条数" min-width="90"><template #default="{ row }">{{ row.issueCount ?? row.rejectedRows }}</template></el-table-column><el-table-column prop="changedCells" label="变更单元格" min-width="110" />
            </el-table>
            <h3 class="section-heading">最终移除归因</h3>
            <el-table v-if="report.finalRemovalByRule !== undefined" :data="finalRemovals" stripe empty-text="本次执行没有最终移除的记录。">
              <el-table-column prop="ruleId" label="规则 ID" min-width="150" />
              <el-table-column label="规则 #" width="85"><template #default="{ row }">{{ row.index ?? '—' }}</template></el-table-column>
              <el-table-column prop="type" label="规则" min-width="150" />
              <el-table-column prop="removedRows" label="最终移除行数" min-width="120" />
            </el-table>
            <p v-else class="form-note">此历史报告没有记录最终移除归因。</p>
            <h3 class="section-heading">清洗后的列质量</h3>
            <el-table :data="report.columnsAfter" stripe><el-table-column prop="name" label="列名" min-width="160" /><el-table-column prop="dtype" label="类型" min-width="110" /><el-table-column prop="nullCount" label="空值数" width="100" /></el-table>
          </template>
        </el-tab-pane>
      </el-tabs>
    </template>
  </el-drawer>
</template>
