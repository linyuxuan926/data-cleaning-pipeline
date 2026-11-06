import { computed, ref, shallowRef } from 'vue'
import { api } from './api'
import type { CursorPage } from './types'

export function pageQuery(filters: Record<string, unknown>, cursor: string | null, pageSize = 20) {
  const params = new URLSearchParams({ pageSize: String(pageSize) })
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '' && value !== 'ALL') params.set(key, String(value))
  }
  if (cursor) params.set('cursor', cursor)
  return params.toString()
}

export function useCursorPage<T>(path: () => string, filters: () => Record<string, unknown> = () => ({}), pageSize = 20) {
  const items = shallowRef<T[]>([])
  const nextCursor = ref<string | null>(null)
  const hasMore = ref(false)
  const asOf = ref('')
  const loading = ref(false)
  const error = ref('')
  const cursors = ref<(string | null)[]>([null])
  const page = computed(() => cursors.value.length)
  const canPrevious = computed(() => page.value > 1)
  let generation = 0
  let activeQuery = ''
  async function fetchPage(reset = false) {
    const scope = JSON.stringify([path(), filters()])
    if (reset || scope !== activeQuery) { cursors.value = [null]; activeQuery = scope }
    const sequence = ++generation
    loading.value = true; error.value = ''
    try {
      const value = await api<CursorPage<T>>(`${path()}?${pageQuery(filters(), cursors.value.at(-1) || null, pageSize)}`)
      if (sequence !== generation) return
      items.value = value.items; nextCursor.value = value.nextCursor; hasMore.value = value.hasMore; asOf.value = value.asOf || ''
    } catch (e) { if (sequence === generation) error.value = e instanceof Error ? e.message : '读取列表失败。' }
    finally { if (sequence === generation) loading.value = false }
  }
  async function next() { if (!loading.value && hasMore.value && nextCursor.value) { cursors.value = [...cursors.value, nextCursor.value]; await fetchPage() } }
  async function previous() { if (!loading.value && canPrevious.value) { cursors.value = cursors.value.slice(0, -1); await fetchPage() } }
  function clear() { generation++; items.value = []; cursors.value = [null]; nextCursor.value = null; hasMore.value = false; error.value = ''; activeQuery = ''; loading.value = false }
  return { items, page, canPrevious, hasMore, loading, error, asOf, fetch: fetchPage, next, previous, clear }
}
