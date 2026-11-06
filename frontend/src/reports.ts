import type { Report, RuleReport } from './types'

export function reportCounts(report: Report) {
  const flaggedOutputRows = report.flaggedOutputRows ?? report.flaggedRows
  return {
    validOutputRows: report.validOutputRows ?? (flaggedOutputRows === undefined ? undefined : Math.max(0, report.outputRows - flaggedOutputRows)),
    flaggedOutputRows,
    finalRemovedRows: report.finalRemovedRows ?? report.removedRows ?? Math.max(0, report.inputRows - report.outputRows),
    duplicateRemovedRows: report.duplicateRemovedRows,
    outlierHandledRows: report.outlierHandledRows,
    missingFilledCells: report.missingFilledCells,
    typeConversionFailedCells: report.typeConversionFailedCells,
    issueRows: report.issueRows ?? report.dirtyRows,
  }
}

export function executionStep(row: RuleReport, order: Report['executionOrder']): number | string {
  if (row.status === 'DISABLED') return '未启用'
  if (row.executionOrder != null && row.executionOrder > 0) return row.executionOrder
  const position = order?.findIndex(item => typeof item === 'string' ? item === row.ruleId : item === row.index)
  if (position !== undefined && position >= 0) return position + 1
  return row.status === undefined && !row.ruleId ? row.index : '—'
}
