   import osmnx as ox
   from lit_route import router_from_osmnx, load_streetlights

   start = (37.5960, 127.0525)   # 출발지 위도, 경도
   end   = (37.6010, 127.0600)   # 도착지 위도, 경도

   # 출발지 주변 2km의 보행자 도로망 다운로드 (처음엔 시간이 좀 걸림)
   G = ox.graph_from_point(start, dist=2000, network_type="walk")

   r = router_from_osmnx(G, light_radius=20, dark_penalty=3)
   r.apply_streetlights(load_streetlights("보안등.csv"))

   res = r.recommend(start, end, min_lit=0.5)
   print("추천:", res["recommended"]["length_m"], "m, 밝음", res["recommended"]["lit_ratio"])
   print("최단:", res["shortest"]["length_m"], "m, 밝음", res["shortest"]["lit_ratio"])
   print("추가 거리:", res["extra_distance_m"], "m / 어두운 길 감소:", res["dark_reduced_m"], "m")