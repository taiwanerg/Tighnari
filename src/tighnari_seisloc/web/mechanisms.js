/* Shared rendering of automatic mechanism research candidates (Hoshi and public detail page). */
import { node, fmt } from './common.js?v=20261127-best-fit-v1';

export const mechanismNames = {
  iris_cap:'IRIS 三分量位移 CMT＋站權重上限', iris_gdms_anchored:'IRIS 基線＋GDMS 補站',
  regional_multiband:'區域多頻帶 CMT', regional_short:'區域較短週期 CMT（逐分量）',
  regional_phase_windows:'區域分波窗加權 CMT', wphase:'GA W-phase',
  global:'全球長週期 CMT', first_motion:'P 波初動',
  regional:'區域波形', joint:'初動＋S/P', joint_sh:'初動＋SH/P',
  regional_cap:'區域短週期 CAP', regional_cap_joint:'區域短週期 CAP＋初動極性',
  first_motion_centroid:'P 波初動（以矩心深度重算射線）', global_long:'全球長週期 CMT（較低頻帶對照）',
};
export const capProfiles = {small:'小規模頻帶', moderate:'中規模頻帶', large:'長週期頻帶'};
export const mechanismStates = {
  waiting:'等待資料', running:'計算中', evaluated:'已計算', experimental:'通過事件品質檢查',
  ambiguous:'有多個可信候選', unconstrained:'機制未約束', insufficient_data:'資料不足', failed:'計算失敗',
  stale:'等待重定位後重算', shadow:'研究試算', research_candidate:'研究候選',
};
export const mechanismReasons = {
  waiting_for_regional_waveforms:'等待 IRIS 波形', waiting_for_long_period_waveforms:'等待完整遠震時窗',
  waiting_for_regional_greens:'等待區域格林函數庫', waiting_for_global_greens:'等待全球格林函數庫',
  no_additional_gdms_stations:'尚無合格的 GDMS 補充測站', common_validation_fit:'共同留站預測不足',
  validation_geometry:'無法保留共同驗證站', too_few_stations:'合格站數不足',
  too_few_validation_stations:'共同留站所需的測站不足', too_few_long_period_stations:'合格長週期站數不足',
  waveform_fit:'全站波形擬合不足', holdout_fit:'逐站省略預測不足', azimuth_gap:'方位覆蓋不足',
  unstable_mechanism:'機制擾動不穩定', unresolved_centroid:'矩心未約束',
  horizontal_grid_edge:'矩心落在水平搜尋網格邊緣', unresolved_horizontal_centroid:'矩心水平位置未約束',
  sensitivity_model_missing:'缺少第二個速度模型驗證', calculation_failed:'計算失敗', calculation_timeout:'計算逾時',
  no_validated_candidate:'候選均未通過', competing_mechanisms:'存在不同機制族',
  waiting_for_wphase_setup:'等待 GA W-phase 環境與模型庫',
  waiting_for_short_period_greens:'等待短週期格林函數庫',
  wphase_independent_validation_pending:'等待獨立品質驗證',
  overlapping_large_event_window:'時窗內有其他大型事件，需查驗波形重疊',
  band_sensitivity:'兩個頻帶的機制不一致',
};
export const polarityChecks = {consistent:'初動一致', mixed:'初動部分不符', conflict:'與初動衝突'};
// One line on whether methods agree; the spread itself tells how far to trust any one ball.
export function agreementText(agreement) {
  if (!agreement) return '';
  const angle = Math.round(agreement.max_kagan_deg);
  if (agreement.scope === 'passed')
    return `${agreement.count} 個通過的候選彼此最多相差 ${angle}°（Kagan 角）` +
      (angle <= 30 ? '，機制大致一致。' : '，機制仍有分歧。');
  return `各方法的結果彼此最多相差 ${angle}°（Kagan 角）` +
    (angle > 60 ? '，沒有一致的機制。' : '，但均未通過品質檢查。');
}
// Several CAP bands can share one method name, so the band goes into the title to tell them apart.
export const mechanismName = row => (mechanismNames[row.previous_of || row.id] || mechanismNames[row.method] || row.previous_of || row.id) +
  (row.profile ? ' · ' + (capProfiles[row.profile] || row.profile) : '');
export const reasonLabel = value => mechanismReasons[value] || value;
const reason = reasonLabel;
export function selectionSummary(data) {
  const chosen = data.selection.representative;
  return chosen
    ? '研究首選候選 · ' + mechanismName(data.candidates.find(row => row.id === chosen) || {id: chosen})
    : mechanismStates[data.selection.status] || data.selection.status;
}
export function jobLines(data) {
  return Object.entries(data.jobs).map(([kind, job]) =>
    node('p', {class:'muted'}, (mechanismNames[kind] || kind) + ' · ' +
      [mechanismStates[job.status], ...(job.reasons || []).map(reason),
        job.previous_result ? '暫列前次定位的結果' : null].filter(Boolean).join(' · ')));
}
export function mechanismCard(row, imageBase) {
  const card = node('article', {class:'mechanism-research-card'});
  if (row.tensor_ned_nm) card.append(node('img', {loading:'lazy', width:'72', height:'72',
    alt:mechanismName(row) + ' 海灘球', src:imageBase + encodeURIComponent(row.id) + '.png'}));
  const body = node('div');
  if (row.previous_location) card.classList.add('previous-location');
  body.append(node("strong", {}, mechanismName(row)));
  if (row.previous_location) body.append(node('p', {class:'muted'}, '依前次定位的結果 · 重算完成後會取代'));
  body.append(node('p', {class:'muted'},
    [mechanismStates[row.status] || row.status,
      row.station_count != null ? row.station_count + ' 站' : null,
      row.band_hz ? row.band_hz.join('–') + ' Hz' : null,
      row.mw != null ? 'Mw ' + fmt(row.mw, 2) : null,
      row.centroid_depth_km != null ? '矩心深度 ' + fmt(row.centroid_depth_km, 0) + ' km' +
        (row.centroid_depth_resolved === false ? '（深度未約束）' : '') : null,
      row.validation_vr_pct != null && row.validation_method !== 'leave_one_station_out'
        ? '共同留站 VR ' + fmt(row.validation_vr_pct, 0) + '%' : null,
      row.holdout_vr_pct != null ? '留一站 VR ' + fmt(row.holdout_vr_pct, 0) + '%' : null,
      row.quality_grade ? '品質 ' + row.quality_grade : null,
    ].filter(Boolean).join(' · ')));
  const extra = [
    row.pnl_band_hz ? 'Pnl ' + row.pnl_band_hz.join('–') + ' Hz' : null,
    row.fallback_from ? (capProfiles[row.fallback_from] || row.fallback_from) + '未通過後改用' : null,
    row.polarity_ray_depth_km != null ? '極性射線深度 ' + fmt(row.polarity_ray_depth_km, 0) + ' km' : null,
    row.ray_depth_km != null ? '射線深度 ' + fmt(row.ray_depth_km, 0) + ' km' : null,
    row.misfit != null ? '初動不符 ' + fmt(row.misfit * 100, 0) + '%' : null,
    row.tensor_spread_deg != null ? '近最佳解分散 ' + fmt(row.tensor_spread_deg, 0) + '°' : null,
    row.representative_kagan_deg != null ? '與研究首選差 ' + fmt(row.representative_kagan_deg, 0) + '°' : null,
    row.band_kagan_deg != null ? '與較低頻帶差 ' + fmt(row.band_kagan_deg, 0) + '°' : null,
  ].filter(Boolean);
  if (extra.length) body.append(node('p', {class:'muted'}, extra.join(' · ')));
  if (row.fit_image) {
    const line = node('p', {class:'muted'});
    line.append(node('a', {target:'_blank', rel:'noopener', href:imageBase + encodeURIComponent(row.id) + '/fits.png'}, '逐站波形擬合圖'));
    body.append(line);
  }
  if (row.reasons?.length) body.append(node('p', {class:'muted'}, row.reasons.map(reason).join(' · ')));
  const check = row.first_motion_check;
  if (check?.misfit != null) body.append(node('p', {class:'muted'},
    (polarityChecks[check.status] || check.status) + ' · 預測初動不符 ' +
    fmt(check.misfit * 100, 0) + '%（' + check.count + ' 站' +
    (check.ray_depth_km != null ? '，射線深度 ' + fmt(check.ray_depth_km, 0) + ' km' : '') + '）'));
  for (const [kind, validation] of Object.entries(row.joint_validation || {})) {
    const label = kind === 'site' ? '留站' : '留方位區';
    const angle = validation.kagan_p90_deg;
    const baseline = validation.prediction?.polarity_only;
    const joint = validation.prediction?.joint;
    body.append(node('p', {class:'muted'},
      label + ' ' + validation.eligible_folds + '/' + validation.attempted_folds +
      (angle == null ? '' : ' · Kagan P90 ' + fmt(angle, 0) + '°')));
    if (baseline?.polarity_misfit_pct != null && joint?.polarity_misfit_pct != null)
      body.append(node('p', {class:'muted'}, label + '極性不符率 ' +
        fmt(baseline.polarity_misfit_pct, 0) + '% → ' + fmt(joint.polarity_misfit_pct, 0) + '%'));
  }
  card.append(body);
  return card;
  }
