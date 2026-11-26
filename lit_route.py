"""
lit_route.py — 가로등 위치 데이터를 반영한 '밝은 길 우선' 경로 추천 로직

핵심 아이디어
1) 가로등 좌표를 KD-Tree에 넣어 "이 지점 근처 R미터 안에 가로등이 있나?"를 빠르게 조회
2) 도로 구간(edge)을 일정 간격(예: 5m)으로 샘플링해서, 가로등 반경 안에 들어오는 비율 = lit_ratio 계산
3) 경로 비용 = 길이 × (1 + 어두움 벌점 × (1 - lit_ratio))
   → 조금 돌아가더라도 밝은 길을 택하도록 유도
4) 비용 기준 A* 탐색 (휴리스틱 = 직선거리, 비용이 항상 길이 이상이므로 admissible)
5) 최단 경로와 비교해 "몇 m 더 걷고, 어두운 구간이 몇 m 줄었는지" 함께 반환

입력 데이터
- 가로등: CSV/GeoJSON. 공공데이터포털 '전국보안등정보표준데이터'의 '위도','경도' 컬럼도 그대로 인식
- 도로망: 노드(id, lat, lon) + 엣지(u, v). 실서비스에선 OSM(osmnx) 보행자 네트워크를 넣으면 됨
"""

from __future__ import annotations

import csv
import heapq
import json
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from scipy.spatial import cKDTree

EARTH_R = 6_371_000.0  # m

LAT_KEYS = ("lat", "latitude", "위도", "y")
LON_KEYS = ("lon", "lng", "longitude", "경도", "x")


# ─────────────────────────────────────────────────────────────
# 좌표 변환: 위경도 → 로컬 평면(m). 도시 규모에선 등장방형 근사로 충분
# ─────────────────────────────────────────────────────────────
class LocalProjector:
    def __init__(self, ref_lat: float, ref_lon: float):
        self.ref_lat = ref_lat
        self.ref_lon = ref_lon
        self.kx = math.radians(1) * EARTH_R * math.cos(math.radians(ref_lat))
        self.ky = math.radians(1) * EARTH_R

    def to_xy(self, lat, lon):
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        return np.stack([(lon - self.ref_lon) * self.kx, (lat - self.ref_lat) * self.ky], axis=-1)


def haversine(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


# ─────────────────────────────────────────────────────────────
# 가로등 데이터 로딩
# ─────────────────────────────────────────────────────────────
def _pick(row: dict, keys: Sequence[str]) -> Optional[float]:
    lowered = {k.strip().lower(): v for k, v in row.items() if k}
    for k in keys:
        v = lowered.get(k.lower())
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                return None
    return None


def load_streetlights(path: str) -> np.ndarray:
    """CSV 또는 GeoJSON에서 가로등 좌표를 읽어 (N, 2) [lat, lon] 배열로 반환."""
    pts: List[Tuple[float, float]] = []
    if path.lower().endswith((".geojson", ".json")):
        with open(path, encoding="utf-8") as f:
            gj = json.load(f)
        for feat in gj.get("features", []):
            g = feat.get("geometry") or {}
            if g.get("type") == "Point":
                lon, lat = g["coordinates"][:2]
                pts.append((lat, lon))
    else:
        # 공공데이터 CSV는 cp949인 경우가 많아서 순서대로 시도
        for enc in ("utf-8-sig", "cp949", "euc-kr"):
            try:
                with open(path, encoding=enc, newline="") as f:
                    for row in csv.DictReader(f):
                        lat, lon = _pick(row, LAT_KEYS), _pick(row, LON_KEYS)
                        if lat is not None and lon is not None:
                            pts.append((lat, lon))
                break
            except UnicodeDecodeError:
                pts.clear()
    arr = np.array(pts, dtype=float).reshape(-1, 2)
    # 위경도가 뒤바뀐 행/이상치 제거 (한국 범위 기준, 필요시 수정)
    ok = (arr[:, 0] > 33) & (arr[:, 0] < 39) & (arr[:, 1] > 124) & (arr[:, 1] < 132)
    return arr[ok]


# ─────────────────────────────────────────────────────────────
# 그래프 + 조명 점수
# ─────────────────────────────────────────────────────────────
@dataclass
class Edge:
    v: str
    length: float           # m
    lit_ratio: float = 0.0  # 0~1, 가로등 반경 안에 들어오는 길이 비율
    geometry: Optional[List[Tuple[float, float]]] = None  # [(lat, lon), ...] 곡선 도로용


@dataclass
class LitRouter:
    light_radius: float = 20.0     # 가로등 하나가 밝힌다고 볼 반경(m). 보안등 15~20m, 도로등 25~30m 정도
    sample_step: float = 5.0       # 엣지 샘플링 간격(m)
    dark_penalty: float = 3.0      # 어두운 1m를 (1+penalty)m로 취급
    nodes: Dict[str, Tuple[float, float]] = field(default_factory=dict)
    adj: Dict[str, List[Edge]] = field(default_factory=dict)

    # --- 그래프 구성 -------------------------------------------------
    def add_node(self, nid: str, lat: float, lon: float):
        self.nodes[str(nid)] = (lat, lon)
        self.adj.setdefault(str(nid), [])

    def add_edge(self, u: str, v: str, geometry=None, bidirectional=True):
        u, v = str(u), str(v)
        geom = geometry or [self.nodes[u], self.nodes[v]]
        length = sum(haversine(*geom[i], *geom[i + 1]) for i in range(len(geom) - 1))
        self.adj[u].append(Edge(v, length, geometry=geom))
        if bidirectional:
            self.adj[v].append(Edge(u, length, geometry=list(reversed(geom))))

    # --- 조명 점수 계산 ----------------------------------------------
    def apply_streetlights(self, lights_latlon: np.ndarray):
        """모든 엣지의 lit_ratio를 계산. 가로등 데이터가 바뀌면 다시 호출."""
        all_pts = np.array(list(self.nodes.values()))
        ref = all_pts.mean(axis=0)
        self.proj = LocalProjector(ref[0], ref[1])
        if len(lights_latlon) == 0:
            for edges in self.adj.values():
                for e in edges:
                    e.lit_ratio = 0.0
            return
        tree = cKDTree(self.proj.to_xy(lights_latlon[:, 0], lights_latlon[:, 1]))

        cache: Dict[Tuple, float] = {}
        for u, edges in self.adj.items():
            for e in edges:
                geometry_key = tuple(map(tuple, e.geometry))
                key = min(geometry_key, geometry_key[::-1])
                if key not in cache:
                    samples = self._sample(e.geometry)
                    d, _ = tree.query(samples, k=1, distance_upper_bound=self.light_radius)
                    cache[key] = float(np.mean(np.isfinite(d)))
                e.lit_ratio = cache[key]

    def _sample(self, geom) -> np.ndarray:
        xy = self.proj.to_xy([p[0] for p in geom], [p[1] for p in geom])
        out = [xy[0]]
        for a, b in zip(xy[:-1], xy[1:]):
            seg = np.linalg.norm(b - a)
            n = max(1, int(math.ceil(seg / self.sample_step)))
            t = np.linspace(0, 1, n + 1)[1:, None]
            out.extend(a + (b - a) * t)
        return np.array(out)

    # --- 비용 함수 ---------------------------------------------------
    def edge_cost(self, e: Edge, dark_penalty: float, min_lit: Optional[float]) -> float:
        if min_lit is not None and e.lit_ratio < min_lit:
            return math.inf  # 너무 어두운 구간은 아예 제외
        return e.length * (1.0 + dark_penalty * (1.0 - e.lit_ratio))

    # --- 탐색 --------------------------------------------------------
    def nearest_node(self, lat: float, lon: float) -> str:
        return min(self.nodes, key=lambda n: haversine(lat, lon, *self.nodes[n]))

    def _astar(self, src: str, dst: str, dark_penalty: float, min_lit: Optional[float], edge_usage=None):
        goal = self.nodes[dst]
        h = lambda n: haversine(*self.nodes[n], *goal)  # 비용 ≥ 길이 이므로 admissible
        g = {src: 0.0}
        prev: Dict[str, Tuple[str, Edge]] = {}
        pq = [(h(src), 0.0, src)]
        closed = set()
        while pq:
            _, gc, u = heapq.heappop(pq)
            if u in closed:
                continue
            if u == dst:
                break
            closed.add(u)
            for e in self.adj[u]:
                c = self.edge_cost(e, dark_penalty, min_lit)
                if edge_usage:
                    c *= 1 + 0.65 * edge_usage.get(tuple(sorted((u, e.v))), 0)
                if math.isinf(c):
                    continue
                ng = gc + c
                if ng < g.get(e.v, math.inf):
                    g[e.v] = ng
                    prev[e.v] = (u, e)
                    heapq.heappush(pq, (ng + h(e.v), ng, e.v))
        if dst not in g:
            return None
        path_edges, n = [], dst
        while n != src:
            p, e = prev[n]
            path_edges.append((p, e))
            n = p
        return list(reversed(path_edges))

    def _summarize(self, path_edges) -> dict:
        length = sum(e.length for _, e in path_edges)
        lit = sum(e.length * e.lit_ratio for _, e in path_edges)
        coords: List[Tuple[float, float]] = []
        for _, e in path_edges:
            coords.extend(e.geometry if not coords else e.geometry[1:])
        nodes = [path_edges[0][0]] + [e.v for _, e in path_edges] if path_edges else []
        return {
            "nodes": nodes,
            "coords": coords,                      # 지도에 폴리라인으로 그리면 됨
            "length_m": round(length, 1),
            "lit_m": round(lit, 1),
            "dark_m": round(length - lit, 1),
            "lit_ratio": round(lit / length, 3) if length else 1.0,
            "segments": [                          # 구간별 색칠용 (밝음/어두움)
                {"from": u, "to": e.v, "length_m": round(e.length, 1), "lit_ratio": round(e.lit_ratio, 2), "coords": e.geometry}
                for u, e in path_edges
            ],
        }

    def recommend(
        self,
        start: Tuple[float, float],
        end: Tuple[float, float],
        dark_penalty: Optional[float] = None,
        min_lit: Optional[float] = None,
        max_detour: float = 1.5,
    ) -> dict:
        """
        start, end: (lat, lon)
        dark_penalty: 어두움 벌점. 클수록 밝은 길에 집착(돌아가는 거리↑)
        min_lit: 지정 시, lit_ratio가 이 값 미만인 구간은 아예 통과 금지 (예: 0.5)
        max_detour: 밝은 경로가 최단경로의 이 배수를 넘으면 벌점을 낮춰 재탐색
        """
        s, t = self.nearest_node(*start), self.nearest_node(*end)
        penalty = self.dark_penalty if dark_penalty is None else dark_penalty

        shortest = self._astar(s, t, 0.0, None)
        if shortest is None:
            return {"error": "출발지와 도착지가 도로망으로 연결되어 있지 않습니다."}
        short = self._summarize(shortest)
        shortest_length = sum(e.length for _, e in shortest)
        distance_limit = shortest_length * max_detour

        lit_path, warning = None, None
        if min_lit is not None:
            lit_path = self._astar(s, t, penalty, min_lit)
            if lit_path is None:
                warning = f"밝기 {int(min_lit*100)}% 이상 구간만으로는 갈 수 없어 벌점 방식으로 대체했습니다."
            elif sum(e.length for _, e in lit_path) > distance_limit + 1e-6:
                lit_path = None
                warning = "밝기 조건을 만족하는 경로가 우회 한도를 넘어 밝기 조건을 완화했습니다."
        if lit_path is None:
            # 우회 한도를 넘으면 벌점을 절반씩 줄여가며 타협점 찾기
            p = penalty
            while True:
                lit_path = self._astar(s, t, p, None)
                if sum(e.length for _, e in lit_path) <= distance_limit + 1e-6:
                    break
                if p < 0.1:
                    lit_path = shortest
                    p = 0.0
                    break
                p /= 2
            if p != penalty:
                warning = (warning or "") + f" 우회 거리 제한({max_detour}배) 때문에 벌점을 {p:.2f}로 낮췄습니다."
        lit = self._summarize(lit_path)

        return {
            "recommended": lit,
            "shortest": short,
            "extra_distance_m": round(lit["length_m"] - short["length_m"], 1),
            "dark_reduced_m": round(short["dark_m"] - lit["dark_m"], 1),
            "warning": warning.strip() if warning else None,
        }


# ─────────────────────────────────────────────────────────────
# OSM 연동 (선택): pip install osmnx 후 사용
# ─────────────────────────────────────────────────────────────
def router_from_osmnx(G, **kwargs) -> LitRouter:
    """osmnx.graph_from_point(..., network_type='walk') 결과를 LitRouter로 변환."""
    r = LitRouter(**kwargs)
    for nid, d in G.nodes(data=True):
        r.add_node(nid, d["y"], d["x"])
    for u, v, d in G.edges(data=True):
        geom = [(lat, lon) for lon, lat in d["geometry"].coords] if "geometry" in d else None
        r.add_edge(u, v, geometry=geom, bidirectional=False)  # OSM은 양방향 엣지가 이미 따로 있음
    return r
