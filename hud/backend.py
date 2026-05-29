# Backend API for the HUD application
# Recieves data from telemetry daemon on belabox and serves it to the frontend, while also maintaining session state (distance, climb, etc.)

from fastapi import FastAPI, File
from fastapi.responses import HTMLResponse
from datetime import datetime, timedelta
import uvicorn
import math
from collections import deque
import os
import requests

app = FastAPI()

# Internal rolling buffer for smoother grade calculation
grade_history = deque(maxlen=10)

# In-memory storage for the current session
session_data = {
    "points": [], # List of [lat, lon]
    "total_distance_m": 0.0,
    "total_climb_m": 0.0,
    "last_metrics": {},
    "weather_cache": None,
    "start_time": datetime.now(),
    "last_update_time": None  # Track when last telemetry was received
}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def calculate_current_grade(lat, lon, alt, speed_ms):
    # Rolling grade calculation logic
    grade_history.append((lat, lon, alt))
    if len(grade_history) == grade_history.maxlen:
        old_lat, old_lon, old_alt = grade_history[0]
        # Haversine distance
        dist = haversine(old_lat, old_lon, lat, lon)
        if dist > 5.0 and speed_ms > 1.0:
            grade = ((alt - old_alt) / dist) * 100
            session_data["last_metrics"]["metrics"]["grade_percent"] = grade

@app.post("/api/v1/telemetry")
async def receive_telemetry(data: dict):
    global session_data
    
    # Extract metrics regardless of GPS fix status
    metrics = data['metrics']
    previous_metrics = session_data["last_metrics"].get("metrics", {}) if session_data["last_metrics"] else {}
    merged_metrics = {}
    metrics_stale = {}
    for key in ["heart_rate", "power", "cadence", "elevation_m", "speed_kmh"]:
        value = metrics.get(key)
        if value is None:
            merged_metrics[key] = previous_metrics.get(key)
            metrics_stale[key] = True
        else:
            merged_metrics[key] = value
            metrics_stale[key] = False
    
    # Always update metrics and timestamp
    session_data["last_update_time"] = datetime.now()
    session_data["last_metrics"] = {
        "timestamp_gps": data.get("timestamp_gps"),
        "gps_fix": data.get("gps_fix"),
        "location": data.get("location"),
        "metrics": merged_metrics,
        "metrics_stale": metrics_stale
    }
    
    # Check GPS fix: only add points if fix is 2D (2) or 3D (3)
    gps_fix = data.get("gps_fix", 0)
    if gps_fix < 2:
        return {"status": "ok", "reason": "No valid GPS fix, metrics updated only"}
    
    # Extract current position
    new_point = [data['location']['lat'], data['location']['lon']]
    
    # Cumulative Distance & Climb logic
    if session_data["points"]:
        last_p = session_data["points"][-1]
        session_data["total_distance_m"] += haversine(last_p[0], last_p[1], new_point[0], new_point[1])

        if gps_fix >= 3:  # Only consider elevation if we have a 3D fix
            calculate_current_grade(new_point[0], new_point[1], 
                                    merged_metrics.get("elevation_m", 0), 
                                    merged_metrics.get("speed_kmh", 0) / 3.6)
            last_alt = previous_metrics.get("elevation_m", 0)
            curr_alt = merged_metrics.get("elevation_m", 0)
            if curr_alt is not None and curr_alt > last_alt:
                session_data["total_climb_m"] += (curr_alt - last_alt)

    session_data["points"].append(new_point)
    return {"status": "ok"}


@app.get("/api/v1/session")
async def get_session():
    # Mark all metrics as stale if no update in 30 seconds
    if session_data["last_update_time"] and datetime.now() - session_data["last_update_time"] > timedelta(seconds=30):
        if session_data["last_metrics"].get("metrics_stale"):
            for key in session_data["last_metrics"]["metrics_stale"]:
                session_data["last_metrics"]["metrics_stale"][key] = True
    
    # print(session_data["last_metrics"])
    return session_data


def fetch_weather_for_location(location):
    """Fetch weather from PirateWeather (or similar) and cache result.

    Expects `location` as dict with `lat` and `lon`. API key pulled from
    `PIRATEWEATHER_KEY` env var. Returns parsed JSON or None on failure.
    """
    key = os.environ.get('PIRATEWEATHER_KEY')
    if not key:
        return {"error": "no_api_key"}

    lat = location.get('lat') if location else None
    lon = location.get('lon') if location else None
    if lat is None or lon is None:
        return {"error": "no_location"}

    url = f"https://api.pirateweather.net/forecast/{key}/{lat},{lon}?units=si&exclude=minutely,alerts,flags"
    try:
        resp = requests.get(url, timeout=6)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        return {"error": "fetch_failed", "reason": str(e)}


@app.get("/api/v1/weather")
async def get_weather():
    """Return cached weather; refresh from PirateWeather if older than 60 minutes."""
    cache = session_data.get('weather_cache') or {}
    if cache and cache.get('ts') and datetime.now() - cache['ts'] < timedelta(minutes=60):
        return cache.get('data', {})

    # Determine location from last_metrics
    location = session_data.get('last_metrics', {}).get('location')
    data = fetch_weather_for_location(location)
    if not data.get('error'):
        session_data['weather_cache'] = {'ts': datetime.now(), 'data': data}
    return data

@app.get("/api/v1/reset")
async def reset_session():
    global session_data
    global grade_history

    grade_history = deque(maxlen=10)
    session_data = {
        "points": [],
        "total_distance_m": 0.0,
        "total_climb_m": 0.0,
        "last_metrics": {},
        "start_time": datetime.now()
    }
    return {"status": "session reset"}

@app.get("/")
async def get_hud():
    with open('hud.html', 'r', encoding='utf-8') as file:
        html_content = file.read()
    return HTMLResponse(content=html_content)


uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
