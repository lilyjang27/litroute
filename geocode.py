"""
geocode.py — 지번(또는 도로명) 주소로 된 가로등 데이터를 위도/경도로 바꿔 lit_route에 넣기

흐름
1) CSV에 위도/경도가 있으면 그대로 사용, 비어 있는 행만 주소로 지오코딩
2) 지오코딩: 카카오 로컬 API(기본) 또는 VWorld(국토부) API
3) 같은 주소는 다시 조회하지 않도록 geocode_cache.json에 저장
4) 지번 좌표는 '필지 중심'이라 실제 가로등 위치와 수십 m 어긋날 수 있음
   → 가장 가까운 도로 위로 붙이는(snap) 보정 함수 제공

필요: pip install requests
API 키
- 카카오: developers.kakao.com → 앱 만들기 → REST API 키
- VWorld: vworld.kr → 인증키 발급 (지오코더 API)
"""

from __future__ import annotations

import csv
import json
import os
import time
from typing import Dict, List, Optional, Tuple

import numpy as np
import requests
from scipy.spatial import cKDTree

from lit_route import LAT_KEYS, LON_KEYS, LocalProjector, LitRouter, _pick

ADDR_KEYS = ("지번주소", "소재지지번주소", "주소", "address", "소재지도로명주소", "도로명주소")
CACHE_FILE = "geocode_cache.json"


# ─────────────────────────────────────────────────────────────
# 지오코더
# ─────────────────────────────────────────────────────────────
def geocode_kakao(address: str, api_key: str) -> Optional[Tuple[float, float]]:
    """카카오 주소 검색. 지번/도로명 모두 인식. (lat, lon) 반환."""
    r = requests.get(
        "https://dapi.kakao.com/v2/local/search/address.json",
        params={"query": address},
        headers={"Authorization": f"KakaoAK {api_key}"},
        timeout=5,
    )
    r.raise_for_status()
    docs = r.json().get("documents", [])
    if not docs:
        return None
    return float(docs[0]["y"]), float(docs[0]["x"])


def geocode_vworld(address: str, api_key: str, kind: str = "PARCEL") -> Optional[Tuple[float, float]]:
    """VWorld 지오코더. kind='PARCEL'(지번) 또는 'ROAD'(도로명)."""
    r = requests.get(
        "https://api.vworld.kr/req/address",
        params={
            "service": "address", "request": "getcoord", "version": "2.0",
            "crs": "epsg:4326", "type": kind, "address": address,
            "format": "json", "key": api_key,
        },
        timeout=5,
    )
    r.raise_for_status()
    resp = r.json().get("response", {})
    if resp.get("status") != "OK":
        return None
    p = resp["result"]["point"]
    return float(p["y"]), float(p["x"])


class Geocoder:
    def __init__(self, api_key: str, provider: str = "kakao", delay: float = 0.05):
        self.api_key, self.provider, self.delay = api_key, provider, delay
        self.cache: Dict[str, Optional[List[float]]] = {}
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, encoding="utf-8") as f:
                self.cache = json.load(f)

    def __call__(self, address: str) -> Optional[Tuple[float, float]]:
        address = " ".join(address.split())
        if address in self.cache:
            v = self.cache[address]
            return tuple(v) if v else None
        try:
            if self.provider == "kakao":
                res = geocode_kakao(address, self.api_key)
            else:
                res = geocode_vworld(address, self.api_key)
        except requests.RequestException as e:
            print(f"[실패] {address}: {e}")
            return None
        self.cache[address] = list(res) if res else None
        time.sleep(self.delay)  # 호출 한도 보호
        return res

    def save(self):
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)


# ─────────────────────────────────────────────────────────────
# CSV 로딩: 위경도 우선, 없으면 주소 지오코딩
# ─────────────────────────────────────────────────────────────
def load_streetlights_with_address(path: str, geocoder: Geocoder, region_prefix: str = "") -> np.ndarray:
    """
    region_prefix: 주소에 시/구가 빠져 있으면 앞에 붙일 문자열 (예: "서울특별시 동대문구 ")
    """
    rows = []
    for enc in ("utf-8-sig", "cp949", "euc-kr"):
        try:
            with open(path, encoding=enc, newline="") as f:
                rows = list(csv.DictReader(f))
            break
        except UnicodeDecodeError:
            continue

    pts, n_geo, n_fail = [], 0, 0
    for row in rows:
        lat, lon = _pick(row, LAT_KEYS), _pick(row, LON_KEYS)
        if lat and lon:
            pts.append((lat, lon))
            continue
        addr = next((row[k].strip() for k in row if k and k.strip() in ADDR_KEYS and row[k] and row[k].strip()), None)
        if not addr:
            continue
        res = geocoder(region_prefix + addr)
        if res:
            pts.append(res)
            n_geo += 1
        else:
            n_fail += 1
    geocoder.save()
    print(f"가로등 {len(pts)}개 (주소 변환 {n_geo}개, 변환 실패 {n_fail}개)")
    return np.array(pts, dtype=float).reshape(-1, 2)


# ─────────────────────────────────────────────────────────────
# 도로 위로 붙이기: 필지 중심 좌표 → 가장 가까운 도로 위 점
# ─────────────────────────────────────────────────────────────
def snap_to_roads(router: LitRouter, lights: np.ndarray, max_snap: float = 40.0, step: float = 2.0) -> np.ndarray:
    """
    max_snap(m) 안에 도로가 있으면 그 도로 위로 옮기고, 없으면 원래 좌표 유지.
    지번 기반 좌표에만 쓰는 것을 권장 (원래 위경도가 정확한 데이터는 그대로 두기).
    """
    if len(lights) == 0:
        return lights
    ref = np.array(list(router.nodes.values())).mean(axis=0)
    proj = LocalProjector(ref[0], ref[1])
    router.proj = proj

    road_pts = []
    seen = set()
    for edges in router.adj.values():
        for e in edges:
            key = (tuple(e.geometry[0]), tuple(e.geometry[-1]))
            if key in seen or key[::-1] in seen:
                continue
            seen.add(key)
            old = router.sample_step
            router.sample_step = step
            road_pts.append(router._sample(e.geometry))
            router.sample_step = old
    road_xy = np.vstack(road_pts)
    tree = cKDTree(road_xy)

    light_xy = proj.to_xy(lights[:, 0], lights[:, 1])
    d, idx = tree.query(light_xy, k=1)
    snapped = np.where((d <= max_snap)[:, None], road_xy[idx], light_xy)

    lat = snapped[:, 1] / proj.ky + proj.ref_lat
    lon = snapped[:, 0] / proj.kx + proj.ref_lon
    print(f"도로 위로 보정: {int((d <= max_snap).sum())}/{len(lights)}개, 평균 이동 {d[d <= max_snap].mean():.1f} m")
    return np.stack([lat, lon], axis=1)
