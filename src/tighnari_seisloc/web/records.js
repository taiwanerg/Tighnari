import { $, fmt, time, node, get, map, grade, reviewLabel, depthText } from "./common.js?v=20261127-best-fit-v1";
let data = null, page = 0, selected = null, requestVersion = 0, baseView = null;
const pageSize = 30, svg = $("records-map"), NS = "http://www.w3.org/2000/svg";
const taiwanDate = (date = new Date()) => new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit" }).format(date);
function preset(days) {
  const end = taiwanDate(), start = new Date(end + "T00:00:00+08:00");
  start.setTime(start.getTime() - (days - 1) * 86400000);
  $("start-date").value = taiwanDate(start); $("end-date").value = end;
  document.querySelectorAll("[data-days]").forEach(b => b.classList.toggle("active", Number(b.dataset.days) === days));
}
function magLabel(e) { return Number.isFinite(e.magnitude) ? `${e.magnitude_type || "M"} ${fmt(e.magnitude)}` : "無規模"; }
function select(e) {
  selected = e.id;
  const block = node("div", { class: "record-selection-body" }), values = node("dl");
  const heading = node("div", { class: "record-selection-heading" });
  heading.append(node("h3", {}, e.place || "推估震央"), node("strong", { class: "record-selection-magnitude" }, magLabel(e)));
  block.append(heading, node("time", {}, time(e.origin) + " UTC+8"));
  for (const [label, value] of [["經緯度", `${fmt(e.lat, 4)}°N / ${fmt(e.lon, 4)}°E`], ["深度", depthText(e)], ["品質", e.quality_grade || "未分級"]]) values.append(node("dt", {}, label), node("dd", {}, value));
  block.append(values, node("a", { href: "/events/" + encodeURIComponent(e.id), class: "summary-link" }, e.summary_only ? "查看地震摘要 →" : "查看地震詳情 →"));
  $("selected-record").replaceChildren(block);
  svg.querySelectorAll(".record-marker").forEach(marker => marker.classList.toggle("selected", marker.dataset.eventId === selected));
  document.querySelectorAll("#record-table tr[data-event-id]").forEach(row => row.classList.toggle("selected", row.dataset.eventId === selected));
}
async function drawMap() {
  const current = data;
  const result = await map(svg, null, [], { events: current?.items || [], fitEvents: $("fit-records").checked, onSelect: select });
  if (current !== data) return;
  baseView = svg.getAttribute("viewBox").split(" ").map(Number);
  resizeMarkers();
  $("map-empty").hidden = Boolean(current?.total);
  $("map-note").hidden = result.visible === result.total;
  $("map-note").textContent = `${result.visible} / ${result.total} 筆在圖內`;
  if (selected && current) { const item = current.items.find(e => e.id === selected); if (item) select(item); }
}
function renderTable() {
  const items = data?.items || [], count = Math.ceil(items.length / pageSize);
  $("record-page").textContent = count ? `${page + 1} / ${count}` : "0 / 0";
  $("record-prev").disabled = page === 0; $("record-next").disabled = page + 1 >= count;
  const rows = items.slice(page * pageSize, (page + 1) * pageSize).map(e => {
    const row = node("tr", { "data-event-id": e.id, class: e.id === selected ? "selected" : "" });
    const when = node("td"), place = node("td"), quality = node("td"), link = node("td");
    const button = node("button", { type: "button", class: "record-select" }, time(e.origin));
    button.onclick = () => select(e); when.append(button);
    place.append(node("strong", {}, e.place || "推估震央"), node("small", {}, `${fmt(e.lat, 4)}°N / ${fmt(e.lon, 4)}°E`));
    if (reviewLabel(e)) place.append(node("small", {}, reviewLabel(e)));
    quality.append(e.quality_grade ? grade(e) : node("span", { class: "muted" }, "未分級"));
    link.append(node("a", { href: "/events/" + encodeURIComponent(e.id) }, e.summary_only ? "摘要" : "詳細資料"));
    const gdms = node("td");
    gdms.append(e.gdms_enriched ? "是" : node("span", { class: "muted" }, "否"));
    row.append(when, place, node("td", {}, magLabel(e)), node("td", {}, depthText(e)), gdms, quality, link);
    return row;
  });
  if (!rows.length) { const cell = node("td", { colspan: "7", class: "empty" }, "此日期範圍沒有符合條件的地震記錄。"); const row = node("tr"); row.append(cell); rows.push(row); }
  $("record-table").replaceChildren(...rows);
}
function chart() {
  const monthly = (Date.parse(data.end) - Date.parse(data.start)) / 86400000 > 120;
  const counts = new Map();
  for (const entry of data.daily) { const key = monthly ? entry.date.slice(0, 7) : entry.date; counts.set(key, (counts.get(key) || 0) + entry.count); }
  const labels = [], values = [], cursor = new Date(data.start + "T00:00:00Z"), end = new Date(data.end + "T00:00:00Z");
  if (monthly) cursor.setUTCDate(1);
  while (cursor <= end) { const key = cursor.toISOString().slice(0, monthly ? 7 : 10); labels.push(key); values.push(counts.get(key) || 0); monthly ? cursor.setUTCMonth(cursor.getUTCMonth() + 1) : cursor.setUTCDate(cursor.getUTCDate() + 1); }
  const out = document.createDocumentFragment(), width = $("count-chart").clientWidth || 440, height = 180, max = Math.max(3, Math.ceil(Math.max(0, ...values) / 3) * 3), step = (width - 64) / Math.max(1, labels.length);
  function shape(tag, attrs, text = "") { const element = document.createElementNS(NS, tag); for (const [key,value] of Object.entries(attrs)) element.setAttribute(key, value); element.textContent = text; return element; }
  for (let i = 0; i <= 3; i++) { const y = 20 + i * 40; out.append(shape("path", { d: `M48 ${y}H${width - 14}`, stroke: "#294047" })); out.append(shape("text", { x: 40, y: y + 4, "text-anchor": "end", fill: "#8fa5a5", "font-size": 16 }, String(Math.round(max * (3 - i) / 3)))); }
  values.forEach((value, i) => { const h = value / max * 120, bar = shape("rect", { x: 50 + i * step, y: 140 - h, width: Math.max(0.6, step * 0.75), height: h, rx: Math.min(3, step / 4), fill: "#8edbc3" }); bar.append(shape("title", {}, `${labels[i]}：${value} 筆`)); out.append(bar); });
  if (labels.length) { out.append(shape("text", { x: 50, y: 166, fill: "#8fa5a5", "font-size": 16 }, labels[0])); out.append(shape("text", { x: width - 14, y: 166, fill: "#8fa5a5", "font-size": 16, "text-anchor": "end" }, labels.at(-1))); }
  $("count-chart").setAttribute("viewBox", `0 0 ${width} ${height}`); $("count-chart").replaceChildren(out);
  $("count-chart-title").textContent = monthly ? "每月記錄數" : "每日記錄數";
}
function distribution() {
  const labels = [["under3", "<3.0"], ["from3to4", "3.0–3.9"], ["from4to5", "4.0–4.9"], ["at_least5", "≥5.0"], ["unknown", "無規模"]];
  $("magnitude-distribution").replaceChildren(...labels.map(([key,label]) => {
    const row = node("div", { class: "distribution-row" }), track = node("div", { class: "distribution-track" }), bar = node("span");
    bar.style.width = `${data.total ? data.magnitude_counts[key] / data.total * 100 : 0}%`; track.append(bar);
    row.append(node("span", {}, label), track, node("strong", {}, data.magnitude_counts[key].toLocaleString())); return row;
  }));
}
async function load() {
  const version = ++requestVersion, params = new URLSearchParams({ start: $("start-date").value, end: $("end-date").value, mode: $("record-mode").value, status: $("record-state").value });
  params.set("exclude_no_magnitude", String($("exclude-no-magnitude").checked));
  $("record-error").hidden = true;
  if (params.get("start") > params.get("end")) { $("record-error").textContent = "開始日期不得晚於結束日期。"; $("record-error").hidden = false; return; }
  $("record-query").disabled = true; $("records-status").textContent = "查詢中…";
  try {
    const result = await get("/api/records?" + params);
    if (version !== requestVersion) return;
    data = result; page = 0; selected = data.items[0]?.id || null;
    history.replaceState(null, "", "/records?" + params);
    $("metric-total").textContent = data.total.toLocaleString(); $("metric-magnitude").textContent = data.with_magnitude.toLocaleString(); $("metric-unknown").textContent = data.without_magnitude.toLocaleString();
    $("metric-largest").textContent = data.largest ? magLabel(data.largest) : "—"; $("metric-largest").title = data.largest?.place || "";

    $("selected-record").replaceChildren(node("p", { class: "muted" }, "此範圍沒有可選取的記錄"));
    renderTable(); chart(); distribution();
    if (data.items.length) select(data.items[0]);
    $("records-status").textContent = "記錄已更新";
    try { await drawMap(); } catch { $("map-note").hidden = false; $("map-note").textContent = "地圖載入失敗，請重新查詢。"; }
  } catch (error) {
    if (version !== requestVersion) return;
    $("record-error").textContent = "無法取得記錄，請確認日期範圍不超過十年，稍後再試。"; $("record-error").hidden = false; $("records-status").textContent = "查詢失敗";
  } finally { if (version === requestVersion) $("record-query").disabled = false; }
}
$("record-filters").onsubmit = event => { event.preventDefault(); load(); };
document.querySelectorAll("[data-days]").forEach(button => button.onclick = () => { preset(Number(button.dataset.days)); load(); });
for (const id of ["start-date", "end-date"]) $(id).addEventListener("input", () => document.querySelectorAll("[data-days]").forEach(button => button.classList.remove("active")));
$("record-prev").onclick = () => { page = Math.max(0, page - 1); renderTable(); };
$("record-next").onclick = () => { page++; renderTable(); };
$("fit-records").onchange = () => drawMap().catch(() => {});
$("exclude-no-magnitude").onchange = () => load();
function resizeMarkers() {
  const matrix = svg.getScreenCTM();
  if (!matrix) return;
  const scale = Math.hypot(matrix.a, matrix.b);
  if (!scale) return;
  svg.querySelectorAll(".record-marker").forEach(marker => marker.setAttribute("r", Number(marker.dataset.radius) / scale));
}
function setView(box) {
  svg.setAttribute("viewBox", box.join(" "));
  resizeMarkers();
}
function zoom(factor) {
  if (!baseView) return;
  const [x,y,w,h] = svg.getAttribute("viewBox").split(" ").map(Number), nextWidth = Math.min(baseView[2] * 2, Math.max(baseView[2] / 12, w * factor)), nextHeight = nextWidth * h / w;
  setView([x + (w - nextWidth) / 2, y + (h - nextHeight) / 2, nextWidth, nextHeight]);
}
$("map-zoom-in").onclick = () => zoom(0.7); $("map-zoom-out").onclick = () => zoom(1 / 0.7); $("map-reset").onclick = () => { if (baseView) setView(baseView); };
let drag = null;
svg.addEventListener("pointerdown", event => { if (event.target.closest(".record-marker")) return; drag = { x: event.clientX, y: event.clientY, box: svg.getAttribute("viewBox").split(" ").map(Number) }; svg.setPointerCapture(event.pointerId); });
svg.addEventListener("pointermove", event => { if (!drag) return; const [x,y,w,h] = drag.box, rect = svg.getBoundingClientRect(); svg.setAttribute("viewBox", `${x - (event.clientX - drag.x) * w / rect.width} ${y - (event.clientY - drag.y) * h / rect.height} ${w} ${h}`); });
for (const name of ["pointerup", "pointercancel"]) svg.addEventListener(name, () => { drag = null; });
let resizeTimer; window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { if (data) chart(); drawMap().catch(() => {}); }, 200); });
preset(7);
const query = new URLSearchParams(location.search);
$("exclude-no-magnitude").checked = query.get("exclude_no_magnitude") === "true";
for (const [key,id] of [["start", "start-date"], ["end", "end-date"], ["mode", "record-mode"], ["status", "record-state"]]) if (query.has(key)) $(id).value = query.get(key);
if (query.has("start") || query.has("end")) document.querySelectorAll("[data-days]").forEach(button => button.classList.remove("active"));
load();
