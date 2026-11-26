<template>
  <section class="replacement-panel" data-testid="currency-replacement">
    <header class="page-head">
      <div>
        <h3>汇率替换申请</h3>
        <p class="page-desc">
          一次提交多个币对，逐项回执区分已确认与待补材料；已确认币对可复查，不会重复生成换算变更。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" data-testid="replacement-refresh" type="button" @click="refreshAll">
          刷新申请
        </button>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="submit">
      <div class="pair-editor">
        <div v-for="(row, index) in draft" :key="index" class="pair-row">
          <input v-model="row.币对" :data-testid="`pair-${index}`" placeholder="币对，如 USD/CNY" />
          <input v-model="row.快照ID" :data-testid="`snapshot-${index}`" placeholder="替换快照ID" />
          <input v-model="row.汇率值" :data-testid="`rate-${index}`" placeholder="补传汇率（可选）" />
          <button class="link" type="button" @click="draft.splice(index, 1)">移除</button>
        </div>
      </div>
      <div class="page-actions">
        <button class="btn" type="button" data-testid="replacement-add" @click="addDraft">增加币对</button>
        <button class="btn primary" data-testid="replacement-submit" type="submit" :disabled="busy">
          {{ busy ? '提交中…' : '提交替换申请' }}
        </button>
      </div>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="replacement-error">{{ errorMessage }}</p>
    <p v-if="message" class="replacement-message" data-testid="replacement-message">{{ message }}</p>

    <table v-if="current" class="data-table" data-testid="replacement-receipts">
      <thead>
        <tr>
          <th>申请编号</th>
          <th>币对</th>
          <th>回执状态</th>
          <th>可重试</th>
          <th>汇率（旧 → 新）</th>
          <th>换算变更</th>
          <th>回执说明</th>
          <th>续办</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in current.items"
          :key="item.币对"
          :data-testid="`receipt-${item.币对}`"
          :data-status="item.状态"
        >
          <td>{{ current.申请编号 }}</td>
          <td>{{ item.币对 }}</td>
          <td>{{ item.状态 }}</td>
          <td>{{ item.可重试 ? '是' : '否' }}</td>
          <td>{{ item.旧汇率 ?? '—' }} → {{ item.汇率值 ?? '待补' }}</td>
          <td>{{ item.变更记录ID ?? '—' }}</td>
          <td class="receipt-text">{{ item.回执 }}</td>
          <td>
            <button
              v-if="item.可重试"
              class="link"
              type="button"
              :data-testid="`retry-${item.币对}`"
              @click="retryPair(item)"
            >
              补传重试
            </button>
            <span v-else>已确认</span>
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="current" class="page-actions consume-actions">
      <button class="btn" type="button" data-testid="consume-per-pair" @click="consume('per_pair')">
        逐币对释出依据
      </button>
      <button class="btn" type="button" data-testid="consume-group" @click="consume('group')">
        等候整组释出
      </button>
      <span class="page-desc">每条换算变更只重算一次，重复消费拿到同一重算凭据。</span>
    </div>

    <table v-if="comparison.length" class="data-table" data-testid="replacement-comparison">
      <thead>
        <tr>
          <th>币对</th>
          <th>汇率版本</th>
          <th>实际换算金额</th>
          <th>重算次数</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in comparison" :key="row.币对">
          <td>{{ row.币对 }}</td>
          <td>{{ row.旧汇率版本 }} → {{ row.新汇率版本 }}</td>
          <td>{{ row.实际换算金额 }}</td>
          <td>{{ row.recompute_count }}</td>
        </tr>
      </tbody>
    </table>

    <table v-if="history.length" class="data-table" data-testid="replacement-history">
      <thead>
        <tr>
          <th>商品标识</th>
          <th>币对</th>
          <th>换算前金额</th>
          <th>换算后金额</th>
          <th>汇率版本</th>
          <th>旧价可查</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in history" :key="String(row.id)">
          <td>{{ row.商品标识 }}</td>
          <td>{{ row.币对 }}</td>
          <td>{{ row.换算前金额 ?? '—' }}</td>
          <td>{{ row.换算后金额 ?? '—' }}</td>
          <td>{{ row.汇率版本 }}</td>
          <td>{{ row.旧价仍可查 === false ? '否' : '是' }}</td>
        </tr>
      </tbody>
    </table>

    <table v-if="metrics.pairs.length" class="data-table" data-testid="replacement-metrics">
      <thead>
        <tr>
          <th>币对</th>
          <th>汇率版本</th>
          <th>实际换算金额合计</th>
          <th>重算次数</th>
          <th>复核范围</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in metrics.pairs" :key="row.币对">
          <td>{{ row.币对 }}</td>
          <td>{{ row.汇率版本.join('、') }}</td>
          <td>{{ row.实际换算金额合计 }}</td>
          <td>{{ row.recompute_count }}</td>
          <td>{{ row.复核范围.join('、') }}</td>
        </tr>
      </tbody>
    </table>
    <p v-if="metrics.pairs.length" class="page-foot">
      实际换算金额合计：{{ metrics.实际换算金额合计 }}
    </p>

    <div v-if="applications.length" class="application-list" data-testid="replacement-applications">
      <h4>历史申请</h4>
      <button
        v-for="app in applications"
        :key="app.id"
        class="link"
        type="button"
        :data-testid="`application-${app.申请编号}`"
        @click="loadApplication(app)"
      >
        {{ app.申请编号 }}（{{ app.status }}：已确认 {{ app.confirmed_count }} / 待补 {{ app.pending_count }}）
      </button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  consumeReplacement,
  fetchConversionMetrics,
  fetchHistoryEvents,
  listReplacements,
  resubmitReplacement,
  submitReplacement,
} from '@/api/backend-service'
import type {
  HistoryEvent,
  ReplacementApplication,
  ReplacementItemInput,
  ReplacementReceipt,
} from '@/api/backend-service'

type DraftRow = { 币对: string; 快照ID: string; 汇率值: string }

const draft = ref<DraftRow[]>([
  { 币对: 'USD/CNY', 快照ID: '', 汇率值: '' },
  { 币对: 'JPY/CNY', 快照ID: '', 汇率值: '' },
])
const current = ref<ReplacementApplication | null>(null)
const applications = ref<ReplacementApplication[]>([])
const comparison = ref<
  { 币对: string; 旧汇率版本: string; 新汇率版本: string; 实际换算金额: number; recompute_count: number }[]
>([])
const history = ref<HistoryEvent[]>([])
const metrics = reactive<{
  pairs: {
    币对: string
    汇率版本: string[]
    实际换算金额合计: number
    recompute_count: number
    复核范围: string[]
  }[]
  实际换算金额合计: number
}>({ pairs: [], 实际换算金额合计: 0 })
const busy = ref(false)
const message = ref('')
const errorMessage = ref('')

function addDraft() {
  draft.value.push({ 币对: '', 快照ID: '', 汇率值: '' })
}

function toInput(row: DraftRow): ReplacementItemInput {
  const item: ReplacementItemInput = { 币对: row.币对.trim() }
  if (row.快照ID.trim()) item.快照ID = Number(row.快照ID)
  if (row.汇率值.trim()) item.汇率值 = Number(row.汇率值)
  return item
}

async function refreshAll() {
  errorMessage.value = ''
  try {
    const [apps, metricPayload] = await Promise.all([
      listReplacements(),
      fetchConversionMetrics(),
    ])
    applications.value = apps.items
    metrics.pairs = metricPayload.pairs
    metrics.实际换算金额合计 = metricPayload.实际换算金额合计
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '申请列表读取失败'
  }
}

async function applyResult(result: Awaited<ReturnType<typeof submitReplacement>>) {
  if (!result.ok && result.code !== 'PAIR_NOT_IN_APPLICATION') {
    throw new Error(result.message)
  }
  message.value = result.message
  if (result.application) current.value = result.application
  if (result.comparison) comparison.value = result.comparison
  if (result.change_history) history.value = result.change_history
  await refreshAll()
}

async function submit() {
  busy.value = true
  errorMessage.value = ''
  message.value = ''
  try {
    const result = await submitReplacement(draft.value.filter((row) => row.币对.trim()).map(toInput))
    await applyResult(result)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '替换申请提交失败'
  } finally {
    busy.value = false
  }
}

async function retryPair(item: ReplacementReceipt) {
  if (!current.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    // 续办只能按原币对补传：优先使用表单里同币对行填写的汇率/快照，
    // 没有填写时沿用该币对原先提交的快照，由回执继续提示待补材料。
    const draftRow = draft.value.find((row) => row.币对.trim() === item.币对)
    const input: ReplacementItemInput = { 币对: item.币对 }
    const snapshotId = draftRow?.快照ID.trim() || item.快照ID || item.提交快照ID
    if (snapshotId) input.快照ID = Number(snapshotId)
    if (draftRow?.汇率值.trim()) input.汇率值 = Number(draftRow.汇率值)
    const result = await resubmitReplacement(current.value.id, [input])
    await applyResult(result)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续办失败'
  } finally {
    busy.value = false
  }
}

async function consume(mode: 'per_pair' | 'group') {
  if (!current.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const result = await consumeReplacement(current.value.id, mode)
    message.value = result.message
    if (current.value) {
      const [apps, events] = await Promise.all([
        listReplacements(),
        fetchHistoryEvents(),
      ])
      applications.value = apps.items
      history.value = events.events
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '消费失败'
  } finally {
    busy.value = false
  }
}

async function loadApplication(app: ReplacementApplication) {
  current.value = app
  message.value = `${app.申请编号}：${app.status}`
  try {
    const events = await fetchHistoryEvents()
    history.value = events.events
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '变价流水读取失败'
  }
}

onMounted(refreshAll)
</script>

<style scoped>
.replacement-panel { margin: 20px 0; padding-top: 12px; border-top: 2px solid var(--border); }
.pair-editor { display: flex; flex-direction: column; gap: 6px; width: 100%; }
.pair-row { display: flex; gap: 8px; }
.pair-row input { flex: 1; padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; }
.receipt-text { max-width: 360px; }
.consume-actions { margin: 10px 0; align-items: center; }
.replacement-message { color: var(--brand); font-size: 13px; }
.application-list { margin-top: 12px; display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
</style>
