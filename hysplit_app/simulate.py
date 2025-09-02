# app/simulate.py
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Iterable
from math import cos, radians

# ---- 디렉터리/실행 파일 ----
WORK_ROOT = Path(os.getenv("WORK_DIR", "/data/working"))
CONC_DIR  = WORK_ROOT / "conc"                 # 확산 전용 작업 폴더
OUT_DIR   = Path(os.getenv("OUT_DIR",  "/data/output"))
MET_DIR   = Path(os.getenv("MET_DIR",  "/data/met"))
CFG_DIR   = Path(os.getenv("CONFIG_DIR","/data/config"))
BDY_DIR   = Path(os.getenv("BDY_DIR",  "/data/bdyfiles"))  # ASCDATA.CFG 위치
EXEC_DIR  = Path(os.getenv("HYSPLIT_EXEC_DIR","/opt/hysplit/exec"))

HYCS = EXEC_DIR / "hycs_std"

# ---- 시간 유틸 ----
def _utc(dt_local: datetime) -> datetime:
    """로컬(KST 가정; tzinfo 없으면 KST로 간주) → UTC"""
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt_local.astimezone(timezone.utc)

# ---- 메테오 ----
def _find_arl_files() -> list[str]:
    mets = sorted(p.name for p in MET_DIR.glob("*.BIN"))
    if not mets:
        raise RuntimeError(f"No ARL files (*.BIN) under {MET_DIR}")
    return mets

def _ensure_bdyfiles():
    """hycs_std는 작업 디렉터리에서 ASCDATA.CFG를 찾음 → conc 폴더에 심볼릭 링크(또는 복사) 보장"""
    CONC_DIR.mkdir(parents=True, exist_ok=True)
    asc_src = BDY_DIR / "ASCDATA.CFG"
    if not asc_src.exists():
        raise FileNotFoundError(f"ASCDATA.CFG not found at {asc_src}")

    asc_dst = CONC_DIR / "ASCDATA.CFG"
    if asc_dst.exists():
        return
    try:
        asc_dst.symlink_to(asc_src)
    except Exception:
        asc_dst.write_text(asc_src.read_text())

# ---- 입력 파일 생성 ----
def write_emittimes_from_entries(entries: list[dict]) -> Path:
    """
    entries: 각 소스(=species)에 대해
      {
        "species": 1..N,
        "lat": float, "lon": float, "h": float,
        "rate": float,
        "start_utc": datetime, "end_utc": datetime, "dur_h": int
      }
    """
    CONC_DIR.mkdir(parents=True, exist_ok=True)
    if not entries:
        raise ValueError("EMITIMES entries is empty")

    entries = sorted(entries, key=lambda e: e["start_utc"])
    head_start = entries[0]["start_utc"]
    head_end   = max(e["end_utc"] for e in entries)

    lines = []
    lines.append(f"{head_start:%Y %m %d %H}")
    lines.append(f"{head_end:%Y %m %d %H}")
    lines.append("1")  # 1-hour resolution

    for e in entries:
        lines.append(
            f'{e["start_utc"]:%Y %m %d %H} {int(e["dur_h"]):3d} '
            f'{float(e["lat"]):8.3f} {float(e["lon"]):9.3f} {float(e["h"]):6.1f} '
            f'{float(e["rate"]):8.3f} 0.0 0.0 0.0 0.0 0.0 {int(e["species"]):3d}'
        )

    p = CONC_DIR / "EMITIMES"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p

def write_control_conc(start_utc: datetime, run_hours: int, grid_center=None) -> Path:
    """
    고정된 CONTROL 템플릿 사용 (날짜/시간만 갱신)
    """
    CONC_DIR.mkdir(parents=True, exist_ok=True)

    txt = f"""{start_utc:%Y %m %d %H}
1
0.0 0.0 0.0
12
2
10000.0
1
{MET_DIR}/
ARLDATA.BIN
1
EMITIMES
1
TAGGED_RUN
1.0
1.0
{start_utc:%Y %m %d %H} 00
1
0.0 0.0
0.1 0.1
4.0 4.0
{OUT_DIR}/
cdump_tagged
1
100
{start_utc:%Y %m %d %H} 00
{(start_utc + timedelta(hours=run_hours)):%Y %m %d %H} 00
00 01 00
"""
    path = CONC_DIR / "CONTROL"
    path.write_text(txt.strip() + "\n", encoding="utf-8")
    return path

def write_setup_cfg() -> Path:
    CONC_DIR.mkdir(parents=True, exist_ok=True)
    tmpl = CFG_DIR / "SETUP.CFG"
    if tmpl.exists():
        (CONC_DIR / "SETUP.CFG").write_text(tmpl.read_text(), encoding="utf-8")
        return CONC_DIR / "SETUP.CFG"

    txt = """&SETUP
efile = 'EMITIMES',
ichem = 10,
cpack = 1,
numpar = 50000,
/
"""
    (CONC_DIR / "SETUP.CFG").write_text(txt, encoding="utf-8")
    return CONC_DIR / "SETUP.CFG"

# ---- 실행 ----
def run_concentration() -> Path:
    CONC_DIR.mkdir(parents=True, exist_ok=True)
    _ensure_bdyfiles()

    # 이전 산출물 정리
    try: (OUT_DIR / "CDUMP").unlink()
    except FileNotFoundError: pass
    try: (CONC_DIR / "MESSAGE").unlink()
    except FileNotFoundError: pass
    try: (CONC_DIR / "WARNING").unlink()
    except FileNotFoundError: pass

    # ★ 여기! 실행 디렉터리를 conc 로
    subprocess.run([str(HYCS)], cwd=str(CONC_DIR), check=True)

    cdump_out = OUT_DIR / "CDUMP"
    if cdump_out.exists():
        return cdump_out
    cdump_local = CONC_DIR / "CDUMP"
    if cdump_local.exists():
        return cdump_local
    raise RuntimeError("CDUMP not found after hycs_std run")


# ---- (선택) 간단 파서 ----
def parse_cdump_species(cdump_path: Path) -> dict:
    return {"path": str(cdump_path)}
