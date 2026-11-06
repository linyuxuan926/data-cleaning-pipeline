const TOKEN_KEY = 'dataflow.session'
export class HttpError extends Error {
  constructor(message: string, readonly status: number) { super(message); this.name = 'HttpError' }
}

export const token = () => sessionStorage.getItem(TOKEN_KEY)
export function saveToken(value: string | null) {
  if (value) sessionStorage.setItem(TOKEN_KEY, value)
  else sessionStorage.removeItem(TOKEN_KEY)
}

async function response(path: string, init: RequestInit = {}) {
  const headers = new Headers(init.headers)
  const bearer = token()
  if (bearer) headers.set('Authorization', `Bearer ${bearer}`)
  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), 120_000)
  try {
    const res = await fetch(`/api${path}`, { ...init, headers, signal: controller.signal })
    if (!res.ok) {
      const body = await res.json().catch(() => ({})) as { message?: string; detail?: string; code?: string }
      if (res.status === 401 && bearer) {
        saveToken(null)
        window.dispatchEvent(new Event('session-expired'))
      }
      const expired = res.status === 410 ? (body.code === 'INPUT_EXPIRED' || body.message?.startsWith('INPUT_EXPIRED') ? '输入文件已过期或已清理，请重新上传；历史任务摘要仍可查看。' : '该执行产物已过期或已清理，无法预览或下载；历史状态与报告摘要仍可查看。') : undefined
      throw new HttpError(expired || body.message || body.detail || `请求失败（HTTP ${res.status}）`, res.status)
    }
    return res
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw new Error('请求超时，请检查网络后重试。')
    if (e instanceof TypeError) throw new Error('无法连接服务，请检查网络或服务状态。')
    throw e
  } finally {
    window.clearTimeout(timeout)
  }
}

export async function api<T>(path: string, method = 'GET', body?: unknown, options: { headers?: HeadersInit } = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const res = await response(path, {
    method,
    headers,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  return res.status === 204 ? undefined as T : await res.json() as T
}

export async function uploadChunk(uploadId: string, index: number, bytes: Blob) {
  await response(`/uploads/${encodeURIComponent(uploadId)}/chunks/${index}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/octet-stream' }, body: bytes,
  })
}

export async function downloadArtifact(taskId: string, kind: string, fallback: string, attemptId?: string) {
  const base = `/tasks/${encodeURIComponent(taskId)}${attemptId ? `/attempts/${encodeURIComponent(attemptId)}` : ''}`
  const res = await response(`${base}/artifacts/${kind}`)
  const blob = await res.blob()
  const disposition = res.headers.get('Content-Disposition') || ''
  let name = fallback
  const utf8 = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  try { name = utf8 ? decodeURIComponent(utf8[1]!) : plain?.[1] || fallback } catch { /* keep safe fallback */ }
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = name
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 10_000)
}
