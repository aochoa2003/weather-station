"""
A small weather website built with Flask and the Open-Meteo API.

Open-Meteo is free and needs no API key, so this runs as-is.
Start it with:  python app.py
Then open:      http://127.0.0.1:5000
"""

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


def hour_label(moment):
    """Format an hour as '2pm'. Written by hand because strftime's
    no-leading-zero flag differs between Windows and everything else."""
    hour = moment.hour % 12 or 12
    return f"{hour}{'am' if moment.hour < 12 else 'pm'}"


def clock(timestamp):
    """Format a timestamp as '6:12 am'."""
    moment = datetime.fromisoformat(timestamp)
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
        "name": place["name"],
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
    """Pull the next 24 hours out of the response, starting from right now."""
    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]

    # The hourly list starts at midnight today, so skip ahead to the
    # current hour before slicing off the next 24 entries.
    now = data["current"]["time"][:13]  # e.g. "2026-08-03T14"
    start = next((i for i, t in enumerate(times) if t[:13] >= now), 0)
    window = list(zip(times[start : start + 24], temps[start : start + 24]))

    return [
        {
            "hour": hour_label(datetime.fromisoformat(time)),
            "temp": temp,
            "index": i,
        }
        for i, (time, temp) in enumerate(window)
    ]


def build_trace(hourly, width=720, height=120, padding=14):
    """Turn the hourly temperatures into SVG coordinates for the line chart."""
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
    daily = data["daily"]
    highs = [round(h) for h in daily["temperature_2m_max"]]
    lows = [round(l) for l in daily["temperature_2m_min"]]
    floor, ceiling = min(lows), max(highs)
    span = ceiling - floor or 1

    days = []
    for i, date in enumerate(daily["time"]):
        days.append(
            {
                "label": "Today"
                if i == 0
                else datetime.fromisoformat(date).strftime("%a"),
                "summary": describe(daily["weather_code"][i]),
                "high": highs[i],
                "low": lows[i],
                # Percentages that position the range bar within the week's
                # overall spread, so the bars line up visually.
                "offset": (lows[i] - floor) / span * 100,
                "length": (highs[i] - lows[i]) / span * 100,
            }
        )
    return days


@app.route("/")
def home():
    city = request.args.get("city", "Denver").strip() or "Denver"
    units = "metric" if request.args.get("units") == "metric" else "imperial"

    try:
        place = find_place(city)
        if place is None:
            return render_template(
                "index.html",
                city=city,
                units=units,
                error=f"No place called \u201c{city}\u201d turned up. Try adding a "
                "state or country.",
            )

        data = get_forecast(place["latitude"], place["longitude"], units)
    except requests.RequestException:
        return render_template(
            "index.html",
            city=city,
            units=units,
            error="Couldn't reach the weather service. Check your connection "
            "and try again.",
        )

    current = data["current"]
    hourly = build_hourly(data)

    return render_template(
        "index.html",
        city=city,
        units=units,
        place=place,
        hourly=hourly,
        trace=build_trace(hourly),
        days=build_days(data),
        current={
            "temp": round(current["temperature_2m"]),
            "feels": round(current["apparent_temperature"]),
            "humidity": round(current["relative_humidity_2m"]),
            "wind": round(current["wind_speed_10m"]),
            "summary": describe(current["weather_code"]),
            "is_day": current["is_day"] == 1,
        },
        sunrise=clock(data["daily"]["sunrise"][0]),
        sunset=clock(data["daily"]["sunset"][0]),
        degree="F" if units == "imperial" else "C",
        speed="mph" if units == "imperial" else "km/h",
    )


if __name__ == "__main__":
    app.run(debug=True)
