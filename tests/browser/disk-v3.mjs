// Actual browser, API, disk Worker and object storage. No mocked routes.
import assert from 'node:assert/strict'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = resolve(root, '.test-output/browser-disk-v3')
const suffix = randomUUID().replaceAll('-', '').slice(0, 10)
const base = process.env.BASE_URL || 'http://localhost:8088'
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', acceptDownloads: true })
const page = await context.newPage()
page.setDefaultTimeout(30_000)
const errors = [], requests = [], badResponses = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => { const url = new URL(request.url()); if (url.pathname.startsWith('/api/')) requests.push(`${request.method()} ${url.pathname}${url.search}`) })
page.on('response', response => { if (new URL(response.url()).pathname.startsWith('/api/') && response.status() >= 400) badResponses.push(`${response.status()} ${response.url()}`) })
const responseFor = (method, path, predicate = () => true) => page.waitForResponse(response => { const url = new URL(response.url()); return url.pathname === path && response.request().method() === method && predicate(url) })
async function api(path, method = 'GET', body) {
  const value = await page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(`/api${path}`, { method, headers: { Authorization: `Bearer ${sessionStorage.getItem('dataflow.session')}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
    return { status: response.status, body: await response.json() }
  }, { path, method, body })
  assert.ok(value.status >= 200 && value.status < 300, `${method} ${path}: ${value.status} ${JSON.stringify(value.body)}`)
  return value.body
}
async function screenshot(name) { await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: true, animations: 'disabled' }) }
async function download(drawer, label, filename) { const pending = page.waitForEvent('download'); await drawer.getByRole('button', { name: label }).click(); const result = await pending; const target = resolve(output, filename); await result.saveAs(target); return readFile(target, 'utf8') }

try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '注册账户', exact: true }).click()
  const password = `BrowserDisk_${randomUUID()}`
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(`disk_ui_${suffix}`)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(password)
  await page.getByPlaceholder('再次输入密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: /创建账户/ }).click()
  await page.getByText('已验证服务端 Schema 3', { exact: false }).waitFor()
  const csv = ['id,name,amount,ceiling', 'A,  foofoo  ,1.005,1.01', 'B,bar,2.675,2.67', 'C,foo,,5', 'D,zap,invalid,10', 'E,foo,922337203685477.125,922337203685477.13', ''].join('\n')
  const uploaded = page.waitForResponse(response => /\/api\/uploads\/[^/]+\/complete$/.test(new URL(response.url()).pathname) && response.request().method() === 'POST')
  await page.getByLabel('上传数据文件').setInputFiles({ name: `disk-ui-${suffix}.csv`, mimeType: 'text/csv', buffer: Buffer.from(csv) })
  const file = await (await uploaded).json()
  assert.equal(file.parseStatus, 'PARSED')
  await page.getByRole('heading', { name: '数据预览', exact: true }).waitFor()
  const logicalFields = ['id', 'name', 'amount', 'ceiling'].map(id => ({ id, label: id }))
  const fieldBindings = { id: 'f0', name: 'f1', amount: 'f2', ceiling: 'f3' }
  const rules = [
    { id: 'trim_name', type: 'trim', enabled: true, fieldIds: ['name'] },
    { id: 'replace_name', type: 'replace', enabled: true, fieldIds: ['name'], search: 'foo', replacement: 'X', mode: 'literal', count: 1 },
    { id: 'decimal_cast', type: 'cast', enabled: true, fieldIds: ['amount', 'ceiling'], dtype: 'decimal', precision: 38, scale: 2, rounding: 'HALF_UP', onError: 'drop' },
    { id: 'fill_amount', type: 'fill_null', enabled: true, fieldIds: ['amount'], value: { type: 'decimal', value: '0.10', scale: 2 } },
    { id: 'compare_limit', type: 'compare_fields', enabled: true, leftFieldId: 'amount', rightFieldId: 'ceiling', operator: 'lte', valueType: 'decimal', nullPolicy: 'violation', onError: 'mark' },
    { id: 'rename_amount', type: 'rename', enabled: true, fieldId: 'amount', target: 'total' },
    { id: 'disabled_replace', type: 'replace', enabled: false, fieldIds: ['name'], search: 'X', replacement: 'Y', mode: 'literal', count: 0 },
  ]
  await page.getByRole('button', { name: '导入 / 导出规则 JSON', exact: true }).click()
  await page.getByRole('textbox', { name: '规则JSON', exact: true }).fill(JSON.stringify({ schemaVersion: 3, engineVersion: 'v3-disk', logicalFields, rules, fieldBindings }))
  await page.getByRole('button', { name: '载入当前草稿', exact: true }).click()
  await page.locator('.rule-name-field input').fill(`Disk rules ${suffix}`)
  const checked = responseFor('POST', '/api/rules/preflight')
  await page.getByRole('button', { name: '预检规则与字段', exact: true }).click()
  assert.equal((await (await checked).json()).valid, true)
  await screenshot('disk-editor-bindings-and-decimal')
  const submitted = responseFor('POST', '/api/tasks')
  await page.getByRole('button', { name: /创建清洗任务/ }).click()
  const submitResponse = await submitted
  assert.equal(submitResponse.status(), 201)
  assert.ok(submitResponse.request().headers()['idempotency-key'])
  const task = await submitResponse.json()
  assert.equal(task.engineVersion, 'v3-disk')
  assert.equal(task.schemaVersion, 3)
  const drawer = page.locator('.el-drawer:visible')
  await drawer.getByRole('button', { name: /下载清洗结果/ }).waitFor({ timeout: 180_000 })
  const report = JSON.parse(await download(drawer, 'JSON 报告', 'report.json'))
  assert.equal(report.inputRows, 5)
  assert.equal(report.outputRows, 4)
  assert.equal(report.flaggedOutputRows, 1)
  assert.equal(report.finalRemovedRows, 1)
  assert.equal(report.rules.find(rule => rule.ruleId === 'disabled_replace').status, 'DISABLED')
  assert.deepEqual(report.executionOrder, rules.filter(rule => rule.enabled).map(rule => rule.id))
  const result = await download(drawer, /下载清洗结果/, 'result.csv')
  assert.ok(result.includes('total'), result)
  assert.ok(result.includes('A,Xfoo,1.01,1.01'), result)
  assert.ok(result.includes('B,bar,2.68,2.67'), result)
  assert.ok(result.includes('C,X,0.10,5.00'), result)
  assert.ok(result.includes('E,X,922337203685477.13,922337203685477.13'), result)
  assert.ok(!result.includes('Y'), 'Disabled replace must not run')
  await drawer.getByRole('tab', { name: '执行快照', exact: true }).click()
  await drawer.getByText('逻辑字段与冻结绑定', { exact: true }).waitFor()
  await screenshot('disk-report-snapshot')
  await drawer.locator('.el-drawer__close-btn').click()

  // Insert 23 genuine saved rule sets. A later insert must not shift the frozen second page.
  const prefix = `cursor_${suffix}`
  for (let index = 0; index < 23; index++) await api('/rule-sets', 'POST', { name: `${prefix}_${index}`, schemaVersion: 3, engineVersion: 'v3-disk', logicalFields, rules: [] })
  await page.locator('nav').getByRole('button', { name: /规则库/ }).click()
  const firstPending = responseFor('GET', '/api/rule-sets/page', url => url.searchParams.get('q') === prefix)
  await page.getByRole('textbox', { name: '搜索规则集名称', exact: true }).fill(prefix)
  const firstPage = await (await firstPending).json()
  assert.equal(firstPage.items.length, 20)
  assert.equal(firstPage.hasMore, true)
  await page.locator('.rules-library article').first().waitFor()
  assert.equal(await page.locator('.rules-library article').count(), 20)
  await api('/rule-sets', 'POST', { name: `${prefix}_inserted_later`, schemaVersion: 3, engineVersion: 'v3-disk', logicalFields, rules: [] })
  const nextPending = responseFor('GET', '/api/rule-sets/page', url => !!url.searchParams.get('cursor'))
  await page.locator('.main-content .cursor-pager').getByRole('button', { name: '下一页', exact: true }).click()
  const nextPage = await (await nextPending).json()
  assert.equal(nextPage.items.length, 3)
  assert.equal(nextPage.asOf, firstPage.asOf)
  assert.equal(new Set([...firstPage.items, ...nextPage.items].map(item => item.id)).size, 23)
  assert.ok(!nextPage.items.some(item => item.name.endsWith('inserted_later')))
  const resetPending = responseFor('GET', '/api/rule-sets/page', url => url.searchParams.get('q') === `${prefix}_missing`)
  await page.getByRole('textbox', { name: '搜索规则集名称', exact: true }).fill(`${prefix}_missing`)
  const resetResponse = await resetPending
  assert.equal(new URL(resetResponse.url()).searchParams.has('cursor'), false)
  assert.equal((await resetResponse.json()).items.length, 0)
  assert.equal(await page.locator('.main-content .cursor-pager').getByRole('button', { name: '上一页', exact: true }).isDisabled(), true)
  await screenshot('cursor-filter-reset')

  await page.locator('.sidebar-user').getByRole('button', { name: '退出', exact: true }).click()
  const secrets = Object.fromEntries((await readFile(resolve(root, '.env'), 'utf8')).split(/\r?\n/).filter(line => line.includes('=') && !line.trimStart().startsWith('#')).map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]))
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(secrets.BOOTSTRAP_ADMIN_USERNAME)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(secrets.BOOTSTRAP_ADMIN_PASSWORD)
  await page.getByRole('button', { name: /登 录/ }).click()
  await page.locator('nav').getByRole('button', { name: /平台管理/ }).click()
  await page.getByRole('tab', { name: '保留期与清理', exact: true }).click()
  await page.getByRole('switch', { name: '启用保留期清理', exact: true }).waitFor({ state: 'attached' })
  await page.getByRole('button', { name: '保存保留期策略', exact: true }).waitFor()
  const baselinePolicy = await api('/admin/retention/policy')
  assert.equal(baselinePolicy.enabled, false)
  await page.getByRole('textbox', { name: '清理任务ID', exact: true }).fill(task.id)
  await page.getByRole('textbox', { name: '清理文件ID', exact: true }).fill(file.id)
  const planPending = responseFor('POST', '/api/admin/retention/plans')
  await page.getByRole('button', { name: '生成预览计划', exact: true }).click()
  const plan = await (await planPending).json()
  assert.equal(plan.state, 'DRY_RUN')
  assert.equal(await page.getByRole('button', { name: '正式执行计划', exact: true }).isDisabled(), true)
  const historyPending = responseFor('GET', '/api/admin/retention/plans/page')
  await page.locator('.retention-console .cursor-pager').first().getByRole('button', { name: '刷新本页', exact: true }).click()
  assert.ok((await (await historyPending).json()).items.some(item => item.id === plan.id))
  assert.deepEqual(await api('/admin/retention/policy'), baselinePolicy)
  assert.equal(requests.some(request => /\/admin\/retention\/plans\/[^/]+\/(execute|retry)/.test(request)), false)
  await screenshot('retention-dry-run-history')
  assert.deepEqual(errors, [], 'Browser runtime errors')
  assert.deepEqual(badResponses, [], 'Failed business requests')
  await writeFile(resolve(output, 'observed-api-requests.json'), JSON.stringify(requests, null, 2))
  console.log('PASS: v3-disk JSON editing, logical bindings, Decimal HALF_UP, replace, compare_fields, disabled rules, immutable snapshots, stable cursor history, filter reset and retention dry-run without enabling deletion.')
  console.log('Task:', task.id, 'Plan:', plan.id, 'Evidence:', output)
} catch (error) { await screenshot('failure').catch(() => {}); await writeFile(resolve(output, 'observed-api-requests.json'), JSON.stringify(requests, null, 2)).catch(() => {}); throw error }
finally { await context.close(); await browser.close() }
