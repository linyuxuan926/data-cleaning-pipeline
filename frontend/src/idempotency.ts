export interface StorageLike {
  getItem(key: string): string | null
  setItem(key: string, value: string): void
  removeItem(key: string): void
}

interface PendingSubmit { key: string; fingerprint: string }

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => [key, stableValue(item)]))
  }
  return value
}

export function requestFingerprint(payload: unknown) { return JSON.stringify(stableValue(payload)) }

export function getOrCreateSubmitKey(
  storage: StorageLike,
  namespace: string,
  payload: unknown,
  createKey: () => string = () => globalThis.crypto.randomUUID(),
) {
  const storageKey = `dataflow.pending-submit.${namespace}`
  const fingerprint = requestFingerprint(payload)
  try {
    const saved = storage.getItem(storageKey)
    if (saved) {
      const pending = JSON.parse(saved) as PendingSubmit
      if (pending.fingerprint === fingerprint && pending.key) return pending.key
    }
    const key = createKey()
    storage.setItem(storageKey, JSON.stringify({ key, fingerprint } satisfies PendingSubmit))
    return key
  } catch {
    return createKey()
  }
}

export function clearSubmitKey(storage: StorageLike, namespace: string, key: string) {
  const storageKey = `dataflow.pending-submit.${namespace}`
  try {
    const saved = storage.getItem(storageKey)
    if (saved && (JSON.parse(saved) as PendingSubmit).key === key) storage.removeItem(storageKey)
  } catch { /* a storage failure must not change the task request result */ }
}
