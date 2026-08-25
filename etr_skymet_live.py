"""
Live PoCRA/Skymet API client (see API_Documentation_Weather_Endpoint.pdf) -
fetches real, current/recent station readings for the Point Lookup tab's
"Skymet (live station)" option.

Confirmed by direct testing (2026-08-25):
  - Observed data only - the server explicitly rejects future dates
    ("Future dates are not allowed"). No forecast capability.
  - A single station over a day/month is fast (well under a second per
    request). Querying ALL ~2,300 stations for one day returns ~1,000,000
    raw (finer-than-hourly) records - about 100 paginated requests, ~7
    minutes. That's why this module is scoped to one station (nearest to
    the clicked point) at a time, not a statewide fetch.
"""

import calendar
from datetime import date, timedelta

import numpy as np
import pandas as pd
import requests

LOGIN_URL = "https://farmers-app.mahapocra.gov.in/api/login"
WEATHER_URL = "https://farmers-app.mahapocra.gov.in/api/weather_data"
PAGE_LIMIT = 10000

STATION_COORDS_PATH = "data/etr_skymet/station_coords.parquet"


def load_station_coords():
    return pd.read_parquet(STATION_COORDS_PATH)


def nearest_station(lat, lon, coords_df, max_km=25.0):
    """Nearest PoCRA station to (lat, lon), or None if the closest one is
    still farther than max_km away."""
    from etr_grid import haversine_km

    d = haversine_km(
        np.array([lat]), np.array([lon]),
        coords_df["lat"].to_numpy(), coords_df["lon"].to_numpy(),
    )
    idx = int(np.argmin(d))
    dist = float(d[idx])
    row = coords_df.iloc[idx]
    result = {
        "stationid": str(int(row["stationid"])),
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "distance_km": dist,
    }
    if dist > max_km:
        return None, result
    return result, result


def login(username, password):
    resp = requests.post(LOGIN_URL, json={"username": username, "password": password}, timeout=20)
    resp.raise_for_status()
    return resp.json()["access_token"]


def _month_chunks(start_date, end_date):
    """Split a date range into (from, to) pairs each within one calendar
    month, since the API rejects a range spanning more than one month."""
    chunks = []
    cur = start_date
    while cur <= end_date:
        last_day_this_month = date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        chunk_end = min(end_date, last_day_this_month)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def fetch_station_range(token, stationid, start_date, end_date):
    """All raw records for one station over a date range - auto-chunked by
    calendar month, auto-paginated. Returns a list of record dicts."""
    all_records = []
    for chunk_start, chunk_end in _month_chunks(start_date, end_date):
        offset = 0
        while True:
            resp = requests.post(
                WEATHER_URL,
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "from_date": chunk_start.isoformat(),
                    "to_date": chunk_end.isoformat(),
                    "stationid": stationid,
                    "limit": PAGE_LIMIT,
                    "offset": offset,
                },
                timeout=30,
            )
            resp.raise_for_status()
            payload = resp.json()["data"]
            if payload.get("status") == "error":
                raise RuntimeError(payload.get("message", "Unknown API error"))
            records = payload.get("weather", [])
            all_records.extend(records)
            total_count = int(payload.get("total_count", 0) or 0)
            offset += PAGE_LIMIT
            if offset >= total_count or not records:
                break
    return all_records


def records_to_daily(records):
    """Raw per-reading records -> daily Tmax/Tmin DataFrame with columns
    matching etr_core.compute_etr_table's expected input (date, tmax, tmin)."""
    if not records:
        return pd.DataFrame(columns=["date", "tmax", "tmin"])
    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["for_date"])
    df["tmax"] = pd.to_numeric(df["temp_max"], errors="coerce")
    df["tmin"] = pd.to_numeric(df["temp_min"], errors="coerce")
    daily = df.groupby("date").agg(tmax=("tmax", "max"), tmin=("tmin", "min")).reset_index()
    return daily.dropna(subset=["tmax", "tmin"]).sort_values("date")
