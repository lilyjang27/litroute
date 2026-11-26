"""Local Lit Route web app. Run: python app.py; prepare only: python app.py --prepare."""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import numpy as np
import requests
from scipy.spatial import cKDTree

from lit_route import Edge, LitRouter, LocalProjector, haversine, load_streetlights

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
# 점(.)으로 시작하는 폴더는 GitHub 웹 업로드에서 빠질 수 있어 이름을 cache로 씁니다.
CACHE = ROOT / "cache"
DEFAULT_CSV = ROOT / "dongjak_lighting_locations.csv"
USER_AGENT = "LitRouteLocalPrototype/0.1 (OpenStreetMap walking route demo)"
EXAMPLE_ROUTE = {
    "start": [37.4968388, 126.9572561],
    "end": [37.4959585, 126.9541188],
    "start_name": "숭실대학교 진리관",
    "end_name": "숭실대입구역 3번 출구",
    "note": "진리관 앞 연결된 보행로에서 출발합니다. 교내 조명은 공공데이터에 누락될 수 있습니다.",
    "sources": ["https://www.openstreetmap.org/way/1381653456",
                "https://www.openstreetmap.org/node/3401294935"],
}


def download(url, *, params=None, timeout=150):
    response = requests.get(url, params=params, timeout=(15, timeout),
                            headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return response


def prepare_assets():
    vendor = WEB / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    for name in ("leaflet.js", "leaflet.css", "LICENSE"):
        path = vendor / name
        if path.exists():
            continue
        suffix = name if name == "LICENSE" else f"dist/{name}"
        errors = []
        for base in ("https://unpkg.com/leaflet@1.9.4/", "https://cdn.jsdelivr.net/npm/leaflet@1.9.4/"):
            try:
                path.write_bytes(download(base + suffix, timeout=30).content)
                break
            except requests.RequestException as exc:
                errors.append(str(exc))
        else:
            raise RuntimeError(f"지도 라이브러리를 다운로드하지 못했습니다: {errors[-1]}")


class Application:
    def __init__(self, csv_path):
        self.csv_path = Path(csv_path).resolve()
        self.raw_lights = load_streetlights(str(self.csv_path))
        if not len(self.raw_lights):
            raise ValueError("CSV에서 유효한 위도·경도를 찾지 못했습니다.")
        self.lights = np.unique(self.raw_lights, axis=0)
        self.bounds = [self.lights.min(axis=0).tolist(), self.lights.max(axis=0).tolist()]
        self.center = self.lights.mean(axis=0).tolist()
        self.light_projector = LocalProjector(*self.center)
        self.light_tree = cKDTree(self.light_projector.to_xy(self.lights[:, 0], self.lights[:, 1]))
        self.router = None
        self.state = "loading"
        self.message = "보행자 도로망을 준비하고 있습니다. 처음에는 다운로드에 시간이 걸립니다."
        self.lock = threading.Lock()
        self.worker = None
        self.source_dates = []
        with self.csv_path.open(encoding="utf-8-sig", newline="") as stream:
            self.source_dates = sorted({row.get("source_date", "") for row in csv.DictReader(stream)} - {""})

    def set_status(self, state, message):
        self.state, self.message = state, message
        print(message, flush=True)

    def prepare(self):
        try:
            CACHE.mkdir(exist_ok=True)
            self.set_status("loading", "지도 라이브러리를 확인하고 있습니다.")
            prepare_assets()
            signature = hashlib.sha256(self.csv_path.read_bytes() + b"litroute-walk-v3-radius20-step5").hexdigest()[:16]
            graph_path = CACHE / f"router-{signature}.json.gz"
            if graph_path.exists():
                self.set_status("loading", "저장된 도로망과 조명 점수를 불러오고 있습니다.")
                with gzip.open(graph_path, "rt", encoding="utf-8") as stream:
                    cached = json.load(stream)
                router = LitRouter()
                for nid, lat, lon in cached["nodes"]:
                    router.add_node(nid, lat, lon)
                for u, v, length, ratio, coords in cached["edges"]:
                    router.adj[u].append(Edge(v, length, ratio, coords))
            else:
                south, west = self.bounds[0]
                north, east = self.bounds[1]
                bbox = f"{south-.006},{west-.006},{north+.006},{east+.006}"
                raw_path = CACHE / f"osm-{hashlib.sha256(bbox.encode()).hexdigest()[:12]}.json"
                if raw_path.exists():
                    raw = json.loads(raw_path.read_text(encoding="utf-8"))
                else:
                    self.set_status("loading", "동작구 주변의 실제 보행자 도로를 다운로드하고 있습니다.")
                    query = ('[out:json][timeout:120];way["highway"]'
                             '["highway"!~"^(motorway|motorway_link|trunk|trunk_link|construction|proposed|raceway)$"]'
                             '["area"!="yes"]["access"!~"^(private|no)$"]["foot"!="no"]'
                             f'({bbox});(._;>;);out body;')
                    errors = []
                    for endpoint in ("https://overpass.private.coffee/api/interpreter",
                                     "https://overpass-api.de/api/interpreter",
                                     "https://overpass.kumi.systems/api/interpreter"):
                        try:
                            raw = download(endpoint, params={"data": query}).json()
                            if raw.get("remark") or not raw.get("elements"):
                                raise ValueError(raw.get("remark", "도로 데이터가 비어 있습니다."))
                            raw_path.write_text(json.dumps(raw), encoding="utf-8")
                            break
                        except (requests.RequestException, ValueError) as exc:
                            errors.append(f"{endpoint}: {exc}")
                            print(errors[-1], flush=True)
                    else:
                        raise RuntimeError("도로망을 다운로드하지 못했습니다. 인터넷 연결을 확인하고 다시 시도해주세요.")
                router = LitRouter()
                for item in raw["elements"]:
                    if item["type"] == "node":
                        router.add_node(item["id"], item["lat"], item["lon"])
                seen = set()
                for item in raw["elements"]:
                    if item["type"] != "way":
                        continue
                    foot_direction = item.get("tags", {}).get("oneway:foot", "no")
                    ids = item["nodes"]
                    if foot_direction == "-1":
                        ids = ids[::-1]
                    directed = foot_direction in ("yes", "1", "true", "-1")
                    for u, v in zip(ids, ids[1:]):
                        u, v = str(u), str(v)
                        if u not in router.nodes or v not in router.nodes or u == v:
                            continue
                        pairs = [(u, v)] if directed else [(u, v), (v, u)]
                        for a, b in pairs:
                            if (a, b) not in seen:
                                router.add_edge(a, b, bidirectional=False)
                                seen.add((a, b))
                connected = {u for u, edges in router.adj.items() if edges}
                connected.update(e.v for edges in router.adj.values() for e in edges)
                router.nodes = {u: coord for u, coord in router.nodes.items() if u in connected}
                router.adj = {u: router.adj[u] for u in connected}
                if not router.nodes:
                    raise ValueError("사용할 수 있는 보행자 도로가 없습니다.")
                self.set_status("loading", f"도로 {len(seen):,}개 구간의 조명 비율을 계산하고 있습니다.")
                router.apply_streetlights(self.lights)
                cached = {"nodes": [[u, *coord] for u, coord in router.nodes.items()],
                          "edges": [[u, e.v, e.length, e.lit_ratio, e.geometry]
                                    for u, edges in router.adj.items() for e in edges]}
                temp_path = graph_path.with_suffix(".tmp")
                with gzip.open(temp_path, "wt", encoding="utf-8") as stream:
                    json.dump(cached, stream, separators=(",", ":"))
                temp_path.replace(graph_path)
            self.router = router
            self.set_status("ready", "준비 완료 · 지도에서 출발지와 도착지를 선택하세요.")
        except Exception as exc:
            import traceback
            traceback.print_exc()
            self.set_status("error", "데이터 준비에 실패했습니다. 인터넷 연결과 CSV 파일을 확인한 뒤 다시 시도해주세요.")

    def start_prepare(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return
            self.state = "loading"
            self.worker = threading.Thread(target=self.prepare, daemon=True)
            self.worker.start()

    def metadata(self):
        return {"state": self.state, "message": self.message, "bounds": self.bounds,
                "center": self.center, "row_count": len(self.raw_lights), "light_count": len(self.lights),
                "source_file": self.csv_path.name, "source_dates": self.source_dates,
                "node_count": len(self.router.nodes) if self.router else 0,
                "example": EXAMPLE_ROUTE}

    def route_light_count(self, coords):
        xy = self.light_projector.to_xy(*np.asarray(coords).T)
        samples = [xy[:1]]
        for a, b in zip(xy[:-1], xy[1:]):
            steps = max(1, math.ceil(np.linalg.norm(b - a) / 5))
            samples.append(a + (b - a) * np.linspace(0, 1, steps + 1)[1:, None])
        nearby = self.light_tree.query_ball_point(np.concatenate(samples), 20)
        return len({index for group in nearby for index in group})

    def route_choices(self, result, source, destination, penalty, detour):
        """Find up to five distinct real routes, keeping the requested detour cap."""
        choices, seen, usage = [], set(), {}
        limit = result["shortest"]["length_m"] * detour + .1

        def add(summary, kind):
            key = tuple(summary["nodes"])
            if key in seen or summary["length_m"] > limit:
                return
            seen.add(key)
            choice = dict(summary)
            choice.update(id=f"route-{len(choices)+1}", kind=kind,
                          recommended=key == tuple(result["recommended"]["nodes"]))
            choices.append(choice)

        add(result["shortest"], "shortest")
        add(result["recommended"], "recommended")
        for attempt in range(18):
            if len(choices) >= 5:
                break
            path = self.router._astar(source, destination, penalty, None, edge_usage=usage)
            if path is None:
                break
            summary = self.router._summarize(path)
            add(summary, "alternative")
            for u, edge in path:
                key = tuple(sorted((u, edge.v)))
                usage[key] = usage.get(key, 0) + 1
        alternative_number = 0
        for rank, choice in enumerate(choices, 1):
            if choice["recommended"]:
                choice["name"] = "밝은 길" if choice["kind"] != "shortest" else "밝은 길 · 최단 경로"
            elif choice["kind"] == "shortest":
                choice["name"] = "최단 경로"
            else:
                alternative_number += 1
                choice["name"] = f"대안 경로 {alternative_number}"
            choice["rank"] = rank
            choice["light_count"] = self.route_light_count(choice["coords"])
        return choices

    def route(self, body):
        if not self.router:
            raise RuntimeError("도로 데이터를 준비하고 있습니다.")
        points = []
        for key in ("start", "end"):
            point = body.get(key)
            if (not isinstance(point, list) or len(point) != 2 or
                    any(type(v) not in (int, float) or not math.isfinite(v) for v in point)):
                raise ValueError("출발지와 도착지는 [위도, 경도] 형식이어야 합니다.")
            if not all(self.bounds[0][i] <= point[i] <= self.bounds[1][i] for i in (0, 1)):
                raise ValueError("가로등 데이터를 제공하는 지도 내 표시 영역에서 선택해주세요.")
            points.append(tuple(point))
        penalty = body.get("dark_penalty", 3)
        detour = body.get("max_detour", 1.5)
        for value, low, high in ((penalty, 0, 10), (detour, 1, 2)):
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError("경로 옵션 값이 허용 범위를 벗어났습니다.")
        snapped = [self.router.nearest_node(*p) for p in points]
        offsets = [haversine(*p, *self.router.nodes[n]) for p, n in zip(points, snapped)]
        if max(offsets) > 120:
            raise ValueError("선택한 지점이 보행자 도로에서 120m 이상 떨어져 있습니다. 도로 가까이 선택해주세요.")
        if snapped[0] == snapped[1]:
            raise ValueError("출발지와 도착지가 같은 도로 지점입니다. 조금 더 떨어진 지점을 선택해주세요.")
        result = self.router.recommend(*points, dark_penalty=penalty, max_detour=detour)
        if "error" in result:
            raise ValueError(result["error"])
        result["snapped"] = {"start": self.router.nodes[snapped[0]], "end": self.router.nodes[snapped[1]],
                             "offsets_m": [round(d, 1) for d in offsets]}
        result["choices"] = self.route_choices(result, *snapped, penalty, detour)
        return result


def make_handler(application):
    class Handler(BaseHTTPRequestHandler):
        def json_response(self, data, status=200):
            content = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/api/status":
                self.json_response(application.metadata())
                return
            if path == "/api/lights":
                self.json_response({"coords": application.lights.tolist()})
                return
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                     "/style.css": ("style.css", "text/css"),
                     "/vendor/leaflet.js": ("vendor/leaflet.js", "text/javascript"),
                     "/vendor/leaflet.css": ("vendor/leaflet.css", "text/css")}
            if path not in files or not (WEB / files[path][0]).exists():
                self.json_response({"error": "파일을 찾을 수 없습니다."}, 404)
                return
            filename, mime = files[path]
            content = (WEB / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("요청 크기가 올바르지 않습니다.")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError("JSON 객체가 필요합니다.")
                if path == "/api/prepare":
                    application.start_prepare()
                    self.json_response(application.metadata(), 202)
                elif path == "/api/routes":
                    if not application.router:
                        self.json_response({"error": application.message}, 503)
                        return
                    self.json_response(application.route(body))
                else:
                    self.json_response({"error": "API를 찾을 수 없습니다."}, 404)
            except (ValueError, TypeError) as exc:
                self.json_response({"error": str(exc)}, 400)
            except Exception:
                import traceback
                traceback.print_exc()
                self.json_response({"error": "경로 계산에 실패했습니다. 다른 지점을 선택해주세요."}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    # 배포 서비스(Render 등)는 PORT 환경변수로 포트를 알려 줍니다. 없으면 로컬용 8000을 씁니다.
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    # 배포 환경(PORT가 있을 때)에서는 외부 접속을 받도록 0.0.0.0, 로컬에서는 127.0.0.1을 씁니다.
    parser.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"))
    parser.add_argument("--prepare", action="store_true")
    args = parser.parse_args()
    application = Application(args.csv)
    if args.prepare:
        application.prepare()
        if application.state != "ready":
            raise SystemExit(1)
        return
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    application.start_prepare()
    print(f"Lit Route: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
