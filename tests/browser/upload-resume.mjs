// Real checkpoint recovery after a client network failure. No request routes are
// mocked: accepted chunks are persisted by Java, then this browser goes offline.
import assert from 'node:assert/strict'
import { mkdir, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createHash, randomUUID } from 'node:crypto'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const output = resolve(root, '.test-output/browser-v3-upload-resume')
const suffix = randomUUID().replaceAll('-', '').slice(0, 12)
const filename = `browser-resume-${suffix}.csv`
const value = 'x'.repeat(100)
const buffer = Buffer.from(`record_id,payload\n${Array.from({ length: 125_000 }, (_, index) => `${index + 1},${value}`).join('\n')}\n`)
const sha256 = createHash('sha256').update(buffer).digest('hex')
const checkpointKey = `dataflow.upload.${filename}:${buffer.length}:${sha256}`
const file = { name: filename, mimeType: 'text/csv', buffer }
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(60_000)
const runtimeErrors = []
const requests = []
const failedRequests = []
const badResponses = []
let resuming = false
const resumedChunkIndices = []
page.on('pageerror', error => runtimeErrors.push(error.message))
page.on('request', request => {
  const path = new URL(request.url()).pathname
  if (!path.startsWith('/api/')) return
  requests.push(`${request.method()} ${path}`)
  const match = path.match(/^\/api\/uploads\/[^/]+\/chunks\/(\d+)$/)
  if (resuming && request.method() === 'PUT' && match) resumedChunkIndices.push(Number(match[1]))
})
page.on('requestfailed', request => {
  const path = new URL(request.url()).pathname
  if (path.startsWith('/api/')) failedRequests.push({ path, error: request.failure()?.errorText })
})
page.on('response', response => {
  const path = new URL(response.url()).pathname
  if (path.startsWith('/api/') && response.status() >= 400) badResponses.push(`${path}: ${response.status()}`)
})
async function checkpoint(uploadId) {
  return page.evaluate(async id => {
    const response = await fetch(`/api/uploads/${id}`, { headers: { Authorization: `Bearer ${sessionStorage.getItem('dataflow.session')}` } })
    if (!response.ok) throw new Error(`Checkpoint request returned HTTP ${response.status}`)
    return response.json()
  }, uploadId)
}
try {
  await page.goto(process.env.BASE_URL || 'http://localhost:8088', { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '注册账户', exact: true }).click()
  const password = `Resume_${randomUUID()}`
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(`ui_resume_${suffix}`)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(password)
  await page.getByPlaceholder('再次输入密码', { exact: true }).fill(password)
  await page.getByRole('button', { name: /创建账户/ }).click()
  await page.getByRole('heading', { name: /清洗工作台/ }).waitFor()
  const acceptedChunk = page.waitForResponse(response => response.request().method() === 'PUT'
    && /\/api\/uploads\/[^/]+\/chunks\/0$/.test(new URL(response.url()).pathname) && response.ok())
  await page.getByLabel('上传数据文件').setInputFiles(file)
  const firstChunkResponse = await acceptedChunk
  const uploadId = new URL(firstChunkResponse.url()).pathname.split('/')[3]
  await context.setOffline(true)
  await page.locator('.el-message--error').filter({ hasText: '无法连接服务' }).waitFor()
  await page.locator('.upload-zone:not(.uploading)').waitFor()
  assert.equal(await page.evaluate(key => sessionStorage.getItem(key), checkpointKey), uploadId)
  assert.ok(failedRequests.some(request => request.path.includes(`/uploads/${uploadId}/chunks/`)), 'A real client upload must fail while offline')
  await page.screenshot({ path: resolve(output, 'offline-upload-failure.png'), fullPage: true, animations: 'disabled' })
  await context.setOffline(false)
  const before = await checkpoint(uploadId)
  assert.equal(before.status, 'UPLOADING')
  assert.ok(before.totalChunks >= 3, 'Fixture must span multiple chunks')
  assert.ok(before.receivedChunks.includes(0), 'First chunk must be persisted on the server')
  assert.ok(before.receivedChunks.length < before.totalChunks, 'Some chunks must remain unuploaded')
  const initializedBeforeRefresh = requests.filter(request => request === 'POST /api/files/init').length
  assert.equal(initializedBeforeRefresh, 1)

  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: /清洗工作台/ }).waitFor()
  assert.equal(await page.evaluate(key => sessionStorage.getItem(key), checkpointKey), uploadId, 'Checkpoint must survive refresh')
  resuming = true
  const recoveredProgress = page.waitForResponse(response => response.request().method() === 'GET'
    && new URL(response.url()).pathname === `/api/uploads/${uploadId}`)
  const completed = page.waitForResponse(response => response.request().method() === 'POST'
    && new URL(response.url()).pathname === `/api/uploads/${uploadId}/complete`, { timeout: 180_000 })
  await page.getByLabel('上传数据文件').setInputFiles(file)
  const recoveredResponse = await recoveredProgress
  assert.equal(recoveredResponse.status(), 200)
  assert.equal((await recoveredResponse.json()).uploadId, uploadId)
  const completeResponse = await completed
  assert.equal(completeResponse.status(), 200)
  const complete = await completeResponse.json()
  assert.equal(complete.id, before.id, 'Reselecting must complete the original file resource')
  assert.equal(complete.uploadId, uploadId)
  assert.equal(complete.sha256, sha256)
  assert.equal(complete.status, 'READY')
  assert.equal(complete.parseStatus, 'PARSED')
  assert.equal(requests.filter(request => request === 'POST /api/files/init').length, initializedBeforeRefresh, 'Resuming must not create another upload')
  for (const index of before.receivedChunks)
    assert.ok(!resumedChunkIndices.includes(index), `Accepted chunk ${index} must be skipped after refresh`)
  assert.ok(resumedChunkIndices.length > 0, 'Missing chunks must actually be sent')
  await page.locator('.preview-area .el-table__body').getByText(value, { exact: true }).first().waitFor()
  assert.equal(await page.evaluate(key => sessionStorage.getItem(key), checkpointKey), null, 'Successful completion must clear the checkpoint')
  await page.screenshot({ path: resolve(output, 'resumed-upload-preview.png'), fullPage: true, animations: 'disabled' })
  assert.deepEqual(runtimeErrors, [], 'Browser runtime errors')
  assert.deepEqual(badResponses, [], 'Unexpected HTTP error responses')
  await writeFile(resolve(output, 'checkpoint-evidence.json'), JSON.stringify({
    fileBytes: buffer.length, sha256, before, resumedChunkIndices,
    completed: { id: complete.id, uploadId, status: complete.status, parseStatus: complete.parseStatus },
    requests, expectedOfflineFailures: failedRequests,
  }, null, 2))
  console.log('PASS: real accepted chunk, client offline failure, persisted checkpoint, refresh/reselect, skipped accepted chunks, same file ID, SHA-256 verification and actual data preview.')
  console.log('Upload:', uploadId, 'Evidence:', output)
} catch (error) {
  await context.setOffline(false).catch(() => {})
  await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true, animations: 'disabled' }).catch(() => {})
  await writeFile(resolve(output, 'failure-requests.json'), JSON.stringify({ requests, failedRequests, badResponses }, null, 2)).catch(() => {})
  throw error
} finally {
  await context.setOffline(false).catch(() => {})
  await context.close()
  await browser.close()
}
