export type Scalar = string | number | boolean | null
export type DataType = 'integer' | 'number' | 'string' | 'datetime' | 'boolean'
export interface User { id: string; username: string; role: 'ADMIN' | 'USER'; enabled: boolean; createdAt?: string }
export interface Settings {
  maxFileBytes: number; maxRows: number; maxColumns: number; maxCells: number; maxExcelExpandedBytes: number;
  previewRows: number; maxActivePerUser: number; maxActiveGlobal: number; submissionsPerMinute: number
  maxRunningGlobal?: number; maxRunningPerUser?: number
}
export interface ParseOptions {
  encoding: 'auto' | 'utf-8-sig' | 'utf-8' | 'gb18030' | 'utf-16' | 'latin-1';
  delimiter: 'auto' | ',' | ';' | '\\t' | '|' | '\t';
  sheetName?: string; missingValues: Scalar[]; fieldTypes?: Record<string, DataType>
}
export interface FileItem {
  id: string; uploadId: string; name: string; format: string; size: number; status: string; createdAt: string;
  sha256: string | null; parseStatus: 'UNPARSED' | 'PARSED' | 'FAILED'; parseError: string | null;
  parseOptions: ParseOptions; fields: ColumnProfile[]; uploader: { id: string; username: string }; objectKey: string
  parseVersion?: number
  objectStatus?: 'AVAILABLE' | 'EXPIRED' | 'DELETING' | null
}
export interface ColumnProfile { id?: string; name: string; dtype: string; inferredType?: DataType; nullCount: number }
export interface Preview {
  columns: ColumnProfile[]; rows: Record<string, unknown>[]; rowCount: number | null; previewRowCount: number;
  sheets?: { name: string; rows: number; columns: number }[]; parseOptions?: ParseOptions; warnings?: string[]
}
export type RuleType = 'trim' | 'case' | 'missing' | 'fill_null' | 'fill_stat' | 'drop_null' | 'deduplicate' | 'cast' | 'range' | 'iqr' | 'required' | 'enum' | 'length' | 'regex' | 'validate_type' | 'compare' | 'select_fields' | 'drop_fields' | 'rename'
export type ErrorPolicy = 'drop' | 'mark' | 'fail' | 'null'
export interface Rule {
  type: RuleType; columns?: string[]; column?: string; mode?: 'lower' | 'upper'; value?: Scalar; dtype?: DataType;
  min?: number; max?: number; inclusiveMin?: boolean; inclusiveMax?: boolean; pattern?: string; target?: string;
  onError?: ErrorPolicy; markers?: Scalar[]; strategy?: 'mean' | 'median' | 'mode';
  emptyPolicy?: 'fail' | 'leave' | 'constant'; keep?: 'first' | 'last'; dateFormat?: string; precision?: number;
  outputFormat?: string; factor?: number; values?: Scalar[];
  operator?: 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'contains' | 'starts_with' | 'ends_with'
}
export interface LogicalField { id: string; label: string }
export type DiskDataType = 'integer' | 'decimal' | 'string' | 'datetime' | 'boolean'
export interface TypedScalar { type: DiskDataType | 'null'; value: string | boolean | null; scale?: number }
export interface DiskRule {
  id: string; enabled: boolean; type: RuleType | 'replace' | 'compare_fields'; fieldIds?: string[]; fieldId?: string;
  leftFieldId?: string; rightFieldId?: string; dtype?: DiskDataType; precision?: number; scale?: number; rounding?: string;
  mode?: string; value?: TypedScalar; values?: TypedScalar[]; markers?: TypedScalar[]; min?: string | number; max?: string | number;
  inclusiveMin?: boolean; inclusiveMax?: boolean; onError?: ErrorPolicy; pattern?: string; target?: string; strategy?: string;
  emptyPolicy?: string; keep?: string; factor?: string | number; operator?: string; valueType?: DiskDataType;
  nullPolicy?: 'violation' | 'skip'; search?: string; replacement?: string; count?: number; dateFormat?: string; outputFormat?: string;
  [key: string]: unknown
}
export interface RuleSet<T = Rule> { id: string; name: string; version: number; rules: T[]; createdAt: string;
  schemaVersion?: number; engineVersion?: string; logicalFields?: LogicalField[]; enabled?: boolean; description?: string }
export interface CursorPage<T> { items: T[]; nextCursor: string | null; hasMore: boolean; asOf?: string }
export interface Preflight { valid: boolean; normalizedRules?: DiskRule[]; outputFields?: unknown[]; warnings?: string[] }
export type TaskStatus = 'QUEUED' | 'RUNNING' | 'CANCELING' | 'SUCCEEDED' | 'FAILED' | 'CANCELED' | 'CANCELLED'
export interface Task {
  id: string; fileId: string; fileName: string; ruleSetId: string; ruleVersion: number; username?: string; ownerId?: string;
  status: TaskStatus; progress: number; attempt: number; outputFormat: string; error: string | null; createdAt: string; updatedAt: string;
  attemptId?: string; currentAttemptId?: string; engineVersion?: string;
  schemaVersion?: number; fieldBindings?: Record<string, string>; logicalFields?: LogicalField[];
  stage?: string; stageProcessedRows?: number | null; totalRows?: number | null;
  artifactStatus?: Record<string, string>;
  logicalFieldsSnapshot?: LogicalField[]; fieldBindingsSnapshot?: Record<string, string>; outputFieldsSnapshot?: unknown[];
  rulesSnapshot?: (Rule | DiskRule)[]; inputSnapshot?: { objectKey?: string; format?: string; name?: string; parseOptions?: ParseOptions; fields?: ColumnProfile[] };
  limitsSnapshot?: Settings
}
export interface TaskAttempt {
  id?: string; attemptId?: string; attempt?: number; attemptNo?: number; status: TaskStatus | string;
  trigger?: string; triggerReason?: string; worker?: string; workerId?: string; stage?: string;
  progress?: number; error?: string | null; createdAt?: string; startedAt?: string;
  completedAt?: string; finishedAt?: string; updatedAt?: string; durationMs?: number
  report?: Report | null; artifacts?: Record<string, string> | null
  artifactStatus?: Record<string, string>; stageProcessedRows?: number | null; totalRows?: number | null
}
export interface LogItem { id: number; level: string; message: string; createdAt: string }
export interface RuleReport {
  index: number; type: string; inputRows: number; outputRows: number; rejectedRows: number; changedCells: number;
  ruleId?: string; status?: string; issueCount?: number; flaggedRows?: number; statistics?: Record<string, unknown>; executionOrder?: number | null;
  removedRows?: number; markedRows?: number; nulledCells?: number; missingFilledCells?: number;
  typeConversionFailedCells?: number; duplicateRemovedRows?: number; outlierHandledRows?: number
}
export interface Report {
  taskId: string; attempt: number; inputRows: number; outputRows: number; dirtyRows: number; changedCells: number;
  removedRows?: number; flaggedRows?: number; issueCount?: number; executionOrder?: (number | string)[]; ruleSnapshot?: (Rule | DiskRule)[];
  validOutputRows?: number; flaggedOutputRows?: number; finalRemovedRows?: number; duplicateRemovedRows?: number;
  outlierHandledRows?: number; missingFilledCells?: number; typeConversionFailedCells?: number; issueRows?: number;
  finalRemovalByRule?: Record<string, number>;
  durationSeconds: number; rules: RuleReport[]; columnsBefore: ColumnProfile[]; columnsAfter: ColumnProfile[]; warnings: string[]
}
