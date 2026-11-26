# 밝은 길 경로 추천 (Lit Route)

밤에 가로등이 켜진 길 위주로 걸을 수 있도록, **가로등 위치 데이터를 반영해 경로를 추천**하는 로직입니다.
최단 경로와 비교해 "몇 m 더 걷고, 어두운 길이 몇 m 줄었는지"를 함께 알려줍니다.

---

## 간단한 웹앱 실행

업로드된 `dongjak_lighting_locations.csv`와 실제 OpenStreetMap 보행자 도로망을 사용하는 로컬 웹앱입니다. 카카오 API 키 없이 실행할 수 있습니다.

```bash
pip install -r requirements.txt
python app.py
```

브라우저에서 <http://127.0.0.1:8000>을 엽니다. 지도에서 출발지와 도착지를 차례로 클릭하고 **밝은 길 찾기**를 누릅니다. **예시 경로로 시작하기**를 누르면 바로 비교할 수도 있습니다.

- 최초 실행: Leaflet 라이브러리와 동작구 주변 보행자 도로망을 다운로드하고 조명 비율을 계산합니다. 인터넷 연결이 필요하며 공개 서버 상태에 따라 몇 분 걸릴 수 있습니다.
- 이후 실행: `.cache/`에 저장한 도로망과 조명 점수를 사용합니다. 배경 지도 타일은 인터넷 연결이 필요합니다.
- 지도에 표시하는 조명은 유효한 좌표 9,097개 행 중 중복을 합친 8,275개 위치입니다. CSV 원본은 수정하지 않습니다.
- 점선 사각형은 조명 좌표의 최소·최대 범위이며, 동작구 행정 경계나 모든 조명이 조사된 영역을 뜻하지 않습니다. 데이터 누락 지역은 실제보다 어둡게 추정될 수 있습니다.
- 초록색 경로 구간은 추정 조명 비율 50% 이상, 주황색은 그 미만입니다. 회색 점선은 최단 경로입니다.
- 출발·도착은 가까운 도로 노드에 연결합니다. 도로까지의 점선 연결 구간은 경로 거리와 조명 계산에서 제외하며, 120m 이상 떨어진 지점은 거절합니다.
- 도보 시간은 분당 75m 기준의 단순 추정치입니다. 조명 비율은 가로등 반경 20m 기준으로 추정하며 실제 점등 상태나 보행 안전을 보장하지 않습니다.
- 현재 버전에는 주소 검색, 음성 안내, 실시간 위치 추적이 없습니다. 보행 도로 정보도 현장과 다를 수 있습니다.

데이터만 먼저 준비하거나 다른 포트를 사용하려면:

```bash
python app.py --prepare
python app.py --port 8001
python app.py --csv 다른파일.csv
python -m unittest test_routes.py
```

파일: `app.py`는 로컬 HTTP 서버와 데이터 준비, `web/`는 화면, `test_routes.py`는 경로 제한과 입력 검증을 담당합니다. 서버는 로컬 PC의 `127.0.0.1`에만 열립니다. 종료는 실행한 터미널에서 `Ctrl+C`를 누릅니다.

배경 지도·도로망: © OpenStreetMap contributors. 지도 라이브러리: Leaflet 1.9.4 (BSD-2-Clause, `web/vendor/LICENSE`).

---

## 파일 구성

| 파일 | 역할 |
|---|---|
| `lit_route.py` | 핵심 로직. 가로등 데이터 로딩, 도로 구간별 밝기 계산, 밝은 길 우선 경로 탐색 |
| `geocode.py` | 지번/도로명 주소로 된 가로등 데이터를 위도·경도로 변환하고, 좌표를 가장 가까운 도로 위로 보정 |
| `demo.py` | 가상의 격자 동네로 로직이 제대로 동작하는지 확인하는 예제 |

---

## 동작 원리

1. **가로등 → 공간 인덱스**
   가로등 위경도를 미터 단위 평면 좌표로 바꿔 KD-Tree에 넣습니다. "이 지점 20m 안에 가로등이 있나?"를 빠르게 조회할 수 있습니다.

2. **도로 구간별 밝기 점수 (`lit_ratio`)**
   각 도로 구간을 5m 간격으로 나눈 점 중, 가로등 반경 안에 들어오는 점의 비율을 0~1 값으로 저장합니다.
   가로등 데이터를 불러올 때 한 번만 계산합니다.

3. **비용 함수**
   ```
   비용 = 길이 × (1 + 어두움 벌점 × (1 − lit_ratio))
   ```
   벌점이 3이면 완전히 어두운 100m를 400m처럼 취급합니다. 그래서 조금 돌아가더라도 밝은 길을 고르게 됩니다.

4. **A\* 탐색**
   위 비용으로 경로를 찾습니다. 휴리스틱은 직선거리를 씁니다. 비용이 항상 실제 길이 이상이므로 최적 경로가 보장됩니다.

5. **안전장치**
   - `min_lit` : 밝기가 이 값 미만인 구간은 아예 지나가지 않습니다. 그런 길로만은 갈 수 없으면 벌점 방식으로 대신 찾고 경고를 남깁니다.
   - `max_detour` : 추천 경로가 최단 경로의 이 배수를 넘으면 벌점을 절반씩 낮춰 다시 찾습니다.

---

## 각 파일 상세

### `lit_route.py`

| 이름 | 설명 |
|---|---|
| `load_streetlights(path)` | CSV 또는 GeoJSON에서 가로등 좌표를 `[lat, lon]` 배열로 읽습니다. `위도`/`경도`, `lat`/`lon` 컬럼을 자동 인식하고 cp949 인코딩도 처리합니다. 한국 범위를 벗어난 이상치는 제거합니다. |
| `LitRouter` | 도로망 그래프와 경로 탐색을 담당하는 클래스 |
| `.add_node(id, lat, lon)` / `.add_edge(u, v)` | 도로망을 직접 구성할 때 사용 |
| `.apply_streetlights(lights)` | 모든 도로 구간의 `lit_ratio` 계산. 가로등 데이터가 바뀌면 다시 호출 |
| `.recommend(start, end, ...)` | 출발·도착 `(lat, lon)`을 받아 추천 경로와 최단 경로를 함께 반환 |
| `router_from_osmnx(G)` | OpenStreetMap 보행자 도로망(osmnx 그래프)을 `LitRouter`로 변환 |

`LitRouter` 설정값:

| 파라미터 | 기본값 | 의미 |
|---|---|---|
| `light_radius` | 20 | 가로등 하나가 밝힌다고 보는 반경(m). 보안등 15~20, 도로등 25~30 정도 |
| `sample_step` | 5 | 밝기 계산 시 도로 샘플링 간격(m) |
| `dark_penalty` | 3.0 | 어두움 벌점. 클수록 밝은 길에 집착 |

`recommend()` 반환값:

```python
{
  "recommended": {...},      # 추천 경로
  "shortest": {...},         # 최단 경로
  "extra_distance_m": 160.0, # 추천 경로가 더 긴 거리
  "dark_reduced_m": 640.0,   # 줄어든 어두운 길 길이
  "warning": None            # 조건을 완화했을 때 이유
}
```

각 경로에는 `coords`(지도에 그릴 좌표 목록), `length_m`, `lit_m`, `dark_m`, `lit_ratio`, `segments`(구간별 밝기, 색칠용)가 들어 있습니다.

### `geocode.py`

| 이름 | 설명 |
|---|---|
| `geocode_kakao(address, key)` | 카카오 로컬 API로 주소 → `(lat, lon)` |
| `geocode_vworld(address, key)` | 국토부 VWorld 지오코더로 주소 → `(lat, lon)` (`PARCEL`=지번, `ROAD`=도로명) |
| `Geocoder` | 위 둘을 감싸고, 결과를 `geocode_cache.json`에 저장해 같은 주소를 다시 조회하지 않음 |
| `load_streetlights_with_address(path, geocoder, region_prefix)` | 위경도가 있는 행은 그대로 쓰고, 빈 행만 주소로 변환. 주소에 시·구가 빠져 있으면 `region_prefix`로 보충 |
| `snap_to_roads(router, lights, max_snap=40)` | 지번 좌표(필지 중심)를 가장 가까운 도로 위로 이동. `max_snap`m보다 멀면 그대로 둠 |

> 지번 주소로 얻은 좌표는 땅의 중심점이라 실제 가로등 위치와 수십 m 어긋날 수 있습니다. 주소로 변환한 데이터에는 `snap_to_roads()`를 적용하는 것을 권장합니다.

### `demo.py`

6×6 교차로, 블록 간격 80m인 가상 동네를 만들고, 큰길에만 가로등을 촘촘히 둔 상태에서 최단 경로와 추천 경로를 비교해 출력합니다. `min_lit`, `max_detour` 옵션도 함께 확인합니다. 마지막에 `OK`가 나오면 정상입니다.

---

## 설치 및 실행

파이썬 3.9 이상이 필요합니다.

```bash
pip install numpy scipy          # 기본 로직
pip install requests             # 주소 변환을 쓸 때
pip install osmnx                # 실제 도로망을 쓸 때

python demo.py                   # 예제 실행
```

### 실제 데이터로 실행

1. 공공데이터포털(data.go.kr)에서 **'전국보안등정보표준데이터'** CSV를 받아 `보안등.csv`로 저장합니다.
2. 아래처럼 실행합니다.

```python
import os
import osmnx as ox
from lit_route import router_from_osmnx, load_streetlights
from geocode import Geocoder, load_streetlights_with_address, snap_to_roads

start = (37.5960, 127.0525)
end   = (37.6010, 127.0600)

G = ox.graph_from_point(start, dist=2000, network_type="walk")
r = router_from_osmnx(G, light_radius=20, dark_penalty=3)

# 위경도가 다 있는 데이터라면
lights = load_streetlights("보안등.csv")

# 주소만 있는 행이 섞여 있다면
# geo = Geocoder(os.environ["KAKAO_REST_KEY"])
# lights = load_streetlights_with_address("보안등.csv", geo, region_prefix="서울특별시 동대문구 ")
# lights = snap_to_roads(r, lights)

r.apply_streetlights(lights)
res = r.recommend(start, end, min_lit=0.5)
print(res["recommended"]["length_m"], res["recommended"]["lit_ratio"])
```

---

## API 키 관리

| 키 | 용도 | 보관 위치 |
|---|---|---|
| 카카오 **REST API 키** (또는 VWorld 인증키) | 주소 → 좌표 변환 | 서버에만. 환경변수나 `.env` 파일로 관리 |
| 카카오 **JavaScript 키** | 웹 화면에 지도 표시 | 프론트엔드 코드에 넣어도 됨. 대신 카카오 개발자 콘솔에서 사이트 도메인을 등록해야 동작 |

- 키는 코드에 직접 쓰지 말고 `os.environ["KAKAO_REST_KEY"]`처럼 환경변수에서 읽습니다.
- `.env`와 `geocode_cache.json`은 `.gitignore`에 추가합니다.

---

## 웹앱 구조 (계획)

```
[보안등 CSV] ──(사전 작업: 지오코딩 + 도로 보정)──▶ [가로등 위경도 파일]
                                                         │
[브라우저: 카카오맵 JS] ──출발/도착──▶ [서버: FastAPI + LitRouter]
        ▲                                                │
        └──────────── 추천 경로 좌표(JSON) ◀─────────────┘
              → Polyline으로 그리기 (밝은 구간 노랑, 어두운 구간 빨강)
```

## 앞으로 확장할 수 있는 것

- CCTV, 24시간 편의점, 대로 여부 같은 안전 요소도 `lit_ratio`처럼 점수로 만들어 비용에 함께 반영
- 보안등 데이터가 없는 지역은 OSM의 `highway=street_lamp` 데이터로 보완
