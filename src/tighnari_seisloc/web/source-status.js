import { node, time } from "./common.js?v=20261118-hoshi-v1";

const stages = {
  idle: "等待符合條件的事件",
  preparing: "準備事件資料",
  preparing_download: "準備下載工作",
  checking_download: "確認下載進度",
  submitting_download: "向 GDMS 申請資料",
  waiting_portal: "等待 GDMS 製作資料",
  waiting_turn: "等待其他事件的下載工作完成",
  download_backoff: "下載暫緩，等待重試",
  reconciling_download: "確認先前申請結果，避免重複送件",
  downloading: "正在下載資料",
  validating_download: "檢查下載資料是否完整",
  download_ready: "下載資料已備妥",
  inventory_fallback: "今日儀器響應申請未成功，暫用最近一份響應檔",
  calibrating: "讀取波形與校正資料",
  analyzing: "辨識震相與重新定位",
  mechanism: "評估初動與震源機制",
  publishing: "製作報告並儲存補強結果",
  completed: "補強結果已發布",
  retained: "已評估，保留原結果",
  retry: "等待重試",
  authenticating: "正在驗證 GDMS 登入",
  auth_retry: "登入檢查重試中",
  auth_recovered: "登入已恢復",
  needs_attention: "需要手動登入 GDMS",
  insufficient_data: "資料不足，未形成可用機制",
  unconstrained: "機制未通過約束或穩定度檢查",
  experimental: "已完成實驗性機制評估",
  failed: "評估失敗",
  stale: "定位已更新，等待重新評估",
  shadow: "已評估，機制發布尚未啟用",
  waiting: "等待符合條件的事件",
  waiting_for_greens: "等待區域格林函數庫",
  inverting: "正在試算區域矩張量",
};

function reason(error) {
  if (!error) return "";
  return {
    portal_network_error: "GDMS 連線暫時失敗",
    portal_busy: "GDMS 系統忙碌",
    session_expired: "登入狀態已失效",
    manual_login_required: "GDMS 登入已失效，請手動登入",
    submission_not_reconciled: "下載申請結果尚未確認，持續查詢原申請",
    external_job_failed: "GDMS 端的資料製作失敗",
  }[error] || (error.startsWith("submission_rejected")
    ? `GDMS 拒絕下載申請（${error.split("_").at(-1)}），將延後重試`
    : error);
}

export function sourceStatus(name, s, now = Date.now() / 1000) {
  const row = node("div", { class: "source-status" });
  const background = ["gdms", "focal", "regional_cmt"].includes(name);
  const queue = s.queue;
  const count = Number.isFinite(s.count) ? s.count : s.station_count;
  row.append(node("strong", {}, {
    earthscope: "IRIS / EarthScope", geofon: "GEOFON", bats: "BATS 震後補強",
    gdms: "GDMS 震後補強", focal: "初動震源機制評估", regional_cmt: "臺灣區域 CMT",
  }[name] || name));
  const badge = name === "gdms" && queue
    ? `${queue.waiting} 筆排隊`
    : Number.isFinite(count) ? `${count} 站` : background ? "背景分析" : "站數待確認";
  row.append(node("span", { class: "source-count" }, badge));
  const phase = s.state === "retry" && s.error === "GDMS 已排入下載佇列，等待取件"
    ? s.download_stage || "waiting_portal" : s.state;
  let label = stages[phase] || phase || "等待狀態";
  if (name === "focal" && !s.active && s.event_id) label = "最近評估：" + label;
  row.append(node("span", { class: "source-state" }, label));
  const detail = (text) => row.append(node("span", { class: "source-detail" }, text));
  if (background && s.event_id) {
    const link = node("a", { class: "source-detail", href: `/events/${encodeURIComponent(s.event_id)}` },
      `${s.active ? "處理中" : "最近處理事件"}：${Number.isFinite(s.event_origin) ? time(s.event_origin) : "查看事件"}${s.event_place ? " · " + s.event_place : ""}`);
    row.append(link);
  }
  if (name === "gdms" && queue) {
    detail(`最近 24 小時：待處理 ${queue.waiting} 筆 · 已發布 ${queue.completed} 筆 · 保留原結果 ${queue.retained} 筆`);
  }
  if (name === "gdms" && Number.isFinite(s.station_count)) {
    detail(`本次 GDMS 資料：${s.station_count} 站`);
  }
  if (name === "gdms" && s.download_kind) {
    const kind = s.download_kind === "inventory" ? "測站響應資料" : `${s.download_kind} 波形`;
    detail(`資料類型：${kind}`);
    if (Number.isFinite(s.download_total) && s.download_total > 0) {
      detail(`本事件下載工作：已完成 ${s.download_completed} / 已建立 ${s.download_total} 筆（可能陸續新增）`);
    }
  }
  if (s.active && Number.isFinite(s.stage_started_at)) {
    detail(`此階段已進行 ${Math.max(0, Math.floor(now - s.stage_started_at))} 秒`);
  }
  if (s.error) detail(`原因：${reason(s.error)}`);
  if (!s.active && Number.isFinite(s.retry_at) && s.retry_at > 0) {
    detail(s.retry_at > now ? `預定重試：${time(s.retry_at)}` : "等待下次輪詢重試");
  }
  if (background && Number.isFinite(s.updated)) detail(`狀態更新：${time(s.updated)}`);
  return row;
}
