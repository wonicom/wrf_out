# app/hysplit_runner.py
from pathlib import Path
import os, subprocess
from datetime import datetime, timezone, timedelta

WORK_ROOT = Path(os.getenv("WORK_DIR", "/data/working"))
TRAJ_DIR  = WORK_ROOT / "traj"               # 궤적 전용 폴더
MET_DIR   = Path(os.getenv("MET_DIR", "/data/met"))
EXEC_DIR  = Path(os.getenv("HYSPLIT_EXEC_DIR","/opt/hysplit/exec"))
HYTS = EXEC_DIR / "hyts_std"

def _utc(dt_local: datetime) -> datetime:
    # 로컬(KST 가정) → UTC (tz-aware)
    if dt_local.tzinfo is None:
        dt_local = dt_local.replace(tzinfo=timezone(timedelta(hours=9)))
    return dt_local.astimezone(timezone.utc)

def run_back_trajectory(*, local_dt, receptor_lat, receptor_lon,
                        levels_m, lookback_h, out_name="tdump") -> Path:
    TRAJ_DIR.mkdir(parents=True, exist_ok=True)

    # 깨끗이
    for fn in ["CONTROL","tdump","MESSAGE","WARNING","SETUP.CFG"]:
        p = TRAJ_DIR / fn
        try:
            p.unlink()
        except FileNotFoundError:
            pass

    start_utc = _utc(local_dt)

    # GUI와 동일한 CONTROL (여러 시작고도 + 출력파일명)
    lines = []
    lines.append(f"{start_utc:%Y %m %d %H}")    # ← 4자리 연도
    lines.append(f"{len(levels_m)}")
    for z in levels_m:
        lines.append(f"{receptor_lat:.4f} {receptor_lon:.4f} {float(z):.1f}")
    lines.append(f"{-abs(int(lookback_h))}")    # BACKWARD
    lines.append("0")                           # vertical motion method (input)
    lines.append("10000.0")                     # top of model (m agl)
    lines.append("1")                           # met files
    lines.append(str(MET_DIR) + "/")
    lines.append("ARLDATA.BIN")
    lines.append(str(TRAJ_DIR) + "/")           # 출력 경로
    lines.append(out_name)                      # 출력 파일명

    (TRAJ_DIR / "CONTROL").write_text("\n".join(lines) + "\n")

    # hyts_std 실행 (cwd=TRAJ_DIR 로 고정)
    subprocess.run([str(HYTS)], cwd=str(TRAJ_DIR), check=True)

    return TRAJ_DIR / out_name
