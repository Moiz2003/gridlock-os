"""
time_series.py — InfluxDB Writer
Pushes each engine cycle's telemetry snapshot into the GridLock bucket.
All other modules are completely unaware of InfluxDB — this is the only file
that touches the database.
"""

import logging
from datetime import datetime, timezone

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import config

log = logging.getLogger("gridlock.db")


def write_snapshot(
    battery_soc: float,
    cloud_cover: int,
    ac_power: bool,
    ac_temp_setpoint: int,
    predicted_soc_at_1700: float = 0.0,
) -> None:
    """
    Writes a single time-series data point to InfluxDB.

    Fields:
        battery_soc            — Battery state of charge (%)
        cloud_cover            — Cloud cover from OWM (%)
        ac_power               — Whether both ACs are on (bool → int 0/1)
        ac_temp_setpoint       — Temperature setpoint sent to the ACs (°C)
        predicted_soc_at_1700  — Model/heuristic SoC prediction for 17:00 (%)
    """
    point = (
        Point("gridlock_snapshot")
        .tag("location", "home")
        .field("battery_soc", battery_soc)
        .field("cloud_cover", cloud_cover)
        .field("ac_power", int(ac_power))
        .field("ac_temp_setpoint", ac_temp_setpoint)
        .field("predicted_soc_at_1700", predicted_soc_at_1700)
        .time(datetime.now(tz=timezone.utc), WritePrecision.S)
    )

    try:
        with InfluxDBClient(
            url=config.INFLUXDB_URL,
            token=config.INFLUXDB_TOKEN,
            org=config.INFLUXDB_ORG,
        ) as client:
            write_api = client.write_api(write_options=SYNCHRONOUS)
            write_api.write(bucket=config.INFLUXDB_BUCKET, record=point)
            log.debug("Snapshot written to InfluxDB.")

    except Exception as exc:
        # Database unavailability should not crash the engine cycle
        log.error("Failed to write snapshot to InfluxDB: %s", exc)
