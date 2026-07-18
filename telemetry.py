## This file lives on a remote belabox device and sends telemetry to the backend.py server

import time
import requests
import subprocess
import threading
import logging
from gps import gps, WATCH_ENABLE, WATCH_NEWSTYLE

# OpenANT imports
from openant.easy.node import Node
from openant.devices import ANTPLUS_NETWORK_KEY
from openant.devices.heart_rate import HeartRate
from openant.devices.power_meter import PowerMeter

# --- CONFIGURATION ---
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("telemetry_daemon")

def run_ubxtool_reset(max_retries=3, delay_seconds=2):
    """Run ubxtool RESET and retry if the command fails on first attempt."""
    for attempt in range(1, max_retries + 1):
        logger.info(f"Running ubxtool RESET (attempt {attempt}/{max_retries})")
        result = subprocess.run(
            ["ubxtool", "-p", "RESET"],
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:
            logger.info("ubxtool RESET succeeded")
            return True

        error_message = result.stderr.strip() or result.stdout.strip()
        logger.warning(
            "ubxtool RESET failed: %s%s",
            error_message,
            "; retrying..." if attempt < max_retries else ""
        )

        if attempt < max_retries:
            time.sleep(delay_seconds)

    logger.error("ubxtool RESET failed after %s attempts", max_retries)
    return False

run_ubxtool_reset()

class TelemetryAggregator:
    def __init__(self):
        self.lock = threading.Lock()
        # GPS State
        self.lat = 0.0
        self.lon = 0.0
        self.gps_time = "1970-01-01T00:00:00Z"
        self.gps_fix = 0 # 0=no fix, 2=2D, 3=3D
        
        # Buffers for averaging
        self.buffers = {
            "hr": [],
            "power": [],
            "cadence": [],
            "gps_elevation": [],  # Separated GPS elevation
            "dps_elevation": [],  # High priority Baro elevation
            "speed_kmh": [],
        }

    def add_sample(self, key, value):
        if value is not None:
            with self.lock:
                self.buffers[key].append(value)

    def add_ant_power_data(self, power, cadence):
        logger.debug(f"PW update: {power}W - {cadence}rpm")
        with self.lock:
            if power is not None:
                self.buffers["power"].append(power)
            if cadence is not None:
                self.buffers["cadence"].append(cadence)

    def update_gps(self, lat, lon, alt, speed_ms, fix, timestamp):
        with self.lock:
            self.lat = lat
            self.lon = lon
            self.gps_fix = fix
            self.gps_time = timestamp
            
            if alt is not None:
                self.buffers["gps_elevation"].append(alt)
            
            if speed_ms is not None:
                self.buffers["speed_kmh"].append(speed_ms * 3.6)

    def add_dps_elevation(self, alt):
        """Dedicated method for adding barometric altitude."""
        if alt is not None:
            with self.lock:
                self.buffers["dps_elevation"].append(alt)

    def flush_and_average(self):
        with self.lock:
            def avg(lst):
                return round(sum(lst) / len(lst), 2) if lst else None
            
            # --- ELEVATION PRIORITY LOGIC ---
            # If we have DPS310 readings in this window, use them. Otherwise fallback to GPS.
            dps_avg = avg(self.buffers["dps_elevation"])
            gps_avg = avg(self.buffers["gps_elevation"])
            final_elevation = dps_avg if dps_avg is not None else gps_avg
            
            payload = {
                "timestamp_gps": self.gps_time,
                "gps_fix": self.gps_fix,
                "location": {"lat": self.lat, "lon": self.lon},
                "metrics": {
                    "heart_rate": avg(self.buffers["hr"]),
                    "power": avg(self.buffers["power"]),
                    "cadence": avg(self.buffers["cadence"]),
                    "elevation_m": final_elevation, 
                    "speed_kmh": avg(self.buffers["speed_kmh"]),
                }
            }
            # Reset buffers for the next 5s window
            for key in self.buffers: self.buffers[key] = []
            return payload

# Initialize Shared State
aggregator = TelemetryAggregator()

# --- HARDWARE THREADS ---
def dps_worker():
    """Worker thread to read altitude from I2C DPS310."""
    try:
        import board
        from adafruit_dps310.basic import DPS310
        
        # Initializes I2C using the SBC's default bus
        i2c = board.I2C() 
        dps310 = DPS310(i2c)
        
        # Barometric altitude requires a reference sea level pressure to be absolutely accurate.
        # 1013.25 is standard. If you notice your altitude is consistently off by a static amount,
        # you can tune this value to match your local QNH.
        dps310.sea_level_pressure = 1013.25
        logger.info("DPS310 Barometric Sensor initialized successfully.")

        while True:
            try:
                alt = dps310.altitude
                if alt is not None:
                    aggregator.add_dps_elevation(alt)
            except Exception as e:
                logger.debug(f"DPS310 read error (I2C glitch?): {e}")
            
            time.sleep(1) # 1Hz read rate is plenty for elevation 

    except Exception as e:
        logger.error(f"DPS310 Thread failed to initialize. Ensure I2C is enabled and wired correctly: {e}")

def ant_worker():
    def on_hr(pg, name, d): 
        hr = getattr(d, 'heart_rate', None)
        if hr is not None:
            aggregator.add_sample("hr", d.heart_rate)
            
    def on_pw(pg, name, d):
        try:
            power = getattr(d, 'instantaneous_power', None)
            cadence = getattr(d, 'cadence', None)
            if power is None:
                power = getattr(d, 'power', None)
            aggregator.add_ant_power_data(power, cadence)

            if not power and not cadence:
                torque = getattr(d, 'accumulated_torque', None)
                logger.debug(f"Got power page {pg} {name} - maybe torque update and I have to calc it manually?: {torque}")
                
        except Exception as e:
            logger.error(f"Error in power callback: {e}")

    try:
        node = Node()
        node.set_network_key(0x00, ANTPLUS_NETWORK_KEY)
        HeartRate(node, device_id=config.HR_ID).on_device_data = on_hr
        PowerMeter(node, device_id=config.PWR_ID).on_device_data = on_pw
        node.start()
    except Exception as e:
        logger.error(f"ANT+ Thread failed: {e}")

def gps_worker():
    while True:
        try:
            session = gps(mode=WATCH_ENABLE | WATCH_NEWSTYLE)
            for report in session:
                if report['class'] == 'TPV':
                    fix = getattr(report, 'mode', 0)

                    aggregator.update_gps(
                        lat = getattr(report, 'lat', 0.0),
                        lon = getattr(report, 'lon', 0.0),
                        alt = getattr(report, 'alt', getattr(report, 'altHAE', None)),
                        speed_ms = getattr(report, 'speed', 0.0),
                        fix = fix,
                        timestamp = getattr(report, 'time', "1970-01-01T00:00:00.000Z")
                    )
        except Exception as e:
            logger.error(f"GPS Thread failed: {e}")

        logger.info("Reconnecting to gpsd in 5 seconds...")
        time.sleep(5)

# --- POSTING THREAD ---
def poster_worker():
    logger.info(f"Reporter thread started. Target: {config.DESTINATION_URL}")
    while True:
        time.sleep(config.POST_INTERVAL)
        data = aggregator.flush_and_average()
        
        try:
            response = requests.post(config.DESTINATION_URL, json=data, timeout=2.0)
            if response.status_code != 200:
                logger.warning(f"Failed to post: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"Network error during POST: {e}")

if __name__ == "__main__":
    t_ant = threading.Thread(target=ant_worker, daemon=True)
    t_gps = threading.Thread(target=gps_worker, daemon=True)
    t_dps = threading.Thread(target=dps_worker, daemon=True)
    t_post = threading.Thread(target=poster_worker, daemon=True)

    t_ant.start()
    t_gps.start()
    t_dps.start()
    t_post.start()

    # Keep main thread alive
    while True:
        time.sleep(1)
