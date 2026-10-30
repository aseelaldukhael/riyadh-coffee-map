"""
Coffee Map - Silver -> Gold
Assigns every cafe to a district (point-in-polygon) and builds a district summary.

Inputs  (relative to this file):
    data/silver/cafes_silver.csv
    data/bronze/districts.geojson
Outputs:
    data/gold/cafes_gold.csv
    data/gold/district_summary.csv
    data/gold/gold_report.json
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent
SILVER_FILE = BASE / "data" / "silver" / "cafes_silver.csv"
DISTRICTS_FILE = BASE / "data" / "bronze" / "districts.geojson"
GOLD = BASE / "data" / "gold"
GOLD.mkdir(parents=True, exist_ok=True)

# Business rule: these categories are not cafes for our analysis
EXCLUDE_CATEGORIES = {"internet_cafe", "coffee_and_tea_supplies", "cafeteria"}

# Polygons that are not real residential districts (huge areas that overlap many
# neighborhoods). Their cafes stay on the map but are excluded from district ranking.
NON_DISTRICT_KEYWORDS = ["airport"]

# Riyadh region id in the districts dataset (region 1 = Riyadh region)
RIYADH_REGION_ID = 1
RIYADH_BBOX = (24.40, 46.40, 25.10, 47.10)  # south, west, north, east


# ---------- geometry helpers (pure numpy, no extra libraries) ----------
def ring_area_deg2(ring):
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def point_in_ring(x, y, ring):
    """Ray casting for one point against one ring (vectorized over the ring)."""
    xs, ys = ring[:, 0], ring[:, 1]
    xs2, ys2 = np.roll(xs, -1), np.roll(ys, -1)
    cond = (ys > y) != (ys2 > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xcross = (xs2 - xs) * (y - ys) / (ys2 - ys) + xs
    return bool(np.count_nonzero(cond & (x < xcross)) % 2)


def load_districts():
    if not DISTRICTS_FILE.exists():
        raise FileNotFoundError(f"Missing file: {DISTRICTS_FILE}")
    with open(DISTRICTS_FILE, encoding="utf-8") as f:
        data = json.load(f)

    districts = []
    for feat in data["features"]:
        geom = feat.get("geometry") or {}
        if geom.get("type") == "Polygon":
            polygons = [geom["coordinates"]]
        elif geom.get("type") == "MultiPolygon":
            polygons = geom["coordinates"]
        else:
            continue
        parsed = []
        for poly in polygons:
            rings = [np.array(r, dtype=float)[:, :2] for r in poly]
            parsed.append(rings)  # first ring = outer, others = holes
        allpts = np.vstack([r for poly in parsed for r in poly[:1]])
        p = feat["properties"]
        districts.append({
            "district_id": p.get("district_id"),
            "city_id": p.get("city_id"),
            "region_id": p.get("region_id"),
            "name_ar": p.get("name_ar"),
            "name_en": p.get("name_en"),
            "polys": parsed,
            "minx": allpts[:, 0].min(), "maxx": allpts[:, 0].max(),
            "miny": allpts[:, 1].min(), "maxy": allpts[:, 1].max(),
        })
    return districts


def district_area_km2(d):
    total = 0.0
    for rings in d["polys"]:
        a = ring_area_deg2(rings[0]) - sum(ring_area_deg2(h) for h in rings[1:])
        total += a
    lat = (d["miny"] + d["maxy"]) / 2
    return total * (111.32 ** 2) * math.cos(math.radians(lat))


def assign_districts(cafes, districts):
    """Returns the index of the matching district for every cafe (-1 = none)."""
    lons = cafes["lon"].values
    lats = cafes["lat"].values
    minx = np.array([d["minx"] for d in districts])
    maxx = np.array([d["maxx"] for d in districts])
    miny = np.array([d["miny"] for d in districts])
    maxy = np.array([d["maxy"] for d in districts])

    result = np.full(len(cafes), -1, dtype=int)
    for k in range(len(cafes)):
        x, y = lons[k], lats[k]
        cand = np.where((minx <= x) & (x <= maxx) & (miny <= y) & (y <= maxy))[0]
        for ci in cand:
            hit = False
            for rings in districts[ci]["polys"]:
                if point_in_ring(x, y, rings[0]):
                    in_hole = any(point_in_ring(x, y, h) for h in rings[1:])
                    if not in_hole:
                        hit = True
                        break
            if hit:
                result[k] = ci
                break
    return result


def main():
    report = {}
    if not SILVER_FILE.exists():
        raise FileNotFoundError(f"Missing file: {SILVER_FILE}")
    cafes = pd.read_csv(SILVER_FILE)
    report["silver_rows"] = len(cafes)

    # business rule: drop non-cafe categories
    excl = cafes["category"].isin(EXCLUDE_CATEGORIES)
    report["dropped_non_cafe_categories"] = int(excl.sum())
    cafes = cafes[~excl].reset_index(drop=True)

    # only districts of the Riyadh region (speeds things up)
    districts = [d for d in load_districts() if d["region_id"] == RIYADH_REGION_ID]
    report["districts_in_region"] = len(districts)

    # sanity check: coordinates are [lon, lat]
    d0 = districts[0]
    if not (16 < d0["miny"] < 33 and 34 < d0["minx"] < 56):
        print("WARNING: district coordinates look swapped (lat/lon). Check the file.")

    idx = assign_districts(cafes, districts)
    cafes["district_id"] = [districts[i]["district_id"] if i >= 0 else pd.NA for i in idx]
    cafes["city_id"] = [districts[i]["city_id"] if i >= 0 else pd.NA for i in idx]
    cafes["district_ar"] = [districts[i]["name_ar"] if i >= 0 else pd.NA for i in idx]
    cafes["district_en"] = [districts[i]["name_en"] if i >= 0 else pd.NA for i in idx]

    matched = cafes["district_id"].notna()
    report["cafes_matched_to_district"] = int(matched.sum())
    report["cafes_unmatched"] = int((~matched).sum())

    # Riyadh city = the city_id that holds most of the matched cafes
    city_counts = cafes.loc[matched, "city_id"].value_counts()
    print("Cafes per city_id (top 5):")
    print(city_counts.head(5), "\n")
    riyadh_city_id = int(city_counts.index[0])
    report["detected_riyadh_city_id"] = riyadh_city_id

    in_city = cafes["city_id"] == riyadh_city_id
    report["cafes_outside_riyadh_city_dropped"] = int((~in_city).sum())
    gold = cafes[in_city].copy()
    report["gold_cafes_final"] = len(gold)

    # flag cafes that fall inside non-district polygons (e.g. the airport area)
    pattern = "|".join(NON_DISTRICT_KEYWORDS)
    gold["in_district_ranking"] = ~gold["district_en"].str.lower().str.contains(
        pattern, na=False)
    report["cafes_in_non_district_polygons"] = int((~gold["in_district_ranking"]).sum())
    ranked = gold[gold["in_district_ranking"]]

    # district summary (ranked cafes only)
    summary = (ranked.groupby(["district_id", "district_ar", "district_en"])
               .agg(cafes=("cafe_id", "count"),
                    avg_confidence=("confidence", "mean"))
               .reset_index())
    area = {d["district_id"]: district_area_km2(d) for d in districts}
    summary["area_km2"] = summary["district_id"].map(area).round(2)
    summary["cafes_per_km2"] = (summary["cafes"] / summary["area_km2"]).round(1)
    summary = summary.sort_values("cafes", ascending=False)
    report["districts_with_cafes"] = len(summary)

    gold.to_csv(GOLD / "cafes_gold.csv", index=False, encoding="utf-8-sig")
    summary.to_csv(GOLD / "district_summary.csv", index=False, encoding="utf-8-sig")
    (GOLD / "gold_report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print("=== Gold report ===")
    for k, v in report.items():
        print(f"{k}: {v}")
    print("\nTop 15 districts by number of cafes:")
    print(summary.head(15)[["district_ar", "district_en", "cafes",
                            "area_km2", "cafes_per_km2"]].to_string(index=False))
    print("\nSaved to:", GOLD)


if __name__ == "__main__":
    main()