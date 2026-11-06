// Real isolated MinIO objects and browser requests; no mocked routes or deletion.
import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomUUID } from 'node:crypto'
import { chromium } from 'playwright'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const base = process.env.BASE_URL || 'http://localhost:8089'
const project = process.env.RETENTION_TEST_PROJECT || 'data-cleaning-pipeline-v3-next'
assert.equal(new URL(base).hostname, 'localhost')
assert.equal(new URL(base).port, '8089')
assert.equal(project, 'data-cleaning-pipeline-v3-next', 'Refuse a main or unknown Compose project')
const output = resolve(root, '.test-output/browser-retention-continuation')
await mkdir(output, { recursive: true })
const scopeId = randomUUID()
const prefix = process.env.RETENTION_FIXTURE_PREFIX || `raw/ffffffff-ffff-ffff-ffff-${scopeId.replaceAll('-', '').slice(0, 12)}/${randomUUID()}/`
function objects(mode) {
  const code = `import json,os,sys,io\nfrom concurrent.futures import ThreadPoolExecutor\nfrom urllib.parse import urlparse\nfrom minio import Minio\np=json.load(sys.stdin)\nu=urlparse(os.environ['MINIO_ENDPOINT'])\nc=Minio(u.netloc,access_key=os.environ['MINIO_ACCESS_KEY'],secret_key=os.environ['MINIO_SECRET_KEY'],secure=u.scheme=='https')\nb=os.environ.get('MINIO_BUCKET','cleaning')\nif p['mode']=='write':\n def put(i):\n  data=b'id,name\\n1,retention-continuation-test\\n'\n  c.put_object(b,p['prefix']+f'{i:04d}.csv',io.BytesIO(data),len(data),content_type='text/csv')\n with ThreadPoolExecutor(max_workers=16) as pool:list(pool.map(put,range(1001)))\nvalues=list(c.list_objects(b,prefix=p['prefix'],recursive=True))\nassert len(values)==1001,len(values)\nprint(json.dumps({'count':len(values),'bytes':sum(v.size for v in values),'prefix':p['prefix']}))`
  const value = spawnSync('docker', ['compose', '-p', project, 'exec', '-T', 'python-api', 'python', '-c', code], { cwd: root, input: JSON.stringify({ mode, prefix }), encoding: 'utf8', timeout: 90000 })
  assert.equal(value.status, 0, value.stderr)
  return JSON.parse(value.stdout.trim())
}
console.log(process.env.RETENTION_FIXTURE_PREFIX ? 'Reusing the retained 1001 isolated fixture objects.' : 'Writing 1001 new isolated fixture objects; existing objects will remain untouched.')
const beforeObjects = objects(process.env.RETENTION_FIXTURE_PREFIX ? 'read' : 'write')
const secrets = Object.fromEntries((await readFile(resolve(root, '.env'), 'utf8')).split(/\r?\n/).filter(line => line.includes('=') && !line.trimStart().startsWith('#')).map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]))
const browser = await chromium.launch({ headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai' })
const page = await context.newPage()
page.setDefaultTimeout(30000)
const requests = [], errors = [], bad = []
page.on('pageerror', error => errors.push(error.message))
page.on('request', request => { const url = new URL(request.url()); if (url.pathname.startsWith('/api/')) requests.push({ method: request.method(), path: url.pathname, ...(url.pathname.startsWith('/api/admin/retention/') && request.method() === 'POST' ? { body: request.postDataJSON() } : {}) }) })
page.on('response', response => { if (new URL(response.url()).pathname.startsWith('/api/') && response.status() >= 400) bad.push(`${response.status()} ${new URL(response.url()).pathname}`) })
async function api(path, method = 'GET', body) {
  const result = await page.evaluate(async ({ path, method, body }) => {
    const response = await fetch(`/api${path}`, { method, headers: { Authorization: `Bearer ${sessionStorage.getItem('dataflow.session')}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) }, ...(body === undefined ? {} : { body: JSON.stringify(body) }) })
    return { status: response.status, body: await response.json() }
  }, { path, method, body })
  assert.equal(result.status, 200, JSON.stringify(result.body))
  return result.body
}
try {
  await page.goto(base, { waitUntil: 'networkidle' })
  await page.getByPlaceholder('请输入用户名', { exact: true }).fill(secrets.BOOTSTRAP_ADMIN_USERNAME)
  await page.getByPlaceholder('请输入密码', { exact: true }).fill(secrets.BOOTSTRAP_ADMIN_PASSWORD)
  await page.getByRole('button', { name: /登 录/ }).click()
  await page.locator('nav').getByRole('button', { name: /平台管理/ }).click()
  await page.getByRole('tab', { name: '保留期与清理', exact: true }).click()
  await page.getByRole('button', { name: '保存保留期策略', exact: true }).waitFor()
  const policy = await api('/admin/retention/policy')
  assert.equal(policy.enabled, false)
  const scope = { fileIds: [], taskIds: [], includeOrphans: true, afterObjectKey: prefix }
  const first = await api('/admin/retention/plans', 'POST', scope)
  assert.equal(first.state, 'DRY_RUN')
  assert.equal(first.truncated, true)
  assert.ok(first.scan.nextContinuation)
  assert.ok(first.scan.scannedObjects > 0 && first.scan.scannedObjects <= 1000)
  assert.equal(first.scan.scannedObjects, first.scan.matchedObjects)
  await page.getByRole('textbox', { name: '清理计划ID', exact: true }).fill(first.id)
  await page.getByRole('button', { name: '读取计划状态', exact: true }).click()
  const button = page.getByRole('button', { name: '继续扫描并生成新预览计划', exact: true })
  await button.waitFor()
  await page.screenshot({ path: resolve(output, 'first-plan-truncated.png'), fullPage: true, animations: 'disabled' })
  const plans = [first]
  let current = first
  for (let step = 0; current.scan.nextContinuation && step < 20; step++) {
    const nextResponse = page.waitForResponse(response => new URL(response.url()).pathname === '/api/admin/retention/plans' && response.request().method() === 'POST')
    await button.click()
    const response = await nextResponse
    assert.equal(response.status(), 200)
    assert.deepEqual(response.request().postDataJSON(), { ...scope, continuation: current.scan.nextContinuation })
    const next = await response.json()
    assert.notEqual(next.id, current.id)
    assert.equal(next.state, 'DRY_RUN')
    assert.ok(next.scan.afterObjectKey >= current.scan.afterObjectKey)
    if (next.scan.scannedObjects) assert.ok(next.scan.afterObjectKey > current.scan.afterObjectKey)
    await page.locator('.selected-attempt-context').getByText(next.id, { exact: true }).waitFor()
    plans.push(next); current = next
  }
  assert.equal(current.scan.nextContinuation, null)
  assert.equal(current.truncated, false)
  assert.ok(plans.reduce((sum, plan) => sum + plan.scan.scannedObjects, 0) >= 1001)
  const targetKeys = plans.flatMap(plan => plan.items.map(item => item.objectKey).filter(key => key?.startsWith(prefix)))
  assert.equal(targetKeys.length, 1001)
  assert.equal(new Set(targetKeys).size, 1001, 'Every fixture object must appear once across the cursor pages')
  assert.equal(await page.getByRole('button', { name: '正式执行计划', exact: true }).isDisabled(), true)
  assert.deepEqual(await api('/admin/retention/policy'), policy)
  assert.equal(requests.some(request => /\/(execute|retry)$/.test(request.path)), false)
  assert.equal(requests.some(request => request.path === '/api/tasks' && request.method === 'POST'), false)
  const afterObjects = objects('read')
  assert.deepEqual(afterObjects, beforeObjects)
  assert.deepEqual(errors, [])
  assert.deepEqual(bad, [])
  await page.screenshot({ path: resolve(output, 'continued-plan-complete.png'), fullPage: true, animations: 'disabled' })
  await writeFile(resolve(output, 'evidence.json'), JSON.stringify({ project, base, prefix, objects: afterObjects, policy, plans, requests, objectsRetained: true, deletionRequests: 0 }, null, 2))
  console.log(`PASS: actual browser continuation POST retained original scope and nextContinuation; ${plans.length} pages reached all 1001 target objects exactly once, distinct dry-run plans ${plans.map(plan => plan.id).join(" / ")}; policy unchanged and disabled; all 1001 objects retained.`)
  console.log('Evidence:', output)
} catch (error) { await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {}); await writeFile(resolve(output, 'requests-failure.json'), JSON.stringify(requests, null, 2)); throw error }
finally { await context.close(); await browser.close() }
