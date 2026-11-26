"use strict";
const $ = id => document.getElementById(id);
let map, metadata, serviceBounds, lightsLayer, routeLayer, markerLayer, locateLayer;
let start = null, end = null, selecting = "start", busy = false, request = null;
let startName = "", endName = "", exampleInitialized = false, autoExamplePending = false;
let lastStatus = "", initialFit = false, mapStarted = false;
let lastResult = null, choices = [], selectedIndex = 0;
const distance = value => value >= 1000 ? `${(value / 1000).toFixed(2)} km` : `${Math.round(value).toLocaleString()} m`;
const minutes = meters => `약 ${Math.max(1, Math.ceil(meters / 75))}분`;
const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const escapeHtml = text => String(text).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[c]);

// ---------- 다크 모드 ----------
// 스위치를 누르면 <html>의 data-theme 값을 바꾸고, 선택을 브라우저에 기억합니다.
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme-toggle").checked = theme === "dark";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "dark" ? "#0F1217" : "#F5F6F7";
  if (choices.length) drawRoutes();
}
let savedTheme = null;
try { savedTheme = localStorage.getItem("litroute-theme"); } catch (error) { savedTheme = null; }
applyTheme(savedTheme === "light" ? "light" : "dark");
$("theme-toggle").addEventListener("change", event => {
  const theme = event.target.checked ? "dark" : "light";
  applyTheme(theme);
  try { localStorage.setItem("litroute-theme", theme); } catch (error) { /* 저장하지 못해도 화면 전환은 됩니다. */ }
});

// ---------- 공통 ----------
async function api(path, body, signal) {
  const response = await fetch(path, body === undefined ? {signal} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body), signal
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청을 처리하지 못했습니다.");
  return data;
}
function showError(message) { $("error").textContent = message; $("error").hidden = !message; }
// 버튼 안에는 글자와 아이콘(svg)이 함께 있으므로, 아이콘은 두고 글자만 바꿉니다.
function setButtonText(id, text) { $(id).firstChild.nodeValue = text; }
function updateButton() { $("find-route").disabled = !start || !end || metadata?.state !== "ready" || busy; }
function pointName(which) {
  const point = which === "start" ? start : end;
  const name = which === "start" ? startName : endName;
  if (!point) return which === "start" ? "출발지" : "도착지";
  return name || `${point[0].toFixed(5)}, ${point[1].toFixed(5)}`;
}
function renderHeading() {
  const text = `${pointName("start")} → ${pointName("end")}`;
  $("route-heading").textContent = text;
  $("mobile-route-label").textContent = text;
}
function invalidate() {
  autoExamplePending = false;
  request?.abort(); request = null; busy = false;
  if (routeLayer) routeLayer.clearLayers();
  lastResult = null; choices = []; selectedIndex = 0;
  $("results").hidden = true;
  $("route-detail").hidden = true;
  $("selected-summary").hidden = true;
  $("view-route").disabled = true;
  showError("");
  setButtonText("find-route", "밝은 길 찾기");
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
    $(which + "-label").textContent = point ? pointName(which) : `지도에서 ${which === "start" ? "출발지" : "도착지"}를 클릭하세요`;
    $(which + "-label").title = point ? `${point[0].toFixed(6)}, ${point[1].toFixed(6)}` : "";
    if (point && map) L.marker(point, {icon: L.divIcon({className: `point-marker ${which}`, html: letter, iconSize: [31, 31], iconAnchor: [15, 15]}), title: pointName(which)}).addTo(markerLayer);
  }
  renderHeading();
  updateButton();
}

// ---------- 지도 ----------
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
  locateLayer = L.layerGroup().addTo(map);
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
  $("example-note").textContent = data.example?.note || "";
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
    $("status").textContent = "앱 서버에 연결하지 못했습니다. 잠시 후 다시 시도해주세요.";
    setTimeout(poll, 5000);
  }
}

// ---------- 경로 그리기와 카드 ----------
function compareText(choice) {
  const shortest = lastResult.shortest;
  const extra = choice.length_m - shortest.length_m;
  const darkReduced = shortest.dark_m - choice.dark_m;
  if (choice.kind === "shortest") return "가장 짧은 경로입니다.";
  if (darkReduced > .5) return `최단 경로보다 ${distance(Math.max(0, extra))} 더 걷고, 어두운 구간이 ${distance(darkReduced)} 줄어요.`;
  return `최단 경로보다 ${distance(Math.max(0, extra))} 더 걷지만, 조명 차이는 크지 않아요.`;
}
function drawRoutes() {
  if (!routeLayer || !lastResult) return;
  routeLayer.clearLayers();
  const accent = cssVar("--accent") || "#4FC3F7", warn = cssVar("--warn-line") || "#FF8A65", other = cssVar("--route-other") || "#4A5160";
  choices.forEach((choice, index) => {
    if (index !== selectedIndex) L.polyline(choice.coords, {color: other, weight: 4, opacity: .8}).on("click", () => selectRoute(index, true)).addTo(routeLayer);
  });
  const selected = choices[selectedIndex];
  L.polyline(selected.coords, {color: cssVar("--surface") || "#fff", weight: 10, opacity: .9, interactive: false}).addTo(routeLayer);
  for (const segment of selected.segments) {
    const lit = segment.lit_ratio >= .5;
    L.polyline(segment.coords, {color: lit ? accent : warn, weight: 5, opacity: 1, dashArray: lit ? null : "7 7", interactive: false}).addTo(routeLayer);
  }
  for (const which of ["start", "end"]) {
    const point = which === "start" ? start : end;
    L.polyline([point, lastResult.snapped[which]], {color: "#8d968b", weight: 2, dashArray: "3 5", interactive: false}).addTo(routeLayer);
  }
}
function renderCards() {
  const list = $("route-list");
  list.innerHTML = "";
  choices.forEach((choice, index) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "route-card" + (index === selectedIndex ? " selected" : "");
    card.setAttribute("aria-pressed", String(index === selectedIndex));
    const lowLight = choice.lit_ratio < .5;
    card.innerHTML = `
      <div class="route-card-top"><span class="rank-badge">${choice.rank}</span><span class="card-name">${escapeHtml(choice.name)}</span>${choice.recommended ? '<span class="recommended-badge">추천</span>' : ""}</div>
      <div class="card-stats">
        <span><span class="stat-value">${minutes(choice.length_m)}</span><span class="stat-label">도보 시간</span></span>
        <span><span class="stat-value">${distance(choice.length_m)}</span><span class="stat-label">거리</span></span>
        <span><span class="stat-value lamp-number">${choice.light_count}개</span><span class="stat-label">경로 주변 가로등</span></span>
      </div>
      <span class="card-description${lowLight ? " warning-text" : ""}" style="display:block">조명 비율 ${Math.round(choice.lit_ratio * 100)}% · 어두운 구간 ${distance(choice.dark_m)}<br>${escapeHtml(compareText(choice))}</span>`;
    card.addEventListener("click", () => selectRoute(index, true));
    list.appendChild(card);
  });
  $("route-count").textContent = `${choices.length}개`;
}
function renderCarousel() {
  $("page-indicator").textContent = `${selectedIndex + 1} / ${choices.length}`;
  $("route-dots").innerHTML = choices.map((_, index) => `<span class="${index === selectedIndex ? "active" : ""}"></span>`).join("");
  $("previous-route").disabled = selectedIndex === 0;
  $("next-route").disabled = selectedIndex === choices.length - 1;
}
function renderSummary() {
  const choice = choices[selectedIndex];
  $("summary-name").textContent = choice.name;
  $("summary-time").textContent = minutes(choice.length_m);
  $("summary-distance").textContent = distance(choice.length_m);
  $("summary-lights").textContent = `가로등 ${choice.light_count}개`;
  $("selected-summary").hidden = !$("route-detail").hidden;
}
function renderDetail() {
  const choice = choices[selectedIndex];
  $("detail-name").textContent = `${choice.name} · ${pointName("start")} → ${pointName("end")}`;
  $("detail-stats").textContent = `${distance(choice.length_m)} · ${minutes(choice.length_m)} · 조명 비율 ${Math.round(choice.lit_ratio * 100)}% · 어두운 구간 ${distance(choice.dark_m)} · 경로 주변 가로등 ${choice.light_count}개`;
  $("comparison").textContent = compareText(choice);
  const warning = choice.recommended ? lastResult.warning : "";
  $("warning").hidden = !warning; $("warning").textContent = warning || "";
  $("snap-note").textContent = `선택 지점에서 가까운 도로까지 ${lastResult.snapped.offsets_m.map(distance).join(" / ")} 연결됩니다. 점선 연결 구간은 거리와 조명 계산에 포함하지 않았습니다. 도보 시간은 분당 75m 기준입니다.`;
}
function selectRoute(index, scroll) {
  if (!choices.length) return;
  selectedIndex = Math.max(0, Math.min(choices.length - 1, index));
  const cards = $("route-list").children;
  for (let i = 0; i < cards.length; i++) {
    cards[i].classList.toggle("selected", i === selectedIndex);
    cards[i].setAttribute("aria-pressed", String(i === selectedIndex));
  }
  if (scroll && cards[selectedIndex]) cards[selectedIndex].scrollIntoView({behavior: "smooth", block: "nearest", inline: "start"});
  drawRoutes(); renderCarousel(); renderSummary(); renderDetail();
}
async function findRoute() {
  if (!start || !end || metadata?.state !== "ready" || busy) return;
  invalidate(); busy = true; updateButton();
  setButtonText("find-route", "길을 비교하고 있어요…");
  const controller = new AbortController(); request = controller;
  try {
    const result = await api("/api/routes", {start, end, dark_penalty: Number($("preference").value), max_detour: Number($("detour").value)}, controller.signal);
    if (request !== controller) return;
    lastResult = result;
    choices = result.choices?.length ? result.choices : [
      {...result.recommended, name: "밝은 길", kind: "recommended", recommended: true, rank: 1, light_count: 0},
      {...result.shortest, name: "최단 경로", kind: "shortest", recommended: false, rank: 2, light_count: 0}];
    selectedIndex = Math.max(0, choices.findIndex(choice => choice.recommended));
    renderCards();
    selectRoute(selectedIndex, false);
    map.fitBounds(L.latLngBounds([...choices.flatMap(choice => choice.coords), start, end]), {padding: [50, 65], maxZoom: 17});
    $("results").hidden = false;
    $("view-route").disabled = false;
    $("app-shell").classList.remove("editing");
    $("map-hint").textContent = "카드를 눌러 경로를 비교해보세요 · 지도를 클릭하면 지점을 바꿀 수 있어요";
  } catch (error) {
    if (error.name !== "AbortError") showError(error.message);
  } finally {
    if (request === controller) { busy = false; request = null; setButtonText("find-route", "밝은 길 찾기"); updateButton(); }
  }
}
function loadExample() {
  if (!metadata || !map) return;
  invalidate(); start = [...metadata.example.start]; end = [...metadata.example.end];
  startName = metadata.example.start_name || ""; endName = metadata.example.end_name || "";
  renderPoints(); selectPoint("end"); map.fitBounds([start, end], {padding: [65, 65]});
  if (metadata.state === "ready") findRoute();
  else autoExamplePending = true;
}

// ---------- 버튼 연결 ----------
$("choose-start").addEventListener("click", () => selectPoint("start"));
$("choose-end").addEventListener("click", () => selectPoint("end"));
$("find-route").addEventListener("click", findRoute);
$("example").addEventListener("click", loadExample);
$("reset").addEventListener("click", () => { invalidate(); start = null; end = null; startName = ""; endName = ""; renderPoints(); selectPoint("start"); });
$("swap").addEventListener("click", () => { invalidate(); [start, end] = [end, start]; [startName, endName] = [endName, startName]; renderPoints(); selectPoint(start ? "end" : "start"); });
for (const id of ["preference", "detour"]) $(id).addEventListener("change", invalidate);
$("fit-area").addEventListener("click", () => { if (serviceBounds) map.fitBounds(serviceBounds, {padding: [35, 55]}); });
$("show-lights").addEventListener("click", () => {
  if (!map) return;
  const show = $("show-lights").getAttribute("aria-pressed") !== "true";
  $("show-lights").setAttribute("aria-pressed", String(show));
  if (show) lightsLayer.addTo(map); else map.removeLayer(lightsLayer);
});
$("previous-route").addEventListener("click", () => selectRoute(selectedIndex - 1, true));
$("next-route").addEventListener("click", () => selectRoute(selectedIndex + 1, true));
$("view-route").addEventListener("click", () => {
  if (!choices.length) return;
  renderDetail();
  $("route-detail").hidden = false; $("selected-summary").hidden = true;
  map.fitBounds(L.latLngBounds(choices[selectedIndex].coords), {padding: [60, 60], maxZoom: 18});
});
$("close-detail").addEventListener("click", () => { $("route-detail").hidden = true; if (choices.length) $("selected-summary").hidden = false; });
$("edit-route").addEventListener("click", () => $("app-shell").classList.toggle("editing"));
$("locate").addEventListener("click", () => {
  if (!map) return;
  if (!navigator.geolocation) { showError("이 브라우저에서는 현재 위치를 확인할 수 없습니다."); return; }
  navigator.geolocation.getCurrentPosition(position => {
    const point = [position.coords.latitude, position.coords.longitude];
    locateLayer.clearLayers();
    L.circleMarker(point, {radius: 7, color: "#fff", weight: 2, fillColor: cssVar("--accent") || "#4FC3F7", fillOpacity: 1}).addTo(locateLayer);
    if (serviceBounds && !serviceBounds.contains(point)) { showError("현재 위치가 데이터 영역(서울 동작구) 밖에 있습니다."); map.setView(point, 15); }
    else map.setView(point, 17);
  }, () => showError("현재 위치를 가져오지 못했습니다. 브라우저의 위치 권한을 확인해주세요."), {enableHighAccuracy: true, timeout: 10000});
});
$("retry").addEventListener("click", async () => { try { renderMetadata(await api("/api/prepare", {})); lastStatus = ""; poll(); } catch (error) { showError(error.message); } });

initMap(); poll();
