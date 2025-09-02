# app/main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
import os
import json
import csv


# our modules
from .hysplit_runner import run_back_trajectory   # ← run_back_trajectory는 levels_m, out_name 지원 버전이어야 함
from .scoring import prefilter_and_score
from .simulate import (
    write_emittimes_from_entries,
    write_control_conc,
    write_setup_cfg,
    run_concentration,
    _utc,
)

app = FastAPI(title="Odor Source Finder (HYSPLIT)")

# ---------- Models ----------

class Receptor(BaseModel):
    lat: float
    lon: float
    z_agl_m: float = 10.0

class AnalyzeReq(BaseModel):
    receptor: Receptor
    complaint_time_local: datetime
    lookback_h: int
    levels_m: list[float] = [10.0]   # ex) [10,100,300]
    radius_km: float = 10.0
    sector_half_deg: float = 45.0
    corridor_km: float = 2.0
    top_n: int = 5
    out_name: Optional[str] = None   # ← tdump 파일명 지정(옵션)

class SimReq(BaseModel):
    complaint_time_local: datetime
    run_hours: int = 6
    source_ids: list[int] | None = None   # indices in sources.csv (0-based)
    top_k: int | None = None              # use top-K from the latest /analyze ranking
    unit_rate_gps: float = 1.0
    grid_center: Receptor | None = None   # optional: center of concentration grid

class OneShotReq(AnalyzeReq):
    # AnalyzeReq(역궤적·랭킹 파라미터)를 상속하고, 시뮬레이션용 옵션 추가
    run_hours: int = 6
    unit_rate_gps: float = 1.0
    sim_top_k: Optional[int] = 5               # 있으면 랭킹 상위 K로 시뮬
    sim_source_ids: Optional[list[int]] = None # 없으면 인덱스 직접 지정(0-based)
    grid_center: Optional[Receptor] = None     # 농도격자 중심(옵션)

# ---------- Health ----------

@app.get("/healthz")
def healthz():
    return {"ok": True}

# ---------- Analyze: back trajectories -> filter/score ----------

@app.post("/analyze")
def analyze(req: AnalyzeReq):
    # 경로
    work = Path(os.getenv("WORK_DIR", "/data/working"))
    out  = Path(os.getenv("OUT_DIR",  "/data/output"))
    cfg  = Path(os.getenv("CONFIG_DIR","/data/config"))
    met  = Path(os.getenv("MET_DIR",  "/data/met"))
    sources_csv = cfg / "sources.csv"

    if not sources_csv.exists():
        raise HTTPException(400, f"sources.csv not found at {sources_csv}")
    if not met.exists() or not any(met.iterdir()):
        raise HTTPException(400, f"ARL met files not found at {met}")

    # 1) 역궤적: GUI CONTROL과 동일 포맷(여러 시작고도 + 출력 파일명)으로 한 번만 실행
    out_name = req.out_name or "tdump"
    tdump_path = run_back_trajectory(
        local_dt=req.complaint_time_local,
        receptor_lat=req.receptor.lat,
        receptor_lon=req.receptor.lon,
        levels_m=req.levels_m,
        lookback_h=req.lookback_h,
        out_name=out_name,
    )

    # 2) 후보지 사전필터 + 점수화
    ranking, meta = prefilter_and_score(
        sources_csv=str(sources_csv),
        tdump_paths=[str(tdump_path)],                    # 한 개여도 리스트로
        receptor=(req.receptor.lat, req.receptor.lon),
        radius_km=req.radius_km,
        sector_half=req.sector_half_deg,
        corridor_km=req.corridor_km
    )

    # 저장
    out.mkdir(parents=True, exist_ok=True)
    out_json = out / f"rank_{int(datetime.now().timestamp())}.json"
    out_json.write_text(
        json.dumps({"meta": meta, "ranking": ranking[:req.top_n]}, ensure_ascii=False, indent=2)
    )

    return {"meta": meta, "topN": ranking[:req.top_n], "tdump": str(tdump_path), "saved": str(out_json)}

# ---------- Simulate: forward concentration (hycs_std) ----------

@app.post("/simulate")
def simulate(req: SimReq):
    cfg  = Path(os.getenv("CONFIG_DIR", "/data/config"))
    outd = Path(os.getenv("OUT_DIR",   "/data/output"))
    sources_csv = cfg / "sources.csv"

    if not sources_csv.exists():
        raise HTTPException(400, f"sources.csv not found at {sources_csv}")

    # load sources.csv
    sources = []
    with open(sources_csv, newline='', encoding='utf-8-sig') as f:
        r = csv.DictReader(f)
        for i, row in enumerate(r):
            lat = float(row.get("lat") or row.get("위도"))
            lon = float(row.get("lon") or row.get("경도"))
            name = (row.get("name") or row.get("시설명") or f"src_{i}").strip()
            h = float(row.get("stack_h") or row.get("굴뚝고") or 10.0)
            src_id = (row.get("id") or row.get("ID") or row.get("시설ID") or f"S{i + 1:04d}").strip()
            sources.append({
                "idx": i, "id": src_id, "name": name,
                "lat": lat, "lon": lon, "h": h,
                # 스케줄/방출율(없으면 기본값)
                "rate": float(row.get("rate_gps") or 1.0),
                "emit_start": (row.get("emit_start") or "09:00"),
                "emit_end":   (row.get("emit_end")   or "18:00"),
                "tz":         (row.get("tz")         or "+09:00"),
            })

    # choose sources to simulate
    chosen = []
    if req.source_ids:
        for i in req.source_ids:
            if i < 0 or i >= len(sources):
                raise HTTPException(400, f"source index {i} out of range (0..{len(sources)-1})")
            chosen.append(sources[i])
    elif req.top_k:
        rank_files = sorted(outd.glob("rank_*.json"))
        if not rank_files:
            raise HTTPException(400, "No rank_*.json found. Run /analyze first or pass source_ids.")
        last = json.loads(rank_files[-1].read_text())
        names_in_rank = [item["name"] for item in last.get("ranking", [])][:req.top_k]
        for nm in names_in_rank:
            for s in sources:
                if s["name"] == nm:
                    chosen.append(s)
                    break
        if not chosen:
            raise HTTPException(400, "No sources matched the latest ranking by name.")
    else:
        raise HTTPException(400, "Provide either 'source_ids' or 'top_k'.")

    # species id 매핑(응답용)
    species_map = []
    for k, s in enumerate(chosen, start=1):
        species_map.append({
            "species": k,
            "source_id": s["id"],
            "name": s["name"],
            "lat": s["lat"],
            "lon": s["lon"],
            "h": s["h"],
        })

    # --- CSV의 로컬 시각을 UTC로 변환해 EMITIMES entries 구성 ---
    def _to_utc_on_day(day, hhmm, tzstr):
        h, m = map(int, hhmm.split(":"))
        sign = 1 if tzstr[0] == '+' else -1
        oh, om = map(int, tzstr[1:].split(":"))
        dt_local = datetime(day.year, day.month, day.day, h, m,
                            tzinfo=timezone(sign*timedelta(hours=oh, minutes=om)))
        return dt_local.astimezone(timezone.utc)

    day = req.complaint_time_local.date()

    entries = []
    for k, s in enumerate(chosen, start=1):
        st_utc = _to_utc_on_day(day, s["emit_start"], s["tz"])
        en_utc = _to_utc_on_day(day, s["emit_end"],   s["tz"])
        dur_h = max(1, int((en_utc - st_utc).total_seconds() // 3600))
        entries.append({
            "species": k,
            "id": s["id"],
            "name": s["name"],
            "lat": s["lat"], "lon": s["lon"], "h": s["h"],
            "rate": s["rate"],
            "start_utc": st_utc, "end_utc": en_utc, "dur_h": dur_h,
        })

    head_start = min(e["start_utc"] for e in entries)
    head_end   = max(e["end_utc"]   for e in entries)
    auto_run_hours = max(1, int((head_end - head_start).total_seconds() // 3600))

    # HYSPLIT 입력 파일 생성
    write_emittimes_from_entries(entries)

    center_lat = (req.grid_center.lat if req.grid_center else chosen[0]["lat"])
    center_lon = (req.grid_center.lon if req.grid_center else chosen[0]["lon"])
    control_start_utc = head_start

    # TIME_FIX(LATER)
    max_hours_from_met = 5  # ARLDATA.BIN 커버 범위 (예: 0~5시)
    run_hours_final = min(max(req.run_hours, auto_run_hours), max_hours_from_met)
    write_control_conc(control_start_utc, run_hours=run_hours_final, grid_center=(center_lat, center_lon))

    write_setup_cfg()

    # run concentration model
    cdump_path = run_concentration()

    return {
        "species_map": species_map,
        "cdump": str(cdump_path),
        "hint": "Use species_map to separate source-specific contributions from CDUMP.",
    }

@app.post("/analyze_and_simulate")
def analyze_and_simulate(req: OneShotReq):
    """
    1) 역궤적 실행(여러 시작고도 한 번에, out_name 지정 가능)
    2) 궤적 기반 후보 소스 랭킹 산출
    3) 선택적으로 상위 K(또는 지정 인덱스) 소스에 대해 농도 시뮬레이션 실행
       - EMITIMES는 CSV의 rate/emit_start/emit_end/tz를 반영해 생성
       - CONTROL은 EMITIMES 헤더 시작시각과 범위를 자동 반영
    """
    # --- 경로/입력 검사 ---
    work = Path(os.getenv("WORK_DIR", "/data/working"))
    out  = Path(os.getenv("OUT_DIR",  "/data/output"))
    cfg  = Path(os.getenv("CONFIG_DIR","/data/config"))
    met  = Path(os.getenv("MET_DIR",  "/data/met"))
    sources_csv = cfg / "sources.csv"

    if not sources_csv.exists():
        raise HTTPException(400, f"sources.csv not found at {sources_csv}")
    if not met.exists() or not any(met.iterdir()):
        raise HTTPException(400, f"ARL met files not found at {met}")

    # --- 1) 역궤적: 여러 시작고도를 한 번에 ---
    out_name = req.out_name or "tdump_combo"
    tdump_path = run_back_trajectory(
        local_dt=req.complaint_time_local,
        receptor_lat=req.receptor.lat,
        receptor_lon=req.receptor.lon,
        levels_m=req.levels_m,
        lookback_h=req.lookback_h,
        out_name=out_name,
    )
    tdumps = [str(tdump_path)]

    # --- 2) 후보 랭킹 산출 ---
    ranking, meta = prefilter_and_score(
        sources_csv=str(sources_csv),
        tdump_paths=tdumps,
        receptor=(req.receptor.lat, req.receptor.lon),
        radius_km=req.radius_km,
        sector_half=req.sector_half_deg,
        corridor_km=req.corridor_km,
    )
    out.mkdir(parents=True, exist_ok=True)
    rank_json = out / f"rank_{int(datetime.now().timestamp())}.json"
    rank_json.write_text(json.dumps({"meta": meta, "ranking": ranking}, ensure_ascii=False, indent=2))

    # --- 3) 시뮬 대상 선택 (sim_source_ids 우선, 없으면 sim_top_k) ---
    chosen_idx: list[int] = []
    if req.sim_source_ids:
        chosen_idx = req.sim_source_ids
    elif req.sim_top_k and ranking:
        chosen_idx = [r["idx"] for r in ranking[:req.sim_top_k]]

    # --- 4) 시뮬레이션 (선택) ---
    cdump_path: str | None = None
    species_map: list[dict] | None = None

    if chosen_idx:
        # sources.csv 읽기 (스케줄/방출율 포함)
        src_rows: list[dict] = []
        with open(sources_csv, newline="", encoding="utf-8-sig") as f:
            r = csv.DictReader(f)
            for i, row in enumerate(r):
                src_rows.append({
                    "idx": i,
                    "id": (row.get("id") or row.get("ID") or row.get("시설ID") or f"S{i+1:04d}").strip(),
                    "name": (row.get("name") or row.get("시설명") or f"src_{i}").strip(),
                    "lat": float(row.get("lat") or row.get("위도")),
                    "lon": float(row.get("lon") or row.get("경도")),
                    "h":  float(row.get("stack_h") or row.get("굴뚝고") or 10.0),
                    "rate": float(row.get("rate_gps") or 1.0),
                    "emit_start": (row.get("emit_start") or "09:00"),
                    "emit_end":   (row.get("emit_end")   or "18:00"),
                    "tz":         (row.get("tz")         or "+09:00"),
                })

        # 인덱스로 선택
        try:
            pick = [src_rows[i] for i in chosen_idx]
        except IndexError:
            raise HTTPException(400, f"sim_source_ids contains out-of-range index (0..{len(src_rows)-1})")

        # species_map (응답/추적용)
        species_map = [
            {
                "species": k,
                "source_id": s["id"],
                "name": s["name"],
                "lat": s["lat"],
                "lon": s["lon"],
                "h": s["h"],
            }
            for k, s in enumerate(pick, start=1)
        ]

        # --- entries 구성: CSV 로컬 시각을 UTC로 변환 ---
        def _to_utc_on_day(day, hhmm, tzstr):
            h, m = map(int, hhmm.split(":"))
            sign = 1 if tzstr[0] == "+" else -1
            oh, om = map(int, tzstr[1:].split(":"))
            dt_local = datetime(day.year, day.month, day.day, h, m,
                                tzinfo=timezone(sign * timedelta(hours=oh, minutes=om)))
            return dt_local.astimezone(timezone.utc)

        day = req.complaint_time_local.date()
        entries: list[dict] = []
        for k, s in enumerate(pick, start=1):
            st_utc = _to_utc_on_day(day, s["emit_start"], s["tz"])
            en_utc = _to_utc_on_day(day, s["emit_end"],   s["tz"])
            dur_h  = max(1, int((en_utc - st_utc).total_seconds() // 3600))
            entries.append({
                "species": k,
                "id": s["id"],
                "name": s["name"],
                "lat": s["lat"], "lon": s["lon"], "h": s["h"],
                "rate": s["rate"],
                "start_utc": st_utc, "end_utc": en_utc, "dur_h": dur_h,
            })

        # EMITIMES 작성
        write_emittimes_from_entries(entries)

        # CONTROL: 헤더 시작/길이 자동
        head_start = min(e["start_utc"] for e in entries)
        head_end   = max(e["end_utc"]   for e in entries)
        auto_run_hours = max(1, int((head_end - head_start).total_seconds() // 3600))

        center_lat = (req.grid_center.lat if req.grid_center else pick[0]["lat"])
        center_lon = (req.grid_center.lon if req.grid_center else pick[0]["lon"])

        write_control_conc(
            head_start,
            run_hours=max(req.run_hours, auto_run_hours),
            grid_center=(center_lat, center_lon),
        )

        # SETUP + 실행
        write_setup_cfg()
        cdump_path = str(run_concentration())

    # --- 5) 응답/저장 ---
    result = {
        "analyze": {
            "tdumps": tdumps,
            "meta": meta,
            "ranking_top": ranking[:req.top_n],
        },
        "simulate": {
            "used_source_indices": chosen_idx,
            "species_map": species_map,
            "cdump": cdump_path,
        },
        "saved": str(rank_json),
    }
    pipe_json = out / f"pipeline_{int(datetime.now().timestamp())}.json"
    pipe_json.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result
