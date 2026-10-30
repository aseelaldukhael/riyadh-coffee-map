"""
Coffee Map - one command to run the whole pipeline.

    Extract (optional)  ->  Silver  ->  Gold  ->  Data quality tests

Usage (PowerShell, from the project folder or with the full path):
    python run_pipeline.py                 # use existing Bronze files, run Silver + Gold + tests
    python run_pipeline.py --extract       # also re-download OSM + Overture first
    python run_pipeline.py --extract-osm   # only re-download OSM
    python run_pipeline.py --extract-overture

Outputs:
    data/bronze/archive/<date>/...   dated copy of every raw download
    data/quality/test_results.json   result of every quality test
    data/quality/last_run.json       metrics used to detect big changes between runs
    logs/pipeline_<date>.log         full log of the run
Exit code is 1 if any quality test fails.
"""
import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
BRONZE = BASE / "data" / "bronze"
SILVER = BASE / "data" / "silver"
GOLD = BASE / "data" / "gold"
QUALITY = BASE / "data" / "quality"
LOGS = BASE / "logs"
for p in (BRONZE, SILVER, GOLD, QUALITY, LOGS):
    p.mkdir(parents=True, exist_ok=True)

OSM_FILE = BRONZE / "riyadh_cafes_raw.csv"
OVERTURE_FILE = BRONZE / "riyadh_places.geojson"
DISTRICTS_FILE = BRONZE / "districts.geojson"

# Riyadh bounding box
S, W, N, E = 24.40, 46.40, 25.10, 47.10

# ---- quality thresholds (judgment calls, documented in the README) ----
MIN_SILVER_ROWS = 2000
MIN_GOLD_ROWS = 2000
MIN_DISTRICT_MATCH_RATE = 0.98      # share of cafes that fall inside a district polygon
MAX_ROW_DROP_VS_LAST_RUN = 0.20     # fail if rows drop by more than 20% vs previous run

SERVERS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HEADERS = {"User-Agent": "coffee-map-project/1.0 (portfolio project)"}

log = logging.getLogger("pipeline")


def setup_logging():
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s", "%H:%M:%S")
    fh = logging.FileHandler(LOGS / f"pipeline_{date.today()}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.handlers = [fh, sh]


def archive(path: Path):
    """Keep a dated copy of a raw file (Bronze must be reproducible)."""
    dest = BRONZE / "archive" / str(date.today())
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest / path.name)
    log.info("archived %s -> %s", path.name, dest)


# ------------------------------------------------------------------ extract
def extract_osm():
    import requests

    steps = 4
    lat_step, lon_step = (N - S) / steps, (E - W) / steps
    elements_all = {}

    def fetch_tile(south, west, north, east):
        query = f"""
        [out:json][timeout:90];
        (
          node["amenity"="cafe"]({south},{west},{north},{east});
          way["amenity"="cafe"]({south},{west},{north},{east});
        );
        out center tags;
        """
        for _attempt in range(3):
            for url in SERVERS:
                try:
                    r = requests.post(url, data={"data": query}, headers=HEADERS, timeout=45)
                    r.raise_for_status()
                    return r.json()["elements"]
                except Exception as ex:
                    log.warning("overpass %s failed: %s", url.split("/")[2], ex)
                    time.sleep(5)
        return None

    failed = []
    for i in range(steps):
        for j in range(steps):
            south = S + i * lat_step
            west = W + j * lon_step
            tile_no = i * steps + j + 1
            log.info("OSM tile %d/%d", tile_no, steps * steps)
            els = fetch_tile(south, west, south + lat_step, west + lon_step)
            if els is None:
                failed.append(tile_no)
                continue
            for e in els:
                elements_all[f'{e["type"]}_{e["id"]}'] = e
            time.sleep(2)

    if failed:
        raise RuntimeError(f"OSM extraction failed for tiles {failed}; old file kept")

    rows = []
    for key, e in elements_all.items():
        t = e.get("tags", {})
        rows.append({
            "osm_id": key, "name": t.get("name"), "name_ar": t.get("name:ar"),
            "name_en": t.get("name:en"),
            "lat": e.get("lat") or e.get("center", {}).get("lat"),
            "lon": e.get("lon") or e.get("center", {}).get("lon"),
            "opening_hours": t.get("opening_hours"), "brand": t.get("brand"),
        })
    df = pd.DataFrame(rows)
    if len(df) < 100:
        raise RuntimeError(f"OSM returned only {len(df)} rows; refusing to overwrite Bronze")
    df.to_csv(OSM_FILE, index=False, encoding="utf-8-sig")
    log.info("OSM extracted: %d rows", len(df))
    archive(OSM_FILE)


def extract_overture():
    tmp = OVERTURE_FILE.with_name("riyadh_places.tmp.geojson")
    for leftover in (tmp, Path(str(tmp) + ".state")):
        if leftover.exists():
            leftover.unlink()
    cmd = [sys.executable, "-m", "overturemaps", "download",
           f"--bbox={W},{S},{E},{N}", "-f", "geojson", "--type=place", "-o", str(tmp)]
    log.info("running: %s", " ".join(cmd))
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not tmp.exists() or tmp.stat().st_size < 1_000_000:
        raise RuntimeError(f"Overture download failed: {res.stderr[-500:]}")
    os.replace(tmp, OVERTURE_FILE)
    state = Path(str(tmp) + ".state")
    if state.exists():
        state.unlink()
    log.info("Overture downloaded: %.1f MB", OVERTURE_FILE.stat().st_size / 1e6)
    archive(OVERTURE_FILE)


# ------------------------------------------------------------------ tests
class Tests:
    def __init__(self):
        self.results = []

    def check(self, name, ok, detail=""):
        self.results.append({"test": name, "passed": bool(ok), "detail": str(detail)})
        (log.info if ok else log.error)("TEST %-42s %s  %s",
                                        name, "PASS" if ok else "FAIL", detail)

    @property
    def failed(self):
        return [r for r in self.results if not r["passed"]]


def run_quality_tests():
    t = Tests()
    silver = pd.read_csv(SILVER / "cafes_silver.csv")
    gold = pd.read_csv(GOLD / "cafes_gold.csv")
    summary = pd.read_csv(GOLD / "district_summary.csv")
    greport = json.loads((GOLD / "gold_report.json").read_text(encoding="utf-8"))

    # ---- Silver ----
    t.check("silver: enough rows", len(silver) >= MIN_SILVER_ROWS,
            f"{len(silver)} rows (min {MIN_SILVER_ROWS})")
    t.check("silver: cafe_id is unique", silver["cafe_id"].is_unique)
    t.check("silver: no null lat/lon/name",
            silver[["lat", "lon", "name_display"]].notna().all().all())
    inside = silver["lat"].between(S, N) & silver["lon"].between(W, E)
    t.check("silver: all coordinates inside Riyadh bbox", inside.all(),
            f"{int((~inside).sum())} outside")
    dup = silver.assign(_la=silver["lat"].round(5), _lo=silver["lon"].round(5)) \
        .duplicated(["name_display", "_la", "_lo"]).sum()
    t.check("silver: no exact duplicates (name + location)", dup == 0, f"{dup} duplicates")
    conf = silver["confidence"].dropna()
    t.check("silver: confidence between 0 and 1", conf.between(0, 1).all())

    # ---- Gold ----
    t.check("gold: enough rows", len(gold) >= MIN_GOLD_ROWS,
            f"{len(gold)} rows (min {MIN_GOLD_ROWS})")
    t.check("gold: gold rows <= silver rows", len(gold) <= len(silver))
    matched = greport["cafes_matched_to_district"]
    total = matched + greport["cafes_unmatched"]
    rate = matched / total if total else 0
    t.check("gold: district match rate", rate >= MIN_DISTRICT_MATCH_RATE,
            f"{rate:.1%} (min {MIN_DISTRICT_MATCH_RATE:.0%})")
    t.check("gold: every cafe has a district_id", gold["district_id"].notna().all())
    t.check("gold: in_district_ranking has no nulls", gold["in_district_ranking"].notna().all())
    ranked = int(gold["in_district_ranking"].astype(str).str.lower().eq("true").sum())
    t.check("gold: district_summary total == ranked cafes",
            int(summary["cafes"].sum()) == ranked,
            f"summary {int(summary['cafes'].sum())} vs ranked {ranked}")
    t.check("gold: area_km2 > 0 and density present",
            (summary["area_km2"] > 0).all() and summary["cafes_per_km2"].notna().all())

    # ---- regression vs previous run ----
    last_file = QUALITY / "last_run.json"
    current = {"silver_rows": len(silver), "gold_rows": len(gold),
               "districts": int(len(summary)), "run_at": datetime.now().isoformat(timespec="seconds")}
    if last_file.exists():
        last = json.loads(last_file.read_text(encoding="utf-8"))
        for key in ("silver_rows", "gold_rows"):
            if last.get(key):
                drop = (last[key] - current[key]) / last[key]
                t.check(f"regression: {key} vs last run", drop <= MAX_ROW_DROP_VS_LAST_RUN,
                        f"{last[key]} -> {current[key]} ({-drop:+.1%})")
    else:
        log.info("no previous run to compare with (first run)")

    (QUALITY / "test_results.json").write_text(
        json.dumps(t.results, indent=2, ensure_ascii=False), encoding="utf-8")
    if not t.failed:                       # only remember metrics of healthy runs
        last_file.write_text(json.dumps(current, indent=2), encoding="utf-8")
    return t


# ------------------------------------------------------------------ main
def timed(name, fn, timings):
    log.info("=== %s : start ===", name)
    t0 = time.time()
    fn()
    timings[name] = round(time.time() - t0, 1)
    log.info("=== %s : done in %.1fs ===", name, timings[name])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true", help="re-download OSM and Overture")
    ap.add_argument("--extract-osm", action="store_true")
    ap.add_argument("--extract-overture", action="store_true")
    args = ap.parse_args()

    setup_logging()
    log.info("pipeline started")
    timings = {}
    try:
        if args.extract or args.extract_osm:
            timed("extract_osm", extract_osm, timings)
        if args.extract or args.extract_overture:
            timed("extract_overture", extract_overture, timings)

        for f in (OSM_FILE, OVERTURE_FILE, DISTRICTS_FILE):
            if not f.exists():
                raise FileNotFoundError(f"Bronze file missing: {f}")

        import silver_clean
        import gold_districts
        timed("silver", silver_clean.main, timings)
        timed("gold", gold_districts.main, timings)

        tests = run_quality_tests()
    except Exception:
        log.exception("pipeline crashed")
        sys.exit(2)

    (QUALITY / "run_summary.json").write_text(
        json.dumps({"timings_sec": timings, "tests_failed": len(tests.failed),
                    "tests_total": len(tests.results)}, indent=2), encoding="utf-8")
    log.info("timings: %s", timings)
    if tests.failed:
        log.error("PIPELINE FAILED: %d of %d tests failed", len(tests.failed), len(tests.results))
        sys.exit(1)
    log.info("PIPELINE OK: all %d tests passed", len(tests.results))


if __name__ == "__main__":
    main()