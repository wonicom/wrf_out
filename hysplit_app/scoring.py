# app/scoring.py
from __future__ import annotations
import csv, math, statistics

def haversine_km(lat1, lon1, lat2, lon2):
    R=6371.0
    from math import radians, sin, cos, asin, sqrt
    a,b,c,d = map(radians, (lat1,lon1,lat2,lon2))
    return 2*R*asin(math.sqrt( sin((c-a)/2)**2 + cos(a)*cos(c)*sin((d-b)/2)**2 ))

def bearing_deg(lat1, lon1, lat2, lon2):
    # from point A(lat1,lon1) to B(lat2,lon2)
    from math import radians, degrees, sin, cos, atan2
    a,b,c,d = map(radians, (lat1,lon1,lat2,lon2))
    y = sin(d-b)*cos(c)
    x = cos(a)*sin(c) - sin(a)*cos(c)*cos(d-b)
    brng = (degrees(atan2(y,x)) + 360.0) % 360.0
    return brng

def angdiff(a, b):
    d = abs((a-b+180) % 360 - 180)
    return d

def parse_tdump_points(tdump_path:str):
    pts=[]
    with open(tdump_path, 'r', encoding='utf-8', errors='ignore') as f:
        for ln in f:
            p = ln.split()
            # TDUMP 데이터 라인은 보통 뒤에서 [ -4:lat, -3:lon ]
            if len(p)>=12 and p[0].isdigit():
                try:
                    lat=float(p[-4]); lon=float(p[-3])
                    pts.append((lat,lon))
                except:
                    pass
    return pts

def mean_upwind_from_points(pts:list[tuple[float,float]]):
    # 아주 단순화: 연속점들의 역방향 평균 방위 (참고값)
    brs=[]
    for i in range(1,len(pts)):
        lat2,lon2 = pts[i-1]
        lat1,lon1 = pts[i]
        brs.append(bearing_deg(lat1,lon1,lat2,lon2))
    return statistics.mean(brs) if brs else 0.0

def prefilter_and_score(
    sources_csv:str,
    tdump_paths:list[str],
    receptor:tuple[float,float],
    radius_km:float=10.0,
    sector_half:float=45.0,
    corridor_km:float=2.0,
    near_km:float=0.5,   # 근접 자동통과
):
    # 1) 궤적 포인트 취합
    all_pts=[]
    for p in tdump_paths:
        all_pts += parse_tdump_points(p)
    if not all_pts:
        return [], {"kept":0, "total":0, "mean_upwind_deg": None,
                    "filters":{"radius_km":radius_km,"sector_half_deg":sector_half,"corridor_km":corridor_km}}
    mean_up = mean_upwind_from_points(all_pts)

    # 2) 소스 읽기 (BOM 대응 / 한글 헤더 대응)
    srcs=[]
    with open(sources_csv, newline='', encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r):
            name = (row.get("name") or row.get("시설명") or f"src_{i}").strip()
            lat = float(row.get("lat") or row.get("위도"))
            lon = float(row.get("lon") or row.get("경도"))
            stack_h = float(row.get("stack_h") or row.get("굴뚝고") or 10.0)
            # ⬇ ID: CSV에 id/ID/시설ID가 있으면 그걸, 아니면 S0001로 자동
            src_id = (row.get("id") or row.get("ID") or row.get("시설ID") or f"S{i + 1:04d}").strip()
            srcs.append({"idx": i, "id": src_id, "name": name, "lat": lat, "lon": lon, "h": stack_h})

    kept=[]
    (rlat, rlon) = receptor

    # 3) 필터링 & 스코어
    for s in srcs:
        d_recp = haversine_km(rlat, rlon, s["lat"], s["lon"])
        # 궤적까지 최근접 거리(포인트 기준; 필요하면 세그먼트 보강 가능)
        d_poly = min(haversine_km(s["lat"], s["lon"], a, b) for (a,b) in all_pts)

        in_radius   = (d_recp <= radius_km)
        in_corridor = (d_poly <= corridor_km)

        # 섹터 비활성화(180 이상) or 근접 자동통과
        passed_sector = True
        if sector_half < 180.0:
            # upwind 기준으로 수용점→소스 방위 각도와 비교
            br = bearing_deg(rlat, rlon, s["lat"], s["lon"])
            passed_sector = (angdiff(br, mean_up) <= sector_half)

        if (d_recp <= near_km) or (in_radius and in_corridor and passed_sector):
            # 간단 점수: 궤적 근접도가 높고(작을수록), 수용점과도 가까울수록 가점
            score = 1.0/(1.0 + d_poly) + 0.3/(1.0 + d_recp)
            kept.append({
                "idx": s["idx"], "id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"],
                "h": s["h"], "d_receptor_km": round(d_recp, 3),
                "d_traj_km": round(d_poly, 3),
                "score": round(score, 6)
            })

    kept.sort(key=lambda x: (-x["score"], x["d_traj_km"]))
    meta = {"kept": len(kept), "total": len(srcs), "mean_upwind_deg": mean_up,
            "filters":{"radius_km":radius_km,"sector_half_deg":sector_half,"corridor_km":corridor_km}}
    return kept, meta
