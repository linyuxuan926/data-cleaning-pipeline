import assert from 'node:assert/strict'
import test from 'node:test'
import { diskRule, diskRuleLabels, migrateLegacyRules, validateDiskRules } from '../src/diskRules.ts'

const fields = [{ id: 'amount', label: '金额' }, { id: 'limit', label: '限额' }]

test('all 21 rule types receive independent identities and new operations use the disk contract', () => {
  assert.equal(Object.keys(diskRuleLabels).length, 21)
  const rules = Object.keys(diskRuleLabels).map(type => diskRule(type))
  assert.equal(new Set(rules.map(rule => rule.id)).size, 21)
  assert.ok(rules.every(rule => rule.enabled === true))
  assert.deepEqual(diskRule('replace').fieldIds, [])
  assert.equal(diskRule('replace').mode, 'literal')
  assert.equal(diskRule('compare_fields').valueType, 'decimal')
})

test('logical bindings are explicit and one-to-one, including disabled rule references', () => {
  const rule = { ...diskRule('compare_fields'), leftFieldId: 'amount', rightFieldId: 'limit', enabled: false }
  assert.throws(() => validateDiskRules([rule], fields, { amount: 'f0' }), /尚未绑定/)
  assert.throws(() => validateDiskRules([rule], fields, { amount: 'f0', limit: 'f0' }), /一一对应/)
  assert.doesNotThrow(() => validateDiskRules([rule], fields, { amount: 'f0', limit: 'f1' }))
  assert.throws(() => validateDiskRules([], [{ id: 'has-hyphen', label: '错误' }]), /下划线/)
})

test('exact int64 and Decimal constants survive validation and JSON serialization', () => {
  const rules = [
    { ...diskRule('fill_null'), fieldIds: ['amount'], value: { type: 'integer', value: '9223372036854775807' } },
    { ...diskRule('fill_null'), fieldIds: ['limit'], value: { type: 'decimal', value: '12345678901234567890123456.125', scale: 3 } },
    { ...diskRule('cast'), fieldIds: ['amount'], dtype: 'decimal', precision: 38, scale: 3, rounding: 'HALF_UP' },
  ]
  validateDiskRules(rules, fields)
  assert.equal(JSON.parse(JSON.stringify(rules))[0].value.value, '9223372036854775807')
  assert.equal(JSON.parse(JSON.stringify(rules))[1].value.value, '12345678901234567890123456.125')
  assert.throws(() => validateDiskRules([{ ...rules[2], precision: 2 }], fields), /precision/)
  assert.throws(() => validateDiskRules([{ ...rules[2], scale: 1.5 }], fields), /整数/)
})

test('legacy conversion is explicit, uses stable logical IDs, and retains source order without mutation', () => {
  const legacy = [
    { type: 'rename', column: 'f0', target: 'new_name' },
    { type: 'cast', columns: ['f1'], dtype: 'number', precision: 3 },
    { type: 'fill_null', columns: ['f1'], value: 0 },
  ]
  const copy = structuredClone(legacy)
  const migrated = migrateLegacyRules(legacy, [{ id: 'f0', name: '名称' }, { id: 'f1', name: '金额' }])
  assert.deepEqual(migrated.rules.map(rule => rule.type), ['rename', 'cast', 'fill_null'])
  assert.equal(migrated.rules[1].dtype, 'decimal')
  assert.equal(migrated.rules[1].scale, 3)
  assert.deepEqual(migrated.rules[2].value, { type: 'integer', value: '0' })
  assert.deepEqual(migrated.fieldBindings, { field_f0: 'f0', field_f1: 'f1' })
  assert.deepEqual(legacy, copy)
  assert.doesNotThrow(() => validateDiskRules(migrated.rules, migrated.logicalFields, migrated.fieldBindings))
})
