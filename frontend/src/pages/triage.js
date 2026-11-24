import {fileIndex, heading, note, recordHeader, stamp} from '../components.js';
import {materials, packings, routes, standbyOutcomes, standbyStates} from '../constants.js';
import {dateTime, escape as e, percent} from '../format.js';

function scored(result) {
  return result?.status === 'scored';
}

function riskSlip(result) {
  return `<div class="scoring-slip"><h3>模型判定 ${stamp(result.risk, {low: '低风险', high: '高风险'})}</h3>
    <div class="probability">${percent(result.probability)}<small>模型风险概率</small></div>
    <div class="risk-bar" role="img" aria-label="风险 ${percent(result.probability)}，阈值 ${percent(result.evidence.risk_threshold)}"><i style="width:${result.probability * 100}%"></i>
      <b style="left:${result.evidence.risk_threshold * 100}%"></b></div><div class="risk-labels"><span>0%</span><span>复核阈值 ${percent(result.evidence.risk_threshold)}</span><span>100%</span></div>
    <p>${result.risk === 'high' ? '已生成开箱复核单。' : '风险环节已放行。派车仍需确认封签与窗口。'}</p></div>`;
}

function riskResult(result) {
  const evidence = result.evidence;
  const snapshot = result.input_snapshot;
  return `<div class="scoring-sheet">${riskSlip(result)}
    <section class="feature-sheet"><h3>包装输入快照</h3><dl class="facts">
      <dt>器物材质</dt><dd>${e(materials[snapshot.material])}</dd><dt>包装结构</dt><dd>${e(packings[snapshot.packing])}</dd>
      <dt>路线类别</dt><dd>${e(routes[snapshot.route_type])}</dd><dt>最高相对湿度</dt><dd>${snapshot.max_rh}% RH</dd>
      <dt>转运次数</dt><dd>${snapshot.transfers} 次</dd></dl></section></div>
    <section class="evidence"><div><h3>模型留档</h3><p>${e(evidence.model_version)} · ${dateTime(result.computed_at)} · 合成样本</p>
      <dl class="facts"><dt>训练 / 留出</dt><dd>${evidence.train_count} / ${evidence.test_count}</dd><dt>ROC AUC</dt><dd>${evidence.roc_auc.toFixed(3)}</dd>
      <dt>Brier 分数</dt><dd>${evidence.brier_score.toFixed(3)}</dd><dt>随机种子</dt><dd>${evidence.random_seed}</dd></dl></div>
      <details><summary>查看编码与输入指纹</summary><dl class="facts">${Object.entries(evidence.encoded_features).map(([label, value]) =>
        `<dt>${e(label)}</dt><dd>${value.toFixed(4)}</dd>`).join('')}</dl><p class="fingerprint mono">${e(evidence.input_sha256)}</p></details></section>`;
}

function standbySlip(standby) {
  const note = standby.status === 'queued' ? '备用模型排队中，出分后本页会更新。'
    : standby.status === 'cancelled' ? '备用评分已停用。' : '正式分流已出分，备用评分不再使用。';
  return `<div class="standby-slip"><h3>备用判定 ${stamp(standby.status, standbyStates)}</h3>
    ${scored(standby) ? `<div class="probability">${percent(standby.probability)}<small>备用模型风险概率</small></div>
      <p>只看器物材质与包装结构。${standby.risk === 'high' ? '备用高分，可先安排开箱见证。' : '备用低分，仍需等正式出分才能安排派车。'}</p>` :
      `<p>${e(standby.error ?? note)}</p>`}</div>`;
}

function standbyFacts(standby) {
  const snapshot = standby.input_snapshot;
  const committed = standby.outcome !== 'pending';
  return `<dl class="facts">${snapshot ? `<dt>备用输入</dt><dd>${e(materials[snapshot.material])} · ${e(packings[snapshot.packing])}</dd>` : ''}
    <dt>接管结果</dt><dd>${e(standbyOutcomes[standby.outcome] ?? standby.outcome)}</dd>
    ${standby.outcome_at ? `<dt>接管时间</dt><dd>${dateTime(standby.outcome_at)}</dd>` : ''}
    ${committed ? '' : `<dt>备用见证</dt><dd>${standby.witnessed_at ? `${e(standby.witness_by)} · ${dateTime(standby.witnessed_at)}` : '尚未签署'}</dd>`}</dl>
    ${standby.witness_note ? `<p class="standby-note">${e(standby.witness_note)}</p>` : ''}`;
}

function standbyEvidence(evidence) {
  return `<details><summary>备用模型留档</summary><dl class="facts"><dt>版本</dt><dd class="mono">${e(evidence.model_version)}</dd>
    <dt>特征</dt><dd>${(evidence.feature_columns ?? []).map((column) => e(column)).join('、')}</dd>
    <dt>ROC AUC</dt><dd>${evidence.roc_auc.toFixed(3)}</dd><dt>Brier 分数</dt><dd>${evidence.brier_score.toFixed(3)}</dd></dl>
    <p class="fingerprint mono">${e(evidence.input_sha256)}</p></details>`;
}

function standbySheet(record, standby, staff) {
  const formal = record.triage;
  const operator = staff.role === 'operator';
  const waiting = ['queued', 'failed'].includes(formal?.status);
  const canRequest = operator && record.crate && waiting && !standby;
  const canRetry = operator && record.crate && waiting && ['failed', 'cancelled'].includes(standby?.status);
  const canWitness = operator && standby?.status === 'scored' && standby.risk === 'high' && standby.outcome === 'pending' && !standby.witnessed_at;
  const detail = !standby ? '<p class="empty">尚未发起备用评分。</p>'
    : `${standbySlip(standby)}${standbyFacts(standby)}${standby.evidence ? standbyEvidence(standby.evidence) : ''}`;
  const carried = standby?.witnessed_at && standby.outcome === 'pending';
  const canWait = operator && standby?.status === 'scored' && standby.risk === 'high' && standby.outcome === 'pending' && standby.witnessed_at;
  return `<section class="standby-sheet"><div><h3>备用评分</h3>
    <p>正式分流卡住时，可用原训练样本另训的备用模型先排开箱见证。备用分数只决定何时开箱，不改变正式放行口径。</p></div>
    ${detail}
    ${canRequest ? `<form data-action="standby"><div class="form-actions"><span>${formal?.status === 'failed'
      ? '正式分流计算失败，可先用材质与包装安排开箱。' : '正式分流仍在排队，可另用材质与包装先行复核。'}</span>
      <button>发起备用评分</button></div></form>` : ''}
    ${canRetry ? `<form data-action="standby"><div class="form-actions"><span>上次备用评分未完成，可重新提交。</span>
      <button>重试备用评分</button></div></form>` : ''}
    ${canWitness ? `<form data-action="standby-witness">${note('备用开箱观察', 'note', '记录内托、器物和外箱的实见情况')}
      <div class="form-actions"><span>务必等待正式出分后再派车。</span><button>签署备用见证</button></div></form>` : ''}
    ${carried ? '<p class="success-line">备用见证已留存，正式出分后自动接管。</p>' : ''}
    ${canWait ? '<p class="muted">备用开箱已完成，等待正式分流出分后再判断能否派车。</p>' : ''}
  </section>`;
}

function intakePrompt(result, staff, record) {
  const queued = result?.status === 'queued';
  const canSubmit = staff.role === 'operator' && record.crate && !queued;
  const message = result?.status === 'queued' ? '模型完成后，本页会更新判定与复核任务。'
    : '先完成包装检查，再将材质、包装、路线、湿度与转运次数归档计算。';
  return `<section class="triage-intake"><h3>${queued ? '风险分流排队中' : '提交包装记录'}</h3>
    <p>${e(result?.error ?? message)}</p>
    ${canSubmit ? `<form data-action="triage"><button>${result?.status === 'failed' ? '重试分流' : '提交风险分流'}</button></form>` : ''}</section>`;
}

export function triagePage({loans, record, staff}) {
  const result = record?.triage;
  const standby = record?.standby;
  return `${heading('风险分流', '以包装快照确定是否安排开箱复核')}
    <div class="workbench">${fileIndex(loans, record, 'triage')}${record ? `<article class="dossier">${recordHeader(record)}
    ${scored(result) ? riskResult(result) : intakePrompt(result, staff, record)}
    ${['queued', 'failed'].includes(result?.status) || standby ? standbySheet(record, standby, staff) : ''}
    </article>` : ''}</div>`;
}
