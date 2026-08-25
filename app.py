"""
ETR (Reference Evapotranspiration) Calculator for Maharashtra - Hargreaves Method
------------------------------------------------------------------------------
Tab 1: click any point inside Maharashtra to get its daily ETR (historical
       range or upcoming forecast).
Tab 2: a statewide ETR raster for a single date, built from a network of
       sampled points IDW-interpolated across the whole state.

Run locally:    streamlit run app.py
Deploy free:    push this folder to GitHub, then deploy on share.streamlit.io
"""

from datetime import date, timedelta

import folium
import pandas as pd
import streamlit as st
from shapely.geometry import Point, mapping
from streamlit_folium import st_folium

import etr_core as core
import etr_grid as grid
import etr_historical as hist
import etr_skymet as skymet
import etr_skymet_live as skymet_live

st.set_page_config(page_title="Maharashtra ETR (Hargreaves)", layout="wide")

# Zoom thresholds for the adaptive drill-down map (folium/leaflet zoom levels)
ZOOM_DISTRICT_MIN = 7
ZOOM_TALUKA_MIN = 9


@st.cache_data
def load_boundary():
    return core.load_boundary_uncached()


@st.cache_data
def load_state_polygon():
    return core.load_state_polygon_uncached()


@st.cache_data
def load_admin_layers():
    return core.load_admin_layers_uncached()


# cache_resource, not cache_data: these are large (up to ~10M rows) and
# read-only after load, so we want the same in-memory object shared across
# reruns rather than a fresh deep copy handed out every time.
@st.cache_resource
def load_history_state():
    return hist.load_state_history()


@st.cache_resource
def load_history_district():
    return hist.load_district_history()


@st.cache_resource
def load_history_taluka():
    return hist.load_taluka_history()


@st.cache_resource
def load_history_availability():
    return hist.load_availability()


@st.cache_resource
def load_skymet_state():
    return skymet.load_state_history()


@st.cache_resource
def load_skymet_district():
    return skymet.load_district_history()


@st.cache_resource
def load_skymet_taluka():
    return skymet.load_taluka_history()


@st.cache_resource
def load_skymet_availability():
    return skymet.load_availability()


@st.cache_data(ttl=3600)
def fetch_weather(lat, lon, mode, start_date=None, end_date=None, forecast_days=7):
    return core.fetch_weather_uncached(lat, lon, mode, start_date, end_date, forecast_days)


@st.cache_data(ttl=3600)
def reverse_geocode(lat, lon):
    return core.reverse_geocode_uncached(lat, lon)


@st.cache_data
def load_skymet_station_coords():
    return skymet_live.load_station_coords()


@st.cache_data(ttl=1500, show_spinner=False)  # 25 min - the API token itself expires at 30 min
def login_skymet_live():
    if "skymet_username" not in st.secrets or "skymet_password" not in st.secrets:
        raise RuntimeError(
            "Skymet live credentials aren't configured. Add skymet_username/skymet_password "
            "to .streamlit/secrets.toml locally, or to the app's Secrets in Streamlit Cloud."
        )
    return skymet_live.login(st.secrets["skymet_username"], st.secrets["skymet_password"])


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_skymet_point_series(lat, lon, start_date, end_date):
    """Returns (station_info_or_None, nearest_info, daily_df). station_info is None
    if the nearest station is farther than the max radius; nearest_info always has
    the nearest station's distance, for a helpful message either way."""
    coords = load_skymet_station_coords()
    station, nearest_info = skymet_live.nearest_station(lat, lon, coords)
    if station is None:
        return None, nearest_info, None
    token = login_skymet_live()
    records = skymet_live.fetch_station_range(token, station["stationid"], start_date, end_date)
    daily = skymet_live.records_to_daily(records)
    return station, nearest_info, daily


@st.cache_data(ttl=3600, show_spinner=False)
def compute_statewide_grid(mode, date_str, station_spacing, fine_resolution):
    polygon = core.load_state_polygon_uncached()
    stations = grid.generate_station_points(polygon, station_spacing)
    weather = grid.fetch_station_weather(stations, mode, date_str)
    if len(weather) < 4:
        return None
    doy = date.fromisoformat(date_str).timetuple().tm_yday
    weather = grid.compute_station_etr(weather, doy)
    lat_grid, lon_grid, inside = grid.generate_fine_grid(polygon, fine_resolution)
    etr_grid_vals = grid.idw_grid(weather, lat_grid, lon_grid, inside)
    return {
        "stations": weather,
        "lat_grid": lat_grid,
        "lon_grid": lon_grid,
        "inside": inside,
        "etr_grid": etr_grid_vals,
        "bounds": polygon.bounds,
    }


@st.cache_data(ttl=3600, show_spinner=False)
def compute_admin_aggregates(mode, date_str, station_spacing, fine_resolution):
    """Statewide grid + its average aggregated up to district and taluka level -
    powers the zoom-adaptive drill-down map (state -> district -> taluka)."""
    result = compute_statewide_grid(mode, date_str, station_spacing, fine_resolution)
    if result is None:
        return None
    districts, talukas = load_admin_layers()
    district_avg = grid.aggregate_by_admin(
        result["lat_grid"], result["lon_grid"], result["etr_grid"],
        districts, ["District"], stations=result["stations"],
    )
    taluka_avg = None
    if talukas is not None:
        taluka_avg = grid.aggregate_by_admin(
            result["lat_grid"], result["lon_grid"], result["etr_grid"],
            talukas, ["District", "TEHSIL"], stations=result["stations"],
        )
    return {
        "grid_result": result,
        "district": district_avg,
        "taluka": taluka_avg,
        "state_avg": grid.state_average(result["etr_grid"]),
    }


st.title("Maharashtra ETR Calculator - Hargreaves Method")
st.caption(
    "Reference evapotranspiration (ETR / ET0) for Maharashtra, computed with the "
    "Hargreaves (1985) temperature-based method."
)

tab_point, tab_state, tab_history = st.tabs(
    ["📍 Point lookup", "🗺️ Statewide map", "📥 Historical data (1951-2025)"]
)

# ============================================================================
# TAB 1: single-point lookup (click a location, see its ETR time series)
# ============================================================================
with tab_point:
    boundary_gj, boundary_polygon, boundary_gdf = load_boundary()
    if boundary_gdf is None:
        st.info(
            "Using the plain Maharashtra state outline. Drop your district/taluka "
            f"Shapefile (.shp + .shx/.dbf/.prj) into `{core.BOUNDARY_SHAPEFILE_DIR}/` and "
            "reload to get district/taluka boundaries and labels.",
            icon="ℹ️",
        )

    if "clicked" not in st.session_state:
        st.session_state.clicked = None

    col_map, col_controls = st.columns([2, 1])

    with col_controls:
        st.subheader("Settings")
        mode_label = st.radio(
            "Period / source",
            ["Upcoming forecast", "Past date range", "Skymet live (nearest station)"],
            help=(
                "Forecast/Past date range use Open-Meteo (modeled). Skymet live uses the real "
                "PoCRA weather station nearest your clicked point - observed readings only, no "
                "forecast, and only where a station is nearby."
            ),
        )

        if mode_label == "Upcoming forecast":
            mode = "forecast"
            forecast_days = st.slider("Number of days ahead (incl. today)", 1, 16, 7)
            start_date = end_date = None
        elif mode_label == "Past date range":
            mode = "historical"
            default_start = date.today() - timedelta(days=7)
            d_range = st.date_input(
                "Date range",
                value=(default_start, date.today() - timedelta(days=1)),
                max_value=date.today() - timedelta(days=1),
            )
            if isinstance(d_range, tuple) and len(d_range) == 2:
                start_date, end_date = d_range
            else:
                start_date = end_date = d_range
            forecast_days = 7
        else:
            mode = "skymet_live"
            default_start = date.today() - timedelta(days=7)
            d_range = st.date_input(
                "Date range", value=(default_start, date.today()),
                max_value=date.today(), key="skymet_live_range",
            )
            if isinstance(d_range, tuple) and len(d_range) == 2:
                start_date, end_date = d_range
            else:
                start_date = end_date = d_range
            forecast_days = 7

        st.divider()
        if st.session_state.clicked:
            lat, lon = st.session_state.clicked
            st.write(f"**Selected point:** {lat:.4f}, {lon:.4f}")
            admin = core.find_admin_names(boundary_gdf, lat, lon)
            if admin.get("District"):
                st.write(f"**District:** {admin['District']}")
            if admin.get("Taluka"):
                st.write(f"**Taluka:** {admin['Taluka']}")
            if not admin:
                place = reverse_geocode(lat, lon)
                if place:
                    st.write(f"**Near:** {place}")
        else:
            st.info("Click a point on the map to select a location.")

    with col_map:
        st.session_state.setdefault("point_zoom", core.MAP_ZOOM)
        st.session_state.setdefault("point_center", core.MAP_CENTER)

        fmap = folium.Map(
            location=st.session_state.point_center,
            zoom_start=st.session_state.point_zoom,
            tiles="OpenStreetMap",
        )

        # Same zoom-adaptive boundary hierarchy as the Statewide map tab (thin
        # taluka -> medium district -> bold state), but with no fill colour at
        # all, so the base map (roads, place names) stays fully visible for
        # picking a location - this tab is about selecting a point, not
        # reading off district/taluka averages.
        zoom = st.session_state.point_zoom
        state_polygon = load_state_polygon()
        no_fill = {"fill": False, "fillOpacity": 0}

        if zoom >= ZOOM_TALUKA_MIN and boundary_gdf is not None:
            point_level = "Taluka"
        elif zoom >= ZOOM_DISTRICT_MIN:
            point_level = "District"
        else:
            point_level = "State"

        if point_level == "Taluka":
            name_cols = [
                c for c in boundary_gdf.columns
                if c.lower() != "geometry" and any(k in c.lower() for k in ("dist", "taluk", "tehsil", "tahsil"))
            ]
            tooltip_fields = list(name_cols)
            tooltip_aliases = list(name_cols)
            geo_data = boundary_gj

            # Add today's ETR as an extra hover row, looked up from the same
            # taluka averages the "Explore" tab computes (cached, so this is
            # near-instant after the first load).
            if "TEHSIL" in boundary_gdf.columns:
                try:
                    today_agg = compute_admin_aggregates("forecast", date.today().isoformat(), 0.3, 0.05)
                except Exception:
                    today_agg = None
                if today_agg and today_agg["taluka"] is not None:
                    etr_lookup = today_agg["taluka"][["District", "TEHSIL", "etr_avg"]]
                    tooltip_gdf = boundary_gdf.merge(etr_lookup, on=["District", "TEHSIL"], how="left")
                    tooltip_gdf["ETR_mm_day"] = tooltip_gdf["etr_avg"].round(2)
                    tooltip_fields.append("ETR_mm_day")
                    tooltip_aliases.append("ETR today (mm/day)")
                    geo_data = tooltip_gdf.to_json()

            tooltip = folium.GeoJsonTooltip(fields=tooltip_fields, aliases=tooltip_aliases) if tooltip_fields else None
            folium.GeoJson(
                geo_data,
                name="Talukas",
                style_function=lambda x: {"color": "#333333", "weight": 0.8, **no_fill},
                tooltip=tooltip,
            ).add_to(fmap)

            districts_gdf, _ = load_admin_layers()
            folium.GeoJson(
                districts_gdf.to_json(),
                name="Districts",
                style_function=lambda f: {"color": "#000000", "weight": 2.8, **no_fill},
            ).add_to(fmap)
            folium.GeoJson(
                mapping(state_polygon),
                name="Maharashtra",
                style_function=lambda x: {"color": "#000000", "weight": 4.0, **no_fill},
            ).add_to(fmap)
        elif point_level == "District":
            districts_gdf, _ = load_admin_layers()
            folium.GeoJson(
                districts_gdf.to_json(),
                name="Districts",
                style_function=lambda f: {"color": "#333333", "weight": 1.8, **no_fill},
                tooltip=folium.GeoJsonTooltip(fields=["District"], aliases=["District"]),
            ).add_to(fmap)
            folium.GeoJson(
                mapping(state_polygon),
                name="Maharashtra",
                style_function=lambda x: {"color": "#000000", "weight": 3.2, **no_fill},
            ).add_to(fmap)
        else:  # State
            folium.GeoJson(
                mapping(state_polygon),
                name="Maharashtra",
                style_function=lambda x: {"color": "#000000", "weight": 3.5, **no_fill},
            ).add_to(fmap)

        if st.session_state.clicked:
            folium.Marker(
                st.session_state.clicked,
                tooltip="Selected location",
                icon=folium.Icon(color="red"),
            ).add_to(fmap)

        map_state = st_folium(fmap, height=520, use_container_width=True, key="point_map")

        if map_state:
            if map_state.get("zoom") is not None:
                st.session_state.point_zoom = map_state["zoom"]
            if map_state.get("center"):
                c = map_state["center"]
                st.session_state.point_center = [c["lat"], c["lng"]] if isinstance(c, dict) else list(c)

        if map_state and map_state.get("last_clicked"):
            lat = map_state["last_clicked"]["lat"]
            lon = map_state["last_clicked"]["lng"]
            if boundary_polygon.contains(Point(lon, lat)):
                if st.session_state.clicked != (lat, lon):
                    st.session_state.clicked = (lat, lon)
                    st.rerun()
            else:
                st.warning("That point is outside Maharashtra. Please click inside the highlighted boundary.")

    st.divider()

    if st.session_state.clicked:
        lat, lon = st.session_state.clicked
        if mode == "skymet_live":
            try:
                with st.spinner("Finding nearest Skymet station and fetching its data..."):
                    station, nearest_info, daily_df = fetch_skymet_point_series(
                        lat, lon, start_date, end_date
                    )
                if station is None:
                    st.warning(
                        f"No Skymet station within 25 km of this point - nearest one is "
                        f"**{nearest_info['distance_km']:.1f} km** away. Try clicking closer to "
                        "a populated agricultural area, or use Open-Meteo instead."
                    )
                elif daily_df.empty:
                    st.error("The nearest station returned no data for this date range.")
                else:
                    st.caption(
                        f"Nearest station: **#{station['stationid']}**, "
                        f"{station['distance_km']:.1f} km from your clicked point "
                        f"({station['lat']:.4f}, {station['lon']:.4f})."
                    )
                    etr_df = core.compute_etr_table(daily_df, station["lat"])
                    st.subheader("Results")
                    st.dataframe(etr_df, use_container_width=True, hide_index=True)
                    st.line_chart(etr_df.set_index("Date")["ETR (mm/day)"])

                    avg_etr = etr_df["ETR (mm/day)"].mean()
                    st.metric("Average ETR over period (mm/day)", f"{avg_etr:.2f}")

                    csv = etr_df.to_csv(index=False).encode("utf-8")
                    st.download_button(
                        "Download results as CSV", csv, "etr_results_skymet_live.csv", "text/csv"
                    )
            except Exception as e:
                st.error(f"Could not fetch Skymet live data: {e}")
        else:
            try:
                with st.spinner("Fetching weather data..."):
                    weather_df = fetch_weather(
                        lat, lon, mode,
                        start_date=start_date, end_date=end_date,
                        forecast_days=forecast_days,
                    )
                if weather_df.empty:
                    st.error("No weather data returned for this location/period.")
                else:
                    etr_df = core.compute_etr_table(weather_df, lat)
                    st.subheader("Results")
                    st.dataframe(etr_df, use_container_width=True, hide_index=True)
                    st.line_chart(etr_df.set_index("Date")["ETR (mm/day)"])

                    avg_etr = etr_df["ETR (mm/day)"].mean()
                    st.metric("Average ETR over period (mm/day)", f"{avg_etr:.2f}")

                    csv = etr_df.to_csv(index=False).encode("utf-8")
                    st.download_button("Download results as CSV", csv, "etr_results.csv", "text/csv")
            except Exception as e:
                st.error(f"Could not fetch weather data: {e}")
    else:
        st.write("Select a location on the map above to see results here.")

    with st.expander("About the Hargreaves method"):
        st.markdown(
            """
            **ET0 = 0.0023 x (Tmean + 17.8) x sqrt(Tmax - Tmin) x Ra**

            - `Tmax`, `Tmin` - daily max/min air temperature (deg C), fetched from the
              [Open-Meteo](https://open-meteo.com) forecast/historical API for the clicked coordinates.
            - `Ra` - extraterrestrial radiation (mm/day equivalent), computed from the
              location's latitude and day of year using the standard FAO-56 astronomical formulas.
            - Hargreaves (1985) needs only temperature data, making it useful where full weather-station
              records (humidity, wind, sunshine hours) required by the FAO Penman-Monteith method are unavailable.
            """
        )

# ============================================================================
# TAB 2: adaptive drill-down map - state avg (zoomed out) -> district avg ->
# taluka avg (zoomed in), with click-to-see-exact-point ETR at any zoom.
# ============================================================================
with tab_state:
    st.subheader("Explore ETR: state → district → taluka → point")
    st.caption(
        "Zoom out for the statewide average ETR, zoom in for district then taluka "
        "averages. Click any location for its exact ETR - past, today, or forecast."
    )

    c1, c2 = st.columns(2)
    with c1:
        selected_date = st.date_input(
            "Date", value=date.today(),
            max_value=date.today() + timedelta(days=15),
            key="explore_date",
            help="Today or up to 15 days ahead uses the weather forecast; earlier dates "
                 "use the historical archive.",
        )
    with c2:
        station_spacing = st.select_slider(
            "Station grid spacing (deg)", options=[0.5, 0.4, 0.3, 0.25, 0.2], value=0.3,
            key="explore_spacing",
            help="Finer spacing = more sample points = slower first load, more faithful averages. "
                 "Cached per date, so later zoom/pan/click is instant.",
        )
    fine_resolution = station_spacing / 6
    explore_mode = "forecast" if selected_date >= date.today() else "historical"

    agg = None
    fetch_error = None
    try:
        with st.spinner(f"Computing ETR for {selected_date.isoformat()}..."):
            agg = compute_admin_aggregates(
                explore_mode, selected_date.isoformat(), station_spacing, fine_resolution
            )
    except Exception as e:
        fetch_error = str(e)

    if fetch_error:
        st.error(f"Could not fetch weather data for the statewide map: {fetch_error}")
    elif agg is None:
        st.error("Not enough weather data returned to build a map for this date. Try another date.")
    else:
        districts_gdf = agg["district"]
        talukas_gdf = agg["taluka"]
        state_avg = agg["state_avg"]
        vmin, vmax = grid.value_range(agg["grid_result"]["etr_grid"])
        colormap = grid.build_colormap(vmin, vmax)

        st.session_state.setdefault("explore_zoom", core.MAP_ZOOM)
        st.session_state.setdefault("explore_center", core.MAP_CENTER)
        st.session_state.setdefault("explore_clicked", None)
        st.session_state.setdefault("explore_clicked_etr", None)
        st.session_state.setdefault("explore_clicked_series", None)

        zoom = st.session_state.explore_zoom
        if zoom >= ZOOM_TALUKA_MIN and talukas_gdf is not None:
            level = "Taluka"
        elif zoom >= ZOOM_DISTRICT_MIN:
            level = "District"
        else:
            level = "State"

        fmap2 = folium.Map(
            location=st.session_state.explore_center,
            zoom_start=st.session_state.explore_zoom,
            tiles="OpenStreetMap",
        )

        if level == "State":
            state_polygon = load_state_polygon()
            state_color = colormap(state_avg)
            folium.GeoJson(
                mapping(state_polygon),
                style_function=lambda x, c=state_color: {
                    "fillColor": c, "color": "#333333", "weight": 1.5, "fillOpacity": 0.6,
                },
                tooltip=folium.Tooltip(f"Maharashtra — avg ETR: {state_avg:.2f} mm/day"),
            ).add_to(fmap2)
        elif level == "District":
            gdf = districts_gdf.copy()
            gdf["ETR_mm_day"] = gdf["etr_avg"].round(2)
            folium.GeoJson(
                gdf.to_json(),
                style_function=lambda f: {
                    "fillColor": colormap(f["properties"]["etr_avg"]),
                    "color": "#333333", "weight": 1, "fillOpacity": 0.6,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["District", "ETR_mm_day"], aliases=["District", "ETR (mm/day)"],
                ),
            ).add_to(fmap2)
            # Bold, unfilled state outline on top so it's clear where Maharashtra
            # itself ends among the district lines.
            folium.GeoJson(
                mapping(load_state_polygon()),
                style_function=lambda f: {
                    "color": "#000000", "weight": 3.2, "fill": False, "fillOpacity": 0,
                },
            ).add_to(fmap2)
        else:  # Taluka
            gdf = talukas_gdf.copy()
            gdf["ETR_mm_day"] = gdf["etr_avg"].round(2)
            folium.GeoJson(
                gdf.to_json(),
                style_function=lambda f: {
                    "fillColor": colormap(f["properties"]["etr_avg"]),
                    "color": "#333333", "weight": 0.7, "fillOpacity": 0.6,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["District", "TEHSIL", "ETR_mm_day"],
                    aliases=["District", "Taluka", "ETR (mm/day)"],
                ),
            ).add_to(fmap2)
            # Bold, unfilled district outline on top so it's clear where a
            # district ends among the much busier mesh of taluka lines.
            folium.GeoJson(
                districts_gdf.to_json(),
                style_function=lambda f: {
                    "color": "#000000", "weight": 2.8, "fill": False, "fillOpacity": 0,
                },
            ).add_to(fmap2)
            # Boldest of all: the state outline, on top of both taluka and
            # district lines, for a clear thin < medium < bold hierarchy.
            folium.GeoJson(
                mapping(load_state_polygon()),
                style_function=lambda f: {
                    "color": "#000000", "weight": 4.0, "fill": False, "fillOpacity": 0,
                },
            ).add_to(fmap2)

        colormap.caption = f"ETR (mm/day) — {selected_date.isoformat()} ({level} level)"
        colormap.add_to(fmap2)

        if st.session_state.explore_clicked:
            clat, clon = st.session_state.explore_clicked
            pt_etr = st.session_state.explore_clicked_etr
            folium.Marker(
                [clat, clon],
                tooltip=f"ETR: {pt_etr:.2f} mm/day" if pt_etr is not None else "Selected point",
                icon=folium.Icon(color="red"),
            ).add_to(fmap2)

        map_state2 = st_folium(fmap2, height=560, use_container_width=True, key="explore_map")

        if map_state2:
            if map_state2.get("zoom") is not None:
                st.session_state.explore_zoom = map_state2["zoom"]
            if map_state2.get("center"):
                c = map_state2["center"]
                st.session_state.explore_center = [c["lat"], c["lng"]] if isinstance(c, dict) else list(c)
            if map_state2.get("last_clicked"):
                lat = map_state2["last_clicked"]["lat"]
                lon = map_state2["last_clicked"]["lng"]
                state_polygon = load_state_polygon()
                if state_polygon.contains(Point(lon, lat)):
                    if st.session_state.explore_clicked != (lat, lon):
                        st.session_state.explore_clicked = (lat, lon)
                        try:
                            if explore_mode == "forecast":
                                pt_weather = fetch_weather(lat, lon, "forecast", forecast_days=16)
                            else:
                                pt_weather = fetch_weather(
                                    lat, lon, "historical",
                                    start_date=selected_date - timedelta(days=6),
                                    end_date=selected_date,
                                )
                            if pt_weather.empty:
                                raise ValueError("No weather data available for this location/date.")
                            pt_table = core.compute_etr_table(pt_weather, lat)
                            st.session_state.explore_clicked_series = pt_table
                            row = pt_table[pt_table["Date"] == selected_date]
                            st.session_state.explore_clicked_etr = (
                                float(row.iloc[0]["ETR (mm/day)"]) if not row.empty
                                else float(pt_table.iloc[0]["ETR (mm/day)"])
                            )
                        except Exception as e:
                            st.session_state.explore_clicked_etr = None
                            st.session_state.explore_clicked_series = None
                            st.warning(f"Could not fetch weather for that point: {e}")
                        st.rerun()
                else:
                    st.warning("That point is outside Maharashtra.")

        st.caption(f"Currently showing: **{level} level** (zoom {zoom}). Pan/zoom to drill down or back out.")

        m1, m2 = st.columns(2)
        m1.metric("State average ETR (mm/day)", f"{state_avg:.2f}")
        if st.session_state.explore_clicked and st.session_state.explore_clicked_etr is not None:
            clat, clon = st.session_state.explore_clicked
            m2.metric(f"Point ETR on {selected_date.isoformat()}", f"{st.session_state.explore_clicked_etr:.2f} mm/day")

        if st.session_state.explore_clicked_series is not None:
            clat, clon = st.session_state.explore_clicked
            with st.expander(f"ETR at clicked point ({clat:.4f}, {clon:.4f})", expanded=True):
                series = st.session_state.explore_clicked_series
                st.dataframe(series, use_container_width=True, hide_index=True)
                st.line_chart(series.set_index("Date")["ETR (mm/day)"])
                st.download_button(
                    "Download this point's ETR (CSV)",
                    series.to_csv(index=False).encode("utf-8"),
                    f"etr_point_{clat:.4f}_{clon:.4f}.csv", "text/csv",
                    key="dl_point_csv",
                )

        st.divider()
        st.subheader("Download admin-level averages")
        if level == "State":
            st.caption("State level is a single value — zoom in (district or taluka) for a downloadable table.")
        elif level == "District":
            table = districts_gdf[["District", "etr_avg", "n_samples"]].rename(
                columns={"etr_avg": "ETR (mm/day)", "n_samples": "Grid samples"}
            ).round(2)
            st.download_button(
                "Download district-level ETR (CSV)",
                table.to_csv(index=False).encode("utf-8"),
                f"maharashtra_etr_district_{selected_date.isoformat()}.csv", "text/csv",
                key="dl_district_csv",
            )
        else:
            table = talukas_gdf[["District", "TEHSIL", "etr_avg", "n_samples"]].rename(
                columns={"TEHSIL": "Taluka", "etr_avg": "ETR (mm/day)", "n_samples": "Grid samples"}
            ).round(2)
            st.download_button(
                "Download taluka-level ETR (CSV)",
                table.to_csv(index=False).encode("utf-8"),
                f"maharashtra_etr_taluka_{selected_date.isoformat()}.csv", "text/csv",
                key="dl_taluka_csv",
            )

# ============================================================================
# TAB 3: historical ETR archive (1951-2025), built from IMD's gridded daily
# temperature dataset - download by state / district / taluka, only ever
# offering the date range actually available for that specific selection.
# ============================================================================
with tab_history:
    st.subheader("Historical ETR archive")

    source_label = st.radio(
        "Data source", ["IMD (1951-2025)", "Skymet / PoCRA (2022-2026)"],
        horizontal=True, key="hist_source",
    )

    if source_label.startswith("IMD"):
        source_mod = hist
        state_loader, district_loader, taluka_loader, avail_loader = (
            load_history_state, load_history_district, load_history_taluka, load_history_availability,
        )
        st.caption(
            "Built from IMD's gridded daily temperature dataset (39 grid points covering "
            "Maharashtra) via the Hargreaves method, aggregated to state/district/taluka level."
        )
        build_hint = "Run `python etr_historical.py` once (reads NetCDF files in `temperature/`)."
        source_tag = "IMD"
    else:
        source_mod = skymet
        state_loader, district_loader, taluka_loader, avail_loader = (
            load_skymet_state, load_skymet_district, load_skymet_taluka, load_skymet_availability,
        )
        st.caption(
            "Built from PoCRA's automatic weather station network (run by Skymet) via the "
            "Hargreaves method. Denser than IMD (1,000-2,300 real stations, growing over time) "
            "but less consistent - stations come and go, and there's a network-wide gap from "
            "Nov 2022 to mid-2023. Missing dates show as **NA**, not an estimate."
        )
        build_hint = "Run `python etr_skymet.py` once (reads NetCDF files in `D:\\VIP\\0.Data\\rain\\0_NetCDF_Data\\PoCRA\\`)."
        source_tag = "Skymet"

    if not source_mod.base_files_exist():
        st.error(f"{source_tag} base files haven't been built yet. {build_hint}")
    else:
        state_hist = state_loader()
        district_hist = district_loader()
        taluka_hist = taluka_loader()
        avail_df = avail_loader()

        level = st.radio("Level", ["State", "District", "Taluka"], horizontal=True, key="hist_level")

        district_name = None
        taluka_name = None
        if level == "District":
            district_name = st.selectbox(
                "District", hist.get_district_list(district_hist), key=f"hist_district_{source_tag}"
            )
        elif level == "Taluka":
            district_name = st.selectbox(
                "District", hist.get_district_list(district_hist), key=f"hist_district_for_taluka_{source_tag}"
            )
            taluka_name = st.selectbox(
                "Taluka", hist.get_taluka_list(taluka_hist, district_name), key=f"hist_taluka_{source_tag}"
            )

        avail_row = hist.get_availability_row(avail_df, level, district_name, taluka_name)

        if avail_row is None or pd.isna(avail_row["start"]):
            st.warning(
                f"No {source_tag} data is available for this selection at all "
                "(e.g. Skymet/PoCRA has no agricultural weather stations in Mumbai City/Suburban)."
            )
        else:
            avail_start = avail_row["start"].date()
            avail_end = avail_row["end"].date()
            st.info(
                f"Data available from **{avail_start}** to **{avail_end}** for this selection "
                + ("." if source_tag == "IMD" else "(gaps within this range are possible - shown as NA below).")
            )

            c1, c2 = st.columns(2)
            with c1:
                dl_start = st.date_input(
                    "From", value=avail_start, min_value=avail_start, max_value=avail_end,
                    key=f"hist_start_{source_tag}",
                )
            with c2:
                dl_end = st.date_input(
                    "To", value=avail_end, min_value=avail_start, max_value=avail_end,
                    key=f"hist_end_{source_tag}",
                )

            if dl_start > dl_end:
                st.error("Start date must be before end date.")
            else:
                series = hist.get_unit_series(
                    level, state_hist, district_hist, taluka_hist, district_name, taluka_name
                )
                mask = (series["Date"] >= pd.Timestamp(dl_start)) & (series["Date"] <= pd.Timestamp(dl_end))
                series_range = series.loc[mask].sort_values("Date")
                valid = series_range["ETR_mm_day"].dropna()

                st.line_chart(series_range.set_index("Date")["ETR_mm_day"])

                m1, m2, m3 = st.columns(3)
                m1.metric("Days with data / in range", f"{len(valid)} / {len(series_range)}")
                if len(valid):
                    m2.metric("Average ETR (mm/day)", f"{valid.mean():.2f}")
                    m3.metric("Range (mm/day)", f"{valid.min():.2f} - {valid.max():.2f}")
                else:
                    m2.metric("Average ETR (mm/day)", "NA")
                    m3.metric("Range (mm/day)", "NA")

                unit_label = {"State": "Maharashtra", "District": district_name, "Taluka": taluka_name}[level]
                out = series_range.rename(columns={"ETR_mm_day": "ETR (mm/day)"}).copy()
                out["Date"] = out["Date"].dt.strftime("%Y-%m-%d")
                out["ETR (mm/day)"] = out["ETR (mm/day)"].map(
                    lambda x: "NA" if pd.isna(x) else f"{x:.2f}"
                )
                st.dataframe(out, use_container_width=True, hide_index=True)

                csv = out.to_csv(index=False).encode("utf-8")
                safe_name = str(unit_label).replace(" ", "_")
                st.download_button(
                    f"Download {level} {source_tag} ETR CSV ({dl_start} to {dl_end})",
                    csv,
                    f"etr_{source_tag}_{safe_name}_{dl_start}_{dl_end}.csv",
                    "text/csv",
                    key=f"hist_download_{source_tag}",
                )
