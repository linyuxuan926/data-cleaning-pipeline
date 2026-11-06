<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import AuthScreen from './components/AuthScreen.vue'
import RuleEditor from './components/RuleEditor.vue'
import DiskRuleEditor from './components/DiskRuleEditor.vue'
import CursorPager from './components/CursorPager.vue'
import { useCursorPage } from './pagination'
import TaskDetail from './components/TaskDetail.vue'
import FilePanel from './components/FilePanel.vue'
import AdminConsole from './components/AdminConsole.vue'
import LimitsDisplay from './components/LimitsDisplay.vue'
import { api, HttpError, saveToken, token } from './api'
import { clearSubmitKey, getOrCreateSubmitKey } from './idempotency'
import { date, isActive, number, statusLabels, statusTypes } from './format'
import { migrateLegacyRules, validateDiskRules } from './diskRules'
import type { DiskRule, FileItem, LogicalField, Preview, Rule, RuleSet, Settings, Task, User, Preflight } from './types'

const user = ref<User | null>(null)
const starting = ref(Boolean(token()))
const page = ref<'workspace' | 'tasks' | 'rules' | 'admin'>('workspace')
const files = ref<FileItem[]>([])
const ruleSets = ref<RuleSet[]>([])
const tasks = ref<Task[]>([])
const loading = ref(false)
const loadError = ref('')
const selectedTask = ref<string | null>(null)
const taskFilter = ref('ALL')
const selectedFile = ref('')
const preview = ref<Preview | null>(null)
const settings = ref<Settings | null>(null)
const fileBusy = ref(false)
const fileOptionsDirty = ref(false)
const ruleSetId = ref('')
const loadedVersion = ref<number | null>(null)
const versions = ref<RuleSet[]>([])
const versionsLoading = ref(false)
const ruleName = ref('我的清洗规则')
const rules = ref<Rule[]>([])
const diskRules = ref<DiskRule[]>([])
const logicalFields = ref<LogicalField[]>([])
const fieldBindings = ref<Record<string, string>>({})
const editorMode = ref<'v3' | 'legacy'>('v3')
const legacyEngine = ref('v2-phased')
const preflightError = ref('')
const preflightWarnings = ref<string[]>([])
const ruleSchema = ref<{ schemaVersion: number; engineVersion: string; ruleCount: number; rules: Record<string, unknown> } | null>(null)
const schemaError = ref('')
const savedSnapshot = ref('')
const savingRules = ref(false)
const creatingTask = ref(false)
const outputFormat = ref('csv')
const fileSearch = ref('')
const ruleSearch = ref('')
const taskSearch = ref('')
const fileFormat = ref('ALL')
const taskFormat = ref('ALL')
const fileDates = ref<string[]>([])
const taskDates = ref<string[]>([])
const ruleDates = ref<string[]>([])
function bounds(dates: string[] | null) { return { createdFrom: dates?.[0] ? new Date(dates[0]).toISOString() : undefined, createdTo: dates?.[1] ? new Date(dates[1]).toISOString() : undefined } }
const fileList = useCursorPage<FileItem>(() => '/files/page', () => ({ q: fileSearch.value, format: fileFormat.value, ...bounds(fileDates.value) }))
const ruleList = useCursorPage<RuleSet>(() => '/rule-sets/page', () => ({ q: ruleSearch.value, ...bounds(ruleDates.value) }))
const taskList = useCursorPage<Task>(() => '/tasks/page', () => ({ q: taskSearch.value, status: taskFilter.value, format: taskFormat.value, ...bounds(taskDates.value) }))
const versionList = useCursorPage<RuleSet>(() => `/rule-sets/${ruleSetId.value}/versions/page`)
watch(fileList.items, values => { const pinned = files.value.find(file => file.id === selectedFile.value); files.value = pinned && !values.some(file => file.id === pinned.id) ? [pinned, ...values] : values })
watch(ruleList.items, values => { const pinned = ruleSets.value.find(rule => rule.id === ruleSetId.value); ruleSets.value = pinned && !values.some(rule => rule.id === pinned.id) ? [pinned, ...values] : values })
watch(taskList.items, values => { tasks.value = values })
watch(versionList.items, values => { versions.value = values })
watch([taskFilter, taskSearch, taskFormat, taskDates], () => { if (user.value) void taskList.fetch(true) })
watch([fileSearch, fileFormat, fileDates], () => { if (user.value) void fileList.fetch(true) })
watch([ruleSearch, ruleDates], () => { if (user.value) void ruleList.fetch(true) })
let pollTimer: ReturnType<typeof setTimeout> | undefined
let pollBusy = false
let versionsSequence = 0
let sessionSequence = 0

const selectedFileItem = computed(() => files.value.find(f => f.id === selectedFile.value))
const activeCount = computed(() => tasks.value.filter(t => isActive(t.status)).length)
const succeededCount = computed(() => tasks.value.filter(t => t.status === 'SUCCEEDED').length)
const filteredTasks = computed(() => tasks.value)
const isV3Editor = computed(() => editorMode.value === 'v3')
const dirtyRules = computed(() => JSON.stringify(editorConfig()) !== savedSnapshot.value)
const pageNames = { workspace: '清洗工作台', tasks: '任务中心', rules: '规则库', admin: '平台管理' }

function message(error: unknown) { return error instanceof Error ? error.message : '操作失败，请重试。' }
function editorConfig() { return isV3Editor.value ? { name: ruleName.value, rules: diskRules.value, logicalFields: logicalFields.value, schemaVersion: 3, engineVersion: 'v3-disk' } : { name: ruleName.value, rules: rules.value } }
function clearSession() {
  sessionSequence++
  versionsSequence++
  saveToken(null); user.value = null; files.value = []; tasks.value = []; ruleSets.value = []; selectedTask.value = null
  selectedFile.value = ''; preview.value = null; ruleSetId.value = ''; rules.value = []; diskRules.value = []; logicalFields.value = []; fieldBindings.value = {}; editorMode.value = 'v3'; loadedVersion.value = null; versions.value = []
  ruleName.value = '我的清洗规则'; savedSnapshot.value = ''; settings.value = null; loadError.value = ''; page.value = 'workspace'
  fileBusy.value = false; fileOptionsDirty.value = false; ruleSchema.value = null; schemaError.value = ''
  fileSearch.value = ''; ruleSearch.value = ''; taskSearch.value = ''; fileFormat.value = 'ALL'; taskFormat.value = 'ALL'; taskFilter.value = 'ALL'; fileDates.value = []; taskDates.value = []; ruleDates.value = []
  fileList.clear(); ruleList.clear(); taskList.clear(); versionList.clear()
  if (pollTimer) clearTimeout(pollTimer)
}
async function authenticated(value: User) {
  user.value = value
  await Promise.all([refresh(), loadSchema()])
  schedulePoll()
}
async function logout() {
  try { await api('/auth/logout', 'POST') } catch { /* local logout always available */ }
  clearSession()
}
async function refresh() {
  loading.value = true
  const sequence = sessionSequence
  try {
    const [, , , nextSettings] = await Promise.all([fileList.fetch(), ruleList.fetch(), taskList.fetch(), api<Settings>('/settings')])
    if (sequence !== sessionSequence) return
    settings.value = nextSettings; loadError.value = ''
  } catch (e) { if (sequence === sessionSequence) loadError.value = message(e) }
  finally { loading.value = false }
}
function schedulePoll() {
  if (pollTimer) clearTimeout(pollTimer)
  if (!user.value) return
  pollTimer = setTimeout(async () => {
    if (!document.hidden && !pollBusy && user.value) {
      pollBusy = true
      const sequence = sessionSequence
      try {
        const [, nextSettings, identity] = await Promise.all([taskList.fetch(), api<Settings>('/settings'), api<User>('/auth/me')])
        if (sequence === sessionSequence) { settings.value = nextSettings; user.value = identity; if (identity.role !== 'ADMIN' && page.value === 'admin') page.value = 'workspace'; loadError.value = '' }
      } catch (e) { if (sequence === sessionSequence) loadError.value = message(e) }
      finally { pollBusy = false }
    }
    schedulePoll()
  }, activeCount.value ? 2500 : 8000)
}
function fileChanged(file: FileItem) { files.value = [file, ...files.value.filter(item => item.id !== file.id)] }
async function identityChanged() {
  try { user.value = await api<User>('/auth/me'); if (user.value.role !== 'ADMIN') page.value = 'workspace' }
  catch (e) { ElMessage.warning(message(e)) }
}
function loadRule(value: RuleSet) {
  ruleName.value = value.name; ruleSetId.value = value.id; loadedVersion.value = value.version
  if (value.schemaVersion === 3 || value.engineVersion === 'v3-disk') {
    editorMode.value = 'v3'; diskRules.value = JSON.parse(JSON.stringify(value.rules)) as DiskRule[]
    logicalFields.value = JSON.parse(JSON.stringify(value.logicalFields || [])) as LogicalField[]
    fieldBindings.value = Object.fromEntries(logicalFields.value.filter(field => fieldBindings.value[field.id]).map(field => [field.id, fieldBindings.value[field.id]!]))
  } else {
    editorMode.value = 'legacy'; legacyEngine.value = value.engineVersion || 'v2-phased'; rules.value = JSON.parse(JSON.stringify(value.rules)) as Rule[]; diskRules.value = []; logicalFields.value = []; fieldBindings.value = {}
  }
  savedSnapshot.value = JSON.stringify(editorConfig())
}
async function loadSchema() {
  schemaError.value = ''
  const sequence = sessionSequence
  try {
    const value = await api<NonNullable<typeof ruleSchema.value>>('/rule-schemas/3')
    if (sequence !== sessionSequence) return
    if (value.schemaVersion !== 3 || value.engineVersion !== 'v3-disk' || !value.rules) throw new Error('服务端规则 Schema 与当前编辑器不兼容。')
    ruleSchema.value = value
  } catch (e) { if (sequence === sessionSequence) schemaError.value = message(e) }
}
function validateSchema() {
  if (!ruleSchema.value) throw new Error(schemaError.value || '规则 Schema 尚未加载，请重试读取。')
  const unsupported = diskRules.value.find(rule => !Object.hasOwn(ruleSchema.value!.rules, rule.type))
  if (unsupported) throw new Error(`执行服务不支持规则 ${unsupported.type}。`)
}
async function discardDraft() {
  if (!dirtyRules.value || (!diskRules.value.length && !rules.value.length)) return true
  try { await ElMessageBox.confirm('此操作会替换当前未保存草稿。请先保存，或确认放弃修改。', '替换规则草稿', { confirmButtonText: '放弃并加载', cancelButtonText: '继续编辑', type: 'warning' }); return true } catch { return false }
}
async function selectRule(id: string) {
  if (!await discardDraft()) return false
  const sequence = ++versionsSequence
  if (!id) { resetRuleSet(); return true }
  const current = ruleSets.value.find(item => item.id === id)
  if (current) loadRule(current)
  versionsLoading.value = true
  try {
    await versionList.fetch(true)
  } catch (e) { ElMessage.error(message(e)) }
  finally { if (sequence === versionsSequence) versionsLoading.value = false }
  return true
}
async function loadVersion(version: number) {
  if (!await discardDraft()) return
  const item = versions.value.find(value => value.version === version)
  if (item) loadRule(item)
}
function resetRuleSet() {
  versionsSequence++; ruleSetId.value = ''; versions.value = []; loadedVersion.value = null
  editorMode.value = 'v3'; ruleName.value = '新的清洗规则'; rules.value = []; diskRules.value = []; fieldBindings.value = {}; savedSnapshot.value = ''
  logicalFields.value = (selectedFileItem.value?.fields || []).map((field, index) => ({ id: `field_${field.id || `f${index}`}`, label: field.name }))
  fieldBindings.value = Object.fromEntries(logicalFields.value.map((field, index) => [field.id, selectedFileItem.value?.fields[index]?.id || `f${index}`]))
}
async function newRuleSet() { if (await discardDraft()) resetRuleSet() }
async function saveRules(asNew = false): Promise<RuleSet> {
  if (isV3Editor.value) { validateSchema(); validateDiskRules(diskRules.value, logicalFields.value) }
  else throw new Error('历史版本保持只读；请先显式转换为新版草稿，再另存。')
  if (!ruleName.value.trim()) throw new Error('请输入规则集名称。')
  savingRules.value = true
  const sequence = sessionSequence
  try {
    const existing = !asNew && ruleSetId.value
    const body = isV3Editor.value
      ? { name: ruleName.value.trim(), schemaVersion: 3, engineVersion: 'v3-disk', logicalFields: logicalFields.value, rules: diskRules.value }
      : { name: ruleName.value.trim(), engineVersion: 'v2-phased', rules: rules.value }
    const result = await api<RuleSet>(existing ? `/rule-sets/${ruleSetId.value}` : '/rule-sets', existing ? 'PUT' : 'POST', body)
    if (sequence !== sessionSequence) throw new Error('登录状态已失效，请重新登录。')
    ruleSets.value = [result, ...ruleSets.value.filter(item => item.id !== result.id)]
    loadRule(result)
    await versionList.fetch(true)
    if (sequence !== sessionSequence) throw new Error('登录状态已失效，请重新登录。')
    return result
  } finally { savingRules.value = false }
}
async function saveClick(asNew = false) {
  try { const result = await saveRules(asNew); ElMessage.success(`规则已保存为 v${result.version}。`) }
  catch (e) { ElMessage.error(message(e)) }
}
async function createTask() {
  if (!selectedFile.value || selectedFileItem.value?.parseStatus !== 'PARSED' || ['EXPIRED', 'DELETING'].includes(selectedFileItem.value.objectStatus || '')) { ElMessage.warning('请选择已解析且仍可用的输入文件。'); return }
  if (fileOptionsDirty.value) { ElMessage.warning('请先应用解析设置并重新解析。'); return }
  if (!settings.value) { ElMessage.warning('请先刷新并读取平台限额。'); return }
  creatingTask.value = true
  const sequence = sessionSequence
  const fileId = selectedFile.value
  const format = outputFormat.value
  const submitNamespace = user.value?.id || 'anonymous'
  try {
    if (isV3Editor.value) {
      validateSchema()
      validateDiskRules(diskRules.value, logicalFields.value, fieldBindings.value)
      preflightError.value = ''; preflightWarnings.value = []
      const checked = await api<Preflight>('/rules/preflight', 'POST', { fileId: selectedFile.value, parseVersion: selectedFileItem.value?.parseVersion, schemaVersion: 3, engineVersion: 'v3-disk', logicalFields: logicalFields.value, fieldBindings: fieldBindings.value, rules: diskRules.value })
      if (!checked.valid) throw new Error('规则预检未通过，请检查字段绑定和规则顺序。')
      preflightWarnings.value = checked.warnings || []
    }
    let id = ruleSetId.value
    let version = loadedVersion.value
    if (!id || dirtyRules.value || !version) {
      const saved = await saveRules()
      id = saved.id; version = saved.version
    }
    const request = { fileId, ruleSetId: id, ruleVersion: version, outputFormat: format, ...(isV3Editor.value ? { schemaVersion: 3, engineVersion: 'v3-disk', fieldBindings: fieldBindings.value, parseVersion: selectedFileItem.value?.parseVersion } : { engineVersion: legacyEngine.value }) }
    const submitKey = getOrCreateSubmitKey(sessionStorage, submitNamespace, request)
    let task: Task
    try {
      task = await api<Task>('/tasks', 'POST', request, { headers: { 'Idempotency-Key': submitKey } })
      clearSubmitKey(sessionStorage, submitNamespace, submitKey)
    } catch (e) {
      if (e instanceof HttpError && e.status < 500 && ![408, 409, 429].includes(e.status)) clearSubmitKey(sessionStorage, submitNamespace, submitKey)
      throw e
    }
    if (sequence !== sessionSequence) return
    tasks.value = [task, ...tasks.value.filter(item => item.id !== task.id)]
    page.value = 'tasks'; selectedTask.value = task.id; schedulePoll()
    ElMessage.success('任务已创建，将在后台异步执行。')
  } catch (e) { preflightError.value = message(e); ElMessage.error(message(e)) }
  finally { creatingTask.value = false }
}
function convertLegacyToV3() {
  try {
    const converted = migrateLegacyRules(rules.value, selectedFileItem.value?.fields || [])
    logicalFields.value = converted.logicalFields; diskRules.value = converted.rules; fieldBindings.value = converted.fieldBindings; editorMode.value = 'v3'; ruleSetId.value = ''; loadedVersion.value = null; versions.value = []; savedSnapshot.value = ''; ElMessage.success('旧版规则已转换为新版草稿；保存后才会创建 v3-disk 版本。')
  } catch (e) { ElMessage.error(e instanceof Error ? e.message : '旧版规则转换失败。') }
}
watch(selectedFileItem, (file, previous) => {
  const changedInput = previous && (file?.id !== previous.id || file?.parseVersion !== previous.parseVersion)
  if (changedInput && logicalFields.value.length && editorMode.value === 'v3') {
    if (!diskRules.value.length && !ruleSetId.value) logicalFields.value = []
    else { fieldBindings.value = {}; preflightWarnings.value = ['输入文件或解析版本已变化，请确认所有逻辑字段与当前文件列的绑定。'] }
  }
  if (editorMode.value === 'v3' && !logicalFields.value.length) {
    logicalFields.value = (file?.fields || []).map((field, index) => ({ id: `field_${field.id || `f${index}`}`, label: field.name }))
    fieldBindings.value = Object.fromEntries(logicalFields.value.map((field, index) => [field.id, file?.fields[index]?.id || `f${index}`]))
  }
})
async function preflightDraft() {
  preflightError.value = ''; preflightWarnings.value = []
  try {
    if (!selectedFile.value || selectedFileItem.value?.parseStatus !== 'PARSED') throw new Error('先选择已解析的输入文件。')
    validateSchema()
    validateDiskRules(diskRules.value, logicalFields.value, fieldBindings.value)
    const checked = await api<Preflight>('/rules/preflight', 'POST', { fileId: selectedFile.value, parseVersion: selectedFileItem.value?.parseVersion, schemaVersion: 3, engineVersion: 'v3-disk', logicalFields: logicalFields.value, fieldBindings: fieldBindings.value, rules: diskRules.value })
    preflightWarnings.value = checked.warnings || []; ElMessage.success(`预检通过，预计输出 ${checked.outputFields?.length ?? 0} 个字段。`)
  } catch (e) { preflightError.value = message(e) }
}
async function useSavedRule(item: RuleSet) { if (await selectRule(item.id)) page.value = 'workspace' }

onMounted(async () => {
  window.addEventListener('session-expired', clearSession)
  if (token()) {
    try { await authenticated(await api<User>('/auth/me')) }
    catch (e) { clearSession(); ElMessage.warning(message(e)) }
  }
  starting.value = false
})
onBeforeUnmount(() => { if (pollTimer) clearTimeout(pollTimer); window.removeEventListener('session-expired', clearSession) })
</script>

<template>
  <div v-if="starting" class="startup"><span class="brand-mark">d<span>↗</span></span><p>正在连接工作空间…</p></div>
  <AuthScreen v-else-if="!user" @authenticated="authenticated" />
  <div v-else class="app-shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark">d<span>↗</span></span><span>dataflow<span class="brand-sub">数据清洗流水线</span></span></div>
      <div class="workspace-label"><span class="workspace-dot"></span> 我的工作空间 <span class="workspace-badge">MVP</span></div>
      <nav class="side-nav" aria-label="主导航">
        <button :disabled="fileBusy || creatingTask || savingRules" :class="{ active: page === 'workspace' }" @click="page = 'workspace'"><span class="nav-icon">▦</span> 清洗工作台 <span class="nav-arrow">↗</span></button>
        <button :disabled="fileBusy || creatingTask || savingRules" :class="{ active: page === 'tasks' }" @click="page = 'tasks'"><span class="nav-icon">≋</span> 任务中心 <span v-if="activeCount" class="count-badge">{{ activeCount }}</span></button>
        <button :disabled="fileBusy || creatingTask || savingRules" :class="{ active: page === 'rules' }" @click="page = 'rules'"><span class="nav-icon">⌘</span> 规则库 <span class="nav-arrow">{{ ruleSets.length }}</span></button>
        <button v-if="user.role === 'ADMIN'" :disabled="fileBusy || creatingTask || savingRules" :class="{ active: page === 'admin' }" @click="page = 'admin'"><span class="nav-icon">⚙</span> 平台管理 <span class="nav-arrow">↗</span></button>
      </nav>
      <div class="sidebar-note"><span class="note-icon">↳</span><strong>每一步，都可追踪</strong><p>任务保存规则版本与执行日志，清洗结果和脏数据分别交付。</p></div>
      <div class="sidebar-user"><span class="avatar">{{ user.username.slice(0, 1).toUpperCase() }}</span><span class="username">{{ user.username }}<small>{{ user.role === 'ADMIN' ? '管理员' : '普通用户' }}</small></span><el-button text size="small" :disabled="fileBusy || creatingTask || savingRules" @click="logout">退出</el-button></div>
    </aside>
    <div class="main-shell">
      <header class="topbar"><span><span class="muted">工作空间</span><span class="breadcrumb-slash">/</span>{{ pageNames[page] }}</span><div><span class="live-indicator"></span><span class="muted">异步执行 · 全程追踪</span><el-button size="small" :loading="loading" @click="refresh">刷新</el-button><el-button class="mobile-logout" size="small" :disabled="fileBusy || creatingTask || savingRules" :aria-label="`退出 ${user.username} 的账户`" @click="logout">退出</el-button></div></header>
      <main class="main-content">
        <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" show-icon class="panel-alert" />
        <section class="page-heading"><div><span class="eyebrow">{{ page === 'workspace' ? 'BUILD YOUR PIPELINE' : page === 'tasks' ? 'TRACK EVERY RUN' : page === 'admin' ? 'OPERATE WITH CONFIDENCE' : 'REUSABLE BY DESIGN' }}</span><h1>{{ pageNames[page] }}<span class="heading-dot">.</span></h1><p>{{ page === 'workspace' ? '选择数据，编排规则，把重复的清洗工作交给流水线。' : page === 'tasks' ? '查看每一次执行的进度、日志与质量报告。' : page === 'admin' ? '管理账户角色、全平台任务和处理限额。' : '保存清洗经验，用不可变版本保持每次执行可追溯。' }}</p></div><span class="page-index">{{ page === 'workspace' ? '01' : page === 'tasks' ? '02' : page === 'admin' ? '04' : '03' }} <span>/ DATAFLOW</span></span></section>

        <template v-if="page === 'workspace'">
          <div class="pipeline-strip"><div class="pipeline-step active"><span>1</span><div><strong>准备数据</strong><small>上传与预览</small></div></div><i>→</i><div class="pipeline-step" :class="{ active: !!selectedFile }"><span>2</span><div><strong>编排规则</strong><small>按配置顺序执行</small></div></div><i>→</i><div class="pipeline-step" :class="{ active: tasks.length > 0 }"><span>3</span><div><strong>执行与交付</strong><small>报告及结果</small></div></div></div>
          <div class="workbench-grid">
            <div class="workbench-main">
              <FilePanel v-model:file-id="selectedFile" :inert="creatingTask || savingRules" :files="files" :settings="settings" @preview="preview = $event" @file-changed="fileChanged" @busy="fileBusy = $event" @parse-dirty="fileOptionsDirty = $event" />
              <div class="filter-toolbar"><el-select v-model="fileFormat" aria-label="筛选文件格式"><el-option value="ALL" label="全部文件格式" /><el-option v-for="format in ['csv', 'xlsx', 'parquet']" :key="format" :value="format" :label="format.toUpperCase()" /></el-select><el-date-picker v-model="fileDates" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ssZ" start-placeholder="文件创建起始时间" end-placeholder="结束时间" clearable /></div><div class="list-navigation"><el-input v-model="fileSearch" clearable placeholder="搜索已上传文件" aria-label="搜索已上传文件" /><CursorPager :page="fileList.page.value" :can-previous="fileList.canPrevious.value" :has-more="fileList.hasMore.value" :loading="fileList.loading.value" :error="fileList.error.value" :count="fileList.items.value.length" @previous="fileList.previous" @next="fileList.next" @refresh="fileList.fetch()" /></div>

              <section class="panel" :inert="savingRules || creatingTask">
                <div class="panel-heading"><div><span class="step-label">STEP 02</span><h2>清洗规则</h2></div><span class="subtle-badge">{{ isV3Editor ? "v3-disk · 按配置顺序执行" : legacyEngine }}</span></div>
                <div class="rule-library-picker"><label class="field"><span>加载规则集</span><el-select :model-value="ruleSetId" filterable clearable placeholder="选择已保存的规则（可选）" @update:model-value="selectRule($event)"><el-option v-for="item in ruleSets" :key="item.id" :value="item.id" :label="`${item.name} · v${item.version}`" /></el-select></label><label v-if="ruleSetId" class="field version-field"><span>历史版本</span><el-select :model-value="loadedVersion" :loading="versionsLoading" aria-label="历史版本" @update:model-value="loadVersion"><el-option v-for="item in versions" :key="item.version" :value="item.version" :label="`v${item.version}`" /></el-select></label><el-button @click="newRuleSet">新建</el-button></div>
                <label class="field rule-name-field"><span>规则集名称 <span v-if="ruleSetId && dirtyRules" class="unsaved-dot">● 有未保存的修改</span></span><el-input v-model="ruleName" maxlength="100" placeholder="为这组规则起个名字" /></label>
                <el-alert v-if="!isV3Editor" :title="`此历史版本使用 ${legacyEngine}，配置保持只读。可显式转换为新版草稿，另存后用于新任务。`" type="warning" :closable="false" class="panel-alert" />
                <el-button v-if="!isV3Editor" class="retry-button" @click="convertLegacyToV3">转换并另存为新版草稿</el-button>
                <el-alert v-if="isV3Editor && schemaError" :title="`规则 Schema 不可用：${schemaError}`" type="error" :closable="false" class="panel-alert" /><div v-if="isV3Editor" class="form-note">{{ ruleSchema ? `已验证服务端 Schema ${ruleSchema.schemaVersion} · ${ruleSchema.ruleCount} 类规则 · ${ruleSchema.engineVersion}` : "正在读取规则 Schema…" }} <el-button v-if="!ruleSchema" link type="primary" @click="loadSchema">重试读取</el-button></div><DiskRuleEditor v-if="isV3Editor" v-model="diskRules" v-model:fields="logicalFields" v-model:bindings="fieldBindings" :columns="preview?.columns || selectedFileItem?.fields || []" />
                <div v-else inert><RuleEditor v-model="rules" :columns="preview?.columns || selectedFileItem?.fields || []" /></div>
                <CursorPager v-if="ruleSetId" :page="versionList.page.value" :can-previous="versionList.canPrevious.value" :has-more="versionList.hasMore.value" :loading="versionList.loading.value" :error="versionList.error.value" :count="versions.length" @previous="versionList.previous" @next="versionList.next" @refresh="versionList.fetch()" />
                <div class="rule-save-row"><span class="form-note">修改后保存为新版本，历史任务始终使用创建时的版本。</span><el-button v-if="isV3Editor" @click="preflightDraft">预检规则与字段</el-button><div><el-button v-if="ruleSetId" :disabled="!isV3Editor" :loading="savingRules" @click="saveClick(true)">另存为规则集</el-button><el-button :disabled="!isV3Editor" :loading="savingRules" @click="saveClick()">{{ ruleSetId ? '保存新版本' : '保存规则' }}</el-button></div></div>
              </section>
            </div>
            <aside class="workbench-summary">
              <section class="panel launch-panel"><span class="step-label">STEP 03</span><h2>准备执行</h2><p class="muted">任务创建后在后台执行，可随时查看进度。</p><dl class="launch-summary"><div><dt>输入文件</dt><dd>{{ selectedFileItem?.name || '尚未选择' }}</dd></div><div><dt>清洗规则</dt><dd>{{ isV3Editor ? diskRules.filter(rule => rule.enabled).length : rules.length }} 条启用规则</dd></div><div><dt>规则版本</dt><dd>{{ ruleSetId && !dirtyRules ? `v${loadedVersion}` : '创建时自动保存' }}</dd></div></dl><label class="field"><span>输出格式</span><el-select v-model="outputFormat" aria-label="输出格式"><el-option value="csv" label="CSV（UTF-8 BOM）" /><el-option value="xlsx" label="Excel（.xlsx）" /><el-option value="parquet" label="Parquet" /></el-select></label><el-button type="primary" class="launch-button" :disabled="selectedFileItem?.parseStatus !== 'PARSED' || ['EXPIRED', 'DELETING'].includes(selectedFileItem?.objectStatus || '') || (isV3Editor && !ruleSchema) || fileBusy || fileOptionsDirty || savingRules || !settings" :loading="creatingTask" @click="createTask">创建清洗任务 <span>→</span></el-button><p class="form-note">结果、脏数据和清洗报告分别保存，完成后可在任务详情中下载。</p></section>
              <el-alert v-if="preflightError" :title="preflightError" type="error" :closable="false" class="panel-alert" /><el-alert v-for="warning in preflightWarnings" :key="warning" :title="warning" type="warning" :closable="false" class="panel-alert" /><LimitsDisplay :settings="settings" compact /><div class="limits-note"><strong>固定快照，可追溯执行</strong><p>当前任务将固定文件解析、字段、规则与平台限额快照；后续修改不会影响已有任务。</p><button class="text-action" @click="page = 'tasks'">查看我的任务 <span>↗</span></button></div>
            </aside>
          </div>
        </template>

        <template v-else-if="page === 'tasks'">
          <div class="overview-metrics"><div><span>本页任务</span><strong>{{ number(tasks.length) }}</strong><small>当前筛选页的任务记录</small></div><div><span>本页正在处理</span><strong>{{ number(activeCount) }}<i class="metric-dot"></i></strong><small>本页排队与执行中的任务</small></div><div><span>本页成功完成</span><strong class="positive">{{ number(succeededCount) }}</strong><small>本页结果与报告可供下载</small></div></div>
          <section class="panel tasks-panel"><div class="panel-heading"><div><h2>执行记录</h2><p class="muted">{{ activeCount ? '有任务正在处理，列表自动更新。' : '所有任务集中管理，支持失败重试和主动取消。' }}</p></div><el-select v-model="taskFilter" aria-label="筛选任务状态" class="status-filter"><el-option value="ALL" label="全部状态" /><el-option v-for="(label, status) in statusLabels" :key="status" :value="status" :label="label" /></el-select></div>
            <el-table v-loading="loading" :data="filteredTasks" stripe empty-text="暂无任务记录。前往清洗工作台创建第一个任务。" @row-click="(row: Task) => selectedTask = row.id" class="clickable-table">
              <el-table-column label="文件 / 任务" min-width="230"><template #default="{ row }"><div class="table-file">{{ row.fileName }}</div><span class="mono muted tiny">{{ row.id.slice(0, 8) }} · 第 {{ row.attempt }} 次执行</span></template></el-table-column>
              <el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="statusTypes[row.status as keyof typeof statusTypes]" effect="light">{{ statusLabels[row.status as keyof typeof statusLabels] }}</el-tag></template></el-table-column>
              <el-table-column label="进度" min-width="150"><template #default="{ row }"><el-progress :percentage="row.progress" :stroke-width="5" :status="row.status === 'SUCCEEDED' ? 'success' : row.status === 'FAILED' ? 'exception' : undefined" /></template></el-table-column>
              <el-table-column label="输出" width="90"><template #default="{ row }"><span class="format-label">{{ row.outputFormat.toUpperCase() }}</span></template></el-table-column>
              <el-table-column label="创建时间" min-width="175"><template #default="{ row }">{{ date(row.createdAt) }}</template></el-table-column>
              <el-table-column label="操作" width="110" fixed="right"><template #default="{ row }"><el-button link type="primary" @click.stop="selectedTask = row.id">查看详情 →</el-button></template></el-table-column>
            </el-table>
            <div class="filter-toolbar"><el-select v-model="taskFormat" aria-label="筛选任务输出格式"><el-option value="ALL" label="全部输出格式" /><el-option v-for="format in ['csv', 'xlsx', 'parquet']" :key="format" :value="format" :label="format.toUpperCase()" /></el-select><el-date-picker v-model="taskDates" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ssZ" start-placeholder="任务创建起始时间" end-placeholder="结束时间" clearable /></div><el-input v-model="taskSearch" clearable placeholder="按文件名搜索任务" aria-label="按文件名搜索任务" /><CursorPager :page="taskList.page.value" :can-previous="taskList.canPrevious.value" :has-more="taskList.hasMore.value" :loading="taskList.loading.value" :error="taskList.error.value" :count="tasks.length" @previous="taskList.previous" @next="taskList.next" @refresh="taskList.fetch()" />
          </section>
        </template>

        <template v-else-if="page === 'rules'">
          <section class="panel"><div class="panel-heading"><div><h2>我的规则集 <span class="inline-count">{{ ruleList.items.value.length }}</span></h2><p class="muted">每次保存产生新的版本，已有任务的规则保持可追溯。</p></div><el-button type="primary" @click="newRuleSet(); page = 'workspace'">＋ 新建规则集</el-button></div>
            <div class="filter-toolbar"><el-date-picker v-model="ruleDates" type="datetimerange" value-format="YYYY-MM-DDTHH:mm:ssZ" start-placeholder="规则创建起始时间" end-placeholder="结束时间" clearable /></div><el-input v-model="ruleSearch" clearable placeholder="搜索规则集名称" aria-label="搜索规则集名称" /><CursorPager :page="ruleList.page.value" :can-previous="ruleList.canPrevious.value" :has-more="ruleList.hasMore.value" :loading="ruleList.loading.value" :error="ruleList.error.value" :count="ruleList.items.value.length" @previous="ruleList.previous" @next="ruleList.next" @refresh="ruleList.fetch()" />
            <el-empty v-if="!ruleList.items.value.length" description="还没有规则集。在工作台编排并保存你的第一组清洗规则。" />
            <div v-else class="rules-library"><article v-for="item in ruleList.items.value" :key="item.id" class="rule-card"><div class="rule-card-icon">⌘</div><div class="rule-card-body"><h3>{{ item.name }}</h3><p>{{ item.rules.length }} 条有序规则 <span>·</span> 最新版本 v{{ item.version }}</p><small class="muted">{{ date(item.createdAt) }}</small></div><el-button @click="useSavedRule(item)">加载与编辑 <span>↗</span></el-button></article></div>
          </section>
        </template>
        <AdminConsole v-else-if="user.role === 'ADMIN'" :current-user="user" @settings-changed="settings = $event" @user-changed="identityChanged" @task="selectedTask = $event" />
        <footer class="content-footer"><span>DATAFLOW / 结构化数据清洗平台</span><span>数据可追溯，结果有依据。</span></footer>
      </main>
    </div>
    <TaskDetail :task-id="selectedTask" :current-user-id="user.id" @close="selectedTask = null" @changed="refresh(); schedulePoll()" />
  </div>
</template>
