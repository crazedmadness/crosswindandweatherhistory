import math
import html
import pandas as pd
import requests
import streamlit as st
import pydeck as pdk
import altair as alt
import streamlit.components.v1 as components
from zoneinfo import ZoneInfo
from pathlib import Path

try:
    from streamlit_js_eval import streamlit_js_eval
except Exception:
    streamlit_js_eval = None

BATCH_SIZE = 250

CLOSED_RUNWAYS = {
    "KABQ": {"17", "35"},
}

st.set_page_config(page_title="Crosswind and Weather History", layout="wide")

if "selected_icao" not in st.session_state:
    st.session_state.selected_icao = None

if "selected_airport_result" not in st.session_state:
    st.session_state.selected_airport_result = None

# Allow mobile HTML cards to select/expand an airport via query string.
try:
    qp_selected = st.query_params.get("selected_icao", None)
    if isinstance(qp_selected, list):
        qp_selected = qp_selected[0] if qp_selected else None
    if qp_selected:
        st.session_state.selected_icao = str(qp_selected).upper().strip()
except Exception:
    pass


def render_raw_html(markup, height=96):
    """Render small HTML UI blocks without showing raw tags in older Streamlit versions."""
    try:
        if hasattr(st, "html"):
            st.html(markup)
        else:
            components.html(markup, height=height, scrolling=False)
    except Exception:
        components.html(markup, height=height, scrolling=False)


def get_screen_width():
    """Return browser viewport width when streamlit-js-eval is installed; otherwise None."""
    if streamlit_js_eval is None:
        return None
    try:
        return streamlit_js_eval(js_expressions="window.innerWidth", key="screen_width")
    except Exception:
        return None


screen_width = get_screen_width()

# Responsive breakpoints are based on viewport width, not device name.
# Phone: compact stacked layout.
# Tablet: compact UI; stacked in portrait/narrow widths, wide in landscape.
# Desktop/unknown: full wide dashboard.
is_phone = screen_width is not None and screen_width < 760
is_tablet = screen_width is not None and 760 <= screen_width < 1180
is_desktop = screen_width is None or screen_width >= 1180
preferred_layout_mode = "Stacked" if (screen_width is not None and screen_width < 980) else "Wide"

WIDE_HEADER_BASENAME = "19c28a51-1047-4e6d-8888-da9a8ebd1d88"
MOBILE_HEADER_BASENAME = "4cb39212-8ac7-44db-a2ec-07f7cff0ff5e"


def find_header_file(base):
    """Find a banner image even if the extension differs."""
    candidates = [
        base,
        f"{base}.png",
        f"{base}.jpg",
        f"{base}.jpeg",
        f"{base}.webp",
    ]

    for candidate in candidates:
        if Path(candidate).exists():
            return candidate

    return None


def get_header_image_path_for_screen():
    """Use wide banner on desktop / iPad landscape and mobile banner on phones / narrow tablets."""
    if is_desktop or (is_tablet and screen_width is not None and screen_width >= 980):
        return find_header_file(WIDE_HEADER_BASENAME)

    return find_header_file(MOBILE_HEADER_BASENAME)


def render_header_image():
    """Render the responsive logo/banner at the top."""
    image_path = get_header_image_path_for_screen()

    st.markdown("""
    <style>
        .block-container { padding-top: 0.25rem !important; }

        div[data-testid="stImage"] {
            margin-bottom: 0.1rem;
        }

        /* Desktop + iPad landscape: very thin widescreen banner */
        div[data-testid="stImage"] img {
            width: 100%;
            height: 75px;
            object-fit: cover;
            object-position: center 40%;
            border-radius: 10px;
        }

        /* Tablet / iPad portrait */
        @media (max-width: 1180px) {
            div[data-testid="stImage"] img {
                height: 85px;
            }
        }

        /* Phone: use taller mobile banner without cropping */
        @media (max-width: 760px) {
            div[data-testid="stImage"] img {
                height: 105px;
                object-fit: contain;
                object-position: center center;
                border-radius: 7px;
            }
        }
    </style>
    """, unsafe_allow_html=True)

    if image_path:
        st.image(image_path, use_container_width=True)
    else:
        st.warning(
            "Header image not found. Add the wide and mobile banner PNG files to the same folder as app.py."
        )


def get_color(cw):
    if cw is None or pd.isna(cw):
        return ("#777777", [120, 120, 120, 180])

    if cw > 30:
        return ("#760299", [118, 2, 153, 245])   # purple (extreme)
    elif cw > 20:
        return ("#ff4d6d", [255, 77, 109, 230])  # red
    elif cw > 15:
        return ("#ff9f1c", [255, 159, 28, 230])  # orange
    else:
        return ("#c9b458", [201, 180, 88, 210])  # yellow


def calc_crosswind(wind_dir, wind_speed, runway_heading):
    angle = abs(wind_dir - runway_heading)
    if angle > 180:
        angle = 360 - angle
    return abs(wind_speed * math.sin(math.radians(angle)))


def offset_point(lat, lon, heading_deg, distance_nm):
    heading = math.radians(heading_deg)
    d_lat = math.cos(heading) * distance_nm / 60
    d_lon = math.sin(heading) * distance_nm / (60 * max(math.cos(math.radians(lat)), 0.01))
    return lat + d_lat, lon + d_lon


def arrowhead_points(tip_lat, tip_lon, direction_to_deg, size_nm=2.2, spread_deg=28):
    back_heading = (direction_to_deg + 180) % 360
    left_lat, left_lon = offset_point(tip_lat, tip_lon, (back_heading - spread_deg) % 360, size_nm)
    right_lat, right_lon = offset_point(tip_lat, tip_lon, (back_heading + spread_deg) % 360, size_nm)
    return left_lat, left_lon, right_lat, right_lon


def chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def parse_metar_time(value):
    """Return a timezone-aware UTC Timestamp from AviationWeather obsTime values."""
    if value in (None, ""):
        return pd.NaT

    # AviationWeather can return ISO strings, epoch seconds, or epoch milliseconds.
    if isinstance(value, (int, float)) and not pd.isna(value):
        unit = "ms" if value > 10_000_000_000 else "s"
        return pd.to_datetime(value, unit=unit, utc=True, errors="coerce")

    text = str(value).strip()
    if text.isdigit():
        number = int(text)
        unit = "ms" if number > 10_000_000_000 else "s"
        return pd.to_datetime(number, unit=unit, utc=True, errors="coerce")

    return pd.to_datetime(text, utc=True, errors="coerce")


def get_viewer_timezone(default="America/Los_Angeles"):
    """Best effort: use browser timezone from Streamlit when available, otherwise fall back to Pacific time."""
    try:
        tz = getattr(st.context, "timezone", None)
        if tz:
            return str(tz)
    except Exception:
        pass

    return default


def format_user_local_time(time_series, timezone_name):
    """Convert UTC observation times to the viewer/browser timezone."""
    try:
        local_times = time_series.dt.tz_convert(ZoneInfo(timezone_name))
    except Exception:
        local_times = time_series.dt.tz_convert(ZoneInfo("America/Los_Angeles"))

    return local_times.dt.strftime("%H:%M")


def format_timestamp_display(ts, timezone_name):
    """Format one timestamp as UTC plus viewer/browser local time."""
    if ts is None or pd.isna(ts):
        return "—"

    timestamp = pd.Timestamp(ts)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    try:
        local = timestamp.tz_convert(ZoneInfo(timezone_name))
    except Exception:
        local = timestamp.tz_convert(ZoneInfo("America/Los_Angeles"))

    return f"{timestamp.strftime('%H:%MZ')} ({local.strftime('%H:%M %Z')})"


def get_metar_observation_time(m):
    obs_raw = m.get("obsTime") or m.get("reportTime") or m.get("receiptTime") or m.get("issueTime")
    return parse_metar_time(obs_raw)


def format_last_updated(timezone_name):
    now_utc = pd.Timestamp.now(tz="UTC")
    return format_timestamp_display(now_utc, timezone_name)


def format_visibility(m):
    """Return METAR visibility as a compact string when available."""
    value = m.get("visib") or m.get("vis") or m.get("visibility")
    if value in (None, "") or pd.isna(value):
        raw = str(m.get("rawOb", ""))
        parts = raw.split()
        for part in parts:
            if part.endswith("SM"):
                return part
        return "—"

    text = str(value).strip()
    if text.endswith("SM"):
        return text
    return f"{text} SM"


def format_ceiling(m):
    """Return the lowest BKN/OVC/VV layer as ceiling. FEW/SCT are not ceilings."""
    ceiling_covers = {"BKN", "OVC", "VV"}
    candidates = []

    clouds = m.get("clouds") or []
    if isinstance(clouds, list):
        for cloud in clouds:
            if not isinstance(cloud, dict):
                continue
            cover = str(cloud.get("cover", "")).upper().strip()
            if cover not in ceiling_covers:
                continue

            base = cloud.get("base") or cloud.get("base_feet_agl") or cloud.get("baseFt")
            if base in (None, "") or pd.isna(base):
                if cover == "VV":
                    candidates.append((0, "VV"))
                continue

            try:
                base_ft = int(float(base))
                candidates.append((base_ft, f"{cover}{base_ft:03d}" if base_ft < 1000 else f"{cover}{base_ft}"))
            except Exception:
                pass

    # Fallback to raw METAR tokens like BKN025 / OVC007 / VV002.
    raw = str(m.get("rawOb", ""))
    for part in raw.split():
        token = part.strip().upper()
        if len(token) >= 6 and token[:3] in ceiling_covers:
            height = token[3:6]
            if height.isdigit():
                base_ft = int(height) * 100
                candidates.append((base_ft, token[:6]))

    if not candidates:
        return "—"

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]



def ceiling_to_feet(value):
    """Convert a ceiling label like BKN025 / OVC007 / VV002 to feet AGL.

    Returns None for no ceiling. FEW/SCT are intentionally excluded upstream.
    """
    if value in (None, "", "—") or pd.isna(value):
        return None

    text = str(value).strip().upper()
    if text == "VV":
        return 0

    if len(text) >= 6 and text[:3] in {"BKN", "OVC", "VV"}:
        height_token = text[3:6]
        if height_token.isdigit():
            return int(height_token) * 100

    # Handles already-expanded labels like BKN2500, if one slips through.
    if len(text) > 6 and text[:3] in {"BKN", "OVC", "VV"}:
        numeric = "".join(ch for ch in text[3:] if ch.isdigit())
        if numeric:
            return int(numeric)

    return None


def safe_number(value):
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def render_weather_history_charts(hist, airport_elevation_ft=None, side_by_side=True, phone=False):
    """Render crosswind and ceiling charts. Desktop/tablet can use side-by-side; phone stacks them."""
    chart_hist = hist.sort_values("Time").copy()
    chart_height = 155 if phone else 205 if is_tablet else 220

    if side_by_side:
        left, right = st.columns(2)
    else:
        left = st.container()
        right = st.container()

    with left:
        xwind_df = chart_hist[["Time", "Xwind"]].dropna().copy()
        if xwind_df.empty:
            st.write("No crosswind data available for chart.")
        else:
            xwind_chart = (
                alt.Chart(xwind_df)
                .mark_line(point=True)
                .encode(
                    x=alt.X("Time:T", title="Time"),
                    y=alt.Y("Xwind:Q", title="Crosswind (kt)"),
                    tooltip=[
                        alt.Tooltip("Time:T", title="Time"),
                        alt.Tooltip("Xwind:Q", title="Crosswind", format=".1f"),
                    ],
                )
                .properties(height=chart_height)
            )
            st.altair_chart(xwind_chart, use_container_width=True)

    with right:
        ceiling_df = chart_hist[["Time", "Ceiling Ft MSL", "Ceiling Ft AGL"]].dropna(subset=["Ceiling Ft MSL"]).copy()
        elev = safe_number(airport_elevation_ft)

        if ceiling_df.empty:
            st.write("No ceiling data available for chart.")
            return

        max_ceiling = ceiling_df["Ceiling Ft MSL"].max()
        max_y = max(max_ceiling, elev or 0, 3500) + 500

        ceiling_line = (
            alt.Chart(ceiling_df)
            .mark_line(point=True)
            .encode(
                x=alt.X("Time:T", title="Time"),
                y=alt.Y(
                    "Ceiling Ft MSL:Q",
                    title="Ceiling / elevation (ft MSL)",
                    scale=alt.Scale(domain=[0, max_y]),
                ),
                tooltip=[
                    alt.Tooltip("Time:T", title="Time"),
                    alt.Tooltip("Ceiling Ft AGL:Q", title="Ceiling AGL", format=".0f"),
                    alt.Tooltip("Ceiling Ft MSL:Q", title="Ceiling MSL", format=".0f"),
                ],
            )
        )

        layers = [ceiling_line]

        if elev is not None:
            elev_df = pd.DataFrame({"Elevation": [elev]})
            elev_line = (
                alt.Chart(elev_df)
                .mark_rule(color="#e6d3a3", strokeDash=[6, 4], size=2)
                .encode(
                    y="Elevation:Q",
                    tooltip=[alt.Tooltip("Elevation:Q", title="Airport elevation", format=".0f")],
                )
            )
            layers.append(elev_line)

        ceiling_chart = alt.layer(*layers).properties(height=chart_height)

        if phone:
            st.altair_chart(ceiling_chart, use_container_width=True)
            st.markdown(
                """
                <div style="color:#e6d3a3; font-size:10px; font-weight:800; margin-top:-8px; margin-bottom:4px;">
                    <span style="letter-spacing:1px;">- - -</span> airport elev
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            chart_col, legend_col = st.columns([0.88, 0.12], gap="small")
            with chart_col:
                st.altair_chart(ceiling_chart, use_container_width=True)
            with legend_col:
                st.markdown(
                    """
                    <div style="height:205px; display:flex; align-items:center; justify-content:flex-start;">
                        <div style="color:#e6d3a3; font-size:12px; font-weight:800; white-space:nowrap;">
                            <span style="letter-spacing:1px;">- - -</span> airport elev
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

def parse_wind_from_metar(m, allow_vrb=False):
    """Return wind_dir, wind_spd, wind_gust, vrb flag. Returns None if unusable."""
    if not m or "wdir" not in m or "wspd" not in m:
        return None

    wdir = m.get("wdir")
    if isinstance(wdir, str):
        if wdir.upper() == "VRB":
            if allow_vrb:
                return None, float(m.get("wspd", 0)), m.get("wgst"), True
            return None
        try:
            wind_dir = float(wdir)
        except Exception:
            return None
    else:
        try:
            wind_dir = float(wdir)
        except Exception:
            return None

    try:
        wind_spd = float(m.get("wspd"))
    except Exception:
        return None

    return wind_dir, wind_spd, m.get("wgst"), False


@st.cache_data
def load_data():
    airports = pd.read_csv("airports.csv")
    runways = pd.read_csv("runways.csv")

    airports["ident"] = airports["ident"].astype(str)

    airports = airports[
        (airports["type"].isin(["large_airport", "medium_airport"]))
        & (airports["scheduled_service"] == "yes")
        & (airports["ident"].str.len() == 4)
    ].copy()

    merged = pd.merge(runways, airports, left_on="airport_ident", right_on="ident")
    runway_ends = preprocess_runway_ends(merged)
    runway_ends_by_icao = {icao: df for icao, df in runway_ends.groupby("icao", sort=False)}
    airport_lookup = {row.ident: row._asdict() for row in airports.itertuples(index=False)}

    return airports, runway_ends, runway_ends_by_icao, airport_lookup


def preprocess_runway_ends(runway_data):
    """Flatten runway records so each physical runway end is one searchable row.

    This avoids repeatedly looping through LE/HE ends inside every airport scan.
    """
    rows = []

    for r in runway_data.itertuples(index=False):
        icao = getattr(r, "airport_ident", None)
        if not icao or pd.isna(getattr(r, "length_ft", None)):
            continue

        runway_length = int(float(getattr(r, "length_ft")))

        for side in ("le", "he"):
            name = getattr(r, f"{side}_ident", None)
            hdg = getattr(r, f"{side}_heading_degT", None)

            if pd.isna(name) or pd.isna(hdg):
                continue

            if icao in CLOSED_RUNWAYS and str(name) in CLOSED_RUNWAYS[icao]:
                continue

            rows.append({
                "icao": icao,
                "runway": str(name),
                "heading": float(hdg),
                "length": runway_length,
                "name": getattr(r, "name", "—"),
                "city": getattr(r, "municipality", "—"),
                "country": getattr(r, "iso_country", "—"),
                "region": getattr(r, "iso_region", "—"),
                "elevation": getattr(r, "elevation_ft", None),
                "lat": getattr(r, "latitude_deg", None),
                "lon": getattr(r, "longitude_deg", None),
            })

    return pd.DataFrame(rows)


def get_runway_ends_for_airport(runway_ends_by_icao, icao, min_len=0):
    airport_rw = runway_ends_by_icao.get(icao)
    if airport_rw is None or airport_rw.empty:
        return pd.DataFrame()
    if min_len <= 0:
        return airport_rw
    return airport_rw[airport_rw["length"] >= min_len]


def find_best_crosswind_runway(icao, runway_ends_by_icao, wind_dir, wind_spd, wind_gust=None, min_len=0):
    """Find the runway end with the highest crosswind component for this wind."""
    airport_rw = get_runway_ends_for_airport(runway_ends_by_icao, icao, min_len=min_len)
    if airport_rw.empty:
        return None

    best = None

    # itertuples is much lighter than iterrows for repeated scans.
    for r in airport_rw.itertuples(index=False):
        cw = calc_crosswind(wind_dir, wind_spd, r.heading)

        gust_cw = None
        if wind_gust is not None and not pd.isna(wind_gust):
            gust_cw = calc_crosswind(wind_dir, float(wind_gust), r.heading)

        if not best or cw > best["cw"]:
            best = {
                "icao": icao,
                "name": r.name,
                "city": r.city,
                "country": r.country,
                "region": r.region,
                "elevation": r.elevation,
                "lat": r.lat,
                "lon": r.lon,
                "runway": r.runway,
                "heading": round(float(r.heading)),
                "length": int(r.length),
                "cw": round(cw, 1),
                "gust_cw": round(gust_cw, 1) if gust_cw is not None else None,
            }

    return best


@st.cache_data(ttl=300)
def get_metars(icaos_tuple):
    icaos = list(icaos_tuple)
    results = {}

    for batch in chunks(icaos, BATCH_SIZE):
        url = f"https://aviationweather.gov/api/data/metar?ids={','.join(batch)}&format=json"

        try:
            response = requests.get(url, timeout=20)
            response.raise_for_status()
            data = response.json()

            for m in data:
                if "icaoId" in m:
                    results[m["icaoId"]] = m

        except Exception:
            pass

    return results


@st.cache_data(ttl=300)
def get_metar_history(icao, hours=24):
    icao = icao.upper().strip()
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours={hours}"

    try:
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception:
        return []


def make_base_airport_result(icao, airport_lookup):
    airport = airport_lookup.get(icao)
    if airport is None:
        return None

    return {
        "icao": icao,
        "name": airport.get("name", "—"),
        "city": airport.get("municipality", "—"),
        "country": airport.get("iso_country", "—"),
        "region": airport.get("iso_region", "—"),
        "elevation": airport.get("elevation_ft"),
        "lat": airport.get("latitude_deg"),
        "lon": airport.get("longitude_deg"),
        "wind": "—",
        "gust": None,
        "wind_dir": None,
        "wind_speed": None,
        "runway": "—",
        "heading": "—",
        "length": "—",
        "cw": None,
        "gust_cw": None,
        "raw_metar": "No METAR available",
    }


def build_single_airport_result(icao, airport_lookup, runway_ends_by_icao, min_len=0):
    icao = icao.upper().strip()

    base = make_base_airport_result(icao, airport_lookup)
    if base is None:
        return None, "Airport not found in airport database."

    metars = get_metars((icao,))
    m = metars.get(icao)

    if not m:
        return base, None

    base["raw_metar"] = m.get("rawOb", "METAR available, raw text not provided")
    base["visibility"] = format_visibility(m)
    base["ceiling"] = format_ceiling(m)

    wind = parse_wind_from_metar(m, allow_vrb=True)
    if wind is None:
        return base, None

    wind_dir, wind_spd, wind_gust, is_vrb = wind
    if is_vrb:
        base["wind"] = f"VRB/{m.get('wspd', '—')}"
        return base, None

    base["wind"] = f"{int(wind_dir):03}/{int(wind_spd)}"
    base["gust"] = int(float(wind_gust)) if wind_gust is not None and not pd.isna(wind_gust) else None
    base["wind_dir"] = round(wind_dir)
    base["wind_speed"] = round(wind_spd)

    best = find_best_crosswind_runway(
        icao,
        runway_ends_by_icao,
        wind_dir,
        wind_spd,
        wind_gust=wind_gust,
        min_len=min_len,
    )

    if not best:
        return base, None

    return {**base, **best}, None


def build_results(airports, runway_ends_by_icao, min_wind, min_len):
    icaos = tuple(sorted(airports["ident"].dropna().unique()))
    metars = get_metars(icaos)

    out = []

    for icao in icaos:
        wind = parse_wind_from_metar(metars.get(icao), allow_vrb=False)
        if wind is None:
            continue

        wind_dir, wind_spd, wind_gust, _ = wind

        if wind_spd < min_wind:
            continue

        best = find_best_crosswind_runway(
            icao,
            runway_ends_by_icao,
            wind_dir,
            wind_spd,
            wind_gust=wind_gust,
            min_len=min_len,
        )

        if best:
            best.update({
                "wind": f"{int(wind_dir):03}/{int(wind_spd)}",
                "gust": int(float(wind_gust)) if wind_gust is not None and not pd.isna(wind_gust) else None,
                "wind_dir": round(wind_dir),
                "wind_speed": round(wind_spd),
                "raw_metar": metars.get(icao, {}).get("rawOb", "—"),
                "visibility": format_visibility(metars.get(icao, {})),
                "ceiling": format_ceiling(metars.get(icao, {})),
            })
            out.append(best)

    return sorted(out, key=lambda x: x["cw"], reverse=True)


def build_history_table(icao, runway_ends_by_icao, min_len, hours=24, timezone_name="America/Los_Angeles"):
    history = get_metar_history(icao, hours=hours)
    if not history:
        return pd.DataFrame()

    rows = []

    for m in history:
        wind = parse_wind_from_metar(m, allow_vrb=False)
        if wind is None:
            continue

        wind_dir, wind_spd, wind_gust, _ = wind

        obs_time = get_metar_observation_time(m)
        if pd.isna(obs_time):
            continue

        best = find_best_crosswind_runway(
            icao,
            runway_ends_by_icao,
            wind_dir,
            wind_spd,
            wind_gust=wind_gust,
            min_len=min_len,
        )

        if best:
            rows.append({
                "Time": obs_time,
                "Wind Speed": round(wind_spd, 1),
                "Wind": f"{int(wind_dir):03}/{int(wind_spd)}",
                "Gust": f"G{int(float(wind_gust))}" if wind_gust is not None and not pd.isna(wind_gust) else "",
                "Visibility": format_visibility(m),
                "Ceiling": format_ceiling(m),
                "Ceiling Ft AGL": ceiling_to_feet(format_ceiling(m)),
                "Elevation": best.get("elevation"),
                "Raw METAR": m.get("rawOb", "—"),
                "RWY": str(best["runway"]),
                "Heading": best["heading"],
                "Length": best["length"],
                "Xwind": best["cw"],
                "Gust Xwind": best["gust_cw"],
            })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df = df.dropna(subset=["Time"]).sort_values("Time", ascending=False)

    now_utc = pd.Timestamp.now(tz="UTC")

    def format_relative(ts):
        if ts is None or pd.isna(ts):
            return ""

        timestamp = pd.Timestamp(ts)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")

        delta = now_utc - timestamp
        minutes = max(0, int(delta.total_seconds() / 60))

        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{minutes}m ago"
        if minutes < 1440:
            hours_ago = minutes // 60
            return f"{hours_ago}h ago"

        days_ago = minutes // 1440
        return f"{days_ago}d ago"

    df["UTC"] = df["Time"].dt.strftime("%H:%MZ")
    df["Local"] = format_user_local_time(df["Time"], timezone_name)
    tz_short = pd.Timestamp.now(tz=ZoneInfo(timezone_name)).strftime("%Z")
    df["Relative"] = df["Time"].apply(format_relative)

    df["Time Display"] = (
        df["UTC"]
        + " (" + df["Local"] + " " + tz_short + ")"
        + " (" + df["Relative"] + ")"
    )

    df["Wind"] = df["Wind"] + df["Gust"]

    df["Elevation"] = pd.to_numeric(df.get("Elevation"), errors="coerce")
    df["Ceiling Ft AGL"] = pd.to_numeric(df.get("Ceiling Ft AGL"), errors="coerce")
    df["Ceiling Ft MSL"] = df["Ceiling Ft AGL"] + df["Elevation"]
    return df


def render_history_panel(icao, runway_ends_by_icao, min_len, title="Past 24 Hours", phone=False, side_by_side_charts=True):
    with st.expander(f"{title} — {icao}", expanded=False):
        timezone_name = get_viewer_timezone()
        hist = build_history_table(icao, runway_ends_by_icao, min_len, hours=24, timezone_name=timezone_name)

        if hist.empty:
            st.write("No 24-hour METAR history available for this airport.")
            return

        peak_wind = hist["Wind Speed"].max()
        latest_time = hist["Time Display"].iloc[0]
        latest_raw_metar = hist["Raw METAR"].iloc[0] if "Raw METAR" in hist.columns else "—"
        st.caption(f"Latest obs: {latest_time} • Peak total wind: {peak_wind:.1f} kt • METAR: {latest_raw_metar}")

        airport_elevation = hist["Elevation"].dropna().iloc[0] if "Elevation" in hist.columns and hist["Elevation"].notna().any() else None
        render_weather_history_charts(hist, airport_elevation_ft=airport_elevation, side_by_side=side_by_side_charts, phone=phone)

        history_columns = [
            "Time Display",
            "Xwind",
            "Ceiling",
            "Wind",
            "Visibility",
            "Gust Xwind",
        ] if phone else [
            "Time Display",
            "Wind",
            "Visibility",
            "Ceiling",
            "RWY",
            "Heading",
            "Length",
            "Xwind",
            "Gust Xwind",
        ]

        table_df = hist[history_columns].copy()

        def xwind_cell_style(value):
            try:
                if value is None or pd.isna(value):
                    return ""
                value = float(value)
            except Exception:
                return ""

            color_hex, _ = get_color(value)
            return (
                f"border: 1px solid {color_hex}; "
                f"background-color: {color_hex}22; "
                f"color: {color_hex}; "
                "font-weight: 900;"
            )

        styled_table = table_df.style.map(
            xwind_cell_style,
            subset=["Xwind", "Gust Xwind"],
        )

        st.dataframe(
            styled_table,
            use_container_width=True,
            hide_index=True,
            height=230 if phone else 260,
            column_config={
                "Time Display": st.column_config.TextColumn("UTC (Local)"),
                "Wind": st.column_config.TextColumn("Wind"),
                "Visibility": st.column_config.TextColumn("Vis"),
                "Ceiling": st.column_config.TextColumn("Ceiling"),
                "RWY": st.column_config.TextColumn("RWY"),
                "Heading": st.column_config.NumberColumn("Hdg", format="%d°"),
                "Length": st.column_config.NumberColumn("Length", format="%d ft"),
                "Xwind": st.column_config.NumberColumn("Xwind", format="%.1f kt"),
                "Gust Xwind": st.column_config.NumberColumn("Gust Xwind", format="%.1f kt"),
            },
        )


def row_click(icao):
    st.session_state.selected_icao = icao


def format_elevation(elevation):
    try:
        if elevation is None or pd.isna(elevation):
            return "—"
        return f"{int(float(elevation))} ft"
    except Exception:
        return "—"


def airport_name_with_elevation(row):
    name = html.escape(str(row.get("name", "—")))
    elev = format_elevation(row.get("elevation"))
    return f"{name} ({elev})" if elev != "—" else name


def render_rows(rows, runway_ends_by_icao, min_len, compact=False, phone=False, side_by_side_charts=True):
    for i, r in enumerate(rows, 1):
        color_hex, _ = get_color(r["cw"])
        selected = st.session_state.selected_icao == r["icao"]

        # Phone gets a single custom HTML card so the crosswind and ICAO/dropdown
        # affordance stay on the same horizontal row instead of Streamlit columns
        # stacking vertically on narrow screens.
        if phone:
            gust_value = r.get("gust_cw")
            gust_html = ""
            if gust_value is not None:
                gust_html = f"""
                    <div style="border-left:1px solid rgba(255,255,255,0.25); padding-left:7px; margin-left:7px; text-align:center;">
                        <div style="font-size:13px; line-height:13px; font-weight:950; color:#ffffff;">{gust_value}</div>
                        <div style="font-size:6px; line-height:7px; color:#dcdcdc; font-weight:900; letter-spacing:.2px;">GUST</div>
                    </div>
                """

            gust_text = f"G{r['gust']}" if r.get("gust") is not None else ""
            airport_short = html.escape(str(r.get("name", "—")))
            if len(airport_short) > 30:
                airport_short = airport_short[:27] + "..."

            selected_ring = "box-shadow:0 0 0 2px rgba(255,255,255,0.32) inset;" if selected else ""
            href = f"?selected_icao={html.escape(str(r['icao']))}"

            card_html = f"""
            <a href="{href}" target="_parent" style="text-decoration:none; color:inherit; display:block;">
                <div style="
                    border:1px solid rgba(255,255,255,0.18);
                    border-radius:12px;
                    padding:5px 6px 6px 6px;
                    margin:0 0 5px 0;
                    background:rgba(255,255,255,0.025);
                    font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
                    {selected_ring}
                ">
                    <div style="
                        display:grid;
                        grid-template-columns:minmax(0, 58%) minmax(0, 42%);
                        min-height:52px;
                    ">
                        <div style="
                            border:1px solid {color_hex};
                            border-right:0;
                            background:{color_hex}22;
                            border-radius:10px 0 0 10px;
                            display:flex;
                            align-items:center;
                            justify-content:center;
                            box-sizing:border-box;
                            min-width:0;
                        ">
                            <div style="text-align:center;">
                                <div style="font-size:25px; line-height:25px; font-weight:950; color:{color_hex};">{r['cw']}</div>
                                <div style="font-size:7px; line-height:8px; color:#f1f1f1; font-weight:900; letter-spacing:.25px;">KT XWIND</div>
                            </div>
                            {gust_html}
                        </div>
                        <div style="
                            border:1px solid {color_hex};
                            background:{color_hex}15;
                            border-radius:0 10px 10px 0;
                            display:flex;
                            align-items:center;
                            justify-content:center;
                            flex-direction:column;
                            box-sizing:border-box;
                            min-width:0;
                        ">
                            <div style="font-size:21px; line-height:22px; font-weight:950; color:#ffffff; letter-spacing:.4px; white-space:nowrap;">{html.escape(str(r['icao']))} ▼</div>
                            <div style="font-size:7px; line-height:8px; color:#dcdcdc; font-weight:800;">tap for history</div>
                        </div>
                    </div>
                    <div style="
                        display:grid;
                        grid-template-columns: 43% 57%;
                        column-gap:5px;
                        color:#d8d8d8;
                        font-size:9.5px;
                        line-height:11px;
                        padding-top:4px;
                        overflow:hidden;
                    ">
                        <div><b>{html.escape(str(r['wind']))}{gust_text}</b></div>
                        <div>RWY {html.escape(str(r['runway']))} · {r['length']} ft</div>
                    </div>
                    <div style="
                        color:#f1f1f1;
                        font-size:10.5px;
                        line-height:12px;
                        padding-top:3px;
                        font-weight:800;
                        display:-webkit-box;
                        -webkit-line-clamp:2;
                        -webkit-box-orient:vertical;
                        overflow:hidden;
                    ">
                        {html.escape(str(r.get('name', '—')))}
                    </div>
                    <div style="
                        color:#bdbdbd;
                        font-size:9.5px;
                        line-height:11px;
                        padding-top:1px;
                        white-space:nowrap;
                        overflow:hidden;
                        text-overflow:ellipsis;
                    ">
                        {html.escape(str(r.get('city', '—')))}, {html.escape(str(r.get('country', '—')))}
                    </div>
                </div>
            </a>
            """
            render_raw_html(card_html, height=112)

            if selected:
                render_history_panel(
                    r["icao"],
                    runway_ends_by_icao,
                    min_len,
                    title="Past 24 Hours",
                    phone=phone,
                    side_by_side_charts=side_by_side_charts,
                )

            continue

        card_height = 46 if compact else 54
        cw_font = 23 if compact else 26
        gust_font = 13 if compact else 16
        label_font = 8 if compact else 9
        row_num_font = 12 if compact else 14
        row_num_pad = 12 if compact else 18

        with st.container(border=True):
            if compact:
                cols = st.columns([0.30, 1.05, 0.78, 0.75, 1.35], gap="small")
            else:
                cols = st.columns([0.35, 1.35, 0.95, 0.8, 1.05, 0.7, 2.35])

            with cols[0]:
                st.markdown(
                    f"<div style='padding-top:{row_num_pad}px; font-weight:900; color:#d8d8d8; font-size:{row_num_font}px;'>#{i}</div>",
                    unsafe_allow_html=True,
                )

            with cols[1]:
                gust_block = ""
                if r.get("gust_cw") is not None:
                    gust_block = f"""
                    <div style="border-left:1px solid rgba(255,255,255,0.25); padding-left:8px; margin-left:7px; text-align:center;">
                        <div style="font-size:{gust_font}px; line-height:{gust_font}px; font-weight:950; color:#ffffff;">{r['gust_cw']}</div>
                        <div style="font-size:7px; color:#dcdcdc; font-weight:900; letter-spacing:.3px;">GUST</div>
                    </div>
                    """

                st.markdown(
                    f"""
                    <div style="
                        border:1px solid {color_hex};
                        background:{color_hex}22;
                        border-radius:12px;
                        height:{card_height}px;
                        display:flex;
                        justify-content:center;
                        align-items:center;
                        box-sizing:border-box;
                        margin-top:1px;
                        margin-bottom:1px;
                    ">
                        <div style="text-align:center;">
                            <div style="font-size:{cw_font}px; line-height:{cw_font}px; font-weight:950; color:{color_hex};">{r['cw']}</div>
                            <div style="font-size:{label_font}px; color:#f1f1f1; font-weight:900; letter-spacing:.4px;">KT XWIND</div>
                        </div>
                        {gust_block}
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with cols[2]:
                st.button(
                    f"{r['icao']} ▾",
                    key=f"select_{r['icao']}_{i}_{'compact' if compact else 'wide'}",
                    on_click=row_click,
                    args=(r["icao"],),
                    use_container_width=True,
                )

            if compact:
                with cols[3]:
                    gust_text = f"G{r['gust']}" if r.get("gust") is not None else ""
                    st.markdown(f"<div style='font-size:12px; line-height:14px;'><b>Wind</b><br>{r['wind']}{gust_text}</div>", unsafe_allow_html=True)
                with cols[4]:
                    airport_short = airport_name_with_elevation(r)
                    st.markdown(f"<div style='font-size:12px; line-height:14px;'><b>RWY {html.escape(str(r['runway']))}</b> ({r['length']} ft)<br>{airport_short}</div>", unsafe_allow_html=True)
            else:
                with cols[3]:
                    gust_text = f"G{r['gust']}" if r.get("gust") is not None else ""
                    st.markdown(f"**Wind**  \n{r['wind']}{gust_text}")

                with cols[4]:
                    st.markdown(f"**Runway**  \nRWY {html.escape(str(r['runway']))} ({r['length']} ft)")

                with cols[5]:
                    st.markdown(f"**Hdg**  \n{r['heading']}°")

                with cols[6]:
                    st.markdown(
                        f"**{airport_name_with_elevation(r)}**  \n"
                        f"{html.escape(str(r['city']))}, {html.escape(str(r['country']))}"
                    )

            if selected:
                render_history_panel(
                    r["icao"],
                    runway_ends_by_icao,
                    min_len,
                    title="Past 24 Hours",
                    phone=phone,
                    side_by_side_charts=side_by_side_charts,
                )

def make_runway_line(row, distance_nm=8):
    if row.get("heading") in (None, "—") or pd.isna(row.get("lat")) or pd.isna(row.get("lon")):
        return None

    lat = float(row["lat"])
    lon = float(row["lon"])
    heading = float(row["heading"])

    lat1, lon1 = offset_point(lat, lon, heading, distance_nm)
    lat2, lon2 = offset_point(lat, lon, (heading + 180) % 360, distance_nm)

    return {
        **row,
        "start_lon": lon1,
        "start_lat": lat1,
        "end_lon": lon2,
        "end_lat": lat2,
        "line_color": [235, 235, 235, 220],
    }


def make_wind_arrow(row, start_nm=18, end_nm=7):
    if row.get("wind_dir") in (None, "—") or pd.isna(row.get("lat")) or pd.isna(row.get("lon")):
        return None

    lat = float(row["lat"])
    lon = float(row["lon"])
    wind_from = float(row["wind_dir"])

    start_lat, start_lon = offset_point(lat, lon, wind_from, start_nm)
    end_lat, end_lon = offset_point(lat, lon, wind_from, end_nm)

    direction_to = (wind_from + 180) % 360
    left_lat, left_lon, right_lat, right_lon = arrowhead_points(end_lat, end_lon, direction_to)

    return {
        **row,
        "wind_start_lon": start_lon,
        "wind_start_lat": start_lat,
        "wind_end_lon": end_lon,
        "wind_end_lat": end_lat,
        "left_lon": left_lon,
        "left_lat": left_lat,
        "right_lon": right_lon,
        "right_lat": right_lat,
        "wind_color": [90, 190, 255, 235],
    }


def render_map(rows, height=650):
    df = pd.DataFrame(rows).dropna(subset=["lat", "lon"]).copy()

    selected_extra = st.session_state.selected_airport_result
    if selected_extra and selected_extra["icao"] not in df.get("icao", pd.Series(dtype=str)).values:
        df = pd.concat([df, pd.DataFrame([selected_extra])], ignore_index=True)

    if df.empty:
        st.info("No map coordinates available.")
        return

    df["color"] = df["cw"].apply(lambda x: get_color(x)[1])
    df["radius"] = 16000

    selected_row = None
    if st.session_state.selected_icao:
        matches = df[df["icao"] == st.session_state.selected_icao]
        if not matches.empty:
            selected_row = matches.iloc[0].to_dict()

    runway_records = [make_runway_line(r) for r in df.to_dict("records")]
    runway_records = [r for r in runway_records if r is not None]
    runway_df = pd.DataFrame(runway_records)

    wind_records = [make_wind_arrow(r) for r in df.to_dict("records")]
    wind_records = [r for r in wind_records if r is not None]
    wind_df = pd.DataFrame(wind_records)

    layers = []

    if not runway_df.empty:
        layers.append(pdk.Layer("LineLayer", data=runway_df, get_source_position="[start_lon, start_lat]", get_target_position="[end_lon, end_lat]", get_color="line_color", get_width=2, width_min_pixels=1, width_max_pixels=3))

    if not wind_df.empty:
        layers.extend([
            pdk.Layer("LineLayer", data=wind_df, get_source_position="[wind_start_lon, wind_start_lat]", get_target_position="[wind_end_lon, wind_end_lat]", get_color="wind_color", get_width=3, width_min_pixels=2, width_max_pixels=5),
            pdk.Layer("LineLayer", data=wind_df, get_source_position="[left_lon, left_lat]", get_target_position="[wind_end_lon, wind_end_lat]", get_color="wind_color", get_width=3, width_min_pixels=2, width_max_pixels=5),
            pdk.Layer("LineLayer", data=wind_df, get_source_position="[right_lon, right_lat]", get_target_position="[wind_end_lon, wind_end_lat]", get_color="wind_color", get_width=3, width_min_pixels=2, width_max_pixels=5),
        ])

    layers.append(pdk.Layer("ScatterplotLayer", data=df, get_position="[lon, lat]", get_radius="radius", get_fill_color="color", get_line_color=[255, 255, 255, 220], line_width_min_pixels=1, radius_min_pixels=4, radius_max_pixels=8, pickable=True))

    if selected_row:
        layers.append(
            pdk.Layer(
                "ScatterplotLayer",
                data=pd.DataFrame([selected_row]),
                get_position="[lon, lat]",
                get_radius=26000,
                get_fill_color=[255, 255, 255, 70],
                get_line_color=[255, 255, 255, 255],
                line_width_min_pixels=3,
                radius_min_pixels=10,
                radius_max_pixels=18,
            )
        )

    view_state = pdk.ViewState(
        latitude=float(selected_row["lat"]) if selected_row else 39.5,
        longitude=float(selected_row["lon"]) if selected_row else -98.35,
        zoom=7.5 if selected_row else 2.7,
        pitch=0,
    )

    deck = pdk.Deck(
        layers=layers,
        initial_view_state=view_state,
        map_style="https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json",
        tooltip={
            "html": """
            <b>{icao}</b><br/>
            {name}<br/>
            Crosswind: <b>{cw} kt</b><br/>
            Gust crosswind: <b>{gust_cw} kt</b><br/>
            Wind: {wind}<br/>
            Runway: {runway} / {heading}°<br/>
            Length: {length} ft
            """,
            "style": {"backgroundColor": "#111", "color": "white", "fontSize": "13px"},
        },
    )

    st.pydeck_chart(deck, use_container_width=True, height=height)
    st.caption("Purple >30 kt • Red 20–30 kt • Orange 15–20 kt • Yellow ≤15 kt • White line = runway • Blue arrow = wind toward airport")

def render_search_panel(active_airports, airport_lookup, runway_ends_by_icao, min_len, compact=False, phone=False, side_by_side_charts=True):
    if phone:
        st.markdown(
            """
            <style>
                div[data-testid="stTextInput"] input {
                    font-size: 12px !important;
                    padding: 0.22rem 0.38rem !important;
                    min-height: 1.75rem !important;
                }
                div[data-testid="stSelectbox"] * {
                    font-size: 12px !important;
                }
                div[data-testid="stSelectbox"] [data-baseweb="select"] > div {
                    min-height: 1.85rem !important;
                    padding-top: 0rem !important;
                    padding-bottom: 0rem !important;
                }
                div[data-testid="stExpander"] details {
                    padding-top: 0rem !important;
                    padding-bottom: 0rem !important;
                }
                div[data-testid="stExpander"] summary {
                    font-size: 0.78rem !important;
                    min-height: 1.6rem !important;
                    padding-top: 0.15rem !important;
                    padding-bottom: 0.15rem !important;
                }
                .mobile-search-title {
                    font-size: 0.84rem;
                    font-weight: 850;
                    margin-bottom: 0.15rem;
                    color: #e8e8e8;
                }
            </style>
            <div class="mobile-search-title">Search</div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown("### Airport Search")

    query = st.text_input(
        "Search ICAO, name, city, or region",
        placeholder="KSEA, Seattle, KBFI, PANC, PHNL",
        label_visibility="collapsed",
    )

    if not query:
        return

    q = query.strip().lower()

    searchable = active_airports.copy()
    searchable["ident_s"] = searchable["ident"].fillna("").astype(str).str.lower()
    searchable["name_s"] = searchable["name"].fillna("").astype(str).str.lower()
    searchable["city_s"] = searchable["municipality"].fillna("").astype(str).str.lower()
    searchable["region_s"] = searchable["iso_region"].fillna("").astype(str).str.lower()

    matches = searchable[
        searchable["ident_s"].str.contains(q, regex=False)
        | searchable["name_s"].str.contains(q, regex=False)
        | searchable["city_s"].str.contains(q, regex=False)
        | searchable["region_s"].str.contains(q, regex=False)
    ].copy()

    if matches.empty:
        st.warning("No matching airport found in current US/global dataset.")
        return

    matches = matches.sort_values(["ident"]).head(25)

    labels = [
        f"{row.ident} — {row.name} ({row.municipality}, {row.iso_region}, {row.iso_country})"
        for row in matches.itertuples()
    ]

    selected_label = st.selectbox("Select airport", labels, label_visibility="collapsed")
    icao = selected_label.split(" — ")[0].strip().upper()

    result, error = build_single_airport_result(icao, airport_lookup, runway_ends_by_icao, min_len=min_len)

    if error:
        st.warning(error)
        return

    has_xwind = result.get("cw") is not None
    color_hex, _ = get_color(result["cw"])
    gust_text = f"G{result['gust']}" if result.get("gust") is not None else ""
    gust_cw_text = f"{result['gust_cw']} kt" if result.get("gust_cw") is not None else "—"
    cw_display = f"{result['cw']} kt" if has_xwind else "—"

    with st.container(border=True):
        st.markdown(
            f"""
            <div style="font-size:15px; font-weight:900; line-height:1.12;">
                {result['icao']} — {airport_name_with_elevation(result)}
            </div>
            <div style="font-size:12px; color:#aaa; margin-bottom:6px;">
                {html.escape(str(result['city']))} • {result['region']} • {result['country']}
            </div>
            """,
            unsafe_allow_html=True,
        )

        if compact:
            c1, c2 = st.columns([1.05, 1.95])
            with c1:
                st.markdown(
                    f"""
                    <div style="
                        border:1px solid {color_hex};
                        background:{color_hex}22;
                        border-radius:12px;
                        height:70px;
                        display:flex;
                        flex-direction:column;
                        justify-content:center;
                        align-items:center;
                        opacity:{'1' if has_xwind else '0.65'};
                    ">
                        <div style="font-size:28px; line-height:30px; font-weight:950; color:{color_hex};">
                            {cw_display}
                        </div>
                        <div style="font-size:10px; font-weight:900; color:#f1f1f1;">CROSSWIND</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with c2:
                st.markdown(
                    f"**Gust Xwind:** {gust_cw_text}  \n"
                    f"**Wind:** {result['wind']}{gust_text}  \n"
                    f"**Runway:** RWY {result['runway']} ({result['length']} ft)  \n"
                    f"**Heading:** {result['heading']}°"
                )
        else:
            c1, c2, c3 = st.columns([1.15, 1, 1])
            with c1:
                st.markdown(
                    f"""
                    <div style="
                        border:1px solid {color_hex};
                        background:{color_hex}22;
                        border-radius:12px;
                        height:76px;
                        display:flex;
                        flex-direction:column;
                        justify-content:center;
                        align-items:center;
                        opacity:{'1' if has_xwind else '0.65'};
                    ">
                        <div style="font-size:30px; line-height:30px; font-weight:950; color:{color_hex};">
                            {cw_display}
                        </div>
                        <div style="font-size:10px; font-weight:900; color:#f1f1f1;">CROSSWIND</div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            with c2:
                st.markdown(f"**Gust Xwind**  \n{gust_cw_text}")
                st.markdown(f"**Wind**  \n{result['wind']}{gust_text}")
            with c3:
                st.markdown(f"**Runway**  \nRWY {result['runway']} ({result['length']} ft)")
                st.markdown(f"**Heading**  \n{result['heading']}°")

        raw_metar = result.get("raw_metar")
        if raw_metar:
            st.caption(f"METAR: {raw_metar}")

        if st.button("Zoom map to this airport", use_container_width=True):
            st.session_state.selected_icao = result["icao"]
            st.session_state.selected_airport_result = result
            st.rerun()

        render_history_panel(result["icao"], runway_ends_by_icao, min_len, title="Past 24 Hours", phone=phone, side_by_side_charts=side_by_side_charts)


airports, runway_ends, runway_ends_by_icao, airport_lookup = load_data()

timezone_name = get_viewer_timezone()
last_updated_text = format_last_updated(timezone_name)

# Keep the old default wind filter without taking up UI space.
min_wind = 10

if "min_len" not in st.session_state:
    st.session_state.min_len = 5000
if "top_n" not in st.session_state:
    st.session_state.top_n = 30
if "layout_mode" not in st.session_state:
    st.session_state.layout_mode = preferred_layout_mode
if "use_global" not in st.session_state:
    st.session_state.use_global = False


def render_top_header_controls(phone=False, tablet=False):
    st.markdown(
        """
        <style>
            div[data-testid="stSlider"] { padding-top: 0rem; padding-bottom: 0rem; }
            div[data-testid="stRadio"] { padding-top: 0rem; padding-bottom: 0rem; }
            div[data-testid="stCheckbox"] { padding-top: 0rem; padding-bottom: 0rem; }
            div[data-testid="stButton"] { padding-top: 0rem; }
            div[role="radiogroup"] { flex-direction: row !important; gap: 0.20rem !important; flex-wrap: nowrap !important; }
            div[role="radiogroup"] label { white-space: nowrap !important; padding: 0rem 0.05rem !important; font-size: 0.72rem !important; }
            div[data-testid="stCheckbox"] label { white-space: nowrap !important; font-size: 0.72rem !important; }
            div[data-testid="stPopover"] button { padding: 0.08rem 0.22rem !important; min-height: 1.35rem !important; font-size: 0.66rem !important; }
            button[kind="secondary"] { padding: 0.08rem 0.22rem !important; min-height: 1.35rem !important; font-size: 0.66rem !important; }
            .last-updated {
                text-align: right;
                color: #aaa;
                font-size: 10.5px;
                margin-top: -4px;
                margin-bottom: 0px;
            }
            .phone-title h1 {
                font-size: 1.12rem !important;
                line-height: 1.08 !important;
                margin-bottom: 0.05rem !important;
            }
            .tablet-title h1 {
                font-size: 1.32rem !important;
                line-height: 1.08 !important;
            }
            .mobile-options-summary {
                color:#aaa;
                font-size:10.5px;
                text-align:right;
                margin-top:-3px;
            }
            @media (max-width: 760px) {
                .block-container { padding-left: 0.38rem !important; padding-right: 0.38rem !important; padding-top: 0.38rem !important; }
                div[data-testid="stMarkdownContainer"] p { font-size: 0.74rem; }
                div[data-testid="stVerticalBlock"] { gap: 0.16rem !important; }
            }
        </style>
        """,
        unsafe_allow_html=True,
    )

    default_layout = "Stacked" if phone or (screen_width is not None and screen_width < 980) else st.session_state.layout_mode

    if phone:
        # Keep mobile controls collapsed so the map starts near the top.
        with st.expander("Options", expanded=False):
            c1, c2 = st.columns(2, gap="small")
            with c1:
                min_len_value = st.slider(
                    "Runway ft",
                    min_value=0,
                    max_value=12000,
                    value=st.session_state.min_len,
                    step=500,
                    key="min_len_header",
                )
            with c2:
                top_n_value = st.slider(
                    "Rows",
                    min_value=5,
                    max_value=100,
                    value=st.session_state.top_n,
                    step=5,
                    key="top_n_header",
                )
            c3, c4, c5 = st.columns([1.15, 0.75, 0.65], gap="small")
            with c3:
                layout_value = st.radio(
                    "Layout",
                    ["Wide", "Stacked"],
                    horizontal=True,
                    label_visibility="collapsed",
                    index=0 if default_layout == "Wide" else 1,
                    key="layout_header",
                )
            with c4:
                global_value = st.checkbox("Global", value=st.session_state.use_global, key="global_header")
            with c5:
                refresh_value = st.button("↻", key="refresh_header", use_container_width=True)
        st.markdown(
            f"<div class='mobile-options-summary'>Last updated: {last_updated_text}</div>",
            unsafe_allow_html=True,
        )
        return min_len_value, top_n_value, layout_value, global_value, refresh_value

    # Tablet/desktop: keep controls tucked into one compact dropdown so the header stays clean.
    with st.popover("Options ▾", use_container_width=True):
        c1, c2 = st.columns(2, gap="small")
        with c1:
            min_len_value = st.slider(
                "Runway ft",
                min_value=0,
                max_value=12000,
                value=st.session_state.min_len,
                step=500,
                key="min_len_header",
            )
        with c2:
            top_n_value = st.slider(
                "Rows",
                min_value=5,
                max_value=100,
                value=st.session_state.top_n,
                step=5,
                key="top_n_header",
            )

        c3, c4, c5 = st.columns([1.2, 0.75, 0.65], gap="small")
        with c3:
            layout_value = st.radio(
                "Layout",
                ["Wide", "Stacked"],
                horizontal=True,
                index=0 if default_layout == "Wide" else 1,
                key="layout_header",
            )
        with c4:
            global_value = st.checkbox("Global", value=st.session_state.use_global, key="global_header")
        with c5:
            refresh_value = st.button("Refresh" if not tablet else "↻", key="refresh_header", use_container_width=True)

    st.markdown(
        f"<div class='last-updated'>Last updated: {last_updated_text}</div>",
        unsafe_allow_html=True,
    )

    return min_len_value, top_n_value, layout_value, global_value, refresh_value


render_header_image()

if is_phone:
    min_len, top_n, layout_mode, use_global, refresh = render_top_header_controls(phone=True)
elif is_tablet:
    controls_left, controls_right = st.columns([1.7, 0.75], gap="small")
    with controls_left:
        st.caption("Tap ICAO ▾ for history.")
    with controls_right:
        min_len, top_n, layout_mode, use_global, refresh = render_top_header_controls(phone=False, tablet=True)
else:
    controls_left, controls_right = st.columns([2.35, 0.65], gap="small")
    with controls_left:
        st.caption("Crosswinds color-coded by strength. Click an ICAO ▾ or search airport to zoom the map.")
    with controls_right:
        min_len, top_n, layout_mode, use_global, refresh = render_top_header_controls(phone=False)

st.session_state.min_len = min_len
st.session_state.top_n = top_n
st.session_state.layout_mode = layout_mode
st.session_state.use_global = use_global

# Responsive behavior based on viewport width.
# Phone and narrow tablet load stacked. iPad landscape / desktop load wide.
if is_phone:
    layout_mode = "Stacked"
    map_height = int((screen_width or 360) * 0.33)  # ~1/3 of screen width
    compact_rows = True
    side_by_side_charts = False
    row_window_height = None
elif is_tablet:
    if screen_width is not None and screen_width < 980:
        layout_mode = "Stacked"
    else:
        layout_mode = "Wide"
    map_height = 470 if layout_mode == "Stacked" else 540
    compact_rows = True
    side_by_side_charts = True
    row_window_height = 820
else:
    layout_mode = "Wide" if screen_width is None else layout_mode
    map_height = 650
    compact_rows = False
    side_by_side_charts = True
    row_window_height = 920

if refresh:
    get_metars.clear()
    get_metar_history.clear()
    st.rerun()

if use_global:
    active_airports = airports.copy()
else:
    active_airports = airports[airports["iso_country"] == "US"].copy()

with st.spinner("Loading airport winds..."):
    results = build_results(active_airports, runway_ends_by_icao, min_wind, min_len)

if st.session_state.selected_icao and not any(
    r["icao"] == st.session_state.selected_icao for r in results[:top_n]
):
    if not st.session_state.selected_airport_result:
        st.session_state.selected_icao = None


def render_responsive_search():
    if is_phone:
        with st.expander("Search Airport", expanded=False):
            render_search_panel(
                active_airports,
                airport_lookup,
                runway_ends_by_icao,
                min_len,
                compact=True,
                phone=True,
                side_by_side_charts=side_by_side_charts,
            )
    else:
        render_search_panel(
            active_airports,
            airport_lookup,
            runway_ends_by_icao,
            min_len,
            compact=True,
            phone=False,
            side_by_side_charts=side_by_side_charts,
        )

if layout_mode == "Wide":
    left, right = st.columns([2, 1])

    with left:
        st.subheader("Global Crosswinds" if use_global else "US Crosswinds")
        with st.container(height=row_window_height, border=False):
            render_rows(
                results[:top_n],
                runway_ends_by_icao,
                min_len,
                compact=compact_rows,
                phone=is_phone,
                side_by_side_charts=side_by_side_charts,
            )

    with right:
        st.subheader("Map")
        render_map(results[:top_n], height=map_height)
        render_responsive_search()

else:
    st.subheader("Map")
    render_map(results[:top_n], height=map_height)

    render_responsive_search()

    st.subheader("Global Crosswinds" if use_global else "US Crosswinds")
    render_rows(
        results[:top_n],
        runway_ends_by_icao,
        min_len,
        compact=True,
        phone=is_phone,
        side_by_side_charts=side_by_side_charts,
    )
