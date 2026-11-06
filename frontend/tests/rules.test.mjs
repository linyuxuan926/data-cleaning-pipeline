import assert from 'node:assert/strict'
import test from 'node:test'
import { executionPlan, validateRules } from '../src/rules.ts'

test('only explicit v2 execution plans follow phase order', () => {
  const rules = [
    { type: 'rename', column: 'f0', target: 'new' },
    { type: 'required', columns: ['f0'], onError: 'mark' },
    { type: 'trim', columns: ['f0'] },
    { type: 'regex', column: 'f0', pattern: '.*' },
    { type: 'deduplicate', keep: 'last' },
    { type: 'cast', columns: ['f1'], dtype: 'number' },
  ]
  assert.deepEqual(executionPlan(rules).map(item => item.index), [0, 1, 2, 3, 4, 5])
  assert.deepEqual(executionPlan(rules, 'v3-sequential').map(item => item.index), [0, 1, 2, 3, 4, 5])
  assert.deepEqual(executionPlan(rules, 'v2-phased').map(item => item.index), [2, 5, 1, 3, 4, 0])
  assert.equal(rules[0].type, 'rename', 'preview must not mutate the persisted rule order')
})

test('global full-row keep-last and single-sided range payloads are allowed', () => {
  assert.doesNotThrow(() => validateRules([{ type: 'deduplicate', keep: 'last' }, { type: 'range', column: 'f1', max: 10, onError: 'null' }]))
  assert.throws(() => validateRules([{ type: 'range', column: 'f1' }]), /边界/)
  assert.throws(() => validateRules([{ type: 'length', column: 'f0', min: 1.5 }]), /整数/)
})

test('typed constants retain zero, false, null and empty string while non-finite values fail', () => {
  assert.doesNotThrow(() => validateRules([
    { type: 'enum', column: 'f0', values: [0, false, null, ''] },
    { type: 'fill_stat', columns: ['f1'], strategy: 'mean', emptyPolicy: 'constant', value: 0 },
    { type: 'missing', columns: ['f0'], markers: ['', null] },
  ]))
  assert.throws(() => validateRules([{ type: 'fill_null', columns: ['f1'], value: Infinity }]), /有效/)
  assert.throws(() => validateRules([{ type: 'fill_stat', columns: ['f1'], emptyPolicy: 'constant' }]), /有效/)
  assert.throws(() => validateRules([{ type: 'enum', column: 'f0', values: [] }]), /至少/)
})
