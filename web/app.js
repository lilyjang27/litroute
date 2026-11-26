"use strict";
const $ = id => document.getElementById(id);
let map, metadata, serviceBounds, lightsLayer, routeLayer, markerLayer;
let start = null, end = null, selecting = "start", busy = false, request = null;
let startName = "", endName = "", exampleInitialized = false, autoExamplePending = false;
let lastStatus = "", initialFit = false, mapStarted = false;
const distance = value => value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${Math.round(value).toLocaleString()} m`;

async function api(path, body, signal) {
  const response = await fetch(path, body === undefined ? {signal} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청을 처리하지 못했습니다.");
  return data;
}
function showError(message) { $("error").textContent = message; $("error").hidden = !message; }
function updateButton() { $("find-route").disabled = !start || !end || metadata?.state !== "ready" || busy; }
function invalidate() {
  autoExamplePending = false;
  request?.abort(); request = null; busy = false;
  if (routeLayer) routeLayer.clearLayers();
  $("results").hidden = true; showError("");
  $("find-route").firstElementChild.textContent = "밝은 길 찾기";
  updateButton();
}
function selectPoint(which) {
  selecting = which;
  for (const name of ["start", "end"]) {
    $("choose-" + name).classList.toggle("active", which === name);
    $("choose-" + name).setAttribute("aria-pressed", String(which === name));
  }
  $("map-hint").textContent = `지도에서 ${which === "start" ? "출발지 A" : "도착지 B"}를 클릭하세요`;
}
function renderPoints() {
  markerLayer?.clearLayers();
  for (const [which, point, letter] of [["start", start, "A"], ["end", end, "B"]]) {
    const name = which === "start" ? startName : endName;
    $(which + "-label").textContent = point ? name || `${point[0].toFixed(5)}, ${point[1].toFixed(5)}` : `지도에서 ${which === "start" ? "출발지" : "도착지"}를 클릭하세요`;
    $(which + "-label").title = point ? `${point[0].toFixed(6)}, ${point[1].toFixed(6)}` : "";
    if (point && map) L.marker(point, {icon: L.divIcon({className: `point-marker ${which}`, html: letter, iconSize: [31, 31], iconAnchor: [15, 15]}), title: name || (which === "start" ? "선택한 출발지" : "선택한 도착지")}).addTo(markerLayer);
  }
  updateButton();
}
function initMap() {
  if (!window.L || mapStarted) return;
  mapStarted = true;
  map = L.map("map", {zoomControl: false, preferCanvas: true}).setView([37.502, 126.946], 14);
  L.control.zoom({position: "bottomright"}).addTo(map);
  const tiles = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors'
  }).addTo(map);
  let tileFailures = 0;
  tiles.on("tileerror", () => { if (++tileFailures >= 4) { $("map-error").textContent = "배경 지도를 불러오지 못했습니다. 인터넷 연결을 확인해주세요."; $("map-error").hidden = false; } });
  tiles.on("tileload", () => { $("map-error").hidden = true; tileFailures = 0; });
  lightsLayer = L.layerGroup().addTo(map);
  routeLayer = L.layerGroup().addTo(map);
  markerLayer = L.layerGroup().addTo(map);
  map.on("click", event => {
    if (!serviceBounds) return;
    if (!serviceBounds.contains(event.latlng)) { showError("점선으로 표시한 데이터 영역 안에서 선택해주세요."); return; }
    invalidate();
    const point = [event.latlng.lat, event.latlng.lng];
    if (selecting === "start") { start = point; startName = ""; selectPoint("end"); }
    else { end = point; endName = ""; if (!start) selectPoint("start"); else $("map-hint").textContent = "준비됐어요. 밝은 길 찾기를 눌러보세요."; }
    renderPoints();
  });
  api("/api/lights").then(data => {
    const renderer = L.canvas({padding: .3});
    for (const point of data.coords) L.circleMarker(point, {renderer, radius: 2.2, stroke: false, fillColor: "#c39c39", fillOpacity: .65, interactive: false}).addTo(lightsLayer);
  }).catch(error => showError("조명 위치를 불러오지 못했습니다: " + error.message));
}
function renderMetadata(data) {
  metadata = data;
  $("light-count").textContent = data.light_count.toLocaleString();
  $("source-info").textContent = `출처: ${data.source_file} · ${data.source_dates.join(", ") || "기준일 미제공"}`;
  $("status").textContent = data.message;
  $("retry").hidden = data.state !== "error";
  if (map && !initialFit) {
    initialFit = true;
    serviceBounds = L.latLngBounds(data.bounds);
    L.rectangle(serviceBounds, {color: "#668462", weight: 1, dashArray: "5 6", fillOpacity: .015, interactive: false}).addTo(map);
    map.fitBounds(serviceBounds, {padding: [35, 55]});
  }
  updateButton();
  if (map && !exampleInitialized) {
    exampleInitialized = true;
    loadExample();
  } else if (autoExamplePending && data.state === "ready") {
    autoExamplePending = false;
    findRoute();
  }
}
async function poll() {
  try {
    const data = await api("/api/status");
    if (!window.L && data.state === "ready") {
      $("map-error").textContent = "지도 라이브러리가 준비됐습니다. 화면을 새로고침해주세요.";
      $("map-error").hidden = false;
    }
    initMap();
    if (lastStatus !== JSON.stringify(data)) { renderMetadata(data); lastStatus = JSON.stringify(data); }
    if (data.state === "loading") setTimeout(poll, 2000);
  } catch (error) {
    $("status").textContent = "앱 서버에 연결하지 못했습니다. python app.py 실행 상태를 확인해주세요.";
    setTimeout(poll, 5000);
  }
}
function fillCard(name, route) {
  $(name + "-distance").textContent = distance(route.length_m);
  $(name + "-time").textContent = `약 ${Math.max(1, Math.ceil(route.length_m / 75))}분`;
  $(name + "-ratio").textContent = `${Math.round(route.lit_ratio * 100)}%`;
  $(name + "-dark").textContent = distance(route.dark_m);
}
async function findRoute() {
  if (!start || !end || metadata?.state !== "ready" || busy) return;
  invalidate(); busy = true; updateButton();
  $("find-route").firstElementChild.textContent = "길을 비교하고 있어요…";
  const controller = new AbortController(); request = controller;
  try {
    const result = await api("/api/routes", {start, end, dark_penalty: Number($("preference").value), max_detour: Number($("detour").value)}, controller.signal);
    if (request !== controller) return;
    L.polyline(result.shortest.coords, {color: "#738493", weight: 5, opacity: .85, dashArray: "8 9"}).addTo(routeLayer);
    L.polyline(result.recommended.coords, {color: "#fff", weight: 9, opacity: .9}).addTo(routeLayer);
    for (const segment of result.recommended.segments) L.polyline(segment.coords, {color: segment.lit_ratio >= .5 ? "#347355" : "#dda446", weight: 5, opacity: 1}).addTo(routeLayer);
    for (const which of ["start", "end"]) {
      const selected = which === "start" ? start : end;
      L.polyline([selected, result.snapped[which]], {color: "#8d968b", weight: 2, dashArray: "3 5"}).addTo(routeLayer);
    }
    map.fitBounds(L.latLngBounds([...result.recommended.coords, ...result.shortest.coords, start, end]), {padding: [50, 65], maxZoom: 17});
    fillCard("recommended", result.recommended); fillCard("shortest", result.shortest);
    $("comparison").textContent = result.dark_reduced_m > .5 ? `${distance(result.extra_distance_m)} 더 걸으면, 어두운 구간이 ${distance(result.dark_reduced_m)} 줄어들어요.` : "이 구간에서는 최단 경로가 밝은 길 추천과 같거나, 조명 차이가 거의 없어요.";
    $("warning").hidden = !result.warning; $("warning").textContent = result.warning || "";
    $("snap-note").textContent = `선택 지점에서 가까운 도로까지 ${result.snapped.offsets_m.map(distance).join(" / ")} 연결됩니다. 점선 연결 구간은 거리와 조명 계산에 포함하지 않았습니다. 도보 시간은 분당 75m 기준입니다.`;
    $("results").hidden = false;
    $("results").scrollIntoView({behavior: "smooth", block: "nearest"});
    $("map-hint").textContent = "두 경로를 비교해보세요 · 지도를 클릭하면 지점을 바꿀 수 있어요";
  } catch (error) {
    if (error.name !== "AbortError") showError(error.message);
  } finally {
    if (request === controller) { busy = false; request = null; $("find-route").firstElementChild.textContent = "밝은 길 찾기"; updateButton(); }
  }
}
$("choose-start").addEventListener("click", () => selectPoint("start"));
$("choose-end").addEventListener("click", () => selectPoint("end"));
$("find-route").addEventListener("click", findRoute);
function loadExample() {
  if (!metadata || !map) return;
  invalidate(); start = [...metadata.example.start]; end = [...metadata.example.end];
  startName = metadata.example.start_name || ""; endName = metadata.example.end_name || "";
  renderPoints(); selectPoint("end"); map.fitBounds([start, end], {padding: [65, 65]});
  if (metadata.state === "ready") findRoute();
  else autoExamplePending = true;
}
$("example").addEventListener("click", loadExample);
$("reset").addEventListener("click", () => { invalidate(); start = null; end = null; startName = ""; endName = ""; renderPoints(); selectPoint("start"); });
$("swap").addEventListener("click", () => { invalidate(); [start, end] = [end, start]; [startName, endName] = [endName, startName]; renderPoints(); selectPoint(start ? "end" : "start"); });
for (const id of ["preference", "detour"]) $(id).addEventListener("change", invalidate);
$("fit-area").addEventListener("click", () => { if (serviceBounds) map.fitBounds(serviceBounds, {padding: [35, 55]}); });
$("show-lights").addEventListener("change", event => { if (!map) return; if (event.target.checked) lightsLayer.addTo(map); else map.removeLayer(lightsLayer); });
$("retry").addEventListener("click", async () => { try { renderMetadata(await api("/api/prepare", {})); lastStatus = ""; poll(); } catch (error) { showError(error.message); } });
initMap(); poll();
