import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 汇率替换批次：多币对提交、续办、消费与指标重算 ----

export type ReplacementReceipt = {
  币对: string
  状态: string
  可重试: boolean
  回执: string
  汇率值: number | null
  旧汇率: number | null
  快照ID: number | null
  提交快照ID: number | null
  旧快照ID: number | null
  冲突: boolean
  冲突说明: string
  变更记录ID: number | null
  旧价可查: boolean
}

export type ReplacementApplication = {
  id: number
  申请编号: string
  status: string
  confirmed_count: number
  pending_count: number
  pending_pairs: string[]
  confirmed_pairs: string[]
  resubmit_count: number
  items: ReplacementReceipt[]
  receipts: ReplacementReceipt[]
}

export type ReplacementResult = {
  ok: boolean
  message: string
  code?: string
  application?: ReplacementApplication
  receipts?: ReplacementReceipt[]
  change_records?: { id: number; 币对: string; 旧汇率: number; 新汇率: number }[]
  comparison?: {
    币对: string
    旧汇率版本: string
    新汇率版本: string
    实际换算金额: number
    recompute_count: number
  }[]
  change_history?: HistoryEvent[]
  released?: { 币对: string; recompute_count: number; recompute_token: string }[]
}

export type HistoryEvent = {
  id: number
  商品标识: string
  币对: string
  旧价格: number | null
  新价格: number | null
  汇率版本: string
  换算前金额: number | null
  换算后金额: number | null
  换算依据?: Record<string, unknown>
  复核范围?: string
  旧价仍可查?: boolean
}

export type ReplacementItemInput = {
  币对: string
  快照ID?: number
  汇率值?: number
  快照日期?: string
}

export async function submitReplacement(
  items: ReplacementItemInput[],
  remark = '',
): Promise<ReplacementResult> {
  const response = await request('/api/currency-replacement', {
    method: 'POST',
    body: JSON.stringify({ values: { items, remark } }),
  })
  if (!response.ok) throw new Error('替换申请提交失败')
  return await response.json()
}

export async function listReplacements(): Promise<{ items: ReplacementApplication[] }> {
  const response = await request('/api/currency-replacement')
  if (!response.ok) throw new Error('替换申请读取失败')
  return await response.json()
}

export async function resubmitReplacement(
  applicationId: number,
  items: ReplacementItemInput[],
): Promise<ReplacementResult> {
  const response = await request(`/api/currency-replacement/${applicationId}/resubmit`, {
    method: 'POST',
    body: JSON.stringify({ values: { items } }),
  })
  if (!response.ok) throw new Error('替换申请续办失败')
  return await response.json()
}

export async function consumeReplacement(
  applicationId: number,
  mode: 'per_pair' | 'group',
): Promise<ReplacementResult> {
  const response = await request(`/api/currency-replacement/${applicationId}/consume`, {
    method: 'POST',
    body: JSON.stringify({ action: mode }),
  })
  if (!response.ok) throw new Error('换算变更消费失败')
  return await response.json()
}

export async function fetchHistoryEvents(pair?: string): Promise<{ events: HistoryEvent[] }> {
  const query = pair ? `?pair=${encodeURIComponent(pair)}` : ''
  const response = await request(`/api/currency-replacement/history/events${query}`)
  if (!response.ok) throw new Error('变价流水读取失败')
  return await response.json()
}

export async function fetchConversionMetrics(): Promise<{
  实际换算金额合计: number
  pairs: {
    币对: string
    汇率版本: string[]
    实际换算金额合计: number
    recompute_count: number
    复核范围: string[]
  }[]
}> {
  const response = await request('/api/currency-replacement/metrics/converted')
  if (!response.ok) throw new Error('换算指标读取失败')
  return await response.json()
}

