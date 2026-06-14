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
#from openant.devices.bike_speed_cadence import BikeCadenceData

# --- CONFIGURATION ---
import config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("telemetry_daemon")

# Due to a bug in the u-blox 7 GPS module I have to send this RESET command
# otherwise it repeats the same GPS location repeatedly (even though it updates the timestamp and says 3d fix)
# As far as I can tell doing it once at the start will fix it 'forever' until reboot
# There is commented out code in the gps_worker that would attempt to detect
# if the GPS data is stale and reset it again, but for now I'm just doing it once at startup since it seems to be working fine
# NOTE sometimes it may fail on first start, it'll output an error if it does
# If so just restart the script and watch for an ACK message
subprocess.run(["ubxtool", "-p", "RESET"], check=True)


# I'm really not sure of this ant reading method.
# it seems to only update every once in a while, possibly randomly, maybe misses things?
# specs say a power meter should have a 4hz update rate (at fastest), I'm seeing like 5 seconds or more

# compared to wahoo which updates seemingly every second-ish the same ant data?
# need to test this while on the trainer and check if it insta-updates while I can watch it directly

# Testing on the trainer shows this seems to be working perfectly, it updates every second or two, which is good enough for our 5s averaging window
# there's also continuous scanning mode but it seems to be completely unecessary based on my testing

# might want to cache data when send fails
# will need some kind of "bulk update" endpoint for that
# just to keep the gps points good
# mind you it'll just 'jump' if not so I don't care that much (speed won't jump cause it's direct from gps)


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
            "elevation": [],
            "speed_kmh": [],
        }

    def add_sample(self, key, value):
        if value is not None:
            with self.lock:
                self.buffers[key].append(value)

    def add_ant_power_data(self, power, cadence):
        """Updates both power and cadence within a single lock window to ensure atomicity."""
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
                self.buffers["elevation"].append(alt)
            
            if speed_ms is not None:
                self.buffers["speed_kmh"].append(speed_ms * 3.6)

    def flush_and_average(self):
        with self.lock:
            def avg(lst):
                # Return None for no new samples, so the backend can preserve the
                # last seen value and mark the metric as stale.
                return round(sum(lst) / len(lst), 2) if lst else None
            
            payload = {
                "timestamp_gps": self.gps_time,
                "gps_fix": self.gps_fix,
                "location": {"lat": self.lat, "lon": self.lon},
                "metrics": {
                    "heart_rate": avg(self.buffers["hr"]),
                    "power": avg(self.buffers["power"]),
                    "cadence": avg(self.buffers["cadence"]),
                    "elevation_m": avg(self.buffers["elevation"]),
                    "speed_kmh": avg(self.buffers["speed_kmh"]),
                }
            }
            # Reset buffers for the next 5s window
            for key in self.buffers: self.buffers[key] = []
            return payload

# Initialize Shared State
aggregator = TelemetryAggregator()
# last_gps = deque(maxlen=10)  # detect GPS staleness if last 10 readings are the same or zero

# --- HARDWARE THREADS ---
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
        # Watch for stale data
        # ubxtool -p RESET or COLDBOOT or WARMBOOT or HOTBOOT ???
        try:
            session = gps(mode=WATCH_ENABLE | WATCH_NEWSTYLE)
            for report in session:
                if report['class'] == 'TPV':
                    fix = getattr(report, 'mode', 0)

                    # ### # Auto fix GPS maybe? ###
                    # if fix > 1 and hasattr(report, 'lat') and hasattr(report, 'lon'):
                    #     last_gps.append((report.lat, report.lon))
                    # ####

                    aggregator.update_gps(
                        lat = getattr(report, 'lat', 0.0),
                        lon = getattr(report, 'lon', 0.0),
                        alt = getattr(report, 'alt', getattr(report, 'altHAE', None)),
                        speed_ms = getattr(report, 'speed', 0.0),
                        fix = fix,
                        timestamp = getattr(report, 'time', "1970-01-01T00:00:00.000Z")
                    )

                # ## More auto-fix GPS stuff ####
                # if len(last_gps) == last_gps.maxlen and all(coord == last_gps[0] for coord in last_gps):
                #     logger.warning("GPS data appears stale. Attempting to reset gps chip...")
                #     session.close()
                
                #     subprocess.run(["ubxtool", "-p", "RESET"], check=True)
                #     break
                # ###

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
            ##################### TESTING ##################
            # logger.info(data)
            # continue
            ####################################################
            
            # We use a timeout to prevent the thread from hanging on network issues
            response = requests.post(config.DESTINATION_URL, json=data, timeout=2.0)
            if response.status_code != 200:
                logger.warning(f"Failed to post: HTTP {response.status_code}")
        except Exception as e:
            logger.error(f"Network error during POST: {e}")

if __name__ == "__main__":
    t_ant = threading.Thread(target=ant_worker, daemon=True)
    t_gps = threading.Thread(target=gps_worker, daemon=True)
    t_post = threading.Thread(target=poster_worker, daemon=True)

    t_ant.start()
    t_gps.start()
    t_post.start()

    # Keep main thread alive
    while True:
        time.sleep(1)
