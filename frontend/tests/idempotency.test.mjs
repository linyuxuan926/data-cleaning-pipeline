import assert from 'node:assert/strict'
import test from 'node:test'
import { clearSubmitKey, getOrCreateSubmitKey, requestFingerprint } from '../src/idempotency.ts'

function memoryStorage() {
  const entries = new Map()
  return {
    getItem: key => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value),
    removeItem: key => entries.delete(key),
  }
}

test('same submit reuses a persistent key; a changed payload receives a fresh key', () => {
  const storage = memoryStorage()
  let next = 0
  const create = () => `key-${++next}`
  const payload = { fileId: 'f1', ruleSetId: 'r1', ruleVersion: 2, outputFormat: 'csv' }
  assert.equal(getOrCreateSubmitKey(storage, 'u1', payload, create), 'key-1')
  assert.equal(getOrCreateSubmitKey(storage, 'u1', { outputFormat: 'csv', ruleVersion: 2, ruleSetId: 'r1', fileId: 'f1' }, create), 'key-1')
  assert.equal(next, 1)
  assert.equal(getOrCreateSubmitKey(storage, 'u1', { ...payload, outputFormat: 'xlsx' }, create), 'key-2')
})

test('submit keys are user-scoped and only the acknowledged key can be cleared', () => {
  const storage = memoryStorage()
  let next = 0
  const create = () => `key-${++next}`
  const payload = { fileId: 'f1' }
  const first = getOrCreateSubmitKey(storage, 'u1', payload, create)
  const second = getOrCreateSubmitKey(storage, 'u2', payload, create)
  assert.notEqual(first, second)
  clearSubmitKey(storage, 'u1', 'stale-key')
  assert.equal(getOrCreateSubmitKey(storage, 'u1', payload, create), first)
  clearSubmitKey(storage, 'u1', first)
  assert.equal(getOrCreateSubmitKey(storage, 'u1', payload, create), 'key-3')
})

test('payload fingerprint recursively sorts object keys but preserves array order', () => {
  assert.equal(requestFingerprint({ a: 1, b: { d: 2, c: 3 }, list: [1, 2] }), requestFingerprint({ list: [1, 2], b: { c: 3, d: 2 }, a: 1 }))
  assert.notEqual(requestFingerprint({ list: [1, 2] }), requestFingerprint({ list: [2, 1] }))
})

test('a timed-out retry retains its key across attempt changes and other task retries stay isolated', () => {
  const storage = memoryStorage()
  let next = 0
  const create = () => `retry-${++next}`
  const namespace = 'u1.retry:task-1'
  const first = getOrCreateSubmitKey(storage, namespace, { taskId: 'task-1' }, create)
  assert.equal(getOrCreateSubmitKey(storage, namespace, { taskId: 'task-1' }, create), first)
  assert.notEqual(getOrCreateSubmitKey(storage, 'u1.retry:task-2', { taskId: 'task-2' }, create), first)
  clearSubmitKey(storage, namespace, first)
  assert.notEqual(getOrCreateSubmitKey(storage, namespace, { taskId: 'task-1' }, create), first)
})
