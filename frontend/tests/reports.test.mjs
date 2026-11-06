import assert from 'node:assert/strict'
import test from 'node:test'
import { executionStep, reportCounts } from '../src/reports.ts'

test('new report counters preserve final disposition independently of issue events', () => {
  const report = {
    inputRows: 8, outputRows: 4, validOutputRows: 3, flaggedOutputRows: 1,
    finalRemovedRows: 4, duplicateRemovedRows: 1, outlierHandledRows: 1,
    missingFilledCells: 1, typeConversionFailedCells: 1, issueRows: 5,
    dirtyRows: 5, removedRows: 4, flaggedRows: 1, issueCount: 9,
  }
  const counts = reportCounts(report)
  assert.equal(report.inputRows, report.outputRows + counts.finalRemovedRows)
  assert.equal(report.outputRows, counts.validOutputRows + counts.flaggedOutputRows)
  assert.equal(counts.issueRows, 5, 'issue rows include records later removed')
  assert.equal(counts.typeConversionFailedCells, 1)
  assert.equal(counts.missingFilledCells, 1)
})

test('V2 reports retain known totals and leave unrecorded processing counts unknown', () => {
  const counts = reportCounts({ inputRows: 8, outputRows: 4, dirtyRows: 5, removedRows: 4, flaggedRows: 1 })
  assert.equal(counts.finalRemovedRows, 4, 'dirtyRows must not substitute for final removals')
  assert.equal(counts.issueRows, 5)
  assert.equal(counts.flaggedOutputRows, 1)
  assert.equal(counts.validOutputRows, 3)
  assert.equal(counts.duplicateRemovedRows, undefined)
  assert.equal(counts.outlierHandledRows, undefined)
  assert.equal(counts.missingFilledCells, undefined)
  assert.equal(counts.typeConversionFailedCells, undefined)
  assert.equal(reportCounts({ inputRows: 8, outputRows: 4, dirtyRows: 4 }).validOutputRows, undefined)
  assert.equal(reportCounts({ inputRows: 8, outputRows: 4, dirtyRows: 4 }).finalRemovedRows, 4)
})

test('rule steps prefer actual execution position and support V3 IDs, V2 indices and disabled rules', () => {
  assert.equal(executionStep({ index: 2, ruleId: 'second', executionOrder: 1 }, ['first', 'second']), 1)
  assert.equal(executionStep({ index: 2, ruleId: 'second' }, ['first', 'second']), 2)
  assert.equal(executionStep({ index: 1 }, [2, 1]), 2)
  assert.equal(executionStep({ index: 1, ruleId: 'off', status: 'DISABLED', executionOrder: null }, ['second']), '未启用')
  assert.equal(executionStep({ index: 3, ruleId: 'unknown', status: 'COMPLETED' }, ['second']), '—')
  assert.equal(executionStep({ index: 2 }, undefined), 2)
})
