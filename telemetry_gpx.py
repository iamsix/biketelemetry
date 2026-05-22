#!/usr/bin/env python3
"""Replay GPX trackpoints as realtime telemetry to the backend."""

import argparse
import datetime
import math
import time
import xml.etree.ElementTree as ET

import requests
import config


def parse_iso8601(timestamp_text):
    if not timestamp_text:
        return None

    value = timestamp_text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"

    try:
        return datetime.datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def extract_trackpoints(gpx_path):
    tree = ET.parse(gpx_path)
    root = tree.getroot()
    points = []

    for element in root.iter():
        if element.tag.endswith("trkpt"):
            lat = float(element.attrib.get("lat", "0"))
            lon = float(element.attrib.get("lon", "0"))
            ele = None
            ts = None
            hr = None
            cadence = None
            power = None

            for child in element.iter():
                if child is element:
                    continue
                if child.tag.endswith("ele") and child.text:
                    try:
                        ele = float(child.text.strip())
                    except ValueError:
                        ele = None
                elif child.tag.endswith("time") and child.text:
                    ts = parse_iso8601(child.text)
                elif child.tag.endswith("hr") and child.text:
                    try:
                        hr = int(child.text.strip())
                    except ValueError:
                        hr = None
                elif child.tag.endswith("cad") and child.text:
                    try:
                        cadence = int(child.text.strip())
                    except ValueError:
                        cadence = None
                elif child.tag.endswith("power") and child.text:
                    try:
                        power = int(child.text.strip())
                    except ValueError:
                        power = None

            points.append({
                "lat": lat,
                "lon": lon,
                "ele": ele,
                "time": ts,
                "heart_rate": hr,
                "cadence": cadence,
                "power": power,
            })

    if not points:
        raise ValueError("No trackpoints found in GPX file")

    return points


def build_payload(point, speed_kmh):
    timestamp = point["time"]
    if timestamp is None:
        timestamp = datetime.datetime.now(datetime.timezone.utc)
    elif timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=datetime.timezone.utc)

    return {
        "timestamp_gps": timestamp.isoformat().replace("+00:00", "Z"),
        "gps_fix": 3,
        "location": {"lat": point["lat"], "lon": point["lon"]},
        "metrics": {
            "heart_rate": point.get("heart_rate"),
            "power": point.get("power"),
            "cadence": point.get("cadence"),
            "elevation_m": point["ele"],
            "speed_kmh": round(speed_kmh, 2) if speed_kmh is not None else None,
        },
    }


def send_payload(url, payload, timeout=5.0):
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()
    return response


def replay_gpx(gpx_path, destination, time_factor, interval_seconds):
    points = extract_trackpoints(gpx_path)
    previous = None

    for index, point in enumerate(points):
        if previous is not None:
            if previous["time"] is not None and point["time"] is not None:
                delta = (point["time"] - previous["time"]).total_seconds()
            else:
                delta = interval_seconds

            sleep_seconds = max(delta / time_factor, 0.0)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        if previous is None:
            speed_kmh = 0.0
        else:
            elapsed = interval_seconds if previous["time"] is None or point["time"] is None else max((point["time"] - previous["time"]).total_seconds(), 0.0001)
            distance_m = haversine(previous["lat"], previous["lon"], point["lat"], point["lon"])
            speed_kmh = (distance_m / elapsed) * 3.6 if elapsed > 0 else 0.0

        payload = build_payload(point, speed_kmh)
        print(f"Sending point {index + 1}/{len(points)}: lat={point['lat']:.6f} lon={point['lon']:.6f} speed={payload['metrics']['speed_kmh']} km/h time={payload['timestamp_gps']}")
        send_payload(destination, payload)
        previous = point

    print("GPX replay complete.")


def main():
    parser = argparse.ArgumentParser(description="Replay GPX data as realtime telemetry to the backend.")
    parser.add_argument("gpx_file", help="Path to the GPX file to replay")
    parser.add_argument("--destination", default=config.DESTINATION_URL, help="Telemetry POST destination URL")
    parser.add_argument("--time-factor", type=float, default=1.0, help="Playback speed factor: 1.0 = realtime, 2.0 = twice as fast")
    parser.add_argument("--interval", type=float, default=1.0, help="Default interval in seconds when the GPX trackpoint has no timestamp")
    args = parser.parse_args()

    replay_gpx(args.gpx_file, args.destination, args.time_factor, args.interval)


if __name__ == "__main__":
    main()
