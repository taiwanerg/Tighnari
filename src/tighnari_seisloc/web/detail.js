import { $, fmt, time, node, get, magnitude, map, drawWave, waveAmplitude, reviewLabel, depthDetailText } from "./common.js?v=20261127-best-fit-v1";
import { agreementText, jobLines, mechanismCard, reasonLabel, selectionSummary } from "./mechanisms.js?v=20261127-cap-band-v1";
const id = decodeURIComponent(location.pathname.split("/").pop()),
  base = "/api/earthquakes/" + encodeURIComponent(id);
let event = null,
  end = null,
  lastRevision = 0,
  waveURL = "",
  waveFrame = null,
  waveRequest = 0;
const travelModels = { "iasp91-first-arrival-v1": "IASP91 一維模型（初達波）" };
const magnitudeReasons = {
  "low-horizontal-SNR": "水平分量訊噪比不足",
  "incomplete-S-window-or-outside-domain": "S 波時窗不完整或超出適用範圍",
  "surface-short-period-excluded": "地表短週期站（寬頻站已足夠）",
  "missing-horizontal-component": "缺少水平分量",
  "co-located-alternative": "同址已有較佳儀器",
  clipped: "波形截波",
  "network-magnitude-outlier": "與測網規模差異過大",
  "distance-outside-30-90-deg": "震央距不在 30°–90°",
  "incomplete-baseline-or-P-window": "基線或 P 波時窗不完整",
  "invalid-broadband-quality-trace": "寬頻波形品質不合格",
  "P-SNR-below-five": "P 波訊噪比低於 5",
  "monotonic-or-unresolved-integral": "位移積分無法判定峰值",
  "nonfinite-magnitude": "規模無法計算",
  "station-identity-unavailable": "測站資訊不足",
  "no-P-pick-near-theory": "理論到時附近無 P 波",
  "fewer-than-three-quality-sites": "合格測站少於 3 站",
  "fewer-than-five-quality-far-stations": "合格遠距測站少於 5 站",
};
// "Shin1993-Taiwan/WA6800-horizontal-RSS" and "Tsuboi-physical-SI/HP0.008Hz" in plain words.
function magnitudeMethod(method) {
  if (!method) return "";
  const ml = /^Shin1993-Taiwan\/WA(\d+)/.exec(method);
  if (ml) return `Shin (1993) 臺灣 ML 公式 · Wood-Anderson 增益 ${ml[1]} · 雙水平分量合成`;
  const mwp = /^Tsuboi.*HP([\d.]+)Hz/.exec(method);
  if (mwp) return `Tsuboi Mwp · ${mwp[1]} Hz 高通`;
  return method;
}
function table(id, rows) {
  $(id).replaceChildren(
    ...rows.map((values) => {
      const tr = node("tr");
      for (const v of values) tr.append(node("td", {}, String(v ?? "—")));
      return tr;
    }),
  );
}
function depthChart() {
  const canvas = $("depth-chart"),
    w = canvas.clientWidth || 600,
    h = 190,
    dpr = devicePixelRatio || 1;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  const data = event.depth_profile || [];
  if (!data.length) {
    ctx.fillStyle = "#8fa5a5";
    ctx.fillText("沒有深度診斷資料", 20, 50);
    return;
  }
  const maxX = Math.max(...data.map((p) => p.depth_km)),
    maxY = Math.max(0.5, ...data.map((p) => p.rms_s));
  ctx.strokeStyle = "#294047";
  for (let i = 0; i < 5; i++) {
    const y = 20 + i * 32;
    ctx.beginPath();
    ctx.moveTo(40, y);
    ctx.lineTo(w - 20, y);
    ctx.stroke();
  }
  ctx.strokeStyle = "#8edbc3";
  ctx.lineWidth = 2;
  ctx.beginPath();
  data.forEach((p, i) => {
    const x = 40 + (p.depth_km / maxX) * (w - 65),
      y = 155 - (p.rms_s / maxY) * 130;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "#8fa5a5";
  ctx.font = "14px sans-serif";
  ctx.fillText("0 km", 35, 177);
  ctx.fillText(maxX + " km", w - 55, 177);
  ctx.fillText("RMS s", 5, 12);
}
async function waves() {
  if (!event) return;
  const request = ++waveRequest;
  try {
    const url = `/api/waveforms?event_id=${encodeURIComponent(id)}&include_candidates=${$("show-candidates").checked}&seconds=${$("window").value}${end !== null ? "&end=" + end : ""}&revision=${event.revision}`;
    const data = url === waveURL && waveFrame ? waveFrame : await get(url);
    if (request !== waveRequest) return;
    waveURL = url;
    waveFrame = data;
    const archived = data.storage === "image";
    for (const control of ["window", "show-candidates", "earlier", "later", "reset"])
      $(control).disabled = archived;
    if (archived) {
      $("detail-waves").replaceChildren(
        node("p", { class: "wave-note" }, `此地震已超過 24 小時，波形封存為圖片，保留最佳 ${data.stations.length} 站；此修訂不再提供原始數值與互動時間窗。`),
        node("img", { src: data.image_url, alt: "事件最佳測站波形封存圖", class: "report-image", loading: "lazy" }),
        node("a", { href: data.image_url, download: `${id}-r${event.revision}-waveform.png`, class: "quiet" }, "下載波形圖片"),
      );
      $("wave-time").textContent = time(data.start) + " — " + time(data.end) + " UTC+8 · 圖片封存";
      return;
    }
    $("detail-waves").replaceChildren(
      ...data.rows.map((row) => {
        const div = node("div", { class: "wave-row" }),
          canvas = node("canvas", { "aria-label": row.id + " 波形" }),
          label = node("span", { class: "wave-label" }, row.id + " · Z · µm/s");
        div.append(canvas, label);
        requestAnimationFrame(() => {
          const amp = drawWave(canvas, row, data);
          label.textContent = row.id + " · Z · ±" + waveAmplitude(amp) + " µm/s";
        });
        return div;
      }),
    );
    if (!data.rows.length)
      $("detail-waves").append(
        node("p", { class: "empty" }, "此修訂沒有保存的波形資料。"),
      );
    $("wave-time").textContent =
      time(data.start) + " — " + time(data.end) + " UTC+8";
  } catch (e) {
    if (request !== waveRequest) return;
    $("detail-waves").replaceChildren(
      node("p", { class: "empty" }, "波形讀取失敗：" + e.message),
    );
  }
}
async function load() {
  try {
    const rev = $("revision").value;
    event = await get(base + (rev ? "?revision=" + rev : ""));
    lastRevision = event.revision;
    const review = event.review || {};
    const reviewText = [review.error ? "此事件已標記為錯誤事件。" : reviewLabel(event),
      review.magnitude_override != null ? "ML 已由管理員手動修訂。" : "", review.comment || ""].filter(Boolean).join("\n");
    $("review-notice").textContent = reviewText;
    $("review-notice").hidden = !reviewText;
    const compact = Boolean(event.summary_only);
    for (const selector of ["#phases", "#magnitudes", "#waveforms", "#revisions", ".detail-tabs", "#depth-chart", "#depth-note", "#depth-diagnostics-title", ".map-view-control"])
      document.querySelector(selector).hidden = compact;
    $("report").hidden = compact; $("summary-map").hidden = !compact; $("download-image").hidden = compact;
    if (compact) {
      $("support-section").hidden = true;
      $("mechanism-section").hidden = true;
      $("regional-cmt-section").hidden = true;
      hideResearch();
      $("title").textContent = event.place || "推估震央";
      $("subtitle").textContent = time(event.origin) + " UTC+8 · 地震記錄摘要";
      $("notice").textContent = "此地震已逾 30 天，僅保留摘要；相位、波形、圖片與修訂歷程已移除。";
      $("download-json").href = base + "/download";
      const fields = [["發震時間", time(event.origin) + " UTC+8"], ["震央位置", event.place || "推估震央"], ["經緯度", `${fmt(event.lat, 4)}°N / ${fmt(event.lon, 4)}°E`]];
      if (Number.isFinite(event.magnitude)) fields.push([event.magnitude_type || "規模", fmt(event.magnitude)]);
      fields.push(["深度", depthDetailText(event)]);
      fields.push(["品質", event.quality_grade || "未分級"]);
      if (Number.isFinite(event.count)) fields.push(["定位資料", `${event.count} 站 / ${event.phase_count || 0} 相位`]);
      if (Number.isFinite(event.rms_s)) fields.push(["RMS / 方位缺口", `${fmt(event.rms_s, 3)} s / ${fmt(event.gap_deg, 0)}°`]);
      const others = (event.magnitudes || []).filter((m) => Number.isFinite(m.value) && m.type !== event.magnitude_type);
      if (others.length) fields.push(["其他規模", others.map((m) => `${m.type} ${fmt(m.value)}`).join("、")]);
      const sources = event.data_sources;
      if (sources?.providers?.length || sources?.networks?.length)
        fields.push(["資料來源", [...(sources.providers || []), ...(sources.networks?.length ? ["測網 " + sources.networks.join("、")] : [])].join(" · ")]);
      $("parameters").replaceChildren(...fields.flatMap(([key,value]) => [node("dt", {}, key), node("dd", {}, value)]));
      $("connection").textContent = "摘要已載入";
      await map($("summary-map"), event);
      return;
    }
    $("title").textContent = event.place || "推估震央";
    $("subtitle").textContent =
      time(event.origin) +
      " UTC+8 · " +
      (event.mode === "historical" ? "歷史驗證" : "自動觀測") +
      " · 第 " +
      event.revision +
      " 次修訂";
    // Depth reasons already appear beside the depth itself.
    const qualityReasons = (event.quality_reasons || []).filter((value) => !value.includes("深度"));
    $("notice").textContent =
      (event.mode === "historical"
        ? "歷史波形驗證，非即時通報。"
        : event.review?.status === "confirmed" ? "已人工確認。" : "自動定位，未經人工覆核。") +
      `品質等級 ${event.quality_grade || "D"}（非機率）` +
      (qualityReasons.length ? "：" + qualityReasons.join("、") : "") + "。";
    updateMapView();
    $("download-json").href = base + "/download?revision=" + event.revision;
    const fields = [
      ["發震時間", time(event.origin) + " UTC+8"],
      ["震央", `${fmt(event.lat, 4)}°N / ${fmt(event.lon, 4)}°E`],
      [
        "深度",
        depthDetailText(event),
      ],
      ["品質等級", event.quality_grade || "D"],
      ["定位資料", `${event.count} 站 / ${event.phase_count || 0} 相位`],
      [
        "RMS / 方位缺口",
        `${fmt(event.rms_s, 3)} s / ${fmt(event.gap_deg, 0)}°`,
      ],
      ["走時模型", travelModels[event.travel_model] || event.travel_model || "—"],
      ...(event.enrichment ? [
        ["震後補強", event.enrichment.source],
        ["補強更新時間", time(event.enrichment.updated_at) + " UTC+8"],
        ["採用補強站", event.enrichment.adopted_stations.join("、")],
      ] : []),
    ];
    if (event.near_solution) fields.push([
      "近站獨立定位", `${event.near_solution.count} 站 / ${event.near_solution.phase_count} 相位 · ${event.near_solution.status === "located" ? "通過" : "未通過"}`,
    ]);
    const decisions = event.support_decisions || [];
    const reasons = {
      "near-solution-not-qualified": "近站尚未通過定位門檻",
      "beyond-support-distance-corroboration-only": "超過 600 km，僅供佐證",
      "low-support-phase-score": "候選分數不足",
      "support-S-without-matching-P": "S 波缺少同站 P 波佐證",
      "inconsistent-support-SP-interval": "P／S 到時間隔不符",
      "support-fit-degraded-near-solution": "加入後近站品質退步或解偏移",
      "support-outlier": "反演未採用",
      "support-candidate": "輔助定位，半權重",
    };
    $("support-section").hidden = !decisions.length;
    table("support-rows", decisions.map(d => [d.id, d.phase, fmt(d.distance_km),
      d.accepted ? "採用" : "未採用", reasons[d.reason] || d.reason]));
    for (const m of event.magnitudes || [])
      fields.push([
        m.type,
        [Number.isFinite(m.value) ? fmt(m.value, 2) : "—", `${m.count ?? 0} 站`,
          magnitudeMethod(m.method) || magnitudeReasons[m.reason] || m.reason].filter(Boolean).join(" · "),
      ]);
    $("parameters").replaceChildren(
      ...fields.flatMap(([k, v]) => [node("dt", {}, k), node("dd", {}, v)]),
    );
    table(
      "phase-rows",
      (event.residuals || []).map((p) => [
        p.id,
        p.phase,
        time(p.observed ?? p.time),
        fmt(p.residual_s, 3),
        fmt(p.score, 3),
        p.used ? "採用" : "拒收",
      ]),
    );
    const accepted = (m, s) =>
      m.short_period_policy === "fallback" && (s.id || "").endsWith(".EH")
        ? "採用（地表短週期補位）"
        : "採用";
    table(
      "mag-rows",
      (event.magnitudes || []).flatMap((m) =>
        (m.stations || []).map((s) => [
          m.type,
          s.id || s.network + "." + s.station,
          fmt(s.magnitude, 2),
          s.distance_deg
            ? fmt(s.distance_deg) + "°"
            : fmt(s.distance_km) + " km",
          fmt(s.snr, 1),
          s.accepted ? accepted(m, s) : magnitudeReasons[s.reason] || s.reason || "拒收",
        ]),
      ),
    );
    $("depth-note").textContent =
      "可接受深度 " + (event.depth_range_km || []).map((v) => fmt(v)).join("–") +
      " km（到時 RMS 不超過最小值 0.3 s）";
    const fm = event.focal_mechanism;
    const preferredMechanism = event.preferred_mechanism;
    $("first-motion-role").textContent = preferredMechanism === "first_motion" ? "主圖代表解" : "";
    $("mechanism-section").hidden = !fm;
    if (fm) {
      $("mechanism-report").src = base + "/mechanism-image?revision=" + event.revision + "&v=20261127-best-fit-v1";
      const fmReasons = fm.reasons || [];
      $("mechanism-note").textContent =
        "P 波初動自動判讀，未經人工覆核。" + (fmReasons.length ? fmReasons.join("、") + "。" : "");
      if (!preferredMechanism && !event.mechanism_conflict) {
        $("mechanism-note").textContent += "定位圖暫無代表機制：初動解未達品質門檻，" +
          (event.regional_cmt?.status === "ambiguous" ? "區域 CMT 有多組候選。" : "亦無已核准的區域 CMT。");
      }
      if (event.mechanism_conflict) {
        const angle = event.mechanism_conflict.kagan_deg;
        const misfit = event.mechanism_conflict.polarity_misfit;
        $("mechanism-note").textContent += "初動與區域 CMT 不一致，定位圖暫無代表機制。" +
          (angle == null ? "" : " Kagan 角 " + fmt(angle, 0) + "°。") +
          (misfit == null ? "" : " CMT 預測初動不符 " + fmt(misfit * 100, 0) + "%（" +
            event.mechanism_conflict.polarity_count + " 站）。");
      }
      const regional = fm.regional_comparison;
      if (regional && fm.status !== "stale") {
        const joint = regional.joint;
        const plane = joint.plane ? joint.plane.join("° / ") + "°（研究候選）" : "觀測不足";
        const note = $("mechanism-note").textContent;
        $("mechanism-note").textContent += (/[。．.]$/.test(note) ? "" : "。") + "臺灣一維模型射線比較：初動 " +
          regional.polarity.station_count + " 站" +
          (regional.polarity.plane ? "（" + [regional.polarity.plane.strike, regional.polarity.plane.dip, regional.polarity.plane.rake].join("° / ") + "°）" : "") +
          "，合格 S/P " + joint.ratio_count +
          " 站，聯合解 " + plane + (joint.plane ? "，初動不符 " + Math.round(joint.polarity_misfit * 100) + "%" : "");
      }
    }
    const cmt = event.regional_cmt;
    $("regional-cmt-role").textContent = preferredMechanism === "regional_cmt" ? "主圖代表解" : "";
    const candidates = $("regional-cmt-candidates");
    const competing = cmt?.status === "ambiguous" ? cmt.solutions || [] : [];
    const comparison = $("regional-cmt-comparison");
    const revisionLabel = String(event.revision);
    if (comparison.dataset.revision !== revisionLabel) {
      comparison.open = false;
      comparison.dataset.revision = revisionLabel;
    }
    comparison.hidden = !competing.length;
    $("regional-cmt-comparison-label").textContent = `比較 ${competing.length} 組候選機制`;
    candidates.replaceChildren();
    const cmtImage = base + "/regional-cmt-image?revision=" + event.revision;
    const strategies = { balanced: "方位均衡", high_snr: "高訊噪比", nearby: "近站" };
    for (const solution of competing) {
      const figure = document.createElement("figure");
      const ball = document.createElement("img");
      ball.src = cmtImage + "&candidate=" + encodeURIComponent(solution.id);
      ball.alt = "候選區域矩張量海灘球";
      ball.width = ball.height = 130;
      ball.loading = "lazy";
      const description = document.createElement("figcaption");
      const strategy = strategies[solution.strategy] || solution.strategy;
      description.textContent = `${strategy} · ${solution.model} · Mw ${fmt(solution.mw, 2)} · ${fmt(solution.centroid_depth_km, 0)} km · 共同留站 VR ${fmt(solution.validation_vr_pct, 0)}%`;
      figure.append(ball, description);
      candidates.append(figure);
    }
    $("regional-cmt-section").hidden = !cmt;
    $("regional-cmt-solution").hidden = cmt?.status !== "experimental";
    if (cmt) {
      const labels = {
        too_few_stations: "合格寬頻站不足", azimuth_gap: "方位缺口過大",
        rank_deficient: "資料無法約束矩張量", unresolved_dc: "雙力偶方向無法約束",
        waveform_fit: "波形擬合不足", holdout_fit: "留一站預測未達標",
        unstable_mechanism: "機制方向不穩定", unresolved_centroid: "矩心深度或延遲尚未約束",
        sensitivity_model_missing: "缺少速度模型敏感度驗證",
        inputs_updated: "定位或波形已更新，等待重新評估",
        calculation_failed: "機制試算暫不可用",
        too_few_validation_stations: "共同留站比較所需的寬頻站不足",
        validation_geometry: "測站幾何不足以保留獨立驗證站",
        no_validated_candidate: "候選解未通過共同留站及機制品質門檻",
        competing_mechanisms: "多組機制對共同留站的預測相近",
      };
      $("regional-cmt-note").textContent = cmt.status === "experimental"
        ? `${cmt.station_count} 站 · VR ${fmt(cmt.vr_pct, 0)}% · 留一站 VR ${fmt(cmt.holdout_vr_pct, 0)}% · Kagan 角 P90 ${fmt(cmt.kagan_p90_deg, 0)}°` +
          (cmt.first_motion_check?.misfit == null ? "" :
            ` · 預測初動不符 ${fmt(cmt.first_motion_check.misfit * 100, 0)}%（${cmt.first_motion_check.count} 站）`)
        : (cmt.reasons || []).map((reason) => labels[reason] || reason).join("、");
      if (cmt.status === "experimental") {
        $("regional-cmt-ball").src = cmtImage;
        const depthRange = cmt.centroid_depth_range_km.map((value) => fmt(value, 0)).join("–");
        $("regional-cmt-source").textContent =
          `CMT Mw ${fmt(cmt.mw, 2)} · 矩心深度 ${fmt(cmt.centroid_depth_km, 0)} km（擾動範圍 ${depthRange} km${cmt.centroid_depth_resolved === false ? "，深度未約束" : ""}） · 矩心延遲 ${fmt(cmt.centroid_delay_s, 0)} 秒` +
          (cmt.validation_vr_pct == null || cmt.validation_method === "leave_one_station_out"
            ? "" : ` · 共同留站 VR ${fmt(cmt.validation_vr_pct, 0)}%`);
        $("regional-cmt-planes").textContent = "最佳雙力偶節面（走向／傾角／滑移角） " +
          cmt.nodal_planes.map((plane) => plane.map((angle) => fmt(angle, 0) + "°").join("／")).join(" · ");
        table("regional-cmt-stations", (cmt.fit_stations || []).map((site) =>
          [site.id, fmt(site.distance_km, 0), fmt(site.snr, 1), fmt(site.holdout_vr_pct, 0)],
        ));
      }
    }
    const gdms = fm?.gdms_diagnostics;
    $("gdms-polarity-details").hidden = !gdms || fm.status === "stale";
    if (gdms && fm.status !== "stale") {
      const labels = {
        used: "採用", conflicting_polarities: "同站極性衝突", not_independent: "未通過獨立站篩選", waveform_quality: "波形品質",
        measurement_rejected: "未通過判讀", geometry_rejected: "未通過走時檢查", no_waveform: "無波形",
      };
      const reasons = {
        clipped: "波形截波", response_uncorrected: "儀器響應未校正",
        vertical_orientation_unverified: "垂直方向未確認", insufficient_window: "波形時窗不足",
        gap_or_nonfinite: "波形缺口或無效樣本", no_p_travel_time: "無預測 P 到時",
        no_p_before_s: "搜尋時窗未避開 S 波", no_coincident_p_onset: "兩頻帶未同時檢出首波",
        repick_timing_disagreement: "候選到時分歧", polarity_probability_low: "極性證據不足",
        polarity_methods_disagree: "兩種判讀的極性相反", p_residual_exceeds_one_second: "P 殘差超過 1 秒",
        pre_P_baseline_ramp: "P 波前有基線斜坡", pre_P_baseline_step: "P 波前有基線跳階",
        P_window_linear_ramp: "P 波時窗有線性斜坡", P_window_level_step: "P 波時窗有跳階",
        sample_rate_unsupported: "取樣率不足", same_site_polarity_conflict: "同站或共址觀測方向相反",
      };
      $("gdms-polarity-summary").textContent =
        "GDMS 逐站初動診斷 · " + gdms.stations + " 站，採用 " +
        (gdms.dispositions.used || 0) + " 站";
      table("gdms-polarity-rows", (fm.refinement_diagnostics || [])
        .filter((row) => row.provider === "gdms")
        .map((row) => {
          const evidence = row.probabilistic;
          const p = evidence?.polarity_probabilities;
          const offset = (t) => Number.isFinite(t) && Number.isFinite(row.predicted_time)
            ? fmt(t - row.predicted_time, 3) : "—";
          const sign = row.disposition === "used" ? (row.polarity > 0 ? " ↑" : " ↓") : "";
          return [row.id, offset(row.original_time), offset(evidence?.refined_time ?? row.refined_time),
            p ? [p.up, p.down, p.unresolved].map((v) => Math.round(100 * v) + "%").join(" / ") : "—",
            (labels[row.disposition] || row.disposition) + sign, (row.reasons || []).map((reason) => reasons[reason] || reason).join(" · ")];
        }));
    }
    depthChart();
    void loadResearch();
    await waves();
    $("connection").textContent = "資料已載入";
    const revisions = await get(base + "/revisions");
    const selected = $("revision").value;
    $("revision").replaceChildren(
      node("option", { value: "" }, "最新修訂"),
      ...revisions.map((r) =>
        node("option", { value: r.revision }, "第 " + r.revision + " 次"),
      ),
    );
    $("revision").value = selected;
    table(
      "revision-rows",
      revisions.map((r) => [
        r.revision,
        time(r.saved_at),
        fmt(r.lat, 4),
        fmt(r.lon, 4),
        depthDetailText(r),
        fmt(magnitude(r), 2),
        fmt(r.rms_s, 3),
      ]),
    );
  } catch (e) {
    $("connection").textContent = "讀取失敗";
    $("notice").textContent = "無法取得事件資料：" + e.message;
  }
}
let researchKey = "",
  researchRequest = null,
  researchPick = "";
// The map footer draws the research pick, so a new pick must reload the map image.
function setResearchPick(pick) {
  if (pick === researchPick) return;
  researchPick = pick;
  updateMapView();
}
function hideResearch() {
  $("research-section").hidden = true;
  researchKey = "";
  if (!$("revision").value) setResearchPick("");
}
// No candidate passed: say so once, with the reasons most methods share.
function unresolvedVerdict(data, rows) {
  const counts = new Map();
  for (const row of rows) for (const code of new Set(row.reasons || [])) counts.set(code, (counts.get(code) || 0) + 1);
  const common = [...counts].filter(([, n]) => n * 2 >= rows.length).map(([code]) => reasonLabel(code)).slice(0, 3);
  const running = Object.values(data.jobs || {}).some((job) => ["running", "waiting"].includes(job.status));
  return "本事件的機制尚未能可靠決定" + (common.length ? "：" + common.join("、") : "") + "。" +
    (running ? "仍有方法在計算中。" : "");
}
// Automatic mechanism research of the latest revision, refreshed while the page is open.
async function loadResearch() {
  if (!event || event.summary_only || $("revision").value) return hideResearch();
  if (researchRequest || document.hidden) return;
  researchRequest = fetch(base + "/mechanisms", { cache: "no-store" });
  let data = null;
  try {
    const response = await researchRequest;
    if (response.ok) data = await response.json();
  } catch {
    data = undefined;
  } finally {
    researchRequest = null;
  }
  if (data === undefined) return; // Keep the last result through a network error.
  if (!data || !data.candidates?.length || data.revision !== event.revision) return hideResearch();
  $("research-section").hidden = false;
  setResearchPick(data.selection.representative || "");
  const key = JSON.stringify(data);
  if (key === researchKey) return;
  researchKey = key;
  $("research-state").textContent = selectionSummary(data);
  const imageBase = base + "/mechanisms/";
  const representative = data.selection.representative;
  // The server marks what passed; failed candidates arrive without a tensor (no ball).
  const current = data.candidates.filter((row) => !row.previous_location);
  const previous = data.candidates.filter((row) => row.previous_location);
  const primary = current
    .filter((row) => row.passed)
    .sort((a, b) => (b.id === representative) - (a.id === representative));
  const others = current.filter((row) => !row.passed);
  const card = (row) => {
    const element = mechanismCard(row, imageBase);
    if (row.id === representative) element.classList.add("representative");
    return element;
  };
  const agreement = agreementText(data.agreement);
  $("research-primary").replaceChildren(
    ...(primary.length
      ? primary.map(card)
      : [node("p", { class: "research-verdict" }, previous.length
        ? "定位已更新，機制重算中。"
        : unresolvedVerdict(data, others))]),
    ...(agreement ? [node("p", { class: "muted" }, agreement)] : []),
    ...(previous.length
      ? [node("p", { class: "muted" }, `依前次定位的結果（${previous.length}），僅供參考`), ...previous.map(card)]
      : []),
  );
  $("research-more").hidden = !others.length;
  $("research-more-label").textContent = `研究診斷：未通過的候選（${others.length}）`;
  $("research-others").replaceChildren(...others.map(card));
  $("research-jobs").replaceChildren(...jobLines(data));
}
setInterval(loadResearch, 60000);
document.addEventListener("visibilitychange", () => void loadResearch());
$("revision").onchange = load;
$("window").onchange = waves;
$("show-candidates").onchange = waves;
$("earlier").onclick = () => {
  end = (end ?? event.origin + 100) - Number($("window").value) / 2;
  waves();
};
$("later").onclick = () => {
  end = (end ?? event.origin + 100) + Number($("window").value) / 2;
  waves();
};
$("reset").onclick = () => {
  end = null;
  waves();
};
window.addEventListener("resize", () => {
  if (event?.summary_only) { map($("summary-map"), event).catch(() => {}); return; }
  if (event) {
    depthChart();
    waves();
  }
});
const stream = new EventSource("/api/stream?event_id=" + encodeURIComponent(id));
stream.onmessage = (m) => {
  try {
    const r = JSON.parse(m.data);
    if (r.id === id && r.revision === null) {
      $("notice").textContent = "此事件已移除或無法公開存取。";
      $("report").hidden = true; $("download-image").hidden = true; $("download-json").hidden = true;
      return;
    }
    if (r.summary_only && !event?.summary_only) { $("revision").value = ""; load(); return; }
    if ($("revision").value) return;
    if (r.id === id && r.revision && r.revision !== lastRevision) load();
  } catch {}
};
stream.onerror = () => {
  $("connection").textContent = "連線中斷 · 保留最後結果";
};
load();

function updateMapView() {
  if (!event) return;
  const url = base + "/image?revision=" + event.revision + "&view=" + $("map-view").value +
    (researchPick && !$("revision").value ? "&research=" + encodeURIComponent(researchPick) : "");
  $("report").src = url;
  $("download-image").href = url;
}
$("map-view").addEventListener("change", updateMapView);
