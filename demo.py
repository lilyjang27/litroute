"""가상의 격자 동네로 lit_route 동작 확인"""
import numpy as np
from lit_route import LitRouter

BASE_LAT, BASE_LON = 37.5960, 127.0525   # 임의의 기준점
STEP = 80                                # 블록 간격(m)
N = 6
dlat = STEP / 111_000
dlon = STEP / (111_000 * np.cos(np.radians(BASE_LAT)))

r = LitRouter(light_radius=20, dark_penalty=3.0)
for i in range(N):
    for j in range(N):
        r.add_node(f"{i},{j}", BASE_LAT + i * dlat, BASE_LON + j * dlon)
for i in range(N):
    for j in range(N):
        if j + 1 < N: r.add_edge(f"{i},{j}", f"{i},{j+1}")
        if i + 1 < N: r.add_edge(f"{i},{j}", f"{i+1},{j}")

# 가로등: 바깥쪽 큰길(0행, 마지막 열)만 30m 간격으로 촘촘, 안쪽 골목은 거의 없음
lights = []
for k in np.arange(0, STEP * (N - 1) + 1, 30):
    lights.append((BASE_LAT, BASE_LON + k / STEP * dlon))                     # 남쪽 큰길
    lights.append((BASE_LAT + k / STEP * dlat, BASE_LON + (N - 1) * dlon))    # 동쪽 큰길
lights.append((BASE_LAT + 2 * dlat, BASE_LON + 2 * dlon))                    # 골목에 하나
r.apply_streetlights(np.array(lights))

start = r.nodes["0,0"]
end = r.nodes[f"{N-1},{N-1}"]

res = r.recommend(start, end)
s, rec = res["shortest"], res["recommended"]
print("최단 경로  :", s["length_m"], "m, 밝은 비율", s["lit_ratio"], "| 어두운 구간", s["dark_m"], "m")
print("추천 경로  :", rec["length_m"], "m, 밝은 비율", rec["lit_ratio"], "| 어두운 구간", rec["dark_m"], "m")
print("추가 거리  :", res["extra_distance_m"], "m / 어두운 길 감소:", res["dark_reduced_m"], "m")
print("추천 노드  :", " → ".join(rec["nodes"]))
print("경고       :", res["warning"])

res2 = r.recommend(start, end, min_lit=0.8)
print("\n[min_lit=0.8] 추천:", res2["recommended"]["lit_ratio"], res2["recommended"]["length_m"], "m", res2["warning"])

res3 = r.recommend(start, r.nodes["5,0"], max_detour=1.2)
print("[우회 1.2배 제한] ", res3["recommended"]["length_m"], "m vs 최단", res3["shortest"]["length_m"], "m |", res3["warning"])

assert rec["lit_ratio"] > s["lit_ratio"] - 1e-9
assert res2["recommended"]["lit_ratio"] >= 0.8 or res2["warning"]
print("\nOK")
