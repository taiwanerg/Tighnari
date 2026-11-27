export const $ = (id) => document.getElementById(id);
export const fmt = (v, n = 1) => (Number.isFinite(v) ? v.toFixed(n) : "—");
// "很淺" (like JMA's ごく浅い) is decided by the server and arrives as depth_display.
export const VERY_SHALLOW = "很淺";
export function depthText(e, digits = 1) {
  if (e?.depth_status === "constrained" && Number.isFinite(e.depth_km) && e.depth_km > 0)
    return `${fmt(e.depth_km, digits)} km`;
  if (e?.depth_display === VERY_SHALLOW) return VERY_SHALLOW;
  return "不明";
}
export function depthDetailText(e, digits = 1) {
  const label = depthText(e, digits);
  if (label === VERY_SHALLOW)
    return Number.isFinite(e?.depth_km) ? `${label} · 最佳擬合 ${fmt(Math.max(e.depth_km, 0), digits)} km` : label;
  if (label !== "不明") return `${label} · 有約束`;
  const reason =
    (e?.quality_reasons || []).find((value) => value.includes("深度")) ||
    (Number.isFinite(e?.depth_km) ? "深度約束不足" : "深度資料不足");
  return `${label} · ${reason}`;
}
export const waveAmplitude = (v) => fmt(v, v < 0.01 ? 5 : v < 0.1 ? 3 : 2);
export const time = (t) =>
  Number.isFinite(t)
    ? new Date(t * 1000).toLocaleString("zh-TW", {
        timeZone: "Asia/Taipei",
        hour12: false,
      })
    : "—";
export function node(tag, attrs = {}, text = "") {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  n.textContent = text;
  return n;
}
export async function get(url) {
  const r = await fetch(url);
  if (!r.ok) throw Error(`HTTP ${r.status}`);
  return r.json();
}
export function grade(e) {
  const reasons = (e.quality_reasons || []).filter((value) => !value.includes("深度"));
  const n = node(
    "span",
    {
      class: "grade " + (e.quality_grade || "D"),
      title: "品質等級，非成功機率" + (reasons.length ? "；" + reasons.join("、") : ""),
    },
    e.quality_grade || "D",
  );
  return n;
}
export function reviewLabel(e) {
  const status = e?.review?.status || (e?.status === "candidate" ? "pending" : "automatic");
  return status === "confirmed" ? "已人工確認" : status === "pending" ? "待確認" : "";
}
export function magnitude(e, kind = "ML") {
  return (
    e?.magnitudes?.find((m) => m.type === kind)?.value ??
    (kind === e?.magnitude_type ? e.magnitude : null)
  );
}
export function summary(e) {
  const out = document.createDocumentFragment();
  if (!e) {
    out.append(
      node("p", { class: "summary-empty" }, "尚無地震定位結果"),
      node(
        "p",
        { class: "summary-empty-note" },
        "收到足夠測站資料後，會自動顯示地震資訊。",
      ),
    );
    return out;
  }
  const main = node("div", { class: "event-summary" });
  const depth = depthText(e);
  const mag = node("div", { class: "primary-magnitude" }, fmt(e.summary_only ? e.magnitude : magnitude(e), 1));
  mag.append(node("span", {}, e.summary_only ? "規模 " + (e.magnitude_type || "") : "規模 ML"));
  main.append(
    mag,
    node("div", { class: "place" }, e.place || "位置計算中"),
    node(
      "div",
      {
        class: "primary-depth",
        title: depth,
      },
      `深度 ${depth}`,
    ),
    node("time", { class: "event-time" }, time(e.origin)),
  );
  if (reviewLabel(e)) main.append(node("p", { class: "muted" }, reviewLabel(e)));
  out.append(
    main,
    node(
      "a",
      { class: "summary-link", href: "/events/" + encodeURIComponent(e.id) },
      e.summary_only ? "查看地震摘要" : "查看地震詳情",
    ),
  );
  return out;
}

let geoPromise;
export async function map(svg, e, stations = [], options = {}) {
  if (!geoPromise)
    geoPromise = Promise.all([
      get("/geography/regional_land.geojson"),
      get("/geography/taiwan_towns_display.geojson"),
    ]).catch(error => { geoPromise = null; throw error; });
  const geos = await geoPromise;
  const used = new Set(e?.used || []),
    points = stations.filter((s) => !e || used.has(s.id));
  if (e) points.push(e);
  if (options.fitEvents) for (const record of options.events || []) points.push(record);
  const locationBounds = { left: 117, right: 125, low: 20, high: 27 };
  let left = 118.8, right = 122.4, low = 21.8, high = 25.4;
  for (const point of points) {
    if (!Number.isFinite(point.lat) || !Number.isFinite(point.lon)) continue;
    left = Math.min(left, point.lon); right = Math.max(right, point.lon);
    low = Math.min(low, point.lat); high = Math.max(high, point.lat);
  }
  left -= 0.35; right += 0.35; low -= 0.3; high += 0.3;
  const width = Math.max(280, svg.clientWidth || 600),
    height = Math.max(240, svg.clientHeight || 600);
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const scale = Math.min(
      (width - 70) / ((right - left) * 0.9135),
      (height - 85) / (high - low),
    ),
    cx = (left + right) / 2,
    cy = (low + high) / 2;
  const xy = (lon, lat) => [
    width / 2 + (lon - cx) * scale * 0.9135,
    (height - 35) / 2 - (lat - cy) * scale,
  ];
  const NS = "http://www.w3.org/2000/svg";
  function s(tag, attrs) {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    return n;
  }
  const out = document.createDocumentFragment();
  out.append(s("rect", { x: 0, y: 0, width, height, fill: "#101c26" }));
  for (let lat = Math.floor(low); lat <= high; lat++) {
    const y = xy(cx, lat)[1];
    out.append(
      s("path", {
        d: `M0 ${y}H${width}`,
        stroke: "#21363e",
        "stroke-width": 0.6,
      }),
    );
  }
  for (let lon = Math.floor(left); lon <= right; lon++) {
    const x = xy(lon, cy)[0];
    out.append(
      s("path", {
        d: `M${x} 0V${height}`,
        stroke: "#21363e",
        "stroke-width": 0.6,
      }),
    );
  }
  geos.forEach((geo, layer) => {
    for (const f of geo.features) {
      const polygons =
        f.geometry.type === "MultiPolygon"
          ? f.geometry.coordinates
          : [f.geometry.coordinates];
      let d = "";
      for (const poly of polygons)
        for (const ring of poly)
          d +=
            ring
              .map(
                (p, i) =>
                  (i ? "L" : "M") +
                  xy(...p)
                    .map((v) => v.toFixed(2))
                    .join(","),
              )
              .join("") + "Z";
      out.append(
        s("path", {
          d,
          fill: "#263a3d",
          stroke: layer ? "#5e7978" : "#607b7c",
          "stroke-width": layer ? 0.4 : 0.8,
          "fill-rule": "evenodd",
        }),
      );
    }
  });
  const [boundaryX, boundaryY] = xy(locationBounds.left, locationBounds.high);
  const [boundaryRight, boundaryBottom] = xy(locationBounds.right, locationBounds.low);
  const boundary = s("rect", {
    x: boundaryX, y: boundaryY,
    width: boundaryRight - boundaryX, height: boundaryBottom - boundaryY,
    fill: "none", stroke: "#21363e", "stroke-width": 3,
    "vector-effect": "non-scaling-stroke", "pointer-events": "none",
    class: "location-boundary",
  });
  const boundaryTitle = s("title", {});
  boundaryTitle.textContent = "定位範圍：北緯 20–27°、東經 117–125°";
  boundary.append(boundaryTitle);
  out.append(boundary);
  // WGS84 observed-P arrival rings come from the same geometry as report PNGs.
  for (const circle of e?.p_arrival_circles || []) {
    if (!circle.points?.length) continue;
    const path = s("path", {
      d: circle.points.map((p, i) => (i ? "L" : "M") +
        xy(...p).map((v) => v.toFixed(2)).join(",")).join("") + "Z",
      fill: "none",
      stroke: "#6da7ff",
      "stroke-width": 1.5,
      "vector-effect": "non-scaling-stroke",
      "pointer-events": "none",
    });
    const title = s("title", {});
    title.textContent = `${circle.id} P 波到時圈`;
    path.append(title);
    out.append(path);
  }
  for (const st of stations) {
    const [x, y] = xy(st.lon, st.lat);
    if (x < 0 || x > width || y < 0 || y > height - 35) continue;
    const mark = s("path", {
      d: `M${x} ${y - 6}l-6 10h12Z`,
      fill: used.has(st.id) ? "#ffc857" : "none",
      stroke: used.has(st.id) ? "#ffc857" : "#b3bec7",
      "stroke-width": 1.5,
    });
    const title = s("title", {});
    title.textContent = st.id;
    mark.append(title);
    out.append(mark);
  }
  if (e) {
    const [x, y] = xy(e.lon, e.lat),
      star = s("text", {
        x,
        y: y + 8,
        "text-anchor": "middle",
        fill: "#fb957b",
        stroke: "#0b171c",
        "stroke-width": 1,
        "font-size": 30,
        "paint-order": "stroke",
      });
    star.textContent = "★";
    out.append(star);
  }
  let visible = 0;
  for (const record of [...(options.events || [])].sort((a, b) => (b.magnitude ?? -1) - (a.magnitude ?? -1))) {
    if (!Number.isFinite(record.lat) || !Number.isFinite(record.lon)) continue;
    const [x, y] = xy(record.lon, record.lat);
    if (x >= 0 && x <= width && y >= 0 && y <= height - 35) visible++;
    const marker = s("circle", {
      cx: x, cy: y, r: 4 + Math.max(0, Math.min(7, record.magnitude ?? 0)) * 1.5,
      "data-radius": 4 + Math.max(0, Math.min(7, record.magnitude ?? 0)) * 1.5,
      "vector-effect": "non-scaling-stroke",
      fill: depthColor(record.depth_km, record.depth_status, record.depth_display), "fill-opacity": 0.72,
      stroke: record.magnitude >= 5 ? "#ffc857" : "#c6e9e0", "stroke-width": record.magnitude >= 5 ? 2 : 1,
      "stroke-dasharray": record.magnitude === null ? "3 2" : "none",
      class: "record-marker", "data-event-id": record.id,
      tabindex: "0", role: "button",
      "aria-label": `${record.place || "震央"} · ${time(record.origin)} · ${record.magnitude_type || "規模"} ${fmt(record.magnitude)}`,
    });
    const title = s("title", {});
    title.textContent = `${record.place || "震央"} · ${time(record.origin)} · ${record.magnitude_type || "規模"} ${fmt(record.magnitude)}`;
    marker.append(title);
    marker.addEventListener("click", () => options.onSelect?.(record));
    marker.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); options.onSelect?.(record); }
    });
    out.append(marker);
  }
  svg.replaceChildren(out);
  return { visible, total: (options.events || []).length };
}
export function depthColor(depth, status, display) {
  if (status !== "constrained" && display === VERY_SHALLOW) return "#8edbc3";
  return status !== "constrained" || !Number.isFinite(depth) || depth <= 0
    ? "#ffffff"
    : depth < 30 ? "#8edbc3" : depth < 70 ? "#77b9dc" : depth < 150 ? "#b8a9df" : "#edb271";
}

export function drawWave(
  canvas,
  row,
  frame,
  { fixed = false, scale = 100, stale = false } = {},
) {
  const w = canvas.clientWidth || 700,
    h = 88,
    dpr = devicePixelRatio || 1;
  canvas.width = w * dpr;
  canvas.height = h * dpr;
  const ctx = canvas.getContext("2d");
  ctx.scale(dpr, dpr);
  ctx.strokeStyle = "#253b42";
  ctx.lineWidth = 0.6;
  ctx.beginPath();
  ctx.moveTo(0, h / 2);
  ctx.lineTo(w, h / 2);
  ctx.stroke();
  for (let i = 1; i < 6; i++) {
    ctx.beginPath();
    ctx.moveTo((w * i) / 6, 0);
    ctx.lineTo((w * i) / 6, h);
    ctx.stroke();
  }
  const all = row.segments
      .flat()
      .filter((p) => p[0] >= frame.start && p[0] <= frame.end),
    amp = fixed
      ? Math.max(0.001, scale)
      : Math.max(
          0.001,
          ...all.flatMap((p) => [Math.abs(p[1]), Math.abs(p[2])]),
        );
  ctx.strokeStyle = stale ? "#7d8c98" : "#7ab8d6";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (const segment of row.segments) {
    let previous = null;
    for (const [t, min, max] of segment) {
      const x = ((t - frame.start) / (frame.end - frame.start)) * w;
      if (x < 0 || x > w) continue;
      const y = h / 2 - ((min + max) / 2 / amp) * 29;
      // Join neighboring bins; separate trace segments and actual gaps stay separate.
      if (previous && t - previous.time <= (frame.bin_s || (frame.end-frame.start)/900) * 2.1) {
        ctx.moveTo(previous.x, previous.y);
        ctx.lineTo(x, y);
      }
      previous = { time: t, x, y };
      ctx.moveTo(x, h / 2 - (min / amp) * 29);
      ctx.lineTo(x, h / 2 - (max / amp) * 29);
    }
  }
  ctx.stroke();
  for (const p of row.picks || []) {
    const x = ((p.time - frame.start) / (frame.end - frame.start)) * w;
    if (x < 0 || x > w) continue;
    const candidate = !p.adopted;
    const trigger = (p.method || "").startsWith("adaptive");
    ctx.strokeStyle = candidate ? "#8197a0" : p.phase === "P" ? "#e4bd82" : "#b8b8e6";
    ctx.setLineDash(candidate ? [3, 4] : []);
    ctx.beginPath();
    ctx.moveTo(x, 19);
    ctx.lineTo(x, h);
    ctx.stroke();
    ctx.fillStyle = ctx.strokeStyle;
    ctx.font = "13px sans-serif";
    ctx.fillText(trigger ? "觸發" : p.phase + (candidate ? "?" : ""), x + 3, 32);
    ctx.setLineDash([]);
  }
  return amp;
}
