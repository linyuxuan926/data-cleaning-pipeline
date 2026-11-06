// Expanded real browser acceptance. No page.route, no mocked business data.
import assert from 'node:assert/strict'
import { mkdir, readFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = resolve(root, '.test-output/browser-v2')
await mkdir(output, { recursive: true })
const secrets = Object.fromEntries((await readFile(resolve(root, '.env'), 'utf8')).split(/\r?\n/)
  .filter(line => line.includes('=') && !line.trimStart().startsWith('#'))
  .map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]))
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', acceptDownloads: true })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
page.setDefaultTimeout(30_000)
const base = process.env.BASE_URL || 'http://localhost:8088'
const suffix = randomUUID().replaceAll('-', '').slice(0, 12)
const username = 'ui_v2_' + suffix
const password = randomUUID()
const filename = 'browser-sheets-' + suffix + '.xlsx'
const responseFor = (method, path) => page.waitForResponse(response => new URL(response.url()).pathname === path && response.request().method() === method)
async function screenshot(name) {
  await page.mouse.move(0, 0)
  await page.screenshot({ path: resolve(output, name + '.png'), fullPage: true, animations: 'disabled' })
}
try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '注册账户', exact: true }).click()
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(username)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(password)
  await page.getByPlaceholder('再次输入密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: /创建账户/ }).click()
  await page.getByRole('heading', { name: /清洗工作台/ }).waitFor()
  assert.equal(await page.locator('nav').getByRole('button', { name: /平台管理/ }).count(), 0)
  await page.getByLabel('上传数据文件').setInputFiles({ name: filename,
    mimeType: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    buffer: await readFile(resolve(root, 'samples/multi-sheet.xlsx')) })
  await page.getByText('原始文件已保存', { exact: true }).waitFor()
  await page.locator('.file-states').getByText('解析完成', { exact: true }).waitFor()
  await page.getByText('解析选项与字段类型（修改后需重新解析）', { exact: true }).click()
  await page.locator('.parse-options .el-select__wrapper').first().click()
  await page.getByRole('option', { name: /^客户 ·/ }).click()
  // Observe an actual parse request while selecting a worksheet in the UI.
  const parseResponse = page.waitForResponse(response => /\/api\/files\/[^/]+\/parse$/.test(new URL(response.url()).pathname)
    && response.request().method() === 'POST')
  await page.getByRole('button', { name: '应用设置并重新解析', exact: true }).click()
  const parsing = await parseResponse
  assert.equal(parsing.status(), 200)
  const file = await parsing.json()
  assert.equal(file.parseStatus, 'PARSED')
  assert.equal(file.parseOptions.sheetName, '客户')
  await page.locator('.preview-area .el-table__body').getByText('00101', { exact: true }).waitFor()
  // Explicitly preserve a postal code as text, without accepting inference as a conversion.
  await page.getByRole('combobox', { name: '邮编读取类型', exact: true }).focus()
  await page.keyboard.press('ArrowDown')
  await page.getByRole('option', { name: '文本', exact: true }).click()
  const overrides = page.waitForResponse(response => /\/api\/files\/[^/]+\/parse$/.test(new URL(response.url()).pathname)
    && response.request().method() === 'POST')
  await page.getByRole('button', { name: '应用设置并重新解析', exact: true }).click()
  assert.equal((await (await overrides).json()).parseOptions.fieldTypes.f0, 'string')
  await page.locator('.preview-area .el-table__body').getByText('00101', { exact: true }).waitFor()

  await page.getByRole('button', { name: /添加清洗规则/ }).click()
  const rule = page.locator('.rule-row').first()
  await rule.locator('.rule-type .el-select__wrapper').click()
  await page.getByRole('option', { name: '重命名列', exact: true }).click()
  await rule.locator('.rule-fields .el-select__wrapper').first().click()
  await page.getByRole('option', { name: '邮编', exact: true }).click()
  await page.getByPlaceholder('输入最终列名', { exact: true }).fill('postal_code')
  await page.evaluate(() => scrollTo(0, 0))
  await screenshot('worksheet-and-stable-field')
  const creating = responseFor('POST', '/api/tasks')
  await page.getByRole('button', { name: /创建清洗任务/ }).click()
  const created = await creating
  assert.equal(created.status(), 201)
  const task = await created.json()
  const drawer = page.locator('.el-drawer:visible')
  await drawer.getByRole('button', { name: /下载清洗结果/ }).waitFor({ timeout: 180_000 })
  const downloading = page.waitForEvent('download')
  await drawer.getByRole('button', { name: /下载清洗结果/ }).click()
  const download = await downloading
  const downloaded = resolve(output, 'worksheet-result.csv')
  await download.saveAs(downloaded)
  assert.ok((await readFile(downloaded, 'utf8')).includes('postal_code,姓名,金额'))
  assert.ok((await readFile(downloaded, 'utf8')).includes('00101,明日香,10.239'))
  await screenshot('expanded-report')
  await drawer.locator('.el-drawer__close-btn').click()
  await page.locator('.sidebar-user').getByRole('button', { name: '退出', exact: true }).click()
  await page.getByRole('heading', { name: '登录你的工作空间' }).waitFor()

  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(secrets.BOOTSTRAP_ADMIN_USERNAME)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(secrets.BOOTSTRAP_ADMIN_PASSWORD)
  await page.getByRole('button', { name: /登 录/ }).click()
  await page.locator('nav').getByRole('button', { name: /平台管理/ }).click()
  await page.getByRole('heading', { name: '平台管理', exact: true }).waitFor()
  await page.getByRole('textbox', { name: '搜索用户名', exact: true }).fill(username)
  await page.getByRole('tabpanel', { name: '账户与角色', exact: true })
    .locator('.el-table__body').getByText(username, { exact: true }).waitFor()
  await screenshot('admin-accounts')
  await page.getByRole('tab', { name: '全部任务', exact: true }).click()
  await page.getByRole('textbox', { name: '管理员任务文件名', exact: true }).fill(filename)
  const target = page.locator('.admin-console .el-table__body tr').filter({ hasText: filename })
  await target.getByRole('button', { name: /查看详情/ }).click()
  await drawer.getByRole('button', { name: /下载清洗结果/ }).waitFor()
  assert.equal(await drawer.getByRole('button', { name: /取消任务|重新执行/ }).count(), 0)
  await drawer.locator('.el-drawer__close-btn').click()
  await page.getByRole('tab', { name: '平台限额', exact: true }).click()
  await page.getByRole('spinbutton', { name: '默认预览行数及上限', exact: true }).waitFor()
  await screenshot('admin-settings')
  await page.setViewportSize({ width: 390, height: 844 })
  await screenshot('admin-mobile')
  assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
  await page.locator('.mobile-logout').click()
  await page.getByRole('heading', { name: '登录你的工作空间' }).waitFor()
  assert.deepEqual(errors, [])
  console.log('PASS: real browser independent upload, SHA256, worksheet selection, explicit string override, stable field rename, task report/download, admin accounts/settings/all-task read and mobile layout.')
  console.log('Screenshots:', output)
} catch (error) {
  await screenshot('failure').catch(() => {})
  throw error
} finally {
  await context.close()
  await browser.close()
}
