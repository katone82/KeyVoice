#!/usr/bin/env python3

import os
import time
import threading
import queue
import subprocess
import collections
import math

import numpy as np
import sounddevice as sd
import webrtcvad
from openwakeword.model import Model


# ============================================================
# CONFIG
# ============================================================

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000

CHANNELS = 2
INPUT_DEVICE_NAME = "reSpeaker XVF3800 4-Mic Array"

BLOCK_MS = 30
BLOCK_SIZE = int(TARGET_SAMPLE_RATE * BLOCK_MS / 1000)

WAKEWORD_THRESHOLD = 0.35
WAKEWORD_MODEL = "hey_jarvis"

MAX_COMMAND_SECONDS = 6.0

# Politica speech gate
SPEECH_START_RATIO = 2.5
SPEECH_END_RATIO = 1.5

SPEECH_START_TIME = 0.12
SPEECH_END_TIME = 0.50

NOISE_FLOOR_ALPHA = 0.02
NOISE_LEARN_SECONDS = 2.0

# watchdog
AUDIO_WATCHDOG_SECONDS = 3.0

# Beep
BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"
BEEP_DEVICE = "plughw:4,0"


# ============================================================
# STATES
# ============================================================

LISTENING = "LISTENING"
WAIT_COMMAND = "WAIT_COMMAND"
RECORDING = "RECORDING"
COOLDOWN = "COOLDOWN"


# ============================================================
# ADAPTIVE SPEECH GATE
# ============================================================

class AdaptiveSpeechGate:

    def __init__(self):
        self.noise_floor = None
        self.last_update = time.monotonic()

    def reset(self):
        self.noise_floor = None
        self.last_update = time.monotonic()

    def rms(self, audio):
        if len(audio) == 0:
            return 0.0

        x = audio.astype(np.float32)

        return float(np.sqrt(np.mean(x * x) + 1e-12))

    def update_noise(self, rms):
        if rms <= 0:
            return

        if self.noise_floor is None:
            self.noise_floor = rms
            return

        self.noise_floor = (
            (1.0 - NOISE_FLOOR_ALPHA) * self.noise_floor
            + NOISE_FLOOR_ALPHA * rms
        )

    def ratio(self, rms):
        if self.noise_floor is None or self.noise_floor <= 0:
            return 0.0

        return rms / self.noise_floor

    def is_speech(self, rms, threshold):
        return self.ratio(rms) >= threshold


# ============================================================
# AUDIO WATCHDOG
# ============================================================

class AudioWatchdog:

    def __init__(self):
        self.last_audio = time.monotonic()

    def update(self):
        self.last_audio = time.monotonic()

    def check(self):
        return (
            time.monotonic() - self.last_audio
            <= AUDIO_WATCHDOG_SECONDS
        )


# ============================================================
# MAIN LISTENER
# ============================================================

class WakeWordListener:

    def __init__(self):

        self.state = LISTENING

        self.audio_queue = queue.Queue()

        self.command_audio = []

        self.state_start = time.monotonic()

        self.watchdog = AudioWatchdog()

        self.speech_gate = AdaptiveSpeechGate()

        self.vad = webrtcvad.Vad(2)

        print("[INIT] Loading OpenWakeWord...")

        self.oww = Model(
            wakeword_models=[WAKEWORD_MODEL]
        )

        print("[INIT] OpenWakeWord loaded")

        self.device = self.find_input_device()

        print(f"[INIT] Input device: {self.device}")

    # --------------------------------------------------------
    # DEVICE
    # --------------------------------------------------------

    def find_input_device(self):

        devices = sd.query_devices()

        for index, device in enumerate(devices):

            name = device["name"]

            if INPUT_DEVICE_NAME.lower() in name.lower():

                print(
                    f"[AUDIO] Found device {index}: {name}"
                )

                return index

        raise RuntimeError(
            f"Input device not found: {INPUT_DEVICE_NAME}"
        )

    # --------------------------------------------------------
    # AUDIO CALLBACK
    # --------------------------------------------------------

    def audio_callback(
        self,
        indata,
        frames,
        callback_time,
        status
    ):

        if status:
            print(
                f"[AUDIO] {status}"
            )

        self.watchdog.update()

        try:

            audio = indata.copy()

            self.audio_queue.put_nowait(audio)

        except queue.Full:
            pass

    # --------------------------------------------------------
    # BEEP
    # --------------------------------------------------------

    def play_beep(self):

        try:

            subprocess.Popen(
                [
                    "aplay",
                    "-q",
                    "-D",
                    BEEP_DEVICE,
                    BEEP_FILE
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

        except Exception as e:

            print(
                f"[BEEP] Error: {e}"
            )

    # --------------------------------------------------------
    # OPENWAKEWORD
    # --------------------------------------------------------

    def detect_wakeword(self, audio):

        if len(audio) == 0:
            return False

        # Channel 0
        channel = audio[:, 0]

        pcm = np.asarray(
            channel,
            dtype=np.float32
        )

        try:

            prediction = self.oww.predict(
                pcm
            )

        except Exception as e:

            print(
                f"[OWW] Error: {e}"
            )

            return False

        score = prediction.get(
            WAKEWORD_MODEL,
            0.0
        )

        if score >= WAKEWORD_THRESHOLD:

            print(
                f"[WAKE] {WAKEWORD_MODEL} "
                f"score={score:.3f}"
            )

            return True

        return False

    # --------------------------------------------------------
    # WEBRTC VAD
    # --------------------------------------------------------

    def vad_detect(self, audio):

        if len(audio) != BLOCK_SIZE:
            return False

        channel = audio[:, 0]

        pcm = np.asarray(
            channel,
            dtype=np.int16
        )

        try:

            return self.vad.is_speech(
                pcm.tobytes(),
                TARGET_SAMPLE_RATE
            )

        except Exception:

            return False

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    def set_state(self, state):

        if state != self.state:

            print(
                f"[STATE] {self.state} -> {state}"
            )

        self.state = state
        self.state_start = time.monotonic()

    # --------------------------------------------------------
    # RESET COMMAND
    # --------------------------------------------------------

    def reset_command(self):

        self.command_audio = []

        self.speech_gate.reset()

    # --------------------------------------------------------
    # PROCESS LISTENING
    # --------------------------------------------------------

    def process_listening(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        # In listening state we continuously
        # learn the ambient noise level.

        self.speech_gate.update_noise(
            rms
        )

        if self.detect_wakeword(audio):

            self.play_beep()

            self.reset_command()

            self.set_state(
                WAIT_COMMAND
            )

    # --------------------------------------------------------
    # PROCESS WAIT COMMAND
    # --------------------------------------------------------

    def process_wait_command(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        print(
            f"[WAIT] RMS={rms:.1f} "
            f"noise={self.speech_gate.noise_floor:.1f} "
            f"ratio={ratio:.2f}",
            end="\r"
        )

        # Do NOT immediately adapt the noise floor here.
        #
        # We want to preserve the noise reference obtained
        # before the wake word.

        if self.speech_gate.is_speech(
            rms,
            SPEECH_START_RATIO
        ):

            print()

            print(
                "[SPEECH] Speech detected"
            )

            self.command_audio = [
                audio.copy()
            ]

            self.speech_start_time = time.monotonic()

            self.last_speech_time = time.monotonic()

            self.speech_candidate_start = (
                time.monotonic()
            )

            self.set_state(
                RECORDING
            )

        else:

            # Keep a small amount of audio so that
            # the beginning of speech is not lost.

            self.command_audio.append(
                audio.copy()
            )

            # Keep approximately 300 ms pre-roll.

            max_blocks = int(
                0.30 / (BLOCK_MS / 1000)
            )

            if len(self.command_audio) > max_blocks:

                self.command_audio = (
                    self.command_audio[-max_blocks:]
                )

            # Safety timeout while waiting.

            if (
                time.monotonic()
                - self.state_start
                > MAX_COMMAND_SECONDS
            ):

                print()

                print(
                    "[WAIT] Command timeout"
                )

                self.set_state(
                    LISTENING
                )

    # --------------------------------------------------------
    # PROCESS RECORDING
    # --------------------------------------------------------

    def process_recording(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        self.command_audio.append(
            audio.copy()
        )

        now = time.monotonic()

        # ----------------------------------------------------
        # SPEECH ACTIVE
        # ----------------------------------------------------

        if ratio >= SPEECH_END_RATIO:

            self.last_speech_time = now

        # ----------------------------------------------------
        # SPEECH ENDED
        # ----------------------------------------------------

        silence_time = (
            now - self.last_speech_time
        )

        if silence_time >= SPEECH_END_TIME:

            print()

            print(
                f"[SPEECH] End detected "
                f"(silence={silence_time:.2f}s)"
            )

            self.finish_command()

            return

        # ----------------------------------------------------
        # MAX COMMAND
        # ----------------------------------------------------

        command_time = (
            now - self.speech_start_time
        )

        if command_time >= MAX_COMMAND_SECONDS:

            print()

            print(
                "[RECORD] Maximum command time"
            )

            self.finish_command()

            return

        print(
            f"[REC] RMS={rms:.1f} "
            f"ratio={ratio:.2f} "
            f"silence={silence_time:.2f}s",
            end="\r"
        )

    # --------------------------------------------------------
    # FINISH COMMAND
    # --------------------------------------------------------

    def finish_command(self):

        if not self.command_audio:

            self.set_state(
                LISTENING
            )

            return

        audio = np.concatenate(
            self.command_audio,
            axis=0
        )

        duration = (
            len(audio)
            / TARGET_SAMPLE_RATE
        )

        print()

        print(
            f"[RECORD] Command audio: "
            f"{duration:.2f}s"
        )

        # ----------------------------------------------------
        # SAVE TEMP WAV
        # ----------------------------------------------------

        filename = (
            "/tmp/keyvoice_command.wav"
        )

        try:

            import soundfile as sf

            # Use channel 0 only for Vosk.

            mono = audio[:, 0]

            sf.write(
                filename,
                mono,
                TARGET_SAMPLE_RATE,
                subtype="PCM_16"
            )

            print(
                f"[RECORD] Saved: {filename}"
            )

        except Exception as e:

            print(
                f"[RECORD] Save error: {e}"
            )

        # ----------------------------------------------------
        # HERE WILL COME VOSK
        # ----------------------------------------------------

        print(
            "[RECORD] Ready for Vosk processing"
        )

        self.set_state(
            COOLDOWN
        )

        self.cooldown_start = time.monotonic()

    # --------------------------------------------------------
    # PROCESS COOLDOWN
    # --------------------------------------------------------

    def process_cooldown(self, audio):

        if (
            time.monotonic()
            - self.cooldown_start
            > 0.8
        ):

            self.reset_command()

            self.set_state(
                LISTENING
            )

    # --------------------------------------------------------
    # MAIN PROCESSOR
    # --------------------------------------------------------

    def process_audio(self, audio):

        if self.state == LISTENING:

            self.process_listening(
                audio
            )

        elif self.state == WAIT_COMMAND:

            self.process_wait_command(
                audio
            )

        elif self.state == RECORDING:

            self.process_recording(
                audio
            )

        elif self.state == COOLDOWN:

            self.process_cooldown(
                audio
            )

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    def run(self):

        print()
        print(
            "========================================"
        )
        print(
            " KeyVoice Wake Word Listener"
        )
        print(
            "========================================"
        )
        print(
            f"Sample rate : {TARGET_SAMPLE_RATE}"
        )
        print(
            f"Channels    : {CHANNELS}"
        )
        print(
            f"Wake word   : {WAKEWORD_MODEL}"
        )
        print(
            f"Threshold   : {WAKEWORD_THRESHOLD}"
        )
        print(
            f"Start ratio : {SPEECH_START_RATIO}"
        )
        print(
            f"End ratio   : {SPEECH_END_RATIO}"
        )
        print()
        print(
            "[LISTENER] In ascolto..."
        )

        with sd.InputStream(
            device=self.device,
            samplerate=DEVICE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=BLOCK_SIZE,
            callback=self.audio_callback,
            latency="low"
        ):

            while True:

                try:

                    audio = self.audio_queue.get(
                        timeout=1.0
                    )

                except queue.Empty:

                    if not self.watchdog.check():

                        print(
                            "[WATCHDOG] Audio stream timeout"
                        )

                    continue

                self.process_audio(
                    audio
                )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        listener = WakeWordListener()

        listener.run()

    except KeyboardInterrupt:

        print()
        print(
            "[EXIT] Listener stopped"
        )

    except Exception as e:

        print(
            f"[FATAL] {e}"
        )

        raise