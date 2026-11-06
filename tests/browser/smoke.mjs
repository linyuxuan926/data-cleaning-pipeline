// Real browser -> Nginx -> Java -> RabbitMQ -> Worker -> MinIO. No route fixtures.
import assert from 'node:assert/strict'
import { mkdir, readFile } from 'node:fs/promises'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = resolve(root, '.test-output/browser')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', acceptDownloads: true })
const page = await context.newPage()
page.setDefaultTimeout(30_000)
const errors = []
page.on('pageerror', error => errors.push(error.message))
const badResponses = []
page.on('response', response => {
  if (new URL(response.url()).pathname.startsWith('/api/') && response.status() >= 400)
    badResponses.push(`${response.request().method()} ${new URL(response.url()).pathname}: ${response.status()}`)
})
try {
  await page.goto(process.env.BASE_URL || 'http://localhost:8088', { waitUntil: 'networkidle' })
  await page.screenshot({ path: resolve(output, 'login-desktop.png'), fullPage: true, animations: 'disabled' })
  await page.getByRole('button', { name: '注册账户', exact: true }).click()
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill('browser_' + randomUUID().replaceAll('-', '').slice(0, 12))
  const password = 'Browser_' + randomUUID()
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(password)
  await page.getByPlaceholder('再次输入密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: /创建账户/ }).click()
  await page.getByRole('heading', { name: /清洗工作台/ }).waitFor()
  await page.getByLabel('上传数据文件').setInputFiles(resolve(root, 'samples/customers.csv'))
  await page.getByRole('heading', { name: '数据预览', exact: true }).waitFor()
  await page.locator('.preview-area .el-table__body').getByText('Alice', { exact: true }).waitFor()
  await page.getByRole('button', { name: /添加清洗规则/ }).click()
  await page.locator('.rule-row .rule-fields .el-select__wrapper').first().click()
  await page.getByRole('option', { name: 'name', exact: true }).click()
  await page.keyboard.press('Escape')
  await page.evaluate(() => window.scrollTo(0, 0))
  await page.screenshot({ path: resolve(output, 'workspace-desktop.png'), fullPage: true, animations: 'disabled' })
  const created = page.waitForResponse(response => new URL(response.url()).pathname === '/api/tasks' && response.request().method() === 'POST')
  await page.getByRole('button', { name: /创建清洗任务/ }).click()
  const response = await created
  assert.equal(response.status(), 201)
  const task = await response.json()
  console.log('Created real task', task.id)
  const drawer = page.locator('.el-drawer:visible')
  await drawer.getByRole('button', { name: /下载清洗结果/ }).waitFor({ timeout: 180_000 })
  assert.ok((await drawer.locator('.report-metrics').innerText()).includes('6'))
  await drawer.getByRole('tab', { name: '执行日志', exact: true }).click()
  assert.ok((await drawer.getByRole('log').innerText()).length > 10)
  await drawer.getByRole('tab', { name: '清洗报告', exact: true }).click()
  await page.screenshot({ path: resolve(output, 'report-desktop.png'), fullPage: true, animations: 'disabled' })
  const downloadEvent = page.waitForEvent('download')
  await drawer.getByRole('button', { name: /下载清洗结果/ }).click()
  const download = await downloadEvent
  const downloadedPath = resolve(output, 'browser-result.csv')
  await download.saveAs(downloadedPath)
  const csv = await readFile(downloadedPath, 'utf8')
  assert.ok(csv.includes('1,Alice,ALICE@example.com,28,Shanghai'), csv)
  await drawer.locator('.el-drawer__close-btn').click()
  // Move back to the workbench after createTask switches the main view to tasks.
  await page.locator('nav').getByRole('button', { name: /清洗工作台/ }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: resolve(output, 'workspace-mobile.png'), fullPage: true, animations: 'disabled' })
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), 'Mobile page overflows horizontally')
  await page.locator('.mobile-logout').click()
  await page.getByRole('heading', { name: '登录你的工作空间' }).waitFor()
  assert.deepEqual(errors, [], 'Browser runtime errors')
  assert.deepEqual(badResponses, [], 'Failed business HTTP requests')
  console.log('PASS: real browser registration, upload, preview, rule editing, async report, authenticated download and mobile logout.')
  console.log('Screenshots:', output)
} catch (error) {
  await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {})
  throw error
} finally {
  await context.close()
  await browser.close()
}
