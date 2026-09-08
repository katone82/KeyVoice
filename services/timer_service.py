#!/usr/bin/env python3

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Dict, Optional

import paho.mqtt.client as mqtt
import uvicorn

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ============================================================
# CONFIGURATION
# ============================================================

HOST = "0.0.0.0"
PORT = 8090

# MQTT
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "zigbee2mqtt")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "doongle")

MQTT_BASE_TOPIC = "keyvoice/timer"

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("keyvoice-timer")


# ============================================================
# MODELS
# ============================================================

@dataclass
class Timer:
    id: str
    name: Optional[str]

    duration: float
    remaining: float

    end_time: Optional[float]

    status: str

    created_at: float

    def get_remaining(self) -> float:

        if self.status == "active" and self.end_time is not None:
            return max(
                0,
                self.end_time - time.monotonic()
            )

        return max(0, self.remaining)

    def to_dict(self):

        remaining = self.get_remaining()

        return {
            "id": self.id,
            "name": self.name,
            "duration": round(self.duration, 1),
            "remaining": round(remaining, 1),
            "remaining_seconds": int(remaining),
            "status": self.status,
            "created_at": self.created_at,
        }


# ============================================================
# REQUEST MODELS
# ============================================================

class CreateTimerRequest(BaseModel):
    duration: float = Field(gt=0)
    name: Optional[str] = None


class AddTimeRequest(BaseModel):
    seconds: float = Field(gt=0)


# ============================================================
# MQTT MANAGER
# ============================================================

class MQTTManager:

    def __init__(self):

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id="keyvoice-timer-service",
        )

        if MQTT_USERNAME:
            self.client.username_pw_set(
                MQTT_USERNAME,
                MQTT_PASSWORD,
            )

        self.connected = False

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    def connect(self):

        logger.info(
            "Connecting to MQTT %s:%s",
            MQTT_HOST,
            MQTT_PORT,
        )

        try:

            self.client.connect(
                MQTT_HOST,
                MQTT_PORT,
                keepalive=60,
            )

            self.client.loop_start()

        except Exception:

            logger.exception(
                "Unable to connect to MQTT"
            )

    # --------------------------------------------------------
    # CALLBACK
    # --------------------------------------------------------

    def _on_connect(
        self,
        client,
        userdata,
        flags,
        reason_code,
        properties=None,
    ):

        if reason_code == 0:

            self.connected = True

            logger.info(
                "Connected to MQTT"
            )

        else:

            logger.error(
                "MQTT connection failed: %s",
                reason_code,
            )

    # --------------------------------------------------------

    def _on_disconnect(
        self,
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties=None,
    ):

        self.connected = False

        logger.warning(
            "Disconnected from MQTT"
        )

    # --------------------------------------------------------
    # PUBLISH
    # --------------------------------------------------------

    def publish(
        self,
        topic: str,
        payload,
        retain: bool = False,
    ):

        if not self.connected:
            return

        if not isinstance(payload, str):

            payload = json.dumps(
                payload,
                ensure_ascii=False,
            )

        self.client.publish(
            topic,
            payload,
            qos=1,
            retain=retain,
        )

    # --------------------------------------------------------
    # DISCOVERY
    # --------------------------------------------------------

    def publish_discovery(
        self,
        timer: Timer,
    ):

        object_id = (
            f"keyvoice_timer_{timer.id.replace('-', '_')}"
        )

        discovery_topic = (
            f"homeassistant/sensor/{object_id}/config"
        )

        state_topic = (
            f"{MQTT_BASE_TOPIC}/{timer.id}/state"
        )

        payload = {

            "name": (
                timer.name
                if timer.name
                else "KeyVoice Timer"
            ),

            "unique_id": object_id,

            "state_topic": state_topic,

            "value_template":
                "{{ value_json.remaining_seconds }}",

            "unit_of_measurement": "s",

            "icon": "mdi:timer-outline",

            "device": {
                "identifiers": [
                    "keyvoice_timer_service"
                ],
                "name": "KeyVoice Timer",
                "manufacturer": "KeyVoice",
                "model": "Timer Service",
            },

            "json_attributes_topic": state_topic,

        }

        self.publish(
            discovery_topic,
            payload,
            retain=True,
        )

        logger.info(
            "MQTT discovery published: %s",
            timer.id,
        )

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    def publish_timer(
        self,
        timer: Timer,
    ):

        state_topic = (
            f"{MQTT_BASE_TOPIC}/{timer.id}/state"
        )

        self.publish(
            state_topic,
            timer.to_dict(),
            retain=True,
        )

    # --------------------------------------------------------
    # EVENT
    # --------------------------------------------------------

    def publish_event(
        self,
        timer: Timer,
        event: str,
    ):

        event_topic = (
            f"{MQTT_BASE_TOPIC}/{timer.id}/event"
        )

        payload = {
            "event": event,
            "id": timer.id,
            "name": timer.name,
            "status": timer.status,
            "timestamp": time.time(),
        }

        self.publish(
            event_topic,
            payload,
            retain=False,
        )


# ============================================================
# TIMER MANAGER
# ============================================================

class TimerManager:

    def __init__(
        self,
        mqtt_manager: MQTTManager,
    ):

        self.timers: Dict[str, Timer] = {}

        self.lock = asyncio.Lock()

        self.mqtt = mqtt_manager

    # --------------------------------------------------------
    # CREATE
    # --------------------------------------------------------

    async def create(
        self,
        duration: float,
        name: Optional[str] = None,
    ) -> Timer:

        timer_id = str(uuid.uuid4())

        now = time.monotonic()

        timer = Timer(
            id=timer_id,
            name=name,
            duration=duration,
            remaining=duration,
            end_time=now + duration,
            status="active",
            created_at=time.time(),
        )

        async with self.lock:
            self.timers[timer_id] = timer

        logger.info(
            "Timer created: id=%s name=%s duration=%ss",
            timer_id,
            name,
            duration,
        )

        self.mqtt.publish_discovery(timer)
        self.mqtt.publish_timer(timer)

        self.mqtt.publish_event(
            timer,
            "started",
        )

        return timer

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    async def get(
        self,
        timer_id: str,
    ) -> Timer:

        async with self.lock:

            timer = self.timers.get(
                timer_id
            )

        if timer is None:
            raise KeyError(timer_id)

        return timer

    # --------------------------------------------------------
    # LIST
    # --------------------------------------------------------

    async def list(self):

        async with self.lock:
            return list(self.timers.values())

    # --------------------------------------------------------
    # PAUSE
    # --------------------------------------------------------

    async def pause(
        self,
        timer_id: str,
    ) -> Timer:

        timer = await self.get(timer_id)

        if timer.status != "active":
            return timer

        timer.remaining = timer.get_remaining()

        timer.end_time = None

        timer.status = "paused"

        logger.info(
            "Timer paused: %s",
            timer_id,
        )

        self.mqtt.publish_timer(timer)

        self.mqtt.publish_event(
            timer,
            "paused",
        )

        return timer

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    async def resume(
        self,
        timer_id: str,
    ) -> Timer:

        timer = await self.get(timer_id)

        if timer.status != "paused":
            return timer

        timer.end_time = (
            time.monotonic()
            + timer.remaining
        )

        timer.status = "active"

        logger.info(
            "Timer resumed: %s",
            timer_id,
        )

        self.mqtt.publish_timer(timer)

        self.mqtt.publish_event(
            timer,
            "resumed",
        )

        return timer

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    async def cancel(
        self,
        timer_id: str,
    ) -> Timer:

        timer = await self.get(timer_id)

        timer.remaining = 0
        timer.end_time = None
        timer.status = "cancelled"

        logger.info(
            "Timer cancelled: %s",
            timer_id,
        )

        self.mqtt.publish_timer(timer)

        self.mqtt.publish_event(
            timer,
            "cancelled",
        )

        return timer

    # --------------------------------------------------------
    # ADD TIME
    # --------------------------------------------------------

    async def add_time(
        self,
        timer_id: str,
        seconds: float,
    ) -> Timer:

        timer = await self.get(timer_id)

        if timer.status == "active":

            timer.end_time += seconds

        elif timer.status == "paused":

            timer.remaining += seconds

        else:

            raise ValueError(
                "Cannot add time to a finished or cancelled timer"
            )

        timer.duration += seconds

        logger.info(
            "Added %ss to timer %s",
            seconds,
            timer_id,
        )

        self.mqtt.publish_timer(timer)

        self.mqtt.publish_event(
            timer,
            "time_added",
        )

        return timer

    # --------------------------------------------------------
    # FINISH
    # --------------------------------------------------------

    async def finish(
        self,
        timer: Timer,
    ):

        timer.remaining = 0
        timer.end_time = None
        timer.status = "finished"

        logger.info(
            "Timer finished: id=%s name=%s",
            timer.id,
            timer.name,
        )

        self.mqtt.publish_timer(timer)

        self.mqtt.publish_event(
            timer,
            "finished",
        )

    # --------------------------------------------------------
    # MONITOR
    # --------------------------------------------------------

    async def monitor(self):

        logger.info(
            "Timer monitor started"
        )

        last_publish = {}

        while True:

            try:

                timers = await self.list()

                now = time.monotonic()

                for timer in timers:

                    if (
                        timer.status == "active"
                        and timer.end_time is not None
                    ):

                        remaining = timer.get_remaining()

                        # Publish countdown every second.
                        current_second = int(
                            remaining
                        )

                        previous_second = (
                            last_publish.get(
                                timer.id
                            )
                        )

                        if (
                            previous_second
                            != current_second
                        ):

                            last_publish[
                                timer.id
                            ] = current_second

                            self.mqtt.publish_timer(
                                timer
                            )

                        # Timer finished.
                        if now >= timer.end_time:

                            await self.finish(
                                timer
                            )

            except Exception:

                logger.exception(
                    "Error in timer monitor"
                )

            await asyncio.sleep(0.1)


# ============================================================
# MQTT
# ============================================================

mqtt_manager = MQTTManager()

timer_manager = TimerManager(
    mqtt_manager
)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="KeyVoice Timer Service",
    version="1.1.0",
)


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    mqtt_manager.connect()

    asyncio.create_task(
        timer_manager.monitor()
    )

    logger.info(
        "KeyVoice Timer Service started"
    )


# ============================================================
# HEALTH
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "keyvoice-timer",
        "mqtt_connected": mqtt_manager.connected,
    }


# ============================================================
# CREATE
# ============================================================

@app.post("/timers")
async def create_timer(
    request: CreateTimerRequest,
):

    timer = await timer_manager.create(
        duration=request.duration,
        name=request.name,
    )

    return timer.to_dict()


# ============================================================
# LIST
# ============================================================

@app.get("/timers")
async def list_timers():

    timers = await timer_manager.list()

    return [
        timer.to_dict()
        for timer in timers
    ]


# ============================================================
# GET
# ============================================================

@app.get("/timers/{timer_id}")
async def get_timer(
    timer_id: str,
):

    try:

        timer = await timer_manager.get(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ============================================================
# PAUSE
# ============================================================

@app.post("/timers/{timer_id}/pause")
async def pause_timer(
    timer_id: str,
):

    try:

        timer = await timer_manager.pause(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ============================================================
# RESUME
# ============================================================

@app.post("/timers/{timer_id}/resume")
async def resume_timer(
    timer_id: str,
):

    try:

        timer = await timer_manager.resume(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ============================================================
# CANCEL
# ============================================================

@app.post("/timers/{timer_id}/cancel")
async def cancel_timer(
    timer_id: str,
):

    try:

        timer = await timer_manager.cancel(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ============================================================
# ADD TIME
# ============================================================

@app.post("/timers/{timer_id}/add")
async def add_time(
    timer_id: str,
    request: AddTimeRequest,
):

    try:

        timer = await timer_manager.add_time(
            timer_id,
            request.seconds,
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )

    except ValueError as exc:

        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    uvicorn.run(
        app,
        host=HOST,
        port=PORT,
    )