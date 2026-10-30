"""
Coffee Map - Bronze -> Silver
Merges OpenStreetMap + Overture cafes, cleans them, removes duplicates.

Folder layout (relative to this file):
    data/bronze/riyadh_cafes_raw.csv      (OSM, from coffee_map.py)
    data/bronze/riyadh_places.geojson     (Overture, raw download)
    data/silver/                          (output)
"""
import json
import math
import re
from difflib import SequenceMatcher
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent
BRONZE = BASE / "data" / "bronze"
SILVER = BASE / "data" / "silver"
SILVER.mkdir(parents=True, exist_ok=True)

OSM_FILE = BRONZE / "riyadh_cafes_raw.csv"
OVERTURE_FILE = BRONZE / "riyadh_places.geojson"

# Riyadh bounding box (same as extraction)
SOUTH, WEST, NORTH, EAST = 24.40, 46.40, 25.10, 47.10

MIN_CONFIDENCE = 0.5     # Overture rows below this are dropped
DUP_DISTANCE_M = 75      # same place if closer than this ...
DUP_NAME_SIM = 0.80      # ... and names are at least this similar


# ---------- helpers ----------
def normalize_name(text):
    """Lowercase, strip punctuation, normalize Arabic letters."""
    if not isinstance(text, str):
        return ""
    t = text.lower().strip()
    t = re.sub(r"[\u064B-\u0652\u0640]", "", t)          # tashkeel + tatweel
    t = re.sub("[\u0622\u0623\u0625]", "\u0627", t)      # alef forms
    t = t.replace("\u0649", "\u064a").replace("\u0629", "\u0647")
    t = re.sub(r"[^\w\s]", " ", t)                       # punctuation
    t = re.sub(r"\s+", " ", t).strip()
    return t


def meters_between(lat1, lon1, lat2, lon2):
    """Fast distance in meters (good enough for short distances)."""
    dy = (lat2 - lat1) * 111_320
    dx = (lon2 - lon1) * 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def names_match(a, b):
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    return SequenceMatcher(None, a, b).ratio() >= DUP_NAME_SIM


# ---------- 1) load bronze ----------
def load_osm():
    if not OSM_FILE.exists():
        raise FileNotFoundError(f"Missing file: {OSM_FILE}")
    df = pd.read_csv(OSM_FILE)
    out = pd.DataFrame({
        "source": "osm",
        "source_id": df["osm_id"].astype(str),
        "name": df["name"],
        "name_ar": df["name_ar"],
        "category": "cafe",
        "lat": df["lat"],
        "lon": df["lon"],
        "confidence": pd.NA,
    })
    return out


def load_overture():
    if not OVERTURE_FILE.exists():
        raise FileNotFoundError(f"Missing file: {OVERTURE_FILE}")
    with open(OVERTURE_FILE, encoding="utf-8") as f:
        data = json.load(f)

    rows = []
    for feat in data["features"]:
        p = feat.get("properties", {}) or {}
        cats = p.get("categories") or {}
        tax = p.get("taxonomy") or {}
        primary = (cats.get("primary") if isinstance(cats, dict) else None) \
            or (tax.get("primary") if isinstance(tax, dict) else None)
        cat_text = json.dumps([cats, tax, p.get("basic_category")],
                              ensure_ascii=False).lower()
        if "cafe" not in cat_text and "coffee" not in cat_text:
            continue
        names = p.get("names") or {}
        common = names.get("common") or {}
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Point":
            continue
        lon, lat = geom["coordinates"][:2]
        rows.append({
            "source": "overture",
            "source_id": str(feat.get("id") or p.get("id")),
            "name": names.get("primary"),
            "name_ar": common.get("ar") if isinstance(common, dict) else None,
            "category": primary,
            "lat": lat,
            "lon": lon,
            "confidence": p.get("confidence"),
        })
    return pd.DataFrame(rows)


# ---------- 2) clean ----------
def clean(df, report):
    report["rows_before_clean"] = len(df)

    df = df.dropna(subset=["lat", "lon"]).copy()
    df["lat"] = pd.to_numeric(df["lat"], errors="coerce")
    df["lon"] = pd.to_numeric(df["lon"], errors="coerce")
    df = df.dropna(subset=["lat", "lon"])
    inside = df["lat"].between(SOUTH, NORTH) & df["lon"].between(WEST, EAST)
    report["dropped_outside_bbox"] = int((~inside).sum())
    df = df[inside]

    df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")
    low_conf = (df["source"] == "overture") & (df["confidence"] < MIN_CONFIDENCE)
    report["dropped_low_confidence"] = int(low_conf.sum())
    df = df[~low_conf]

    for c in ["name", "name_ar"]:
        df[c] = df[c].astype("string").str.strip()
        df.loc[df[c] == "", c] = pd.NA

    no_name = df["name"].isna() & df["name_ar"].isna()
    report["dropped_no_name"] = int(no_name.sum())
    df = df[~no_name]

    # display name: prefer the Arabic one if it exists, else the main name
    df["name_display"] = df["name_ar"].fillna(df["name"])
    df["name_key"] = (df["name"].fillna("") + " " + df["name_ar"].fillna("")
                      ).map(normalize_name)
    report["rows_after_clean"] = len(df)
    return df.reset_index(drop=True)


# ---------- 3) deduplicate (within and across sources) ----------
def deduplicate(df, report):
    n = len(df)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    cell = 0.0008  # ~90 m grid
    grid = {}
    lats, lons, keys = df["lat"].values, df["lon"].values, df["name_key"].values
    for i in range(n):
        grid.setdefault((int(lats[i] / cell), int(lons[i] / cell)), []).append(i)

    for i in range(n):
        gx, gy = int(lats[i] / cell), int(lons[i] / cell)
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((gx + dx, gy + dy), []):
                    if j <= i:
                        continue
                    if meters_between(lats[i], lons[i], lats[j], lons[j]) <= DUP_DISTANCE_M \
                            and names_match(keys[i], keys[j]):
                        union(i, j)

    df = df.copy()
    df["cluster"] = [find(i) for i in range(n)]

    # survivor per cluster: has Arabic name > higher confidence > OSM
    df["_has_ar"] = df["name_ar"].notna().astype(int)
    df["_conf"] = df["confidence"].fillna(1.0)
    df = df.sort_values(["cluster", "_has_ar", "_conf"], ascending=[True, False, False])

    agg_sources = df.groupby("cluster")["source"].agg(lambda s: "+".join(sorted(set(s))))
    best = df.drop_duplicates("cluster", keep="first").set_index("cluster")
    best["sources"] = agg_sources
    best["n_sources"] = best["sources"].str.count(r"\+") + 1
    best = best.reset_index(drop=True).drop(columns=["_has_ar", "_conf"])

    report["duplicates_removed"] = n - len(best)
    report["rows_in_both_sources"] = int((best["n_sources"] > 1).sum())
    return best


# ---------- main ----------
def main():
    report = {}
    osm = load_osm()
    ovt = load_overture()
    report["osm_rows_bronze"] = len(osm)
    report["overture_cafe_rows_bronze"] = len(ovt)

    print("Overture categories included (check these look like cafes):")
    print(ovt["category"].value_counts(dropna=False).head(15), "\n")

    merged = pd.concat([osm, ovt], ignore_index=True)
    merged = clean(merged, report)
    silver = deduplicate(merged, report)

    silver.insert(0, "cafe_id", range(1, len(silver) + 1))
    cols = ["cafe_id", "name_display", "name", "name_ar", "category",
            "lat", "lon", "confidence", "sources", "n_sources"]
    silver = silver[cols]
    report["silver_rows_final"] = len(silver)

    silver.to_csv(SILVER / "cafes_silver.csv", index=False, encoding="utf-8-sig")
    try:
        silver.to_parquet(SILVER / "cafes_silver.parquet", index=False)
    except Exception as ex:
        print("parquet skipped:", ex)
    (SILVER / "quality_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=== Quality report ===")
    for k, v in report.items():
        print(f"{k}: {v}")
    print("\nSaved to:", SILVER)
    print(silver.head(10).to_string())


if __name__ == "__main__":
    main()