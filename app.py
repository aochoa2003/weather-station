"""
A small weather website built with Flask and the Open-Meteo API,
with the National Weather Service Area Forecast Discussion layered on top.

The page's colour scheme is derived from the actual sky at the searched
location -- see build_sky() near the bottom. Nothing is hardcoded; a clear
noon and an overcast midnight produce genuinely different pages.

Start it with:  python app.py
Then open:      http://127.0.0.1:5000

BEFORE YOU DEPLOY: change CONTACT below to a real email address of yours.
The National Weather Service asks every caller to identify itself, and
requests carrying a generic User-Agent get blocked.
"""

import re
import time
import traceback
from datetime import datetime, timedelta

import requests
from flask import Flask, render_template, request

app = Flask(__name__)

# --- CHANGE THIS to your own email address --------------------------------
CONTACT = "you@example.com"
# --------------------------------------------------------------------------

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

NWS_POINT_URL = "https://api.weather.gov/points/{lat},{lon}"
NWS_LIST_URL = "https://api.weather.gov/products/types/AFD/locations/{office}"
NWS_PRODUCT_URL = "https://api.weather.gov/products/{product_id}"

NWS_HEADERS = {
    "User-Agent": f"weather-station-hobby-project ({CONTACT})",
    "Accept": "application/geo+json",
}

_point_cache = {}
_afd_cache = {}
AFD_TTL = 900

AFD_SECTION = re.compile(r"^\.([A-Z][^\n.]{1,90})\.\.\.", re.MULTILINE)
AFD_TIMESTAMP = re.compile(r"^\d{3,4}\s+(AM|PM)\s+\w{2,4}\s+\w{3}\s+\w{3}", re.I)

WEATHER_CODES = {
    0: "Clear",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    56: "Freezing drizzle",
    57: "Freezing drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with hail",
    99: "Thunderstorm with hail",
}

COMPASS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]

# Daytime gradient for each kind of sky: horizon-down, mid, zenith.
CONDITION_STOPS = {
    "clear": ("#B9DCF5", "#5FA3E0", "#1B62B5"),
    "cloudy": ("#C2CFD9", "#7E9FBC", "#3E6E9E"),
    "overcast": ("#AEB7BF", "#7C8894", "#4E5A66"),
    "fog": ("#C4C6C7", "#989C9F", "#6E7478"),
    "rain": ("#7E97A5", "#4F6B7A", "#2E4451"),
    "snow": ("#D2DEE7", "#96A8B8", "#5D6F80"),
    "storm": ("#576072", "#333B4C", "#161C29"),
}

CONDITION_ACCENTS = {
    "clear": "#FFC24D",
    "cloudy": "#FFD08A",
    "overcast": "#E8C46A",
    "fog": "#D9D3BA",
    "rain": "#6FD3E8",
    "snow": "#E8F4FF",
    "storm": "#FFE066",
}

NIGHT_TINT = "#060A1A"
DAWN_TINT = "#F09A5A"
DUSK_TINT = "#E06A45"


# ---------------------------------------------------------------------------
# Small helpers for surviving missing data
# ---------------------------------------------------------------------------

def describe(code):
    return WEATHER_CODES.get(code, "Unknown")


def num(value):
    """Open-Meteo sends null wherever it has no data, which arrives here as
    None. Anything that reaches round() or min() as None raises TypeError, so
    every number from the API gets funnelled through this first."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def whole(value):
    """Round to a whole number, but pass None through untouched."""
    number = num(value)
    return None if number is None else round(number)


def at(values, index):
    """Read one item out of a list without assuming the list is long enough."""
    if not values or index < 0 or index >= len(values):
        return None
    return values[index]


def moment(timestamp):
    """Parse an ISO timestamp, returning None rather than raising."""
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None


@app.template_filter("dash")
def dash(value):
    """Render missing readings as a dash rather than the word 'None'.
    Checks for None specifically rather than falsiness, so a genuine
    reading of 0% humidity still displays as 0."""
    return "\u2014" if value is None else value


# ---------------------------------------------------------------------------
# Colour maths for the sky palette
# ---------------------------------------------------------------------------

def to_rgb(value):
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def to_hex(rgb):
    return "#" + "".join(f"{max(0, min(255, round(c))):02X}" for c in rgb)


def mix(first, second, amount):
    """Blend two colours. amount=0 returns the first, 1 returns the second."""
    a, b = to_rgb(first), to_rgb(second)
    return to_hex(tuple(a[i] + (b[i] - a[i]) * amount for i in range(3)))


def brightness(value):
    """Perceived lightness, 0 to 1. Green dominates because human eyes
    are most sensitive to it -- this is why a pure blue reads as darker
    than a pure green of the same numeric value."""
    r, g, b = to_rgb(value)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255


def condition_group(code):
    """Collapse 27 WMO codes into the seven skies worth colouring."""
    if code in (0, 1):
        return "clear"
    if code == 2:
        return "cloudy"
    if code == 3:
        return "overcast"
    if code in (45, 48):
        return "fog"
    if code in (95, 96, 99):
        return "storm"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    return "cloudy"


def day_phase(sunrise, sunset, now):
    """Which of the four light phases we're in. The 50-minute windows
    around sunrise and sunset are where the sky does its most interesting
    colour work, so they get phases of their own."""
    if not (sunrise and sunset and now):
        return "day"
    edge = timedelta(minutes=50)
    if abs(now - sunrise) <= edge:
        return "dawn"
    if abs(now - sunset) <= edge:
        return "dusk"
    if sunrise <= now <= sunset:
        return "day"
    return "night"


def build_sky(code, sunrise, sunset, now):
    """Derive the whole page palette from the actual sky.

    Returns CSS custom property values. Everything downstream -- text,
    panels, borders, the accent -- is computed from these three gradient
    stops so the page stays legible whatever the weather does."""
    condition = condition_group(code)
    phase = day_phase(sunrise, sunset, now)
    stops = CONDITION_STOPS.get(condition, CONDITION_STOPS["cloudy"])
    accent = CONDITION_ACCENTS.get(condition, "#FFD08A")

    if phase == "night":
        stops = tuple(mix(stop, NIGHT_TINT, 0.80) for stop in stops)
        accent = "#AFC3F0" if condition == "clear" else mix(accent, "#8FA6D8", 0.45)
    elif phase == "dawn":
        stops = tuple(mix(stop, DAWN_TINT, 0.34) for stop in stops)
    elif phase == "dusk":
        stops = tuple(mix(stop, DUSK_TINT, 0.36) for stop in stops)

    # Pick ink that survives whatever gradient we just built.
    light_background = brightness(stops[1]) > 0.52
    ink = "#0B1119" if light_background else "#F2F7FB"
    veil = "255, 255, 255" if light_background else "12, 18, 30"
    panel = 0.46 if light_background else 0.34

    # An accent tuned for a bright sky can vanish on a dark one.
    if light_background and brightness(accent) > 0.72:
        accent = mix(accent, "#6B4A00", 0.42)

    return {
        "phase": phase,
        "condition": condition,
        "horizon": stops[0],
        "middle": stops[1],
        "zenith": stops[2],
        "ink": ink,
        "veil": veil,
        "panel": panel,
        "accent": accent,
        "night": phase == "night",
    }


def sun_arc(sunrise, sunset, now, width=280, height=64, inset=10):
    """Where the sun sits on its daily arc, as SVG coordinates.

    The arc is a half-ellipse from sunrise on the left to sunset on the
    right. Before sunrise or after sunset the marker parks at the nearest
    end and the template swaps it for a moon."""
    if not (sunrise and sunset and now) or sunset <= sunrise:
        return None

    total = (sunset - sunrise).total_seconds()
    elapsed = (now - sunrise).total_seconds()
    progress = max(0.0, min(1.0, elapsed / total))
    up = 0 <= elapsed <= total

    radius_x = (width - inset * 2) / 2
    radius_y = height - inset
    centre_x = width / 2
    baseline = height

    # A half-ellipse swept left to right, drawn with one SVG arc command.
    path = (
        f"M {inset},{baseline} "
        f"A {radius_x},{radius_y} 0 0 1 {width - inset},{baseline}"
    )

    # Parametric position along that same ellipse.
    from math import cos, pi, sin

    angle = pi * progress
    x = centre_x - radius_x * cos(angle)
    y = baseline - radius_y * sin(angle)

    return {
        "path": path,
        "x": round(x, 1),
        "y": round(y, 1),
        "width": width,
        "height": height,
        "baseline": baseline,
        "up": up,
        "progress": round(progress * 100),
    }


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def hour_label(when):
    """Format an hour as '2pm'. Written by hand because strftime's
    no-leading-zero flag differs between Windows and everything else."""
    hour = when.hour % 12 or 12
    return f"{hour}{'am' if when.hour < 12 else 'pm'}"


def clock(timestamp):
    """Format a timestamp as '6:12 am'. Returns None on missing or malformed
    input -- polar locations genuinely have no sunrise on some dates."""
    when = moment(timestamp)
    if when is None:
        return None
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {'am' if when.hour < 12 else 'pm'}"


def compass(degrees):
    """Turn a wind bearing in degrees into a label like 'NNE'."""
    value = num(degrees)
    if value is None:
        return None
    return COMPASS[int(value % 360 / 22.5 + 0.5) % 16]


def uv_label(value):
    """The standard WHO exposure bands."""
    if value is None:
        return None
    if value < 3:
        return "Low"
    if value < 6:
        return "Moderate"
    if value < 8:
        return "High"
    if value < 11:
        return "Very high"
    return "Extreme"


def visibility_label(meters, imperial):
    """Open-Meteo always reports visibility in metres regardless of the
    temperature unit, so this converts by hand."""
    value = num(meters)
    if value is None:
        return None
    if imperial:
        miles = value / 1609.34
        return f"{miles:.1f} mi" if miles < 10 else f"{round(miles)} mi"
    km = value / 1000
    return f"{km:.1f} km" if km < 10 else f"{round(km)} km"


# ---------------------------------------------------------------------------
# Open-Meteo
# ---------------------------------------------------------------------------

def find_place(name):
    """Turn a city name into coordinates. Returns None if nothing matches."""
    response = requests.get(
        GEOCODE_URL, params={"name": name, "count": 1}, timeout=10
    )
    response.raise_for_status()
    results = response.json().get("results")
    if not results:
        return None

    place = results[0]
    region = ", ".join(
        part for part in (place.get("admin1"), place.get("country")) if part
    )
    return {
        "name": place.get("name", name),
        "region": region,
        "latitude": place["latitude"],
        "longitude": place["longitude"],
    }


def get_forecast(latitude, longitude, units):
    """Fetch current conditions, the next 24 hours, and the next 5 days.

    Everything below arrives in one single request -- adding names to these
    three strings costs no extra API calls."""
    imperial = units == "imperial"
    response = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "wind_speed_10m,wind_direction_10m,wind_gusts_10m,weather_code,"
            "is_day,cloud_cover,pressure_msl,precipitation",
            "hourly": "temperature_2m,precipitation_probability,uv_index,"
            "dew_point_2m,visibility,pressure_msl",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "sunrise,sunset,uv_index_max,precipitation_probability_max,"
            "precipitation_sum",
            "timezone": "auto",
            "forecast_days": 5,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
            "precipitation_unit": "inch" if imperial else "mm",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# National Weather Service: the forecaster's own discussion
# ---------------------------------------------------------------------------

def nws_point(latitude, longitude):
    """Ask the NWS which office and radar cover these coordinates.

    Returns None outside the United States -- api.weather.gov only covers
    US territory and answers 404 for anywhere else, which is expected
    rather than an error."""
    key = f"{latitude:.3f},{longitude:.3f}"
    if key in _point_cache:
        return _point_cache[key]

    response = requests.get(
        NWS_POINT_URL.format(lat=f"{latitude:.4f}", lon=f"{longitude:.4f}"),
        headers=NWS_HEADERS,
        timeout=8,
    )
    if response.status_code == 404:
        _point_cache[key] = None
        return None
    response.raise_for_status()

    props = response.json().get("properties") or {}
    office = props.get("gridId")
    if not office:
        _point_cache[key] = None
        return None

    point = {
        "office": office,
        "radar": props.get("radarStation"),  # e.g. "KFTG" for Denver
        "office_url": f"https://www.weather.gov/{office.lower()}/",
    }
    _point_cache[key] = point
    return point


def fetch_afd(office):
    """Fetch the most recent Area Forecast Discussion text for an office."""
    listing = requests.get(
        NWS_LIST_URL.format(office=office), headers=NWS_HEADERS, timeout=8
    )
    if listing.status_code == 404:
        return None
    listing.raise_for_status()

    products = listing.json().get("@graph") or []
    if not products:
        return None

    product_id = products[0].get("id")
    if not product_id:
        return None

    detail = requests.get(
        NWS_PRODUCT_URL.format(product_id=product_id),
        headers=NWS_HEADERS,
        timeout=8,
    )
    detail.raise_for_status()
    return detail.json()


def parse_afd(payload):
    """Split the raw discussion into titled sections.

    An AFD is fixed-width teletype text. Sections are marked by a leading
    dot and trailing ellipsis -- '.SYNOPSIS...' -- with '&&' between them
    and '$$' at the very end. Those markers are stripped out here."""
    text = (payload or {}).get("productText") or ""
    if not text.strip():
        return None

    body = text.replace("\r\n", "\n").replace("\r", "\n")
    matches = list(AFD_SECTION.finditer(body))

    head = body[: matches[0].start()] if matches else body
    office_name = None
    issued = None
    for line in head.split("\n"):
        line = line.strip()
        if line.lower().startswith("national weather service"):
            office_name = line
        elif AFD_TIMESTAMP.match(line):
            issued = line

    sections = []
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        chunk = body[start:end].replace("&&", "").replace("$$", "").strip()
        if not chunk:
            continue
        sections.append({"title": match.group(1).strip().title(), "body": chunk})

    if not sections:
        return None

    return {"office_name": office_name, "issued": issued, "sections": sections}


def cod_links(point):
    """Links out to College of DuPage NEXLAB.

    COD publishes no API and reserves rights over its imagery, so this
    hands visitors off to their site rather than pulling anything in.
    Their radar pages key on the station code without its leading K."""
    links = [
        {"label": "Satellite & radar", "url": "https://weather.cod.edu/satrad/"},
        {"label": "Forecast models", "url": "https://weather.cod.edu/forecast/"},
    ]
    radar = point.get("radar")
    if radar and len(radar) == 4:
        links.insert(
            0,
            {
                "label": f"{radar} radar",
                "url": "https://weather.cod.edu/satrad/nexrad/index.php"
                f"?type={radar[1:]}-N0Q-1-24",
            },
        )
    return links


def get_discussion(latitude, longitude):
    """The forecaster's own narrative, or None if unavailable.

    This is a bonus panel on top of a working page, so it swallows every
    error rather than being allowed to take the site down. A slow or
    grumpy NWS server should cost you this section, not the forecast."""
    try:
        point = nws_point(latitude, longitude)
        if point is None:
            return None

        office = point["office"]
        cached = _afd_cache.get(office)
        if cached and time.time() - cached["at"] < AFD_TTL:
            discussion = cached["value"]
        else:
            discussion = parse_afd(fetch_afd(office))
            _afd_cache[office] = {"at": time.time(), "value": discussion}

        if discussion is None:
            return None
        return {**discussion, **point, "links": cod_links(point)}
    except Exception:
        traceback.print_exc()
        return None


# ---------------------------------------------------------------------------
# Shaping the Open-Meteo response for the template
# ---------------------------------------------------------------------------

def current_index(data):
    """Find where 'now' sits in the hourly arrays. The hourly series starts
    at midnight local time, so this is how dew point, UV and visibility get
    lined up with the present moment."""
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    marker = ((data.get("current") or {}).get("time") or "")[:13]
    if not marker or not times:
        return 0
    return next((i for i, t in enumerate(times) if t[:13] >= marker), 0)


def build_hourly(data, index):
    """Pull the next 24 hours out of the response, starting from right now.
    Hours with no temperature reading are dropped rather than carried
    forward as None, which would poison the chart maths downstream."""
    block = data.get("hourly") or {}
    times = block.get("time") or []
    temps = block.get("temperature_2m") or []
    chances = block.get("precipitation_probability") or []
    if not times or not temps:
        return []

    window = []
    for offset in range(24):
        position = index + offset
        when = moment(at(times, position))
        reading = num(at(temps, position))
        if when is None or reading is None:
            continue
        window.append((when, reading, whole(at(chances, position))))

    return [
        {"hour": hour_label(when), "temp": temp, "chance": chance, "index": i}
        for i, (when, temp, chance) in enumerate(window)
    ]


def build_trace(hourly, width=720, height=140, padding=18):
    """Turn the hourly temperatures into SVG coordinates for the line chart.
    Returns None when there's nothing to draw -- guard with {% if trace %}."""
    if not hourly:
        return None

    temps = [point["temp"] for point in hourly]
    low, high = min(temps), max(temps)
    span = high - low or 1  # avoid dividing by zero on a very flat day
    step = width / max(len(hourly) - 1, 1)

    points = []
    for point in hourly:
        x = point["index"] * step
        # SVG y grows downward, so subtract from the height to flip it.
        y = height - padding - ((point["temp"] - low) / span) * (height - padding * 2)
        points.append({"x": round(x, 1), "y": round(y, 1), **point})

    return {
        "points": points,
        "line": " ".join(f"{p['x']},{p['y']}" for p in points),
        "area": (
            f"0,{height} "
            + " ".join(f"{p['x']},{p['y']}" for p in points)
            + f" {width},{height}"
        ),
        "width": width,
        "height": height,
        "high": round(high),
        "low": round(low),
    }


def build_conditions(data, index, units):
    """The detail panel: everything beyond temperature and wind."""
    imperial = units == "imperial"
    hourly = data.get("hourly") or {}
    current = data.get("current") or {}

    uv = num(at(hourly.get("uv_index"), index))

    # Pressure trend: compare now against three hours ago. Falling pressure
    # broadly means weather moving in; rising means it's settling down.
    pressures = hourly.get("pressure_msl") or []
    now_pressure = num(at(pressures, index))
    past_pressure = num(at(pressures, index - 3))
    trend = None
    if now_pressure is not None and past_pressure is not None:
        change = now_pressure - past_pressure
        trend = "rising" if change > 1 else "falling" if change < -1 else "steady"

    return {
        "dew_point": whole(at(hourly.get("dew_point_2m"), index)),
        "visibility": visibility_label(at(hourly.get("visibility"), index), imperial),
        "uv": whole(uv),
        "uv_label": uv_label(uv),
        "cloud_cover": whole(current.get("cloud_cover")),
        "pressure": whole(current.get("pressure_msl")),
        "trend": trend,
        "gusts": whole(current.get("wind_gusts_10m")),
        "direction": compass(current.get("wind_direction_10m")),
        "chance": whole(at(hourly.get("precipitation_probability"), index)),
    }


def build_days(data):
    """Build the 5-day outlook. Days missing a high or a low are skipped
    entirely rather than crashing the whole page."""
    daily = data.get("daily") or {}
    dates = daily.get("time") or []

    rows = []
    for i, date in enumerate(dates):
        high = whole(at(daily.get("temperature_2m_max"), i))
        low = whole(at(daily.get("temperature_2m_min"), i))
        if high is None or low is None:
            continue
        rows.append(
            {
                "date": date,
                "code": at(daily.get("weather_code"), i),
                "high": high,
                "low": low,
                "chance": whole(at(daily.get("precipitation_probability_max"), i)),
            }
        )

    if not rows:
        return []

    floor = min(row["low"] for row in rows)
    ceiling = max(row["high"] for row in rows)
    span = ceiling - floor or 1

    days = []
    for i, row in enumerate(rows):
        when = moment(row["date"])
        days.append(
            {
                "label": "Today" if i == 0 else (when.strftime("%a") if when else ""),
                "summary": describe(row["code"]),
                "chance": row["chance"],
                "high": row["high"],
                "low": row["low"],
                "offset": (row["low"] - floor) / span * 100,
                "length": (row["high"] - row["low"]) / span * 100,
            }
        )
    return days


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    city = request.args.get("city", "Denver").strip() or "Denver"
    units = "metric" if request.args.get("units") == "metric" else "imperial"

    def page(**extra):
        """Every render needs the unit labels and a palette, including the
        error ones -- otherwise a failed search renders unstyled."""
        extra.setdefault(
            "sky", build_sky(3, None, None, None)  # neutral overcast fallback
        )
        return render_template(
            "index.html",
            city=city,
            units=units,
            degree="F" if units == "imperial" else "C",
            speed="mph" if units == "imperial" else "km/h",
            **extra,
        )

    try:
        place = find_place(city)
        if place is None:
            return page(
                error=f"No place called \u201c{city}\u201d turned up. Try adding "
                "a state or country."
            )
        data = get_forecast(place["latitude"], place["longitude"], units)
    except requests.Timeout:
        return page(error="The weather service took too long to answer. Try again.")
    except requests.RequestException:
        return page(
            error="Couldn't reach the weather service. Check your connection "
            "and try again."
        )
    except ValueError:
        return page(error="The weather service sent back something unreadable.")

    try:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        index = current_index(data)
        hourly = build_hourly(data, index)

        # All three are local time for the searched city, because the
        # request asked for timezone=auto. That's what makes the palette
        # reflect the sky there rather than the sky where the server is.
        now = moment(current.get("time"))
        sunrise = moment(at(daily.get("sunrise"), 0))
        sunset = moment(at(daily.get("sunset"), 0))
        code = current.get("weather_code")

        return page(
            place=place,
            hourly=hourly,
            trace=build_trace(hourly),
            days=build_days(data),
            conditions=build_conditions(data, index, units),
            discussion=get_discussion(place["latitude"], place["longitude"]),
            sky=build_sky(code, sunrise, sunset, now),
            arc=sun_arc(sunrise, sunset, now),
            current={
                "temp": whole(current.get("temperature_2m")),
                "feels": whole(current.get("apparent_temperature")),
                "humidity": whole(current.get("relative_humidity_2m")),
                "wind": whole(current.get("wind_speed_10m")),
                "summary": describe(code),
                "is_day": current.get("is_day", 1) == 1,
            },
            sunrise=clock(at(daily.get("sunrise"), 0)),
            sunset=clock(at(daily.get("sunset"), 0)),
        )
    except Exception:
        traceback.print_exc()
        return page(
            error="Something went wrong assembling the forecast. The details "
            "are in the server log."
        )


if __name__ == "__main__":
    app.run(debug=True)
