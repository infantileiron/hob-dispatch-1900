import {api} from '../api/client.js';
import {badge, empty} from '../components/common/views.js';
import {ImportShiftStatuses, ImportSlotStates, ImportStatuses} from '../constants/enums.js';
import {state} from '../stores/state.js';
import {h, number, time} from '../utils/format.js';

function localStamp(moment) {
  // Shanghai wall-clock stamp with an explicit offset, as the API expects.
  const shifted = new Date(moment.getTime() + 8 * 3600000);
  return `${shifted.toISOString().slice(0, 16)}:00+08:00`;
}

/** Build a shuffled 抄表/点火 sample for a real open shift, showing blank slots. */
export function sampleFor(shift) {
  if (!shift) return '';
  const start = new Date(shift.starts_at);
  const end = new Date(shift.ends_at);
  if (!(end > start)) return '';
  const slotMs = 15 * 60000;
  const slotCount = Math.max(1, Math.ceil((end - start) / slotMs));
  const edges = slotCount === 1 ? [0, 1] : [0, 1, 2];
  const at = index => new Date(Math.min(start.getTime() + index * slotMs, end.getTime()));
  const meters = edges.map(index => ({
    sampled_at: localStamp(at(index)), value_m3: (100 + index * 0.5).toFixed(3), meter_no: 'M1',
  }));
  meters.reverse(); // Shuffled on purpose: the server must not depend on order.
  const logs = [];
  for (let index = 0; index < edges.length - 1; index += 1) {
    logs.push({sampled_at: localStamp(new Date(at(index).getTime() + 2 * 60000)), burner_minutes: 12});
    logs.push({sampled_at: localStamp(new Date(at(index).getTime() + 9 * 60000)), burner_minutes: 13});
  }
  return JSON.stringify({shifts: [{code: shift.code, meter_readings: meters, ignition_logs: logs}]}, null, 2);
}

function tone(shiftState) {
  if (shiftState === 'imported' || shiftState === 'ready') return 'live';
  if (shiftState === 'rejected') return 'warm';
  return 'muted';
}

function reasonList(reasons) {
  if (!reasons?.length) return '';
  return `<ul class="reason-list">${reasons.map(reason => `<li>${h(reason.text)}</li>`).join('')}</ul>`;
}

function slotTable(entry) {
  const blanks = entry.slots.filter(slot => slot.status === 'blank');
  const rows = entry.slots.map(slot => [
    `<span class="mono">${time(slot.sampled_at)}</span>`,
    slot.start_value == null ? '—' : number(slot.start_value),
    slot.end_value == null ? '—' : number(slot.end_value),
    number(slot.gas_m3),
    slot.burner_minutes ?? '—',
    number(slot.intensity),
    badge(ImportSlotStates[slot.status], slot.status === 'import' ? 'live' : 'muted'),
    slot.reason_text ? h(slot.reason_text) : '—',
  ]);
  return `<details class="slot-fold" ${blanks.length ? 'open' : ''}><summary>逐格明细 <span class="mono">${entry.slots.length} 格 · ${blanks.length} 格留空</span></summary>
    <div class="table-wrap"><table><thead><tr>${['时格起点', '起表值', '止表值', '表差 m³', '炉分钟', 'm³ / 炉时', '处理', '留空原因'].map(label => `<th scope="col">${label}</th>`).join('')}</tr></thead>
    <tbody>${rows.map(cells => `<tr>${cells.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('')}</tbody></table></div></details>`;
}

function previewCard(entry) {
  const totals = entry.totals;
  return `<article class="import-card">
    <header><div><strong class="mono">${h(entry.code)}</strong><small class="cell-sub">${h(entry.station_label)} · ${time(entry.starts_at)} → ${time(entry.ends_at)} · 计划投递 ${time(entry.due_at)}</small></div>
    ${badge(ImportShiftStatuses[entry.state] ?? entry.state, tone(entry.state))}</header>
    ${reasonList(entry.reasons)}
    ${entry.state === 'ready' ? `<p class="import-total">可入账 <strong class="mono">${totals.import_slots}</strong> 格 · 表差 <strong class="mono">${number(totals.gas_m3)}</strong> m³ · 炉分钟 <strong class="mono">${totals.burner_minutes}</strong> · 强度 <strong class="mono">${number(totals.intensity)}</strong> m³/炉时</p>` : ''}
    ${slotTable(entry)}</article>`;
}

function resultCard(entry) {
  const totals = entry.totals ?? {};
  const blanks = (entry.slots ?? []).filter(slot => slot.status === 'blank');
  const blankSummary = Object.entries(blanks.reduce((acc, slot) => {
    acc[slot.reason_text] = (acc[slot.reason_text] ?? 0) + 1;
    return acc;
  }, {})).map(([text, count]) => `<li>${h(text)} × ${count}</li>`).join('');
  return `<article class="import-card">
    <header><div><strong class="mono">${h(entry.code)}</strong><small class="cell-sub">${h(entry.station_label)} · ${time(entry.starts_at)} → ${time(entry.ends_at)}</small></div>
    ${badge(ImportShiftStatuses[entry.status] ?? entry.status, tone(entry.status))}</header>
    ${entry.status === 'imported' ? `<p class="import-total">已入账 <strong class="mono">${entry.imported_slots}</strong> 格 · 表差 <strong class="mono">${number(totals.gas_m3)}</strong> m³ · 炉分钟 <strong class="mono">${totals.burner_minutes}</strong> · 强度 <strong class="mono">${number(totals.intensity)}</strong> m³/炉时</p>` : ''}
    ${reasonList(entry.reasons)}
    ${blankSummary ? `<details class="slot-fold" open><summary>留空时格</summary><ul class="reason-list">${blankSummary}</ul></details>` : ''}
    ${entry.status === 'pending' ? '' : slotTable(entry)}
  </article>`;
}

export function importPanel() {
  if (!state.account || state.account.role === 'observer') return '';
  const batch = state.importBatch;
  const preview = state.importPreview;
  const draft = state.importDraft;
  const batchStatus = batch?.batch?.status;
  const pending = batch?.shifts?.some(row => row.status === 'pending');
  return `<section class="panel" id="batch-import">
    <div class="panel-head"><div><h2>成批入账</h2><p>粘贴抄表与点火日志。乱序抄表按时格两端求差，同格点火记录合并；缺端点、跨表的时格留空并注明原因。</p></div>
      <button type="button" class="neutral" id="import-sample">填入示例</button></div>
    <form id="import-form" class="side-form">
      <label>批内容（JSON：shifts[] 含 code、meter_readings[]、ignition_logs[]）<textarea name="payload" rows="10" spellcheck="false" placeholder='{"shifts":[{"code":"HL-1121-E01","meter_readings":[{"sampled_at":"2026-11-21T17:00:00+08:00","value_m3":"30.600","meter_no":"H01-M"},...],"ignition_logs":[{"sampled_at":"2026-11-21T17:02:00+08:00","burner_minutes":12},...]}]}'>${h(draft)}</textarea></label>
      <div class="import-actions"><button class="primary" type="submit">预览入账</button>
      ${preview ? '<button type="button" class="neutral" id="import-clear">清空</button>' : ''}</div>
      <p class="form-note"></p>
    </form>
    ${preview ? `<div class="import-section" id="import-preview"><div class="panel-head"><div><h3>入账预览</h3><p>${preview.importable} 个班可入账${preview.unmatched.length ? ` · ${preview.unmatched.length} 个班次编号未匹配` : ''}。确认后整批提交，遇到并发手录或封账的班整班退回。</p></div>
      ${preview.shifts.length ? `<button type="button" class="primary" id="import-confirm">确认入账</button>` : ''}</div>
      ${preview.unmatched.length ? `<p class="form-note">未匹配：${h(preview.unmatched.join('、'))}</p>` : ''}
      ${preview.shifts.length ? preview.shifts.map(previewCard).join('') : empty('没有可预览的班次', '检查班次编号是否已登记。')}</div>` : ''}
    ${batch ? `<div class="import-section"><div class="panel-head"><div><h3>批次 #${batch.batch.id} · ${ImportStatuses[batchStatus] ?? batchStatus}</h3><p>${h(batch.batch.actor_name)} · 提交 ${time(batch.batch.created_at, true)}${batch.batch.finished_at ? ` · 完成 ${time(batch.batch.finished_at, true)}` : ''}${batch.batch.unmatched?.length ? ` · 未匹配 ${h(batch.batch.unmatched.join('、'))}` : ''}</p></div>
      ${pending ? '<button type="button" class="neutral" id="import-resume">续跑未完成班次</button>' : ''}</div>
      ${batch.shifts.map(resultCard).join('')}</div>` : ''}
    ${recentBatches(batch)}
    <p id="import-note" role="status" class="form-note"></p>
  </section>`;
}

function recentBatches(open) {
  const rows = (state.importBatches ?? []).filter(row => row.id !== open?.batch?.id);
  if (!rows.length) return '';
  return `<div class="import-section"><div class="panel-head"><div><h3>最近批次</h3><p>中断后可用原幂等键重试，或在此续跑未完成班次；已入账的班保留当时结果。</p></div></div>
    <ul class="import-history">${rows.map(row => `<li><span class="mono">#${row.id}</span>
      ${badge(ImportStatuses[row.status] ?? row.status, row.status === 'completed' ? 'live' : row.status === 'importing' ? 'warm' : 'muted')}
      <span>${h(row.actor_name)} · ${time(row.created_at, true)} · ${row.total_shifts} 班${row.pending_shifts ? ` · ${row.pending_shifts} 班待处理` : ''}${row.unmatched?.length ? ` · 未匹配 ${h(row.unmatched.join('、'))}` : ''}</span>
      <button type="button" class="text-button" data-open-batch="${row.id}">查看</button>
      ${row.pending_shifts ? `<button type="button" class="text-button" data-resume-batch="${row.id}">续跑</button>` : ''}</li>`).join('')}</ul></div>`;
}

function parseDraft(raw) {
  let body;
  try {
    body = JSON.parse(raw);
  } catch {
    throw new Error('批内容不是有效 JSON，请检查括号、引号和逗号。');
  }
  if (!Array.isArray(body?.shifts) || !body.shifts.length) throw new Error('批内容须包含至少一个 shifts 条目。');
  return {shifts: body.shifts};
}

function generateKey() {
  return globalThis.crypto?.randomUUID?.() ?? `batch-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function bindImport(reload) {
  const form = document.querySelector('#import-form');
  if (!form) return;
  const note = document.querySelector('#import-note');
  const formNote = form.querySelector('.form-note');
  const draftField = form.elements.payload;

  document.querySelector('#import-sample').onclick = () => {
    const target = state.shifts?.find(row => row.id === state.shiftId)
      ?? state.shifts?.find(row => !row.evaluated_at && Date.parse(row.due_at) > Date.now());
    const draft = sampleFor(target);
    if (!draft) {
      formNote.textContent = '请先登记一个尚未冻结的班次，示例会按该班的时格生成。';
      return;
    }
    state.importDraft = draft;
    state.importPreview = null;
    state.importKey = null;
    reload();
  };
  document.querySelector('#import-clear')?.addEventListener('click', () => {
    state.importDraft = '';
    state.importPreview = null;
    state.importKey = null;
    state.importBatch = null;
    reload();
  });

  draftField.oninput = () => {
    state.importDraft = draftField.value;
    if (state.importPreview) {
      // A changed draft no longer matches the shown preview or its idempotency key.
      state.importPreview = null;
      state.importKey = null;
      document.querySelector('#import-preview')?.remove();
    }
  };

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type=submit]');
    button.disabled = true;
    formNote.textContent = '';
    note.textContent = '';
    try {
      const body = parseDraft(draftField.value);
      state.importDraft = draftField.value;
      state.importPreview = await api.previewImport(body);
      state.importBatch = null;
      // Reuse the key for an unchanged draft so an interrupted submit resumes the same
      // batch instead of creating a second one.
      state.importKey ||= generateKey();
      reload();
    } catch (error) {
      formNote.textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });

  document.querySelector('#import-confirm')?.addEventListener('click', async event => {
    const button = event.currentTarget;
    button.disabled = true;
    note.textContent = '';
    try {
      const body = parseDraft(state.importDraft);
      state.importBatch = await api.submitImport({...body, idempotency_key: state.importKey});
      state.importPreview = null;
      reload();
    } catch (error) {
      button.disabled = false;
      note.textContent = error.message;
    }
  });

  document.querySelector('#import-resume')?.addEventListener('click', event => resume(event.currentTarget, note, reload));
  document.querySelectorAll('[data-open-batch]').forEach(button => button.addEventListener('click', async () => {
    note.textContent = '';
    try {
      state.importBatch = await api.importBatch(Number(button.dataset.openBatch));
      state.importPreview = null;
      reload();
    } catch (error) {
      note.textContent = error.message;
    }
  }));
  document.querySelectorAll('[data-resume-batch]').forEach(button => button.addEventListener('click', event => {
    resume(event.currentTarget, note, reload, Number(button.dataset.resumeBatch));
  }));
}

async function resume(button, note, reload, batchId) {
  button.disabled = true;
  note.textContent = '';
  try {
    const target = batchId ?? state.importBatch.batch.id;
    state.importBatch = await api.resumeImport(target);
    state.importPreview = null;
    reload();
  } catch (error) {
    button.disabled = false;
    note.textContent = error.message;
  }
}
