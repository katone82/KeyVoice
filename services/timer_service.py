#!/usr/bin/env python3

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn


# ============================================================
# CONFIGURATION
# ============================================================

HOST = "0.0.0.0"
PORT = 8090

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

    # Remaining time when paused.
    remaining: float

    # Timestamp at which the current running period expires.
    end_time: Optional[float]

    status: str

    created_at: float

    def to_dict(self):
        remaining = self.remaining

        if self.status == "active" and self.end_time is not None:
            remaining = max(0, self.end_time - time.monotonic())

        return {
            "id": self.id,
            "name": self.name,
            "duration": self.duration,
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
# TIMER MANAGER
# ============================================================

class TimerManager:

    def __init__(self):
        self.timers: Dict[str, Timer] = {}
        self.lock = asyncio.Lock()

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

        return timer

    # --------------------------------------------------------
    # GET
    # --------------------------------------------------------

    async def get(self, timer_id: str) -> Timer:

        async with self.lock:
            timer = self.timers.get(timer_id)

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

    async def pause(self, timer_id: str) -> Timer:

        timer = await self.get(timer_id)

        if timer.status != "active":
            return timer

        timer.remaining = max(
            0,
            timer.end_time - time.monotonic()
        )

        timer.end_time = None
        timer.status = "paused"

        logger.info("Timer paused: %s", timer_id)

        return timer

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    async def resume(self, timer_id: str) -> Timer:

        timer = await self.get(timer_id)

        if timer.status != "paused":
            return timer

        timer.end_time = (
            time.monotonic() + timer.remaining
        )

        timer.status = "active"

        logger.info("Timer resumed: %s", timer_id)

        return timer

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    async def cancel(self, timer_id: str) -> Timer:

        timer = await self.get(timer_id)

        timer.end_time = None
        timer.remaining = 0
        timer.status = "cancelled"

        logger.info("Timer cancelled: %s", timer_id)

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

        return timer

    # --------------------------------------------------------
    # FINISH
    # --------------------------------------------------------

    async def finish(self, timer: Timer):

        timer.remaining = 0
        timer.end_time = None
        timer.status = "finished"

        logger.info(
            "Timer finished: id=%s name=%s",
            timer.id,
            timer.name,
        )

        # ----------------------------------------------------
        # FUTURE:
        #
        # publish MQTT event
        #
        # timer/finished
        #
        # or notify Home Assistant
        # ----------------------------------------------------

    # --------------------------------------------------------
    # BACKGROUND LOOP
    # --------------------------------------------------------

    async def monitor(self):

        logger.info("Timer monitor started")

        while True:

            try:

                timers = await self.list()

                now = time.monotonic()

                for timer in timers:

                    if (
                        timer.status == "active"
                        and timer.end_time is not None
                        and now >= timer.end_time
                    ):
                        await self.finish(timer)

            except Exception:
                logger.exception(
                    "Error in timer monitor"
                )

            await asyncio.sleep(0.25)


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="KeyVoice Timer Service",
    version="1.0.0",
)

manager = TimerManager()


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():

    asyncio.create_task(
        manager.monitor()
    )

    logger.info(
        "KeyVoice Timer Service started"
    )


# ============================================================
# API
# ============================================================

@app.get("/health")
async def health():

    return {
        "status": "ok",
        "service": "keyvoice-timer",
    }


# ------------------------------------------------------------
# CREATE TIMER
# ------------------------------------------------------------

@app.post("/timers")
async def create_timer(
    request: CreateTimerRequest,
):

    timer = await manager.create(
        duration=request.duration,
        name=request.name,
    )

    return timer.to_dict()


# ------------------------------------------------------------
# LIST TIMERS
# ------------------------------------------------------------

@app.get("/timers")
async def list_timers():

    timers = await manager.list()

    return [
        timer.to_dict()
        for timer in timers
    ]


# ------------------------------------------------------------
# GET TIMER
# ------------------------------------------------------------

@app.get("/timers/{timer_id}")
async def get_timer(
    timer_id: str,
):

    try:

        timer = await manager.get(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ------------------------------------------------------------
# PAUSE
# ------------------------------------------------------------

@app.post("/timers/{timer_id}/pause")
async def pause_timer(
    timer_id: str,
):

    try:

        timer = await manager.pause(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ------------------------------------------------------------
# RESUME
# ------------------------------------------------------------

@app.post("/timers/{timer_id}/resume")
async def resume_timer(
    timer_id: str,
):

    try:

        timer = await manager.resume(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ------------------------------------------------------------
# CANCEL
# ------------------------------------------------------------

@app.post("/timers/{timer_id}/cancel")
async def cancel_timer(
    timer_id: str,
):

    try:

        timer = await manager.cancel(
            timer_id
        )

        return timer.to_dict()

    except KeyError:

        raise HTTPException(
            status_code=404,
            detail="Timer not found",
        )


# ------------------------------------------------------------
# ADD TIME
# ------------------------------------------------------------

@app.post("/timers/{timer_id}/add")
async def add_time(
    timer_id: str,
    request: AddTimeRequest,
):

    try:

        timer = await manager.add_time(
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