import { reviewLabel,
  $,
  fmt,
  time,
  node,
  get,
  magnitude,
  summary,
  depthText,
  map,
  drawWave,
  waveAmplitude,
} from "./common.js?v=20261127-best-fit-v1";
import { sourceStatus } from "./source-status.js?v=20261125-inventory-fallback-v1";
let state = null,
  events = [],
  offset = 0,
  total = 0,
  focused = null,
  revision = null,
  paused = false,
  frame = null,
  rows = new Map(),
  socket,
  selection = null,
  available = [],
  subscription = "",
  lastHeartbeat = 0;
const scroll = $("wave-scroll"),
  rowHeight = 88;
let listRequest = null,
  listGeneration = 0,
  lastListUrl = "";
function eventListUrl() {
  return `/api/earthquakes?mode=${$("mode").value}&limit=20&offset=${offset}&require_ml=${$("require-ml").checked}`;
}
function refreshEvents(force = false) {
  const url = eventListUrl();
  if (!force && listRequest?.url === url) return listRequest.promise;
  const generation = ++listGeneration;
  const request = { url, promise: null };
  listRequest = request;
  request.promise = (async () => {
    try {
      const data = await get(url);
      if (generation !== listGeneration || url !== eventListUrl()) return;
      events = data.items;
      total = data.total;
      lastListUrl = url;
      lastListRefresh = Date.now();
      $("event-total").textContent = total;
      $("prev").disabled = offset === 0;
      $("next").disabled = offset + 20 >= total;
      renderEvents();
      if (!state?.event) await focusEvent();
    } catch (e) {
      if (generation !== listGeneration || url !== eventListUrl()) return;
      $("events").replaceChildren(
        node("p", { class: "empty" }, "事件列表暫時無法讀取：" + e.message),
      );
    } finally {
      if (listRequest === request) listRequest = null;
    }
  })();
  return request.promise;
}
function renderEvents() {
  const out = events.map((e) => {
    const a = node("a", {
      class: "event" + (focused === e.id ? " selected" : ""),
      href: "/events/" + encodeURIComponent(e.id),
    });
    const name = node("div");
    name.append(
      node("strong", {}, e.place || "推估震央"),
      node(
        "small",
        {},
        `${time(e.origin)}${e.mode === "historical" ? " · 歷史" : reviewLabel(e) ? " · " + reviewLabel(e) : ""}`,
      ),
    );
    const mag = node("span", { class: "mag" }, fmt(magnitude(e)));
    mag.append(node("small", {}, "ML"));
    a.append(
      name,
      mag,
      node("span", { class: "event-depth" }, depthText(e, 0)),
    );
    return a;
  });
  $("events").replaceChildren(
    ...(out.length
      ? out
      : [node("p", { class: "empty" }, "尚無符合篩選條件的地震報告。")]),
  );
}
async function focusEvent() {
  const e =
    $("mode").value === "historical"
      ? events[0]
      : state?.event || state?.latest_event;

  const key = e
    ? `${e.id}:${e.revision}:${Boolean(e.summary_only)}`
    : "none:" +
      (state?.stations || [])
        .map((s) => s.id)
        .sort()
        .join(",");
  if (key === revision) return;
  revision = key;
  focused = e?.id;
  // Stream/list summaries already contain everything shown in the summary card.
  // Keep it responsive while the detailed stations and arrival circles load.
  $("summary").replaceChildren(summary(e));
  renderEvents();
  try {
    const full = e
      ? await get("/api/earthquakes/" + encodeURIComponent(e.id))
      : null;
    if (key !== revision) return;
    $("summary").replaceChildren(summary(full));
    await map($("map"), full, full?.stations || state?.stations || []);
    $("source-badge").textContent =
      full?.mode === "historical"
        ? "實測回放"
        : state?.event
          ? "正在定位"
          : full
            ? (full.enrichment ? "BATS 震後補強" : "最近一筆")
            : "即時資料";
    renderEvents();
  } catch {
    if (key !== revision) return;
    revision = null;
    $("pipeline-state").textContent = "定位圖暫時無法載入";
  }
}
function filtered() {
  let stations = available.filter(
    (s) => selection === null || selection.has(s.id),
  );
  const q = $("search").value.toLowerCase();
  stations = stations.filter((s) =>
    (s.id + " " + s.name).toLowerCase().includes(q),
  );
  const mode = $("sort").value;
  stations.sort(
    mode === "lat"
      ? (a, b) => b.lat - a.lat
      : mode === "amplitude"
        ? (a, b) => {
            const peak = (r) =>
              Math.max(
                0,
                ...(rows.get(r.id)?.segments.flat() || []).flatMap((p) => [
                  Math.abs(p[1]),
                  Math.abs(p[2]),
                ]),
              );
            return peak(b) - peak(a);
          }
        : (a, b) => {
            const local = (s) => /^(TW\.|TQ\.|IU\.TATO\.)/.test(s.id) ? 0 : 1;
            return local(a) - local(b) || a.id.localeCompare(b.id);
          },
  );
  return stations;
}
function virtualRows() {
  const stations = filtered();
  $("wave-empty").hidden = stations.length > 0;
  if (!stations.length) {
    $("wave-empty").textContent =
      state?.mode === "offline"
        ? "離線模式：請至地震詳情查看歷史波形。"
        : available.length > 0
          ? "沒有符合搜尋或顯示選擇的測站。"
          : "等待測站波形資料";
  }
  $("wave-spacer").style.height = stations.length * rowHeight + "px";
  const first = Math.max(0, Math.floor(scroll.scrollTop / rowHeight) - 1),
    count = Math.min(16, Math.ceil(scroll.clientHeight / rowHeight) + 2);
  const visible = stations.slice(first, first + count);
  $("wave-rows").replaceChildren();
  visible.forEach((station, i) => {
    const row = rows.get(station.id),
      div = node("div", {
        class:
          "wave-row" +
          (row && Date.now() / 1000 - row.end > 90 ? " stale" : ""),
      });
    div.style.top = (first + i) * rowHeight + "px";
    const canvas = node("canvas", {
        "aria-label": station.id + " 垂直速度波形",
      }),
      label = node(
        "span",
        { class: "wave-label" },
        station.id + " · 此時間窗無資料",
      );
    div.append(canvas, label);
    $("wave-rows").append(div);
    if (row && frame) {
      const stale = Date.now() / 1000 - row.end > 90;
      const amp = drawWave(canvas, row, frame, {
        fixed: $("fixed").checked,
        scale: Number($("scale").value) || 100,
        stale,
      });
      label.textContent = `${station.id} · Z · ±${waveAmplitude(amp)} µm/s · ${Math.max(0, Math.round(Date.now() / 1000 - row.end))}s 延遲`;
    }
  });
  const sub = JSON.stringify({
    stations: paused || document.hidden ? [] : visible.map((s) => s.id),
    seconds: Number($("window").value),
    bins: Math.min(1200, Math.max(300, Math.ceil(scroll.clientWidth / 100) * 100)),
  });
  if (socket?.readyState === WebSocket.OPEN && sub !== subscription) {
    socket.send(sub);
    subscription = sub;
  }
}
function choices() {
  const list = available
    .sort((a, b) => a.id.localeCompare(b.id));
  $("station-choices").replaceChildren(
    ...list.map((s) => {
      const label = node("label"),
        input = node("input", { type: "checkbox" });
      input.checked = selection === null || selection.has(s.id);
      input.onchange = () => {
        if (selection === null) selection = new Set(available.map((s) => s.id));
        input.checked ? selection.add(s.id) : selection.delete(s.id);
        virtualRows();
      };
      label.append(input, document.createTextNode(" " + s.id));
      return label;
    }),
  );
}
function connectWave() {
  socket = new WebSocket(
    (location.protocol === "https:" ? "wss:" : "ws:") +
      "//" +
      location.host +
      "/ws/waveforms",
  );
  socket.onopen = () => {
    subscription = "";
    $("wave-status").textContent = available.length
      ? "接收波形"
      : "等待 IRIS 資料";
    virtualRows();
  };
  socket.onmessage = (message) => {
    if (paused) return;
    try {
      const data = JSON.parse(message.data);
      if (data.type === "snapshot") rows.clear();
      for (const id of data.removed || []) rows.delete(id);
      for (const row of data.rows) {
        if (data.type === "delta" && !data.replace_rows && rows.has(row.id)) {
          const old = rows.get(row.id);
          const cutoff = (row.segments.flat()[0]?.[0] ?? data.end) - 0.01;
          const kept = old.segments
            .map((seg) =>
              seg.filter((p) => p[0] >= data.start && p[0] < cutoff),
            )
            .filter((s) => s.length);
          row.segments = [...kept, ...row.segments.filter((s) => s.length)];
        }
        rows.set(row.id, row);
      }
      frame = data;
      $("wave-time").textContent =
        data.end > 0 && rows.size
          ? `波形截至 ${time(data.end)} · 垂直速度 µm/s`
          : "垂直速度 µm/s · 等待資料";
      virtualRows();
    } catch {
      $("wave-status").textContent = "波形格式錯誤";
    }
  };
  socket.onclose = () => {
    $("wave-status").textContent = "波形斷線 · 重連中";
    setTimeout(connectWave, 2500);
  };
}
$("mode").onchange = $("require-ml").onchange = () => {
  offset = 0;
  revision = null;
  refreshEvents();
};
$("prev").onclick = () => {
  offset = Math.max(0, offset - 20);
  refreshEvents();
};
$("next").onclick = () => {
  offset += 20;
  refreshEvents();
};
for (const id of ["search", "sort", "window", "fixed", "scale"])
  $(id).addEventListener("input", () => {
    if (id === "window") subscription = "";
    $("gain-note").textContent = $("fixed").checked
      ? "固定尺度 · 垂直速度 µm/s"
      : "自動縮放，不可直接比較。";
    virtualRows();
  });
$("pause").onclick = () => {
  paused = !paused;
  $("pause").textContent = paused ? "繼續" : "暫停";
  subscription = "";
  virtualRows();
};
$("live").onclick = () => {
  paused = false;
  $("pause").textContent = "暫停";
  subscription = "";
  virtualRows();
};
$("choose").onclick = () => {
  $("station-choices").hidden = !$("station-choices").hidden;
  choices();
};
document.addEventListener("visibilitychange", virtualRows);
scroll.addEventListener("scroll", virtualRows);
scroll.addEventListener(
  "wheel",
  (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    const options = [60, 120, 300, 600],
      i = options.indexOf(Number($("window").value));
    $("window").value =
      options[Math.max(0, Math.min(3, i + (e.deltaY > 0 ? 1 : -1)))];
    subscription = "";
    virtualRows();
  },
  { passive: false },
);
let resizeTimer;
window.addEventListener("resize", () => {
  virtualRows();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    revision = null;
    focusEvent();
  }, 150);
});
let listVersion = null,
  lastListRefresh = 0;
// Draw the base map independently of the stream and event-detail requests.
map($("map"), null).catch(() => {});
const stream = new EventSource("/api/stream?compact=true");
stream.onmessage = (message) => {
  try {
    state = JSON.parse(message.data);
    lastHeartbeat = Date.now();
    $("connection").textContent = "已連線";
    $("connection").classList.remove("error");
    $("clock").textContent = time(state.now) + " UTC+8";
    $("state-label").textContent =
      $("mode").value === "historical"
        ? "歷史地震"
        : state.event
          ? "地震定位"
          : state.latest_event
            ? "最近地震"
            : "等待定位";
    $("wave-status").textContent =
      state.mode === "offline"
        ? "離線模式"
        : Number.isFinite(state.source_delay_s) && Number.isFinite(state.processing_lag_s)
          ? `來源 ${Math.round(state.source_delay_s)} 秒 · 處理落後 ${Math.round(state.processing_lag_s)} 秒`
        : Number.isFinite(state.data_delay_s)
          ? `資料延遲 ${Math.round(state.data_delay_s)} 秒`
          : "等待 IRIS 資料";
    const picking = state.picking;
    const missingModels = Object.keys(picking?.methods || {}).some(m => m === "adaptive-fallback:FileNotFoundError");
    $("picker-status").textContent = missingModels && !picking.neural_stations && picking.requested === "auto"
      ? "PhaseNet 模型檔未找到：僅顯示觸發候選，自動定位暫停。請執行 setup-models。"
      : picking?.trigger_stations
      ? `${picking.neural_stations} 站可辨識 P/S · ${picking.trigger_stations} 站僅有觸發候選，無 S 波辨識`
      : picking?.neural_stations
        ? `${picking.neural_stations} 站可辨識 P/S · 虛線 P? / S? 為尚未定位採用的候選`
        : "等待相位辨識資料";
    $("picker-status").title = Object.entries(picking?.methods || {}).map(([method, n]) => `${method}: ${n} 站`).join("\n");
    const sources = Object.entries(state.sources || {});
    if (state.error || !sources.length) {
      $("pipeline-state").textContent =
        state.error || "離線模式，不接收即時資料。";
    } else {
      $("pipeline-state").replaceChildren(
        ...sources.map(([name, s]) => sourceStatus(name, s, state.now)),
      );
    }
    available = state.stations;
    const version = JSON.stringify(
      (state.earthquakes || []).map((e) => [e.id, e.revision, Boolean(e.summary_only)]),
    );
    const firstVersion = listVersion === null;
    const changed = !firstVersion && version !== listVersion;
    listVersion = version;
    // The initial HTTP list already covers the first stream snapshot. Later
    // revisions supersede in-flight responses, including matching filter URLs.
    if (changed) {
      refreshEvents(true);
    } else if (Date.now() - lastListRefresh > 60000 || lastListUrl !== eventListUrl()) {
      refreshEvents();
    }
    focusEvent();
    virtualRows();
  } catch {
    $("connection").textContent = "資料處理錯誤";
  }
};
stream.onerror = () => {
  $("connection").textContent = "連線中斷 · 重試中";
  $("connection").classList.add("error");
};
setInterval(() => {
  if (lastHeartbeat && Date.now() - lastHeartbeat > 5000) {
    $("connection").textContent = "資料逾時";
    $("connection").classList.add("error");
  }
}, 2000);
refreshEvents();
map($("map"), null).catch(() => {});
connectWave();
