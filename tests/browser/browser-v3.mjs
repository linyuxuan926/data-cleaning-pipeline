// Real browser -> Nginx -> Java -> RabbitMQ -> Worker -> MinIO. No route mocks.
// The actual API seeds saved rules; upload, task submission, previews, report,
// authenticated downloads and attempt selection are exercised through the UI.
import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = resolve(root, '.test-output/browser-v3')
const base = process.env.BASE_URL || 'http://localhost:8088'
const suffix = randomUUID().replaceAll('-', '').slice(0, 12)
const filename = `browser-v3-${suffix}.csv`
const ruleName = `Browser V3 ${suffix}`
const csv = [
  'id,name,amount,state,category,required_a,required_b',
  'A, Alice ,10, BAD , BAD ,x,x',
  'B,Bob,,OK,OK,x,x',
  'C, Carol ,bad,BAD,BAD,x,x',
  'D, Dan ,1000,OK,OK,x,x',
  'A,Alice last,12,OK,OK,x,x',
  'E, Eve ,30,OK,OK,,',
  'F, Frank ,20, BAD , BAD ,x,x',
  'G, Grace ,40,OK,OK,x,x',
  '',
].join('\n')
const rules = [
  { type: 'trim', columns: ['f1', 'f3', 'f4'] },
  { type: 'cast', columns: ['f2'], dtype: 'number', precision: 2, onError: 'drop' },
  { type: 'fill_stat', columns: ['f2'], strategy: 'mean', emptyPolicy: 'fail' },
  { type: 'range', column: 'f2', min: 0, max: 200, onError: 'drop' },
  { type: 'required', columns: ['f5', 'f6'], onError: 'drop' },
  { type: 'enum', column: 'f3', values: ['OK'], onError: 'mark' },
  { type: 'enum', column: 'f4', values: ['OK'], onError: 'mark' },
  { type: 'deduplicate', columns: ['f0'], keep: 'last', onError: 'drop' },
]
const expected = {
  inputRows: 8, outputRows: 4, validOutputRows: 3, flaggedOutputRows: 1,
  finalRemovedRows: 4, issueRows: 5, issueCount: 9, duplicateRemovedRows: 1,
  outlierHandledRows: 1, missingFilledCells: 1, typeConversionFailedCells: 1,
}
const metricLabels = {
  inputRows: '输入行数', outputRows: '输出行数', validOutputRows: '有效输出行数',
  flaggedOutputRows: '标记输出行数', finalRemovedRows: '最终移除行数',
  issueRows: '含违规的唯一行数', issueCount: '违规明细条数',
  duplicateRemovedRows: '重复移除行数', outlierHandledRows: '异常处理行数',
  missingFilledCells: '缺失填充单元格', typeConversionFailedCells: '类型转换失败单元格',
}

await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({
  viewport: { width: 1440, height: 1000 }, locale: 'zh-CN',
  timezoneId: 'Asia/Shanghai', acceptDownloads: true,
})
const page = await context.newPage()
page.setDefaultTimeout(30_000)
const runtimeErrors = []
const badResponses = []
const observedRequests = []
page.on('pageerror', error => runtimeErrors.push(error.message))
page.on('request', request => {
  const url = new URL(request.url())
  if (url.pathname.startsWith('/api/')) observedRequests.push(`${request.method()} ${url.pathname}${url.search}`)
})
page.on('response', response => {
  const url = new URL(response.url())
  if (url.pathname.startsWith('/api/') && response.status() >= 400)
    badResponses.push(`${response.request().method()} ${url.pathname}: ${response.status()}`)
})
const responseFor = (method, path, kind) => page.waitForResponse(response => {
  const url = new URL(response.url())
  return url.pathname === path && response.request().method() === method
    && (kind === undefined || url.searchParams.get('kind') === kind)
})
async function api(path, method = 'GET', body) {
  const result = await page.evaluate(async ({ path, method, body }) => {
    const bearer = sessionStorage.getItem('dataflow.session')
    const response = await fetch(`/api${path}`, {
      method,
      headers: { Authorization: `Bearer ${bearer}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    return { status: response.status, body: await response.json() }
  }, { path, method, body })
  assert.ok(result.status >= 200 && result.status < 300, `${method} ${path}: ${result.status} ${JSON.stringify(result.body)}`)
  return result.body
}
async function screenshot(name) {
  await page.mouse.move(0, 0)
  await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true, animations: 'disabled' })
}
async function download(drawer, label, name) {
  const downloading = page.waitForEvent('download')
  await drawer.getByRole('button', { name: label }).click()
  const result = await downloading
  const path = resolve(output, name)
  await result.saveAs(path)
  return readFile(path, 'utf8')
}
async function refreshTasks() {
  const refreshed = responseFor('GET', '/api/tasks/page')
  await page.locator('.topbar').getByRole('button', { name: '刷新', exact: true }).click()
  assert.equal((await refreshed).status(), 200)
}
async function waitForTask(id, status, attempt) {
  const deadline = Date.now() + 180_000
  while (Date.now() < deadline) {
    const task = await api(`/tasks/${id}`)
    if (task.status === status && (attempt === undefined || task.attempt === attempt)) return task
    if (['FAILED', 'CANCELED', 'SUCCEEDED'].includes(task.status) && task.status !== status)
      throw new Error(`Task ${id} reached ${task.status}: ${task.error || ''}`)
    await new Promise(resolve => setTimeout(resolve, 1000))
  }
  throw new Error(`Task ${id} did not reach ${status} / attempt ${attempt} before timeout`)
}

try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '注册账户', exact: true }).click()
  const password = `BrowserV3_${randomUUID()}`
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(`ui_v3_${suffix}`)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(password)
  await page.getByPlaceholder('再次输入密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: /创建账户/ }).click()
  await page.getByRole('heading', { name: /清洗工作台/ }).waitFor()
  await page.getByText('v3-disk · 按配置顺序执行', { exact: true }).waitFor()

  const completed = page.waitForResponse(response => /\/api\/uploads\/[^/]+\/complete$/.test(new URL(response.url()).pathname)
    && response.request().method() === 'POST')
  await page.getByLabel('上传数据文件').setInputFiles({ name: filename, mimeType: 'text/csv', buffer: Buffer.from(csv) })
  const uploadedResponse = await completed
  assert.equal(uploadedResponse.status(), 200)
  const file = await uploadedResponse.json()
  assert.equal(file.parseStatus, 'PARSED')
  await page.locator('.preview-area .el-table__body').getByText('Carol', { exact: false }).waitFor()

  const saved = await api('/rule-sets', 'POST', { name: ruleName, rules })
  const rulesRefreshed = responseFor('GET', '/api/rule-sets/page')
  await page.locator('.topbar').getByRole('button', { name: '刷新', exact: true }).click()
  assert.equal((await rulesRefreshed).status(), 200)
  await page.locator('.rule-library-picker .el-select__wrapper').first().click()
  await page.getByRole('option', { name: `${ruleName} · v${saved.version}`, exact: true }).click()
  assert.equal(await page.locator('.rule-row').count(), 8)
  const submitting = responseFor('POST', '/api/tasks')
  await page.getByRole('button', { name: /创建清洗任务/ }).click()
  const submittedResponse = await submitting
  assert.equal(submittedResponse.status(), 201)
  assert.ok(submittedResponse.request().headers()['idempotency-key'], 'UI submission must send Idempotency-Key')
  const task = await submittedResponse.json()
  assert.equal(task.engineVersion, 'v3-sequential')
  const attemptId = task.currentAttemptId || task.attemptId
  assert.ok(attemptId)
  const attemptBase = `/api/tasks/${task.id}/attempts/${attemptId}`
  const drawer = page.locator('.el-drawer:visible')
  await drawer.getByRole('button', { name: /下载清洗结果/ }).waitFor({ timeout: 180_000 })
  await drawer.getByRole('tab', { name: '清洗报告', exact: true }).click()
  const reportPanel = drawer.getByRole('tabpanel', { name: '清洗报告', exact: true })
  await reportPanel.locator('.report-metrics').waitFor()
  for (const [key, value] of Object.entries(expected)) {
    const metric = reportPanel.locator('.report-metrics > div').filter({ has: page.getByText(metricLabels[key], { exact: true }) }).locator('strong')
    assert.equal((await metric.innerText()).replaceAll(',', '').trim(), String(value), `UI metric ${key}`)
  }
  const report = JSON.parse(await download(drawer, 'JSON 报告', 'report.json'))
  for (const [key, value] of Object.entries(expected)) assert.equal(report[key], value, `Downloaded report ${key}`)
  assert.deepEqual(report.executionOrder, rules.map((_, index) => `rule-${index + 1}`))
  assert.deepEqual(report.finalRemovalByRule, { 'rule-2': 1, 'rule-4': 1, 'rule-5': 1, 'rule-8': 1 })
  const ruleRows = reportPanel.locator('.el-table').first().locator('.el-table__body tr')
  assert.equal(await ruleRows.count(), 8)
  for (let index = 0; index < 8; index++)
    assert.equal((await ruleRows.nth(index).locator('td').nth(2).innerText()).trim(), String(index + 1), 'V3 execution step must use actual rule order')
  const attributionRows = reportPanel.locator('.el-table').nth(1).locator('.el-table__body tr')
  assert.equal(await attributionRows.count(), 4)
  for (const ruleId of Object.keys(report.finalRemovalByRule)) await attributionRows.getByText(ruleId, { exact: true }).waitFor()
  await screenshot('report-counters-and-removal-attribution')

  const resultResponse = responseFor('GET', `${attemptBase}/preview`, 'result')
  await drawer.getByRole('tab', { name: '结果预览', exact: true }).click()
  const resultPreviewResponse = await resultResponse
  assert.equal(resultPreviewResponse.status(), 200)
  const resultPreview = await resultPreviewResponse.json()
  assert.equal(resultPreview.previewRowCount, 4)
  assert.deepEqual(resultPreview.rows.map(row => row.id), ['B', 'A', 'F', 'G'])
  const resultPanel = drawer.getByRole('tabpanel', { name: '结果预览', exact: true })
  await resultPanel.getByText('Frank', { exact: true }).waitFor()
  assert.equal(await resultPanel.locator('.el-table__body tr').count(), 4)
  const flaggedRow = resultPanel.locator('.el-table__body tr').filter({ has: page.getByText('Frank', { exact: true }) })
  await flaggedRow.getByRole('button', { name: '查看明细', exact: true }).click()
  const qualityDetail = page.locator('.el-popover:visible .quality-error-detail')
  await qualityDetail.waitFor()
  const qualityIssues = JSON.parse(await qualityDetail.innerText())
  assert.equal(qualityIssues.length, 2)
  assert.deepEqual(qualityIssues.map(issue => issue.ruleId), ['rule-6', 'rule-7'])
  await screenshot('actual-result-preview-and-quality-errors')
  await drawer.getByRole('heading', { name: filename, exact: true }).click()

  const dirtyResponse = responseFor('GET', `${attemptBase}/preview`, 'dirty')
  await drawer.getByRole('tab', { name: '违规明细', exact: true }).click()
  const dirtyPreviewResponse = await dirtyResponse
  assert.equal(dirtyPreviewResponse.status(), 200)
  const dirtyPreview = await dirtyPreviewResponse.json()
  assert.equal(dirtyPreview.previewRowCount, 9)
  assert.ok(dirtyPreview.columns.some(column => column.name === '_final_action'))
  assert.ok(dirtyPreview.rows.every(row => row._file_id === file.id))
  const earlyMarks = dirtyPreview.rows.filter(row => Number(row._source_row) === 1)
  assert.deepEqual(earlyMarks.map(row => row._final_action), ['REMOVED_BY_LATER_RULE', 'REMOVED_BY_LATER_RULE', 'REMOVED_BY_THIS_RULE'])
  const dirtyPanel = drawer.getByRole('tabpanel', { name: '违规明细', exact: true })
  await dirtyPanel.locator('.el-table__body tr').first().waitFor()
  assert.equal(await dirtyPanel.locator('.el-table__body tr').count(), 9)
  await screenshot('actual-dirty-preview')
  await writeFile(resolve(output, 'result-preview.json'), JSON.stringify(resultPreview, null, 2))
  await writeFile(resolve(output, 'dirty-preview.json'), JSON.stringify(dirtyPreview, null, 2))
  const resultCsv = await download(drawer, /下载清洗结果/, 'result.csv')
  assert.ok(resultCsv.includes('Alice last'), 'keep-last result must retain the later duplicate')
  assert.ok(!resultCsv.includes('Carol'), 'failed cast row must be removed')

  await drawer.getByRole('tab', { name: '执行尝试', exact: true }).click()
  const attemptsPanel = drawer.getByRole('tabpanel', { name: '执行尝试', exact: true })
  await attemptsPanel.getByText(attemptId, { exact: true }).waitFor()
  assert.equal(await attemptsPanel.getByRole('button', { name: '查看中', exact: true }).count(), 1)
  const logsResponse = responseFor('GET', `${attemptBase}/logs/page`)
  await drawer.getByRole('tab', { name: '执行日志', exact: true }).click()
  await drawer.getByRole('button', { name: '刷新日志', exact: true }).click()
  assert.equal((await logsResponse).status(), 200)
  assert.ok((await drawer.getByRole('log').innerText()).includes('Worker'))
  await screenshot('attempt-and-real-logs')
  await drawer.locator('.el-drawer__close-btn').click()

  // A deterministic failed task exercises immutable attempt history and real UI retry.
  const failureRules = await api('/rule-sets', 'POST', {
    name: `Browser attempt failure ${suffix}`,
    rules: [{ type: 'cast', columns: ['f2'], dtype: 'number', onError: 'fail' }],
  })
  const failed = await api('/tasks', 'POST', { fileId: file.id, ruleSetId: failureRules.id, ruleVersion: failureRules.version, outputFormat: 'csv' })
  const firstFailure = await waitForTask(failed.id, 'FAILED', 1)
  const firstAttemptId = firstFailure.currentAttemptId || firstFailure.attemptId
  await refreshTasks()
  await page.locator('.tasks-panel .el-table__body tr').filter({ hasText: failed.id.slice(0, 8) }).getByRole('button', { name: /查看详情/ }).click()
  await drawer.getByRole('button', { name: '重新执行', exact: true }).waitFor()
  const retrying = responseFor('POST', `/api/tasks/${failed.id}/retry`)
  await drawer.getByRole('button', { name: '重新执行', exact: true }).click()
  const retriedResponse = await retrying
  assert.equal(retriedResponse.status(), 200)
  assert.ok(retriedResponse.request().headers()['idempotency-key'], 'UI retry must send persistent Idempotency-Key')
  const retried = await retriedResponse.json()
  const secondAttemptId = retried.currentAttemptId || retried.attemptId
  assert.notEqual(secondAttemptId, firstAttemptId)
  await waitForTask(failed.id, 'FAILED', 2)
  await drawer.getByRole('button', { name: '重新执行', exact: true }).waitFor()
  await drawer.getByRole('tab', { name: '执行尝试', exact: true }).click()
  const historyPanel = drawer.getByRole('tabpanel', { name: '执行尝试', exact: true })
  await historyPanel.getByText(firstAttemptId, { exact: true }).waitFor()
  assert.equal(await historyPanel.locator('.el-table__body tr').count(), 2)
  const historyLogs = responseFor('GET', `/api/tasks/${failed.id}/attempts/${firstAttemptId}/logs/page`)
  await historyPanel.locator('.el-table__body tr').filter({ hasText: firstAttemptId }).getByRole('button', { name: '查看', exact: true }).click()
  const oldLogsResponse = await historyLogs
  assert.equal(oldLogsResponse.status(), 200)
  const oldLogs = (await oldLogsResponse.json()).items
  assert.ok(oldLogs.some(log => log.message.includes('任务已排队')))
  assert.ok(oldLogs.every(log => !log.message.includes('用户重试任务')))
  await drawer.locator('.selected-attempt-context').getByText('历史执行', { exact: true }).waitFor()
  assert.equal(await drawer.getByRole('button', { name: /重新执行|取消任务|下载清洗结果/ }).count(), 0)
  await drawer.getByRole('tab', { name: '执行日志', exact: true }).click()
  assert.ok(!(await drawer.getByRole('log').innerText()).includes('用户重试任务'))
  await screenshot('historical-attempt-isolated-logs')
  await drawer.getByRole('tab', { name: '执行尝试', exact: true }).click()
  const currentLogs = responseFor('GET', `/api/tasks/${failed.id}/attempts/${secondAttemptId}/logs/page`)
  await historyPanel.locator('.el-table__body tr').filter({ hasText: secondAttemptId }).getByRole('button', { name: '查看', exact: true }).click()
  assert.ok((await (await currentLogs).json()).items.some(log => log.message.includes('用户重试任务')))
  await drawer.locator('.selected-attempt-context').getByText('当前执行', { exact: true }).waitFor()
  await drawer.getByRole('button', { name: '重新执行', exact: true }).waitFor()

  assert.deepEqual(runtimeErrors, [], 'Browser runtime errors')
  assert.deepEqual(badResponses, [], 'Failed business HTTP requests')
  await writeFile(resolve(output, 'observed-api-requests.json'), JSON.stringify(observedRequests, null, 2))
  console.log('PASS: real V3 UI submission, report counters/removal attribution, result/dirty artifact previews, quality details, download, attempt retry and isolated history logs.')
  console.log('Task:', task.id, 'History task:', failed.id, 'Evidence:', output)
} catch (error) {
  await screenshot('failure').catch(() => {})
  await writeFile(resolve(output, 'observed-api-requests.json'), JSON.stringify(observedRequests, null, 2)).catch(() => {})
  throw error
} finally {
  await context.close()
  await browser.close()
}
