"""
Shared, Streamlit-free logic for the Maharashtra ETR app: boundary loading,
the Hargreaves formula, and Open-Meteo fetch helpers. Kept independent of
`streamlit` so it can be imported and unit-tested (or used by the grid
pipeline in etr_grid.py) outside of a running Streamlit session.
"""

import glob
import json
import math

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import Point, shape

MAHARASHTRA_GEOJSON_PATH = "data/maharashtra.geojson"  # fallback state-only outline
MAHARASHTRA_STATE_GEOJSON_PATH = "data/maharashtra_state.geojson"  # precise official state polygon
MAHARASHTRA_DISTRICTS_GEOJSON_PATH = "data/maharashtra_districts.geojson"
BOUNDARY_SHAPEFILE_DIR = "data/boundary"  # drop district/taluka .shp + companions here
SIMPLIFY_TOLERANCE_DEG = 0.003  # ~300m, keeps map fast without visibly distorting shape
MAP_CENTER = [19.75, 75.71]
MAP_ZOOM = 6

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"
GEOCODE_API = "https://nominatim.openstreetmap.org/reverse"


# --------------------------------------------------------------------------
# Boundary loading
# --------------------------------------------------------------------------
def load_boundary_uncached():
    """Prefer a user-supplied district/taluka Shapefile; fall back to the
    plain state-outline GeoJSON if none has been dropped in yet.

    Returns (geojson_for_map, union_polygon_for_containment_check, gdf_or_None).
    gdf is kept (with its original district/taluka attribute columns) so we
    can look up admin names for a clicked point; it's None for the fallback.
    """
    shp_files = glob.glob(f"{BOUNDARY_SHAPEFILE_DIR}/*.shp")
    if shp_files:
        gdf = gpd.read_file(shp_files[0])
        if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs(epsg=4326)
        if "TEHSIL" in gdf.columns:
            # a few rows (e.g. Mumbai City/Suburban) have no taluka subdivision -
            # keep this consistent with load_admin_layers_uncached() so merges on
            # (District, TEHSIL) between the two loaders match cleanly
            gdf["TEHSIL"] = gdf["TEHSIL"].fillna("N/A")
        gdf["geometry"] = gdf.geometry.simplify(SIMPLIFY_TOLERANCE_DEG, preserve_topology=True)
        union_polygon = gdf.geometry.union_all()
        gj = json.loads(gdf.to_json())
        return gj, union_polygon, gdf

    with open(MAHARASHTRA_GEOJSON_PATH, encoding="utf-8") as f:
        gj = json.load(f)
    polygon = shape(gj["features"][0]["geometry"])
    return gj, polygon, None


def load_admin_layers_uncached():
    """Maharashtra districts (36) and talukas (358), from the official
    shapefiles the user provided, preprocessed into data/ - see README."""
    districts = gpd.read_file(MAHARASHTRA_DISTRICTS_GEOJSON_PATH)
    taluka_files = glob.glob(f"{BOUNDARY_SHAPEFILE_DIR}/*.shp")
    talukas = None
    if taluka_files:
        talukas = gpd.read_file(taluka_files[0])
        if talukas.crs is not None and talukas.crs.to_epsg() != 4326:
            talukas = talukas.to_crs(epsg=4326)
        if "TEHSIL" in talukas.columns:
            # a few rows (e.g. Mumbai City/Suburban) have no taluka subdivision
            talukas["TEHSIL"] = talukas["TEHSIL"].fillna("N/A")
    return districts, talukas


def load_state_polygon_uncached():
    """The precise official Maharashtra state polygon (single feature),
    used for the statewide grid (clipping/masking), independent of whichever
    district/taluka boundary happens to be in BOUNDARY_SHAPEFILE_DIR."""
    with open(MAHARASHTRA_STATE_GEOJSON_PATH, encoding="utf-8") as f:
        gj = json.load(f)
    return shape(gj["features"][0]["geometry"])


def find_admin_names(gdf, lat, lon):
    """Look up district/taluka name columns (by fuzzy column-name match) for
    whichever feature contains the clicked point."""
    if gdf is None:
        return {}
    pt = Point(lon, lat)
    matches = gdf[gdf.geometry.contains(pt)]
    if matches.empty:
        return {}
    row = matches.iloc[0]
    result = {}
    for col in gdf.columns:
        cl = col.lower()
        if "district" not in result and ("dist" in cl and "code" not in cl):
            result["District"] = row[col]
        elif "taluka" not in result and any(k in cl for k in ("taluk", "tehsil", "tahsil")):
            result["Taluka"] = row[col]
    return result


# --------------------------------------------------------------------------
# Weather fetch (single point)
# --------------------------------------------------------------------------
def fetch_weather_uncached(lat, lon, mode, start_date=None, end_date=None, forecast_days=7):
    """Return a DataFrame with date, tmax, tmin for the requested period."""
    if mode == "forecast":
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Kolkata",
            "forecast_days": forecast_days,
        }
        resp = requests.get(FORECAST_API, params=params, timeout=15)
    else:
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "Asia/Kolkata",
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
        }
        resp = requests.get(ARCHIVE_API, params=params, timeout=15)

    resp.raise_for_status()
    daily = resp.json()["daily"]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime(daily["time"]),
            "tmax": daily["temperature_2m_max"],
            "tmin": daily["temperature_2m_min"],
        }
    ).dropna()
    return df


def reverse_geocode_uncached(lat, lon):
    try:
        resp = requests.get(
            GEOCODE_API,
            params={"lat": lat, "lon": lon, "format": "json", "zoom": 10},
            headers={"User-Agent": "maharashtra-etr-app"},
            timeout=8,
        )
        resp.raise_for_status()
        addr = resp.json().get("address", {})
        place = addr.get("county") or addr.get("state_district") or addr.get("city") or addr.get("town")
        return place
    except Exception:
        return None


# --------------------------------------------------------------------------
# Hargreaves method
# --------------------------------------------------------------------------
def hargreaves_et0(tmax, tmin, lat_deg, day_of_year):
    """Daily reference evapotranspiration (mm/day) - Hargreaves (1985).

    ET0 = 0.0023 * (Tmean + 17.8) * sqrt(Tmax - Tmin) * Ra
    Ra (extraterrestrial radiation) from FAO-56 astronomical formulas.
    """
    tmean = (tmax + tmin) / 2.0
    lat_rad = math.radians(lat_deg)

    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    delta = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    ws_arg = max(-1.0, min(1.0, -math.tan(lat_rad) * math.tan(delta)))
    ws = math.acos(ws_arg)

    ra_mj = (
        (24 * 60 / math.pi)
        * 0.0820
        * dr
        * (ws * math.sin(lat_rad) * math.sin(delta) + math.cos(lat_rad) * math.cos(delta) * math.sin(ws))
    )
    ra_mm = 0.408 * ra_mj  # MJ/m2/day -> mm/day equivalent

    tdiff = max(tmax - tmin, 0.0)
    et0 = 0.0023 * (tmean + 17.8) * math.sqrt(tdiff) * ra_mm
    return et0, ra_mm, tmean


def compute_etr_table(df, lat_deg):
    rows = []
    for _, r in df.iterrows():
        doy = r["date"].timetuple().tm_yday
        et0, ra, tmean = hargreaves_et0(r["tmax"], r["tmin"], lat_deg, doy)
        rows.append(
            {
                "Date": r["date"].date(),
                "Tmax (C)": round(r["tmax"], 1),
                "Tmin (C)": round(r["tmin"], 1),
                "Tmean (C)": round(tmean, 1),
                "Ra (mm/day)": round(ra, 2),
                "ETR (mm/day)": round(et0, 2),
            }
        )
    return pd.DataFrame(rows)
