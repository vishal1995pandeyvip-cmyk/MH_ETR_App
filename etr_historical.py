"""
Builds the historical ETR "base file" (1951-2025) from the IMD gridded
temperature dataset the user supplied in temperature/, and serves lookups
against it for the app's download feature.

Pipeline:
  1. Extract daily Tmax/Tmin at the 39 fixed IMD grid points covering
     Maharashtra, for the full NetCDF archive (1951-2025).
  2. Compute daily Hargreaves ETR at each of those 39 points (vectorized).
  3. Precompute a linear weight matrix mapping the 39 station values onto
     each admin unit (state / district / taluka) - IDW onto a fine grid,
     then averaged within each polygon, composed into one small matrix per
     level. Because both steps are linear in the station values, applying
     that matrix to the *entire* 75-year station series in one matrix
     multiply reproduces "IDW-interpolate then average per polygon for
     every single day" without ever looping over 27,000+ days.
  4. Save one Parquet file per level (state/district/taluka) - the "base
     file" - plus a small availability table recording each unit's actual
     first/last non-null date (computed genuinely, not assumed, in case a
     future dataset update has gaps some units don't).

Run standalone to (re)build the base files:  python etr_historical.py
"""

import math
import os

import numpy as np
import pandas as pd
import shapely
# xarray is only needed to read the raw NetCDF files during the offline build
# (build_base_files / load_station_series) - imported lazily there so the
# deployed app, which only ever reads the small parquet outputs, doesn't need
# xarray/netCDF4 installed at all.

import etr_core as core
import etr_grid as grid

NC_DIR = "temperature/0_NetCDF_Data/IMD"
MH_COORDS_CSV = "temperature/0_IMD_Maharashtra_Coordinate_List.csv"
START_YEAR = 1951
END_YEAR = 2025
FINE_RESOLUTION_DEG = 0.05  # same resolution used for the live Explore-map grid

OUTPUT_DIR = "data/etr_history"
STATE_PARQUET = f"{OUTPUT_DIR}/state.parquet"
DISTRICT_PARQUET = f"{OUTPUT_DIR}/district.parquet"
TALUKA_PARQUET = f"{OUTPUT_DIR}/taluka.parquet"
AVAILABILITY_PARQUET = f"{OUTPUT_DIR}/availability.parquet"


def load_station_coords():
    coords_df = pd.read_csv(MH_COORDS_CSV, nrows=1)
    coord_cols = [c for c in coords_df.columns if c != "Date"]
    points = [(float(c.split(",")[0]), float(c.split(",")[1])) for c in coord_cols]
    return coord_cols, points  # coord_cols e.g. "19.5,75.5", points = [(19.5,75.5), ...]


def load_station_series(var, coord_cols, points):
    """var: 'tmax' or 'tmin'. Returns a DataFrame, Date index, one column per station."""
    import xarray as xr

    files = [f"{NC_DIR}/{var}_{y}.nc" for y in range(START_YEAR, END_YEAR + 1)]
    files = [f for f in files if os.path.exists(f)]
    ds = xr.open_mfdataset(files, combine="by_coords")
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    sel = ds[var].sel(
        lat=xr.DataArray(lats, dims="point"),
        lon=xr.DataArray(lons, dims="point"),
        method="nearest",
    )
    df = sel.to_pandas()
    df.columns = coord_cols
    ds.close()
    return df


def hargreaves_et0_vectorized(tmax, tmin, lat_deg, doy):
    """Same formula as etr_core.hargreaves_et0, vectorized over numpy arrays
    (tmax, tmin, doy all same-length arrays; lat_deg a scalar for one station)."""
    tmean = (tmax + tmin) / 2.0
    lat_rad = math.radians(lat_deg)

    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    delta = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    ws_arg = np.clip(-math.tan(lat_rad) * np.tan(delta), -1.0, 1.0)
    ws = np.arccos(ws_arg)

    ra_mj = (
        (24 * 60 / math.pi) * 0.0820 * dr
        * (ws * math.sin(lat_rad) * np.sin(delta) + math.cos(lat_rad) * np.cos(delta) * np.sin(ws))
    )
    ra_mm = 0.408 * ra_mj

    tdiff = np.clip(tmax - tmin, 0.0, None)
    return 0.0023 * (tmean + 17.8) * np.sqrt(tdiff) * ra_mm


def compute_station_etr_series(tmax_df, tmin_df, points):
    """Daily Hargreaves ETR at each station, full time series. Returns a
    DataFrame shaped like tmax_df (Date index, one column per station)."""
    doy = tmax_df.index.dayofyear.to_numpy()
    out = {}
    for col, (lat, lon) in zip(tmax_df.columns, points):
        out[col] = hargreaves_et0_vectorized(
            tmax_df[col].to_numpy(), tmin_df[col].to_numpy(), lat, doy
        )
    return pd.DataFrame(out, index=tmax_df.index)


def build_station_to_grid_idw(points, lat_grid, lon_grid, power=2.0):
    """(n_grid, n_stations) IDW weight matrix - fixed for all dates since
    only positions matter, not values."""
    station_lat = np.array([p[0] for p in points])
    station_lon = np.array([p[1] for p in points])
    d = grid.haversine_km(
        lat_grid.ravel()[:, None], lon_grid.ravel()[:, None],
        station_lat[None, :], station_lon[None, :],
    )
    d = np.where(d < 1e-6, 1e-6, d)
    w = 1.0 / (d ** power)
    return w / w.sum(axis=1, keepdims=True)


def build_admin_weight_matrix(admin_gdf, group_cols, points, lat_grid, lon_grid, inside_mask, grid_to_station_w):
    """Combined (n_admin_units, n_stations) weight matrix: IDW-to-fine-grid
    composed with per-polygon averaging, so admin_etr = W @ station_etr for
    every day at once. Units with no interior grid cell fall back to a
    direct single-point IDW weight at their centroid."""
    flat_lat = lat_grid.ravel()[inside_mask.ravel()]
    flat_lon = lon_grid.ravel()[inside_mask.ravel()]
    w_inside = grid_to_station_w[inside_mask.ravel()]  # (n_inside_grid, n_stations)

    n_units = len(admin_gdf)
    n_stations = len(points)
    W = np.zeros((n_units, n_stations))

    for i, poly in enumerate(admin_gdf.geometry):
        member = shapely.contains_xy(poly, flat_lon, flat_lat)
        if member.any():
            W[i] = w_inside[member].mean(axis=0)
        else:
            centroid = poly.centroid
            station_lat = np.array([p[0] for p in points])
            station_lon = np.array([p[1] for p in points])
            d = grid.haversine_km(
                np.array([centroid.y]), np.array([centroid.x]), station_lat, station_lon
            )
            d = np.where(d < 1e-6, 1e-6, d)
            w = 1.0 / (d ** 2.0)
            W[i] = (w / w.sum())[0]
    return W


def build_base_files():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("Loading station coordinates...")
    coord_cols, points = load_station_coords()

    print("Extracting 1951-2025 Tmax/Tmin at 39 stations (this reads all NetCDF files)...")
    tmax_df = load_station_series("tmax", coord_cols, points)
    tmin_df = load_station_series("tmin", coord_cols, points)

    print("Computing daily Hargreaves ETR at each station...")
    station_etr = compute_station_etr_series(tmax_df, tmin_df, points)

    print("Loading boundaries...")
    state_polygon = core.load_state_polygon_uncached()
    districts, talukas = core.load_admin_layers_uncached()

    print("Building fine grid + IDW weight matrix...")
    lat_grid, lon_grid, inside_mask = grid.generate_fine_grid(state_polygon, FINE_RESOLUTION_DEG)
    grid_to_station_w = build_station_to_grid_idw(points, lat_grid, lon_grid)

    print("Building district weight matrix...")
    district_w = build_admin_weight_matrix(
        districts, ["District"], points, lat_grid, lon_grid, inside_mask, grid_to_station_w
    )
    print("Building taluka weight matrix...")
    taluka_w = build_admin_weight_matrix(
        talukas, ["District", "TEHSIL"], points, lat_grid, lon_grid, inside_mask, grid_to_station_w
    )
    state_w = grid_to_station_w[inside_mask.ravel()].mean(axis=0, keepdims=True)

    etr_matrix = station_etr.to_numpy()  # (n_days, n_stations)

    print("Applying weight matrices to the full 75-year series (matrix multiply)...")
    state_series = etr_matrix @ state_w.T  # (n_days, 1)
    district_series = etr_matrix @ district_w.T  # (n_days, n_districts)
    taluka_series = etr_matrix @ taluka_w.T  # (n_days, n_talukas)

    dates = station_etr.index

    print("Writing Parquet base files...")
    state_out = pd.DataFrame({"Date": dates, "ETR_mm_day": state_series[:, 0]})
    state_out.to_parquet(STATE_PARQUET, index=False)

    district_out = pd.DataFrame(district_series, index=dates, columns=districts["District"].tolist())
    district_out = district_out.reset_index(names="Date").melt(
        id_vars="Date", var_name="District", value_name="ETR_mm_day"
    )
    district_out.to_parquet(DISTRICT_PARQUET, index=False)

    taluka_names = list(zip(talukas["District"], talukas["TEHSIL"]))
    taluka_out = pd.DataFrame(taluka_series, index=dates, columns=pd.MultiIndex.from_tuples(taluka_names))
    taluka_out = taluka_out.stack(level=[0, 1], future_stack=True).reset_index()
    taluka_out.columns = ["Date", "District", "Taluka", "ETR_mm_day"]
    taluka_out.to_parquet(TALUKA_PARQUET, index=False)

    print("Computing per-unit data availability (actual first/last non-null date)...")

    def first_last_valid(values_1d):
        valid = ~np.isnan(values_1d)
        if not valid.any():
            return None, None
        idx = np.nonzero(valid)[0]
        return dates[idx[0]], dates[idx[-1]]

    avail_rows = []
    s0, s1 = first_last_valid(state_series[:, 0])
    avail_rows.append({"Level": "State", "District": None, "Taluka": None, "start": s0, "end": s1})

    for j, d in enumerate(districts["District"]):
        d0, d1 = first_last_valid(district_series[:, j])
        avail_rows.append({"Level": "District", "District": d, "Taluka": None, "start": d0, "end": d1})

    for j, (d, t) in enumerate(taluka_names):
        t0, t1 = first_last_valid(taluka_series[:, j])
        avail_rows.append({"Level": "Taluka", "District": d, "Taluka": t, "start": t0, "end": t1})

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


def get_district_list(district_df):
    return sorted(district_df["District"].unique().tolist())


def get_taluka_list(taluka_df, district):
    subset = taluka_df[taluka_df["District"] == district]
    return sorted(subset["Taluka"].unique().tolist())


def get_availability_row(avail_df, level, district=None, taluka=None):
    if level == "State":
        match = avail_df[avail_df["Level"] == "State"]
    elif level == "District":
        match = avail_df[(avail_df["Level"] == "District") & (avail_df["District"] == district)]
    else:
        match = avail_df[
            (avail_df["Level"] == "Taluka") & (avail_df["District"] == district) & (avail_df["Taluka"] == taluka)
        ]
    return match.iloc[0] if len(match) else None


def get_unit_series(level, state_df, district_df, taluka_df, district=None, taluka=None):
    if level == "State":
        return state_df[["Date", "ETR_mm_day"]]
    if level == "District":
        return district_df.loc[district_df["District"] == district, ["Date", "ETR_mm_day"]]
    return taluka_df.loc[
        (taluka_df["District"] == district) & (taluka_df["Taluka"] == taluka), ["Date", "ETR_mm_day"]
    ]


if __name__ == "__main__":
    build_base_files()
