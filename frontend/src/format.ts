import type { TaskStatus } from './types'
export const statusLabels: Record<TaskStatus, string> = { QUEUED: '排队中', RUNNING: '执行中', CANCELING: '正在取消', SUCCEEDED: '已完成', FAILED: '执行失败', CANCELED: '已取消', CANCELLED: '已取消（旧状态）' }
export const statusTypes: Record<TaskStatus, 'info' | 'warning' | 'success' | 'danger'> = { QUEUED: 'info', RUNNING: 'warning', CANCELING: 'warning', SUCCEEDED: 'success', FAILED: 'danger', CANCELED: 'info', CANCELLED: 'info' }
export function statusLabel(status: TaskStatus | string) { return statusLabels[status as TaskStatus] || status }
export function statusType(status: TaskStatus | string) { return statusTypes[status as TaskStatus] || 'info' }
export function date(value: string) { return value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '—' }
export function bytes(value: number) { return value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${(value / 1024).toFixed(1)} KB` }
export function number(value: number | undefined) { return (value ?? 0).toLocaleString('zh-CN') }
export function isActive(status: TaskStatus) { return status === 'QUEUED' || status === 'RUNNING' || status === 'CANCELING' }
