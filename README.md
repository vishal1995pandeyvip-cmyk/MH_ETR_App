# Maharashtra ETR Calculator (Hargreaves Method)

Click any point on Maharashtra to get its reference evapotranspiration (ETR / ET0),
computed with the Hargreaves (1985) temperature-based method, for a chosen historical
date range or for the upcoming weather forecast.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

Then open the URL it prints (usually http://localhost:8501).

## District/taluka boundary

The app currently falls back to a plain Maharashtra state outline
(`data/maharashtra.geojson`). To get district/taluka boundaries and labels instead,
drop your Shapefile's files (`.shp`, `.shx`, `.dbf`, `.prj`, etc. — all with the same
base filename) into `data/boundary/` and rerun the app. It will automatically:

- draw those boundaries on the map instead of the state outline,
- restrict/validate clicks to inside Maharashtra using the union of all features,
- label the selected point with its District/Taluka (matched from any column whose
  name contains "dist", "taluk", "tehsil", or "tahsil").

## How ETR is computed

```
ET0 = 0.0023 x (Tmean + 17.8) x sqrt(Tmax - Tmin) x Ra
```

- `Tmax`/`Tmin` — fetched for the clicked lat/lon from the free [Open-Meteo](https://open-meteo.com)
  API (forecast endpoint for upcoming days, archive endpoint for past dates).
- `Ra` (extraterrestrial radiation) — computed purely from latitude and day-of-year
  using the standard FAO-56 astronomical formulas (no external data needed).

## Deploy for free (shareable link)

1. Push this folder to a GitHub repo (public or private).
2. Go to [share.streamlit.io](https://share.streamlit.io), sign in, click "New app".
3. Point it at the repo/branch and `app.py`.
4. Deploy — you get a public `*.streamlit.app` URL anyone can open, no install needed.

## Files

- `app.py` — the Streamlit app (map, weather fetch, Hargreaves calculation, UI).
- `requirements.txt` — Python dependencies.
- `data/maharashtra.geojson` — fallback state boundary (simplified for fast rendering).
- `data/boundary/` — drop your district/taluka Shapefile here (see above).
