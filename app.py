"""
A small weather website built with Flask and the Open-Meteo API.

Open-Meteo is free and needs no API key, so this runs as-is.
Start it with:  python app.py
Then open:      http://127.0.0.1:5000
"""

import traceback
from datetime import datetime

import requests
from flask import Flask, render_template, request

app = Flask(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo reports conditions as WMO weather codes. This maps the ones
# you'll actually see to plain English.
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
    """Round to a whole number, but pass None through untouched instead of
    crashing. Templates can test for None with {% if %}."""
    number = num(value)
    return None if number is None else round(number)


def at(values, index):
    """Read one item out of a list without assuming the list is long enough.
    Open-Meteo's daily arrays are occasionally shorter than the date array."""
    if not values or index >= len(values):
        return None
    return values[index]


def hour_label(moment):
    """Format an hour as '2pm'. Written by hand because strftime's
    no-leading-zero flag differs between Windows and everything else."""
    hour = moment.hour % 12 or 12
    return f"{hour}{'am' if moment.hour < 12 else 'pm'}"


def clock(timestamp):
    """Format a timestamp as '6:12 am'. Returns None on missing or malformed
    input -- polar locations genuinely have no sunrise on some dates."""
    if not timestamp:
        return None
    try:
        moment = datetime.fromisoformat(timestamp)
    except (TypeError, ValueError):
        return None
    hour = moment.hour % 12 or 12
    return f"{hour}:{moment.minute:02d} {'am' if moment.hour < 12 else 'pm'}"


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
    """Fetch current conditions, the next 24 hours, and the next 5 days."""
    imperial = units == "imperial"
    response = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
            "wind_speed_10m,weather_code,is_day",
            "hourly": "temperature_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
            "sunrise,sunset",
            "timezone": "auto",
            "forecast_days": 5,
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()


def build_hourly(data):
    """Pull the next 24 hours out of the response, starting from right now.
    Hours with no temperature reading are dropped rather than carried
    forward as None, which would poison the chart maths downstream."""
    block = data.get("hourly") or {}
    times = block.get("time") or []
    temps = block.get("temperature_2m") or []
    if not times or not temps:
        return []

    # The hourly list starts at midnight today, so skip ahead to the
    # current hour before slicing off the next 24 entries.
    now = (data.get("current") or {}).get("time") or ""
    marker = now[:13]  # e.g. "2026-08-03T14"
    start = 0
    if marker:
        start = next((i for i, t in enumerate(times) if t[:13] >= marker), 0)

    window = []
    for time, temp in zip(times[start : start + 24], temps[start : start + 24]):
        reading = num(temp)
        if reading is None:
            continue
        try:
            moment = datetime.fromisoformat(time)
        except (TypeError, ValueError):
            continue
        window.append((moment, reading))

    return [
        {"hour": hour_label(moment), "temp": temp, "index": i}
        for i, (moment, temp) in enumerate(window)
    ]


def build_trace(hourly, width=720, height=120, padding=14):
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
        # Same path, closed along the bottom, so it can be filled.
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
            }
        )

    if not rows:
        return []

    floor = min(row["low"] for row in rows)
    ceiling = max(row["high"] for row in rows)
    span = ceiling - floor or 1

    days = []
    for i, row in enumerate(rows):
        try:
            weekday = datetime.fromisoformat(row["date"]).strftime("%a")
        except (TypeError, ValueError):
            weekday = ""
        days.append(
            {
                "label": "Today" if i == 0 else weekday,
                "summary": describe(row["code"]),
                "high": row["high"],
                "low": row["low"],
                # Percentages that position the range bar within the week's
                # overall spread, so the bars line up visually.
                "offset": (row["low"] - floor) / span * 100,
                "length": (row["high"] - row["low"]) / span * 100,
            }
        )
    return days


@app.route("/")
def home():
    city = request.args.get("city", "Denver").strip() or "Denver"
    units = "metric" if request.args.get("units") == "metric" else "imperial"

    def page(**extra):
        """Every render needs the unit labels, including the error ones --
        the original only passed them on the success path."""
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
        # raised by .json() when the response body isn't valid JSON
        return page(error="The weather service sent back something unreadable.")

    # Anything past this point is our own parsing, not the network. A crash
    # here used to become a blank 500; now the reason gets printed to the
    # terminal and the visitor gets a real sentence.
    try:
        current = data.get("current") or {}
        daily = data.get("daily") or {}
        hourly = build_hourly(data)

        return page(
            place=place,
            hourly=hourly,
            trace=build_trace(hourly),
            days=build_days(data),
            current={
                "temp": whole(current.get("temperature_2m")),
                "feels": whole(current.get("apparent_temperature")),
                "humidity": whole(current.get("relative_humidity_2m")),
                "wind": whole(current.get("wind_speed_10m")),
                "summary": describe(current.get("weather_code")),
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
