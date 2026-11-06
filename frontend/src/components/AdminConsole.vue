<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'
import { date, number, statusLabels, statusTypes } from '../format'
import type { Settings, Task, User } from '../types'
import { useCursorPage } from '../pagination'
import CursorPager from './CursorPager.vue'
import RetentionConsole from './RetentionConsole.vue'

const props = defineProps<{ currentUser: User }>()
const emit = defineEmits<{ settingsChanged: [value: Settings]; userChanged: []; task: [id: string] }>()
const tab = ref('users')
const users = ref<User[]>([])
const tasks = ref<Task[]>([])
const settings = ref<Settings | null>(null)
const originalSettings = ref('')
const loading = ref(false)
const error = ref('')
const saving = ref('')
const search = ref('')
const status = ref('ALL')
const taskSearch = ref('')
const ownerId = ref('')
const userList = useCursorPage<User>(() => '/admin/users/page', () => ({ q: search.value }))
const taskList = useCursorPage<Task>(() => '/admin/tasks/page', () => ({ status: status.value, q: taskSearch.value, ownerId: ownerId.value }))
watch(userList.items, values => { users.value = values; userDrafts.value = Object.fromEntries(values.map(user => [user.id, { role: user.role, enabled: user.enabled }])) })
watch(taskList.items, values => { tasks.value = values })
watch(search, () => void userList.fetch(true))
watch([status, taskSearch, ownerId], () => void taskList.fetch(true))
const userDrafts = ref<Record<string, { role: User['role']; enabled: boolean }>>({})
let alive = true
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
const filteredUsers = computed(() => users.value)
const filteredTasks = computed(() => tasks.value)
const settingsDirty = computed(() => settings.value && JSON.stringify(settings.value) !== originalSettings.value)
const settingFields: { key: keyof Settings; label: string; cap: number; unit: string; min: number }[] = [
  { key: 'maxFileBytes', label: '单文件字节上限', cap: 104857600, unit: '100 MiB', min: 1 },
  { key: 'maxRows', label: '最大数据行数', cap: 500000, unit: '500,000 行', min: 1 },
  { key: 'maxColumns', label: '最大字段数', cap: 200, unit: '200 列', min: 1 },
  { key: 'maxCells', label: '最大单元格数', cap: 5000000, unit: '5,000,000 个', min: 1 },
  { key: 'maxExcelExpandedBytes', label: 'Excel 解压字节上限', cap: 536870912, unit: '512 MiB', min: 1 },
  { key: 'previewRows', label: '默认预览行数及上限', cap: 1000, unit: '1,000 行', min: 1 },
  { key: 'maxActivePerUser', label: '每用户活跃任务数', cap: 20, unit: '20 个', min: 1 },
  { key: 'maxActiveGlobal', label: '平台活跃任务数', cap: 100, unit: '100 个', min: 1 },
  { key: 'submissionsPerMinute', label: '每用户每分钟提交次数', cap: 120, unit: '120 次', min: 1 },
  { key: 'maxRunningGlobal', label: '平台同时执行任务数', cap: 100, unit: '100 个', min: 1 },
  { key: 'maxRunningPerUser', label: '每用户同时执行任务数', cap: 20, unit: '20 个', min: 1 },
]
async function refresh() {
  const sequence = ++generation
  loading.value = true; error.value = ''
  try {
    const [, , nextSettings] = await Promise.all([userList.fetch(), taskList.fetch(), api<Settings>('/settings')])
    if (!alive || sequence !== generation) return
    if (!settingsDirty.value) { settings.value = nextSettings; originalSettings.value = JSON.stringify(nextSettings) }
  } catch (e) { if (alive && sequence === generation) error.value = e instanceof Error ? e.message : '读取管理员数据失败。' }
  finally { if (alive && sequence === generation) loading.value = false }
}
async function saveUser(user: User) {
  const draft = userDrafts.value[user.id]
  if (!draft) return
  if (!draft.enabled || (user.role === 'ADMIN' && draft.role === 'USER')) {
    try { await ElMessageBox.confirm(`${!draft.enabled ? '停用后该账户将无法登录，已有会话也会立即失效。' : '该账户将失去管理员权限。'}${user.id === props.currentUser.id ? '你正在修改自己的账户。' : ''}`, '确认修改账户', { confirmButtonText: '确认修改', cancelButtonText: '返回检查', type: 'warning' }) }
    catch { return }
  }
  saving.value = user.id
  try {
    await api(`/admin/users/${user.id}`, 'PUT', draft)
    ElMessage.success('账户状态与角色已更新。')
    emit('userChanged')
    await refresh()
  } catch (e) { ElMessage.error(e instanceof Error ? e.message : '修改失败。') }
  finally { saving.value = '' }
}
async function saveSettings() {
  if (!settings.value) return
  for (const field of settingFields) {
    const value = settings.value[field.key]
    if (typeof value !== 'number' || !Number.isInteger(value) || value < field.min || value > field.cap) { ElMessage.warning(`${field.label}需为 ${field.min}–${number(field.cap)} 的整数。`); return }
  }
  if (settings.value.maxActivePerUser > settings.value.maxActiveGlobal) { ElMessage.warning('每用户活跃任务数不能超过平台活跃任务数。'); return }
  saving.value = 'settings'
  try {
    const result = await api<Settings>('/admin/settings', 'PUT', settings.value)
    if (!alive) return
    settings.value = result; originalSettings.value = JSON.stringify(result)
    emit('settingsChanged', result)
    ElMessage.success('平台限额已保存，将用于后续解析和新任务快照。')
  } catch (e) { ElMessage.error(e instanceof Error ? e.message : '保存限额失败。') }
  finally { saving.value = '' }
}
function schedule() {
  timer = setTimeout(async () => {
    if (!alive) return
    if (!document.hidden && tab.value === 'tasks') {
      try { await taskList.fetch() }
      catch (e) { if (alive) error.value = e instanceof Error ? e.message : '更新任务列表失败。' }
    }
    if (alive) schedule()
  }, 5000)
}
onMounted(() => { void refresh(); schedule() })
onBeforeUnmount(() => { alive = false; generation++; if (timer) clearTimeout(timer) })
</script>

<template>
  <section class="panel admin-console">
    <div class="panel-heading"><div><h2>平台管理</h2><p class="muted">管理账户、查看全平台任务与配置处理限额。</p></div><el-button :loading="loading" :disabled="!!saving" @click="refresh">刷新管理数据</el-button></div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="panel-alert" />
    <div v-if="tab === 'tasks'" class="admin-toolbar"><el-input v-model="taskSearch" clearable placeholder="按文件名搜索全平台任务" aria-label="管理员任务文件名" /><el-input v-model="ownerId" clearable placeholder="所属用户ID（可空）" aria-label="任务所属用户ID" /></div>
    <el-tabs v-model="tab">
      <el-tab-pane label="账户与角色" name="users">
        <div class="admin-toolbar"><el-input v-model="search" placeholder="搜索用户名" clearable aria-label="搜索用户名" /><span class="form-note">至少保留一位启用的管理员；服务端始终校验权限。</span></div>
        <el-table v-loading="loading" :data="filteredUsers" stripe empty-text="没有匹配的用户。"><el-table-column label="用户名" min-width="150"><template #default="{ row }"><strong>{{ row.username }}</strong><el-tag v-if="row.id === currentUser.id" size="small" class="self-badge">我</el-tag></template></el-table-column><el-table-column label="角色" min-width="150"><template #default="{ row }"><el-select v-if="userDrafts[row.id]" v-model="userDrafts[row.id]!.role" :aria-label="`${row.username}角色`" :disabled="!!saving"><el-option value="USER" label="普通用户" /><el-option value="ADMIN" label="管理员" /></el-select></template></el-table-column><el-table-column label="账户状态" min-width="115"><template #default="{ row }"><el-switch v-if="userDrafts[row.id]" v-model="userDrafts[row.id]!.enabled" active-text="启用" inactive-text="停用" :aria-label="`${row.username}启用状态`" :disabled="!!saving" inline-prompt /></template></el-table-column><el-table-column label="创建时间" min-width="175"><template #default="{ row }">{{ date(row.createdAt || '') }}</template></el-table-column><el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" :loading="saving === row.id" :disabled="!!saving && saving !== row.id || userDrafts[row.id]?.role === row.role && userDrafts[row.id]?.enabled === row.enabled" @click="saveUser(row)">保存</el-button></template></el-table-column></el-table>
        <CursorPager :page="userList.page.value" :can-previous="userList.canPrevious.value" :has-more="userList.hasMore.value" :loading="userList.loading.value" :error="userList.error.value" :count="users.length" @previous="userList.previous" @next="userList.next" @refresh="userList.fetch()" />
      </el-tab-pane>
      <el-tab-pane label="保留期与清理" name="retention"><RetentionConsole v-if="tab === 'retention'" /></el-tab-pane>
      <el-tab-pane label="全部任务" name="tasks"><div class="admin-toolbar"><el-select v-model="status" class="status-filter" aria-label="全平台任务状态"><el-option value="ALL" label="全部状态" /><el-option v-for="(label, value) in statusLabels" :key="value" :value="value" :label="label" /></el-select><span class="form-note">查看全平台执行记录；其他用户的任务仅可查看与下载，不可取消或重试。</span></div><el-table :data="filteredTasks" stripe empty-text="暂无平台任务记录。"><el-table-column label="任务 / 文件" min-width="230"><template #default="{ row }"><div class="table-file">{{ row.fileName }}</div><small class="mono muted">{{ row.id.slice(0, 8) }}</small></template></el-table-column><el-table-column prop="username" label="所属用户" min-width="120" /><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="statusTypes[row.status as keyof typeof statusTypes]">{{ statusLabels[row.status as keyof typeof statusLabels] }}</el-tag></template></el-table-column><el-table-column label="进度" min-width="140"><template #default="{ row }"><el-progress :percentage="row.progress" :stroke-width="5" /></template></el-table-column><el-table-column label="创建时间" min-width="170"><template #default="{ row }">{{ date(row.createdAt) }}</template></el-table-column><el-table-column label="操作" width="110" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="emit('task', row.id)">查看详情 →</el-button></template></el-table-column></el-table></el-tab-pane>
      <el-tab-pane label="平台限额" name="settings"><el-alert title="修改影响后续文件解析与新建任务；历史任务及重试沿用创建时的限制快照。提高硬上限需要另行评估引擎容量。" type="info" :closable="false" show-icon class="panel-alert" /><form v-if="settings" class="settings-form" @submit.prevent="saveSettings"><label v-for="field in settingFields" :key="field.key" class="field"><span>{{ field.label }}</span><el-input-number v-model="settings[field.key]" :min="field.min" :max="field.cap" :precision="0" :controls="false" :aria-label="field.label" :disabled="saving === 'settings'" /><small class="muted">安全上限 {{ field.unit }}</small></label><div class="settings-save"><span class="form-note">{{ settingsDirty ? '有未保存的限额修改。' : '当前显示已保存的平台限额。' }}</span><el-button type="primary" native-type="submit" :loading="saving === 'settings'" :disabled="!settingsDirty">保存平台限额</el-button></div></form></el-tab-pane>
    </el-tabs>
    <CursorPager v-if="tab === 'tasks'" :page="taskList.page.value" :can-previous="taskList.canPrevious.value" :has-more="taskList.hasMore.value" :loading="taskList.loading.value" :error="taskList.error.value" :count="tasks.length" @previous="taskList.previous" @next="taskList.next" @refresh="taskList.fetch()" />
  </section>
</template>
