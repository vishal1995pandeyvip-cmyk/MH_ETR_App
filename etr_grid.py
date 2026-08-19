"""
Statewide ETR grid: query a coarse network of points across Maharashtra,
compute Hargreaves ETR at each, then IDW-interpolate onto a fine grid for
a raster map of the whole state.
"""

import math
import time

import branca.colormap as bcm
import geopandas as gpd
import numpy as np
import requests
import shapely

from etr_core import FORECAST_API, ARCHIVE_API, hargreaves_et0

# Green (low ETR) -> yellow (medium) -> red (high) - the standard convention for
# agricultural water-stress/demand maps (ColorBrewer RdYlGn, reversed).
ETR_COLOR_STEPS = [
    "#1a9850", "#66bd63", "#a6d96a", "#d9ef8b",
    "#fee08b", "#fdae61", "#f46d43", "#d73027",
]

EARTH_RADIUS_KM = 6371.0
BATCH_SIZE = 140  # points per Open-Meteo request; confirmed working up to ~150 before URL-length errors


def haversine_km(lat1, lon1, lat2, lon2):
    """Vectorized great-circle distance (km) between (lat1,lon1) arrays and (lat2,lon2) arrays."""
    lat1, lon1, lat2, lon2 = map(np.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def generate_station_points(polygon, spacing_deg):
    """Regular lat/lon grid at spacing_deg, kept only where it falls inside polygon."""
    minx, miny, maxx, maxy = polygon.bounds
    lons = np.arange(minx, maxx + spacing_deg, spacing_deg)
    lats = np.arange(miny, maxy + spacing_deg, spacing_deg)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    inside = shapely.contains_xy(polygon, lon_grid, lat_grid)
    return [
        (round(float(lat), 4), round(float(lon), 4))
        for lat, lon in zip(lat_grid[inside], lon_grid[inside])
    ]


def generate_fine_grid(polygon, resolution_deg):
    """Fine meshgrid over polygon's bounds; returns (lat_grid, lon_grid, inside_mask)."""
    minx, miny, maxx, maxy = polygon.bounds
    lons = np.arange(minx, maxx + resolution_deg, resolution_deg)
    lats = np.arange(miny, maxy + resolution_deg, resolution_deg)
    lon_grid, lat_grid = np.meshgrid(lons, lats)  # shape (n_lat, n_lon)
    inside = shapely.contains_xy(polygon, lon_grid, lat_grid)
    return lat_grid, lon_grid, inside


MAX_RETRIES = 4
RETRY_BACKOFF_SECONDS = 2.0  # doubles each retry: 2s, 4s, 8s, 16s
INTER_BATCH_DELAY_SECONDS = 0.4  # spacing between successive batch requests


def _fetch_chunk(lats, lons, mode, date_str):
    api = FORECAST_API if mode == "forecast" else ARCHIVE_API
    params = {
        "latitude": ",".join(f"{v:.4f}" for v in lats),
        "longitude": ",".join(f"{v:.4f}" for v in lons),
        "daily": "temperature_2m_max,temperature_2m_min",
        "timezone": "Asia/Kolkata",
        "start_date": date_str,
        "end_date": date_str,
    }

    for attempt in range(MAX_RETRIES + 1):
        resp = requests.get(api, params=params, timeout=30)
        if resp.status_code == 429 and attempt < MAX_RETRIES:
            # Open-Meteo's free tier has a short burst limit - a handful of
            # rapid-fire batched requests (e.g. finer station spacing needing
            # 6-7 chunks) can trip it even though the daily quota is nowhere
            # close. Back off and retry rather than failing the whole map.
            time.sleep(RETRY_BACKOFF_SECONDS * (2 ** attempt))
            continue
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            data = [data]
        return data
    resp.raise_for_status()  # exhausted retries - raise the last 429


def fetch_station_weather(points, mode, date_str, progress_cb=None):
    """points: list of (lat, lon). Returns list of dicts with lat, lon, tmax, tmin
    (points where the API returned no data for that date are skipped)."""
    results = []
    n_chunks = math.ceil(len(points) / BATCH_SIZE)
    for chunk_idx, i in enumerate(range(0, len(points), BATCH_SIZE)):
        if chunk_idx > 0:
            time.sleep(INTER_BATCH_DELAY_SECONDS)
        chunk = points[i : i + BATCH_SIZE]
        lats = [p[0] for p in chunk]
        lons = [p[1] for p in chunk]
        data = _fetch_chunk(lats, lons, mode, date_str)
        for (lat, lon), rec in zip(chunk, data):
            daily = rec.get("daily", {})
            tmax_list = daily.get("temperature_2m_max", [])
            tmin_list = daily.get("temperature_2m_min", [])
            if tmax_list and tmin_list and tmax_list[0] is not None and tmin_list[0] is not None:
                results.append({"lat": lat, "lon": lon, "tmax": tmax_list[0], "tmin": tmin_list[0]})
        if progress_cb:
            progress_cb(min(1.0, (i + BATCH_SIZE) / len(points)))
    return results


def compute_station_etr(stations, day_of_year):
    """Adds an 'etr' key (Hargreaves ET0, mm/day) to each station dict, using its own latitude for Ra."""
    for s in stations:
        et0, ra, tmean = hargreaves_et0(s["tmax"], s["tmin"], s["lat"], day_of_year)
        s["etr"] = et0
    return stations


def idw_grid(stations, lat_grid, lon_grid, inside_mask, power=2.0):
    """Inverse-distance-weighted interpolation of station['etr'] onto the fine grid.
    Returns an array shaped like lat_grid, with NaN outside inside_mask."""
    station_lat = np.array([s["lat"] for s in stations])
    station_lon = np.array([s["lon"] for s in stations])
    station_val = np.array([s["etr"] for s in stations])

    out = np.full(lat_grid.shape, np.nan)
    flat_lat = lat_grid[inside_mask]
    flat_lon = lon_grid[inside_mask]

    # distance matrix: (n_grid_points, n_stations)
    d = haversine_km(
        flat_lat[:, None], flat_lon[:, None],
        station_lat[None, :], station_lon[None, :],
    )
    d = np.where(d < 1e-6, 1e-6, d)  # avoid div-by-zero when grid point coincides with a station
    weights = 1.0 / (d ** power)
    interpolated = (weights * station_val[None, :]).sum(axis=1) / weights.sum(axis=1)

    out[inside_mask] = interpolated
    return out


def value_range(etr_grid_vals):
    return float(np.nanmin(etr_grid_vals)), float(np.nanmax(etr_grid_vals))


def state_average(etr_grid_vals):
    return float(np.nanmean(etr_grid_vals))


def idw_single_point(stations, lat, lon, power=2.0):
    """IDW-interpolated ETR at one arbitrary point, from the same coarse
    station network - used as a fallback for admin units too small to
    contain any fine-grid sample."""
    station_lat = np.array([s["lat"] for s in stations])
    station_lon = np.array([s["lon"] for s in stations])
    station_val = np.array([s["etr"] for s in stations])
    d = haversine_km(np.array([lat]), np.array([lon]), station_lat, station_lon)
    d = np.where(d < 1e-6, 1e-6, d)
    w = 1.0 / (d ** power)
    return float((w * station_val).sum() / w.sum())


def aggregate_by_admin(lat_grid, lon_grid, etr_grid_vals, admin_gdf, group_cols, stations=None):
    """Average the fine ETR grid's cells that fall inside each admin polygon
    (district or taluka), via a spatial join - reuses the grid already
    computed for the raster, no extra API calls. Returns admin_gdf with two
    new columns: etr_avg, n_samples. Admin units too small to contain any
    grid cell fall back to a direct IDW estimate at their centroid."""
    mask = ~np.isnan(etr_grid_vals)
    points_gdf = gpd.GeoDataFrame(
        {"etr": etr_grid_vals[mask]},
        geometry=gpd.points_from_xy(lon_grid[mask], lat_grid[mask]),
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(points_gdf, admin_gdf[group_cols + ["geometry"]], predicate="within", how="inner")
    stats = joined.groupby(group_cols).agg(etr_avg=("etr", "mean"), n_samples=("etr", "count")).reset_index()

    result = admin_gdf.merge(stats, on=group_cols, how="left")
    missing = result["etr_avg"].isna()
    if missing.any() and stations:
        centroids = result.loc[missing, "geometry"].centroid
        result.loc[missing, "etr_avg"] = [idw_single_point(stations, pt.y, pt.x) for pt in centroids]
        result.loc[missing, "n_samples"] = 0
    result["n_samples"] = result["n_samples"].fillna(0).astype(int)
    return result


def build_colormap(vmin, vmax):
    """branca LinearColormap: green (low ETR) -> yellow -> red (high ETR),
    shared by the choropleth fill and the on-map legend so both agree exactly."""
    return bcm.LinearColormap(colors=ETR_COLOR_STEPS, vmin=vmin, vmax=vmax)


def raster_colorizer(colormap):
    """Wraps a branca colormap so NaN cells (outside the state polygon) render
    fully transparent instead of erroring - passed as folium ImageOverlay's
    `colormap` callable, applied directly to the mono ETR grid array."""
    def _colorize(x):
        if np.isnan(x):
            return (0.0, 0.0, 0.0, 0.0)
        return colormap.rgba_floats_tuple(x)
    return _colorize
