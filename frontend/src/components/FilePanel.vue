<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, token, uploadChunk } from '../api'
import { bytes, date, number } from '../format'
import { dataTypeLabels } from '../rules'
import type { DataType, FileItem, ParseOptions, Preview, Settings } from '../types'
import ScalarListInput from './ScalarListInput.vue'
import LimitsDisplay from './LimitsDisplay.vue'

const props = defineProps<{ files: FileItem[]; fileId: string; settings: Settings | null }>()
const emit = defineEmits<{ 'update:fileId': [id: string]; preview: [value: Preview | null]; fileChanged: [file: FileItem]; busy: [value: boolean]; parseDirty: [value: boolean] }>()
const preview = ref<Preview | null>(null)
const previewLoading = ref(false)
const previewError = ref('')
const uploading = ref(false)
const parsing = ref(false)
const uploadProgress = ref(0)
const uploadName = ref('')
const uploadPhase = ref('')
const dropped = ref(false)
const input = ref<HTMLInputElement | null>(null)
const parseOptions = ref<ParseOptions>(defaultOptions())
const previewLimit = ref<number | undefined>()
const optionsBaseline = ref('')
const hash = ref('')
let generation = 0
let alive = true
const selected = computed(() => props.files.find(file => file.id === props.fileId))
const completeFiles = computed(() => props.files.filter(file => ['READY', 'COMPLETED'].includes(file.status)))
const busy = computed(() => uploading.value || parsing.value)
const optionsDirty = computed(() => JSON.stringify(parseOptions.value) !== optionsBaseline.value)
const fieldProfiles = computed(() => preview.value?.columns || selected.value?.fields || [])
const parsed = computed(() => selected.value?.parseStatus === 'PARSED')
watch(busy, value => emit('busy', value))
watch(optionsDirty, value => emit('parseDirty', Boolean(props.fileId && value)), { immediate: true })
function defaultOptions(): ParseOptions { return { encoding: 'auto', delimiter: 'auto', missingValues: [''], fieldTypes: {} } }
function cloneOptions(value?: ParseOptions): ParseOptions { return JSON.parse(JSON.stringify({ ...defaultOptions(), ...value })) as ParseOptions }
function message(error: unknown) { return error instanceof Error ? error.message : '读取文件失败。' }
function cell(value: unknown) { return value === null || value === undefined ? 'NULL' : typeof value === 'object' ? JSON.stringify(value) : String(value) }
function clearPreview() { preview.value = null; emit('preview', null) }
async function getPreview() {
  const id = props.fileId
  const sequence = ++generation
  clearPreview(); previewError.value = ''; previewLoading.value = Boolean(id)
  if (!id || !parsed.value) { previewLoading.value = false; return }
  try {
    const limit = previewLimit.value || props.settings?.previewRows
    const result = await api<Preview>(`/files/${id}/preview${limit ? `?limit=${limit}` : ''}`)
    if (!alive || sequence !== generation) return
    preview.value = result; emit('preview', result)
    parseOptions.value = cloneOptions(result.parseOptions || selected.value?.parseOptions)
    optionsBaseline.value = JSON.stringify(parseOptions.value)
  } catch (e) { if (alive && sequence === generation) previewError.value = message(e) }
  finally { if (sequence === generation) previewLoading.value = false }
}
watch(() => props.fileId, () => {
  parseOptions.value = cloneOptions(selected.value?.parseOptions)
  optionsBaseline.value = JSON.stringify(parseOptions.value)
  hash.value = selected.value?.sha256 || ''
  previewLimit.value = props.settings?.previewRows
  void getPreview()
}, { immediate: true })
watch(() => props.settings?.previewRows, value => { previewLimit.value = value })
onBeforeUnmount(() => { alive = false; generation++; emit('busy', false); emit('parseDirty', false); emit('preview', null) })

async function reparse() {
  if (!selected.value || !props.settings || busy.value) return
  const id = selected.value.id
  const sequence = ++generation
  parsing.value = true; previewError.value = ''
  const options = cloneOptions(parseOptions.value)
  try {
    const file = await api<FileItem>(`/files/${id}/parse`, 'POST', options)
    if (!alive || sequence !== generation) return
    emit('fileChanged', file)
    if (file.parseStatus === 'PARSED') {
      parseOptions.value = cloneOptions(file.parseOptions)
      optionsBaseline.value = JSON.stringify(parseOptions.value)
      // The emitted metadata is reactive in the parent on the next update tick.
      const result = await api<Preview>(`/files/${id}/preview?limit=${previewLimit.value || props.settings.previewRows}`)
      if (!alive || sequence !== generation) return
      preview.value = result; emit('preview', result)
      parseOptions.value = cloneOptions(result.parseOptions || file.parseOptions)
      optionsBaseline.value = JSON.stringify(parseOptions.value)
      ElMessage.success('解析完成，字段与解析选项已更新。')
    } else { clearPreview(); ElMessage.warning(file.parseError || '文件已保存，但解析失败。请调整解析选项后重试。') }
  } catch (e) { if (alive && sequence === generation) previewError.value = message(e) }
  finally { if (alive) parsing.value = false }
}
function overrideType(id: string, value?: DataType | '') {
  const fieldTypes = { ...(parseOptions.value.fieldTypes || {}) }
  if (value) fieldTypes[id] = value
  else delete fieldTypes[id]
  parseOptions.value = { ...parseOptions.value, fieldTypes }
}
async function upload(file?: File) {
  if (!file || busy.value) return
  if (!props.settings) { ElMessage.error('请先刷新并读取平台限额。'); return }
  if (!/\.(csv|xlsx|parquet)$/i.test(file.name)) { ElMessage.error('请选择 CSV、.xlsx 或 Parquet 文件。'); return }
  if (!file.size || file.size > props.settings.maxFileBytes) { ElMessage.error(`文件大小需为 1 字节至 ${bytes(props.settings.maxFileBytes)}。`); return }
  if (!globalThis.crypto?.subtle) { ElMessage.error('计算文件校验值需要安全连接，请使用 HTTPS 或 localhost 访问。'); return }
  uploading.value = true; uploadProgress.value = 0; uploadName.value = file.name; hash.value = ''; uploadPhase.value = '正在计算 SHA-256 校验值'
  try {
    const digest = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
    if (!alive) return
    hash.value = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('')
    uploadPhase.value = '正在分片上传'
    type UploadSession = { id: string; uploadId: string; chunkSize: number; totalChunks: number; receivedChunks?: number[]; status?: string }
    const checkpointKey = `dataflow.upload.${file.name}:${file.size}:${hash.value}`
    let session: UploadSession | undefined
    const pendingUpload = sessionStorage.getItem(checkpointKey)
    if (pendingUpload) {
      try {
        const existing = await api<UploadSession>(`/uploads/${encodeURIComponent(pendingUpload)}`)
        if (existing.status === 'UPLOADING' || existing.status === 'READY') session = existing
      } catch { sessionStorage.removeItem(checkpointKey) }
    }
    session ??= await api<UploadSession>('/files/init', 'POST', { name: file.name, size: file.size, sha256: hash.value, chunkSize: 4 * 1024 ** 2, parseOptions: defaultOptions() })
    sessionStorage.setItem(checkpointKey, session.uploadId)
    if (!session.uploadId || session.uploadId === session.id) throw new Error('服务未返回有效的独立上传会话，请检查服务版本。')
    const received = new Set(session.receivedChunks || [])
    for (let index = 0; index < session.totalChunks; index++) {
      if (!alive) return
      if (session.status === 'READY' || received.has(index)) {
        uploadProgress.value = Math.round((index + 1) / session.totalChunks * 95)
        continue
      }
      const chunk = file.slice(index * session.chunkSize, Math.min((index + 1) * session.chunkSize, file.size))
      let lastError: unknown
      for (let attempt = 0; attempt < 3; attempt++) {
        try { await uploadChunk(session.uploadId, index, chunk); lastError = undefined; break }
        catch (e) { lastError = e; if (!token()) throw e }
      }
      if (lastError) throw lastError
      uploadProgress.value = Math.round((index + 1) / session.totalChunks * 95)
    }
    uploadPhase.value = '正在合并、校验并解析文件'
    const complete = await api<FileItem>(`/uploads/${session.uploadId}/complete`, 'POST')
    sessionStorage.removeItem(checkpointKey)
    if (!alive) return
    uploadProgress.value = 100; emit('fileChanged', complete); emit('update:fileId', complete.id)
    if (complete.parseStatus === 'PARSED') ElMessage.success('文件上传、校验与解析完成。')
    else ElMessage.warning('原始文件上传完成，但解析失败。可调整编码、分隔符或工作表后重新解析。')
  } catch (e) { if (alive) ElMessage.error(message(e)) }
  finally { if (alive) { uploading.value = false; uploadPhase.value = ''; if (input.value) input.value.value = '' } }
}
function onDrop(event: DragEvent) { dropped.value = false; void upload(event.dataTransfer?.files[0]) }
function onInput(event: Event) { void upload((event.target as HTMLInputElement).files?.[0]) }
</script>

<template>
  <section class="panel file-panel">
    <div class="panel-heading"><div><span class="step-label">STEP 01</span><h2>数据源</h2></div><span class="subtle-badge">CSV / XLSX / PARQUET</span></div>
    <LimitsDisplay :settings="settings" compact />
    <input ref="input" type="file" accept=".csv,.xlsx,.parquet" class="sr-only" aria-label="上传数据文件" @change="onInput" />
    <button class="upload-zone" :class="{ dragging: dropped, uploading }" :disabled="busy || !settings" @click="input?.click()" @dragover.prevent="dropped = true" @dragleave.prevent="dropped = false" @drop.prevent="onDrop"><span class="upload-icon">↥</span><strong>{{ uploading ? `${uploadPhase} · ${uploadName}` : '点击选择文件，或拖拽到这里' }}</strong><span>{{ uploading ? '请保持页面打开，服务端将核对完整文件校验值' : `支持 CSV、Excel（.xlsx）、Parquet${settings ? '，文件最大 ' + bytes(settings.maxFileBytes) : ''}` }}</span><el-progress v-if="uploading" :percentage="uploadProgress" :stroke-width="5" class="upload-progress" /></button>
    <div class="file-picker"><label class="field"><span>或选择已上传的文件</span><el-select :model-value="fileId" filterable clearable placeholder="选择数据文件" :disabled="busy" @update:model-value="emit('update:fileId', $event)"><el-option v-for="file in completeFiles" :key="file.id" :value="file.id" :label="file.name"><span>{{ file.name }}</span><span class="option-meta">{{ file.parseStatus === 'PARSED' ? '解析完成' : file.parseStatus === 'FAILED' ? '解析失败' : '待解析' }} · {{ bytes(file.size) }}</span></el-option></el-select></label><span v-if="selected" class="file-size">{{ bytes(selected.size) }}<br /><span>{{ selected.format.toUpperCase() }}</span></span></div>
    <template v-if="selected">
      <el-alert v-if="selected.objectStatus === 'EXPIRED' || selected.objectStatus === 'DELETING'" title="此输入文件已过期或正在清理，无法用于新任务；请重新上传原始文件。" type="warning" :closable="false" class="panel-alert" />
      <div class="file-states"><el-tag type="success">原始文件已保存</el-tag><el-tag :type="parsed ? 'success' : selected.parseStatus === 'FAILED' ? 'danger' : 'info'">{{ parsed ? '解析完成' : selected.parseStatus === 'FAILED' ? '解析失败' : '尚未解析' }}</el-tag><span>{{ selected.uploader?.username || '我的文件' }} · {{ date(selected.createdAt) }}</span></div>
      <el-alert v-if="selected.parseError" :title="selected.parseError" type="error" :closable="false" show-icon class="panel-alert" />
      <el-collapse class="file-metadata"><el-collapse-item title="查看文件信息与校验值" name="metadata"><dl><div><dt>文件 ID</dt><dd class="mono">{{ selected.id }}</dd></div><div><dt>上传会话</dt><dd class="mono">{{ selected.uploadId }}</dd></div><div><dt>SHA-256</dt><dd class="mono">{{ selected.sha256 || '尚未完成校验' }}</dd></div><div><dt>存储对象</dt><dd class="mono">{{ selected.objectKey }}</dd></div></dl></el-collapse-item></el-collapse>
      <el-collapse class="parse-options" :model-value="selected.parseStatus === 'FAILED' ? ['parse'] : undefined"><el-collapse-item title="解析选项与字段类型（修改后需重新解析）" name="parse">
        <div class="parse-options-grid" :inert="busy">
          <template v-if="selected.format === 'csv'"><label class="field"><span>CSV 字符编码</span><el-select v-model="parseOptions.encoding" aria-label="CSV字符编码"><el-option value="auto" label="自动识别" /><el-option value="utf-8-sig" label="UTF-8（含 BOM）" /><el-option value="utf-8" label="UTF-8" /><el-option value="gb18030" label="GB18030 / GBK" /><el-option value="utf-16" label="UTF-16" /><el-option value="latin-1" label="Latin-1" /></el-select></label><label class="field"><span>CSV 分隔符</span><el-select v-model="parseOptions.delimiter" aria-label="CSV分隔符"><el-option value="auto" label="自动识别" /><el-option value="," label="逗号 ," /><el-option value=";" label="分号 ;" /><el-option value="\t" label="制表符 Tab" /><el-option value="|" label="竖线 |" /></el-select></label></template>
          <label v-if="selected.format === 'xlsx'" class="field full-field"><span>Excel 工作表</span><el-select v-model="parseOptions.sheetName" filterable allow-create default-first-option clearable placeholder="默认第一张表，也可输入工作表名" aria-label="Excel工作表"><el-option v-for="sheet in preview?.sheets || []" :key="sheet.name" :value="sheet.name" :label="`${sheet.name} · ${number(sheet.rows)} 行 × ${number(sheet.columns)} 列`" /></el-select></label>
          <label class="field full-field"><span>解析时识别为缺失的值（默认包含空字符串）</span><ScalarListInput v-model="parseOptions.missingValues" label="解析缺失标记" /></label>
        </div>
        <div v-if="fieldProfiles.length" class="field-type-overrides"><h3>字段推断与手动覆盖</h3><p class="form-note">推断来自预览样例；完整执行仍会校验全部数据。稳定 ID 保持字段引用不受最终重命名影响。</p><el-table :data="fieldProfiles" stripe><el-table-column label="字段" min-width="135"><template #default="{ row }">{{ row.name }}<small class="column-dtype">{{ row.id }}</small></template></el-table-column><el-table-column label="推断建议" min-width="100"><template #default="{ row }">{{ dataTypeLabels[row.inferredType as keyof typeof dataTypeLabels] || row.inferredType || row.dtype }}</template></el-table-column><el-table-column label="样例空值" prop="nullCount" width="90" /><el-table-column label="读取类型覆盖" min-width="155"><template #default="{ row }"><el-select :model-value="parseOptions.fieldTypes?.[row.id] || ''" clearable :disabled="busy" placeholder="保持原读取类型" :aria-label="`${row.name}读取类型`" @update:model-value="overrideType(row.id || row.name, $event)"><el-option v-for="(label, value) in dataTypeLabels" :key="value" :value="value" :label="label" /></el-select></template></el-table-column></el-table></div>
        <div class="parse-actions"><span class="form-note">{{ optionsDirty ? '解析设置有未应用的修改。已创建任务始终使用原始解析快照。' : '上传成功与解析成功分别记录；重新解析不会更改已有任务。' }}</span><el-button type="primary" :loading="parsing" :disabled="uploading || !settings" @click="reparse">应用设置并重新解析</el-button></div>
      </el-collapse-item></el-collapse>
      <el-alert v-if="optionsDirty" title="解析选项有未应用的修改，请先重新解析，再创建任务。" type="warning" :closable="false" show-icon class="panel-alert" />
      <div class="preview-area"><div class="inline-heading"><h3>数据预览</h3><div class="preview-controls"><el-input-number v-model="previewLimit" :min="1" :max="settings?.previewRows || 100" :precision="0" :controls="false" aria-label="预览行数" /><el-button size="small" :loading="previewLoading" :disabled="!parsed || busy || optionsDirty" @click="getPreview">读取预览</el-button></div></div>
        <el-alert v-if="previewError" :title="previewError" type="error" :closable="false" show-icon />
        <el-alert v-if="!parsed" title="文件尚未解析成功，调整上方解析设置后重试；原始文件仍已保存。" type="warning" :closable="false" show-icon />
        <el-skeleton v-if="previewLoading" :rows="3" animated />
        <template v-if="preview"><el-alert v-for="(warning, index) in preview.warnings || []" :key="index" :title="warning" type="warning" :closable="false" class="panel-alert" /><el-table :data="preview.rows" max-height="360" stripe empty-text="文件包含列定义，但没有数据行。"><el-table-column v-for="column in preview.columns" :key="column.id || column.name" min-width="150" show-overflow-tooltip><template #header><span>{{ column.name }}</span><small class="column-dtype">{{ column.id }} · {{ column.dtype }} · 建议 {{ dataTypeLabels[column.inferredType || 'string'] }}</small></template><template #default="{ row }"><span :class="{ 'null-cell': row[column.name] == null }">{{ cell(row[column.name]) }}</span></template></el-table-column></el-table><p class="form-note">前 {{ number(preview.previewRowCount) }} 行 · {{ preview.columns.length }} 列{{ preview.rowCount === null ? ' · 总行数将在完整执行时核验' : ' · 当前文件 ' + number(preview.rowCount) + ' 行' }}。预览不能代替完整文件容量与质量校验。</p></template>
      </div>
    </template>
  </section>
</template>
