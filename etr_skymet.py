"""
Builds the Skymet/PoCRA ETR "base file" (2022-2026) from PoCRA's automatic
weather station network (run by Skymet), as an alternate data source to the
IMD gridded archive in etr_historical.py. Mirrors that module's output
schema (Date [+ District [+ Taluka]], ETR_mm_day) so the app can switch
between them with the same downstream code.

Unlike IMD's 39 fixed, always-complete grid points, PoCRA has ~1400-2300 real
stations whose count and reporting change year to year, with real gaps
(a station offline, a whole network gap between 2022-11-09 and 2023-06-01).
So instead of IMD's fixed IDW weight matrix (built once, reused for every
day), this aggregates directly: each day, each admin unit's value is the
mean of whichever of its assigned stations actually reported that day - and
if none did, the result is left NaN, which the app shows as "NA" rather
than guessing.

Run to (re)build the base files:  python etr_skymet.py
"""

import os
import warnings

import numpy as np
import pandas as pd
import shapely

import etr_core as core
from etr_historical import hargreaves_et0_vectorized

NC_DIR = r"D:\VIP\0.Data\rain\0_NetCDF_Data\PoCRA"
YEARS = [2022, 2023, 2024, 2025, 2026]

OUTPUT_DIR = "data/etr_skymet"
STATE_PARQUET = f"{OUTPUT_DIR}/state.parquet"
DISTRICT_PARQUET = f"{OUTPUT_DIR}/district.parquet"
TALUKA_PARQUET = f"{OUTPUT_DIR}/taluka.parquet"
AVAILABILITY_PARQUET = f"{OUTPUT_DIR}/availability.parquet"


def load_year_daily(year):
    """Hourly temp_max/temp_min for one year -> daily Tmax/Tmin per station
    (nanmax/nanmin across the day's hours), plus that year's station coords."""
    import xarray as xr

    path = f"{NC_DIR}/pocra_meteorological_{year}.nc"
    ds = xr.open_dataset(path)
    tmax_daily = ds["temp_max"].resample(time="1D").max(skipna=True)
    tmin_daily = ds["temp_min"].resample(time="1D").min(skipna=True)

    stationid = ds["stationid"].values
    dates = pd.to_datetime(tmax_daily["time"].values)
    tmax_df = pd.DataFrame(tmax_daily.values, index=dates, columns=stationid)
    tmin_df = pd.DataFrame(tmin_daily.values, index=dates, columns=stationid)
    coords = pd.DataFrame({"stationid": stationid, "lat": ds["lat"].values, "lon": ds["lon"].values})
    ds.close()
    return tmax_df, tmin_df, coords


def build_station_series():
    """Concatenate all years into one (Date x StationID) Tmax/Tmin matrix.
    pd.concat aligns columns by stationid automatically, so a station only
    present in some years just gets NaN for the years it's absent."""
    tmax_parts, tmin_parts, coord_parts = [], [], []
    for yr in YEARS:
        print(f"  loading {yr}...")
        tmax_df, tmin_df, coords = load_year_daily(yr)
        tmax_parts.append(tmax_df)
        tmin_parts.append(tmin_df)
        coord_parts.append(coords)

    tmax_full = pd.concat(tmax_parts, axis=0).sort_index()
    tmin_full = pd.concat(tmin_parts, axis=0).sort_index()

    coords_all = pd.concat(coord_parts, ignore_index=True)
    dupe_coord_spread = coords_all.groupby("stationid")[["lat", "lon"]].agg(lambda s: s.max() - s.min())
    drifted = dupe_coord_spread[(dupe_coord_spread["lat"] > 0.01) | (dupe_coord_spread["lon"] > 0.01)]
    if len(drifted):
        print(f"  note: {len(drifted)} station id(s) have shifting coordinates across years - using latest.")
    coords_all = coords_all.drop_duplicates(subset="stationid", keep="last").set_index("stationid")

    return tmax_full, tmin_full, coords_all


def compute_station_etr(tmax_full, tmin_full, coords_all):
    doy = tmax_full.index.dayofyear.to_numpy()
    out = {}
    with np.errstate(invalid="ignore"):  # NaN in Tmax/Tmin -> NaN out is expected, not an error
        for col in tmax_full.columns:
            if col not in coords_all.index:
                continue
            lat = float(coords_all.loc[col, "lat"])
            out[col] = hargreaves_et0_vectorized(
                tmax_full[col].to_numpy(), tmin_full[col].to_numpy(), lat, doy
            )
    return pd.DataFrame(out, index=tmax_full.index)


def assign_stations_to_polygons(lons, lats, polygons):
    """Vectorized point-in-polygon: returns an index array (-1 = no match)
    into `polygons` for each (lon, lat)."""
    assignment = np.full(len(lons), -1, dtype=int)
    for i, poly in enumerate(polygons):
        mask = shapely.contains_xy(poly, lons, lats)
        assignment[mask] = i
    return assignment


def build_base_files():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading PoCRA station data (2022-2026)...")
    tmax_full, tmin_full, coords_all = build_station_series()
    print(f"  {len(coords_all)} unique stations, {len(tmax_full)} days ({tmax_full.index.min().date()} to {tmax_full.index.max().date()})")

    print("Computing daily Hargreaves ETR per station...")
    station_etr = compute_station_etr(tmax_full, tmin_full, coords_all)

    print("Loading boundaries and assigning stations to districts/talukas...")
    state_polygon = core.load_state_polygon_uncached()
    districts, talukas = core.load_admin_layers_uncached()

    coords_valid = coords_all[coords_all.index.isin(station_etr.columns)].copy()
    lons = coords_valid["lon"].to_numpy()
    lats = coords_valid["lat"].to_numpy()

    inside_state = shapely.contains_xy(state_polygon, lons, lats)
    taluka_idx = assign_stations_to_polygons(lons, lats, talukas.geometry.to_numpy())
    district_idx = assign_stations_to_polygons(lons, lats, districts.geometry.to_numpy())

    station_admin = pd.DataFrame({
        "stationid": coords_valid.index,
        "in_state": inside_state,
        "District": [districts.iloc[i]["District"] if i >= 0 else None for i in district_idx],
        "TalukaDistrict": [talukas.iloc[i]["District"] if i >= 0 else None for i in taluka_idx],
        "Taluka": [talukas.iloc[i]["TEHSIL"] if i >= 0 else None for i in taluka_idx],
    })
    n_outside = (~station_admin["in_state"]).sum()
    if n_outside:
        print(f"  {n_outside} station(s) fall outside Maharashtra's boundary - excluded.")
    station_admin = station_admin[station_admin["in_state"]]

    print("Reshaping to long format and aggregating (mean of whichever stations reported)...")
    long_df = station_etr[list(station_admin["stationid"])].reset_index(names="Date").melt(
        id_vars="Date", var_name="stationid", value_name="etr"
    )
    long_df = long_df.merge(station_admin, on="stationid", how="inner")
    long_df = long_df.dropna(subset=["etr"])

    full_dates = pd.date_range(tmax_full.index.min(), tmax_full.index.max(), freq="D")

    state_agg = long_df.groupby("Date")["etr"].mean()
    state_out = pd.DataFrame({"Date": full_dates}).merge(
        state_agg.rename("ETR_mm_day").reset_index(), on="Date", how="left"
    )
    state_out.to_parquet(STATE_PARQUET, index=False)

    district_agg = long_df.groupby(["Date", "District"])["etr"].mean().rename("ETR_mm_day").reset_index()
    district_full_index = pd.MultiIndex.from_product(
        [full_dates, districts["District"]], names=["Date", "District"]
    ).to_frame(index=False)
    district_out = district_full_index.merge(district_agg, on=["Date", "District"], how="left")
    district_out.to_parquet(DISTRICT_PARQUET, index=False)

    taluka_agg = long_df.dropna(subset=["Taluka"]).groupby(["Date", "TalukaDistrict", "Taluka"])["etr"].mean()
    taluka_agg = taluka_agg.rename("ETR_mm_day").reset_index().rename(columns={"TalukaDistrict": "District"})
    taluka_keys = talukas[["District", "TEHSIL"]].rename(columns={"TEHSIL": "Taluka"})
    taluka_full_index = full_dates.to_frame(index=False, name="Date").merge(taluka_keys, how="cross")
    taluka_out = taluka_full_index.merge(taluka_agg, on=["Date", "District", "Taluka"], how="left")
    taluka_out.to_parquet(TALUKA_PARQUET, index=False)

    print("Computing per-unit data availability (first/last date with any value)...")
    avail_rows = []
    s_valid = state_out.dropna(subset=["ETR_mm_day"])
    avail_rows.append({
        "Level": "State", "District": None, "Taluka": None,
        "start": s_valid["Date"].min() if len(s_valid) else None,
        "end": s_valid["Date"].max() if len(s_valid) else None,
    })
    for d, grp in district_out.dropna(subset=["ETR_mm_day"]).groupby("District"):
        avail_rows.append({"Level": "District", "District": d, "Taluka": None,
                            "start": grp["Date"].min(), "end": grp["Date"].max()})
    for (d, t), grp in taluka_out.dropna(subset=["ETR_mm_day"]).groupby(["District", "Taluka"]):
        avail_rows.append({"Level": "Taluka", "District": d, "Taluka": t,
                            "start": grp["Date"].min(), "end": grp["Date"].max()})
    # units with zero valid data anywhere still need a row (start/end = None -> app shows "no data")
    covered = {(r["District"], r["Taluka"]) for r in avail_rows}
    for _, row in districts.iterrows():
        if (row["District"], None) not in covered:
            avail_rows.append({"Level": "District", "District": row["District"], "Taluka": None, "start": None, "end": None})
    for _, row in talukas.iterrows():
        if (row["District"], row["TEHSIL"]) not in covered:
            avail_rows.append({"Level": "Taluka", "District": row["District"], "Taluka": row["TEHSIL"], "start": None, "end": None})

    pd.DataFrame(avail_rows).to_parquet(AVAILABILITY_PARQUET, index=False)

    print("Done.")
    print(f"  {STATE_PARQUET}: {len(state_out)} rows")
    print(f"  {DISTRICT_PARQUET}: {len(district_out)} rows")
    print(f"  {TALUKA_PARQUET}: {len(taluka_out)} rows")


def base_files_exist():
    return all(os.path.exists(p) for p in [STATE_PARQUET, DISTRICT_PARQUET, TALUKA_PARQUET, AVAILABILITY_PARQUET])


def load_state_history():
    return pd.read_parquet(STATE_PARQUET)


def load_district_history():
    df = pd.read_parquet(DISTRICT_PARQUET)
    df["District"] = df["District"].astype("category")
    return df


def load_taluka_history():
    df = pd.read_parquet(TALUKA_PARQUET)
    df["District"] = df["District"].astype("category")
    df["Taluka"] = df["Taluka"].astype("category")
    return df


def load_availability():
    return pd.read_parquet(AVAILABILITY_PARQUET)


if __name__ == "__main__":
    build_base_files()
