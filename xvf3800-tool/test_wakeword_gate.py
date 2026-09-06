#!/usr/bin/env python3

import time
import queue
import subprocess

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
BLOCK_SIZE = int(
    TARGET_SAMPLE_RATE * BLOCK_MS / 1000
)

# ------------------------------------------------------------
# Wake word
# ------------------------------------------------------------

WAKEWORD_THRESHOLD = 0.35
WAKEWORD_MODEL = "hey_jarvis"

# ------------------------------------------------------------
# Command
# ------------------------------------------------------------

MAX_COMMAND_SECONDS = 6.0

# ------------------------------------------------------------
# Adaptive speech gate
# ------------------------------------------------------------

SPEECH_START_RATIO = 2.5
SPEECH_END_RATIO = 1.5

# La voce deve rimanere sopra START_RATIO
# per almeno questo tempo prima di iniziare.
SPEECH_START_TIME = 0.12

# La voce deve rimanere sotto END_RATIO
# per almeno questo tempo prima di terminare.
SPEECH_END_TIME = 0.50

# ------------------------------------------------------------
# Noise floor
# ------------------------------------------------------------

NOISE_FLOOR_ALPHA = 0.02

# ------------------------------------------------------------
# Pre-roll
# ------------------------------------------------------------

PRE_ROLL_SECONDS = 0.30

# ------------------------------------------------------------
# Audio watchdog
# ------------------------------------------------------------

AUDIO_WATCHDOG_SECONDS = 3.0

# ------------------------------------------------------------
# Beep
# ------------------------------------------------------------

BEEP_FILE = (
    "/home/homeassistant/KeyVoice/sounds/wake.wav"
)

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

    # --------------------------------------------------------
    # RESET
    # --------------------------------------------------------

    def reset(self):

        self.noise_floor = None

    # --------------------------------------------------------
    # RMS
    # --------------------------------------------------------

    def rms(self, audio):

        if audio is None or len(audio) == 0:

            return 0.0

        x = np.asarray(
            audio,
            dtype=np.float32
        )

        return float(
            np.sqrt(
                np.mean(x * x)
                + 1e-12
            )
        )

    # --------------------------------------------------------
    # UPDATE NOISE
    # --------------------------------------------------------

    def update_noise(self, rms):

        if rms <= 0:

            return

        # Prima misura

        if self.noise_floor is None:

            self.noise_floor = rms

            return

        # Exponential moving average

        self.noise_floor = (
            (1.0 - NOISE_FLOOR_ALPHA)
            * self.noise_floor
            +
            NOISE_FLOOR_ALPHA
            * rms
        )

    # --------------------------------------------------------
    # RATIO
    # --------------------------------------------------------

    def ratio(self, rms):

        if (
            self.noise_floor is None
            or self.noise_floor <= 0
        ):

            return 0.0

        return rms / self.noise_floor

    # --------------------------------------------------------
    # SPEECH
    # --------------------------------------------------------

    def is_speech(
        self,
        rms,
        threshold
    ):

        return (
            self.ratio(rms)
            >= threshold
        )


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
            time.monotonic()
            - self.last_audio
            <= AUDIO_WATCHDOG_SECONDS
        )


# ============================================================
# MAIN LISTENER
# ============================================================

class WakeWordListener:

    def __init__(self):

        # ----------------------------------------------------
        # State
        # ----------------------------------------------------

        self.state = LISTENING

        self.state_start = time.monotonic()

        # ----------------------------------------------------
        # Audio queue
        # ----------------------------------------------------

        self.audio_queue = queue.Queue(
            maxsize=100
        )

        # ----------------------------------------------------
        # Command audio
        # ----------------------------------------------------

        self.command_audio = []

        # ----------------------------------------------------
        # Speech timers
        # ----------------------------------------------------

        self.speech_candidate_start = None

        self.speech_start_time = None

        self.last_speech_time = None

        # ----------------------------------------------------
        # Watchdog
        # ----------------------------------------------------

        self.watchdog = AudioWatchdog()

        # ----------------------------------------------------
        # Speech gate
        # ----------------------------------------------------

        self.speech_gate = AdaptiveSpeechGate()

        # ----------------------------------------------------
        # WebRTC VAD
        # ----------------------------------------------------

        self.vad = webrtcvad.Vad(2)

        # ----------------------------------------------------
        # OpenWakeWord
        # ----------------------------------------------------

        print(
            "[INIT] Loading OpenWakeWord..."
        )

        self.oww = Model(
            wakeword_models=[
                WAKEWORD_MODEL
            ]
        )

        print(
            "[INIT] OpenWakeWord loaded"
        )

        # ----------------------------------------------------
        # Input device
        # ----------------------------------------------------

        self.device = (
            self.find_input_device()
        )

        print(
            f"[INIT] Input device: "
            f"{self.device}"
        )

    # ========================================================
    # DEVICE
    # ========================================================

    def find_input_device(self):

        devices = sd.query_devices()

        for index, device in enumerate(devices):

            name = device["name"]

            if (
                INPUT_DEVICE_NAME.lower()
                in name.lower()
            ):

                print(
                    f"[AUDIO] Found device "
                    f"{index}: {name}"
                )

                return index

        raise RuntimeError(
            "Input device not found: "
            f"{INPUT_DEVICE_NAME}"
        )

    # ========================================================
    # AUDIO CALLBACK
    # ========================================================

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

            self.audio_queue.put_nowait(
                audio
            )

        except queue.Full:

            print(
                "[AUDIO] Queue full"
            )

    # ========================================================
    # BEEP
    # ========================================================

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

    # ========================================================
    # OPENWAKEWORD
    # ========================================================

    def detect_wakeword(self, audio):

        if (
            audio is None
            or len(audio) == 0
        ):

            return False

        # ----------------------------------------------------
        # Channel 0
        # ----------------------------------------------------

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
                f"[WAKE] "
                f"{WAKEWORD_MODEL} "
                f"score={score:.3f}"
            )

            return True

        return False

    # ========================================================
    # WEBRTC VAD
    # ========================================================

    def vad_detect(self, audio):

        if (
            audio is None
            or len(audio) != BLOCK_SIZE
        ):

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

    # ========================================================
    # STATE
    # ========================================================

    def set_state(self, state):

        if state != self.state:

            print(
                f"[STATE] "
                f"{self.state} -> {state}"
            )

        self.state = state

        self.state_start = (
            time.monotonic()
        )

    # ========================================================
    # RESET COMMAND
    # ========================================================

    def reset_command(self):

        # IMPORTANT:
        #
        # Non resettiamo il noise floor.
        #
        # Il noise floor appreso prima della wake word
        # deve essere mantenuto durante WAIT_COMMAND
        # e RECORDING.

        self.command_audio = []

        self.speech_candidate_start = None

        self.speech_start_time = None

        self.last_speech_time = None

    # ========================================================
    # RESET EVERYTHING
    # ========================================================

    def reset_all(self):

        self.reset_command()

        self.speech_gate.reset()

    # ========================================================
    # PROCESS LISTENING
    # ========================================================

    def process_listening(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        # ----------------------------------------------------
        # Learn ambient noise
        # ----------------------------------------------------

        self.speech_gate.update_noise(
            rms
        )

        # ----------------------------------------------------
        # Debug noise
        # ----------------------------------------------------

        noise = (
            self.speech_gate.noise_floor
            or 0.0
        )

        ratio = (
            self.speech_gate.ratio(rms)
        )

        print(
            f"[LISTEN] "
            f"RMS={rms:.1f} "
            f"noise={noise:.1f} "
            f"ratio={ratio:.2f}",
            end="\r"
        )

        # ----------------------------------------------------
        # Wake word
        # ----------------------------------------------------

        if self.detect_wakeword(audio):

            print()

            self.play_beep()

            # IMPORTANT:
            #
            # Non facciamo reset del noise floor.
            #

            self.reset_command()

            self.set_state(
                WAIT_COMMAND
            )

    # ========================================================
    # PROCESS WAIT COMMAND
    # ========================================================

    def process_wait_command(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        noise = (
            self.speech_gate.noise_floor
        )

        # ----------------------------------------------------
        # Safety
        # ----------------------------------------------------

        if (
            noise is None
            or noise <= 0
        ):

            # Non abbiamo ancora una baseline
            # affidabile.

            noise = rms

            self.speech_gate.noise_floor = (
                noise
            )

        # ----------------------------------------------------
        # Ratio
        # ----------------------------------------------------

        ratio = self.speech_gate.ratio(
            rms
        )

        # ----------------------------------------------------
        # Debug
        # ----------------------------------------------------

        print(
            f"[WAIT] "
            f"RMS={rms:.1f} "
            f"noise={noise:.1f} "
            f"ratio={ratio:.2f}",
            end="\r"
        )

        now = time.monotonic()

        # ----------------------------------------------------
        # SPEECH CANDIDATE
        # ----------------------------------------------------

        if ratio >= SPEECH_START_RATIO:

            # Primo blocco sopra soglia

            if (
                self.speech_candidate_start
                is None
            ):

                self.speech_candidate_start = (
                    now
                )

                return

            # Quanto tempo siamo rimasti
            # sopra la soglia?

            speech_candidate_time = (
                now
                - self.speech_candidate_start
            )

            # ------------------------------------------------
            # Speech confirmed
            # ------------------------------------------------

            if (
                speech_candidate_time
                >= SPEECH_START_TIME
            ):

                print()

                print(
                    "[SPEECH] "
                    "Speech detected"
                )

                # ------------------------------------------------
                # Il comando parte includendo il pre-roll
                # già presente nel buffer.
                # ------------------------------------------------

                self.command_audio.append(
                    audio.copy()
                )

                self.speech_start_time = now

                self.last_speech_time = now

                self.speech_candidate_start = (
                    None
                )

                self.set_state(
                    RECORDING
                )

                return

        else:

            # ------------------------------------------------
            # Tornati sotto soglia:
            # la candidatura viene annullata.
            # ------------------------------------------------

            self.speech_candidate_start = (
                None
            )

        # ====================================================
        # PRE-ROLL
        # ====================================================

        self.command_audio.append(
            audio.copy()
        )

        max_blocks = int(
            PRE_ROLL_SECONDS
            / (BLOCK_MS / 1000)
        )

        if (
            len(self.command_audio)
            > max_blocks
        ):

            self.command_audio = (
                self.command_audio[
                    -max_blocks:
                ]
            )

        # ====================================================
        # WAIT TIMEOUT
        # ====================================================

        if (
            now
            - self.state_start
            > MAX_COMMAND_SECONDS
        ):

            print()

            print(
                "[WAIT] "
                "Command timeout"
            )

            self.reset_command()

            self.set_state(
                LISTENING
            )

    # ========================================================
    # PROCESS RECORDING
    # ========================================================

    def process_recording(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        # ----------------------------------------------------
        # Add audio
        # ----------------------------------------------------

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
        # SPEECH SILENCE
        # ----------------------------------------------------

        if self.last_speech_time is None:

            self.last_speech_time = now

        silence_time = (
            now
            - self.last_speech_time
        )

        # ----------------------------------------------------
        # Debug
        # ----------------------------------------------------

        print(
            f"[REC] "
            f"RMS={rms:.1f} "
            f"ratio={ratio:.2f} "
            f"silence={silence_time:.2f}s",
            end="\r"
        )

        # ====================================================
        # SPEECH ENDED
        # ====================================================

        if (
            silence_time
            >= SPEECH_END_TIME
        ):

            print()

            print(
                "[SPEECH] "
                f"End detected "
                f"(silence="
                f"{silence_time:.2f}s)"
            )

            self.finish_command()

            return

        # ====================================================
        # MAX COMMAND
        # ====================================================

        command_time = (
            now
            - self.speech_start_time
        )

        if (
            command_time
            >= MAX_COMMAND_SECONDS
        ):

            print()

            print(
                "[RECORD] "
                "Maximum command time"
            )

            self.finish_command()

            return

    # ========================================================
    # FINISH COMMAND
    # ========================================================

    def finish_command(self):

        if not self.command_audio:

            self.reset_command()

            self.set_state(
                LISTENING
            )

            return

        # ----------------------------------------------------
        # Concatenate
        # ----------------------------------------------------

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
            f"[RECORD] "
            f"Command audio: "
            f"{duration:.2f}s"
        )

        # ====================================================
        # SAVE TEMP WAV
        # ====================================================

        filename = (
            "/tmp/keyvoice_command.wav"
        )

        try:

            import soundfile as sf

            # Channel 0 only for Vosk

            mono = audio[:, 0]

            sf.write(
                filename,
                mono,
                TARGET_SAMPLE_RATE,
                subtype="PCM_16"
            )

            print(
                f"[RECORD] "
                f"Saved: {filename}"
            )

        except Exception as e:

            print(
                f"[RECORD] "
                f"Save error: {e}"
            )

        # ====================================================
        # VOSK PLACEHOLDER
        # ====================================================

        print(
            "[RECORD] "
            "Ready for Vosk processing"
        )

        # ====================================================
        # COOLDOWN
        # ====================================================

        self.cooldown_start = (
            time.monotonic()
        )

        self.set_state(
            COOLDOWN
        )

    # ========================================================
    # PROCESS COOLDOWN
    # ========================================================

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

    # ========================================================
    # PROCESS AUDIO
    # ========================================================

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

    # ========================================================
    # RUN
    # ========================================================

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
            f"Sample rate : "
            f"{TARGET_SAMPLE_RATE}"
        )

        print(
            f"Channels    : "
            f"{CHANNELS}"
        )

        print(
            f"Wake word   : "
            f"{WAKEWORD_MODEL}"
        )

        print(
            f"Threshold   : "
            f"{WAKEWORD_THRESHOLD}"
        )

        print(
            f"Start ratio : "
            f"{SPEECH_START_RATIO}"
        )

        print(
            f"End ratio   : "
            f"{SPEECH_END_RATIO}"
        )

        print(
            f"Start time  : "
            f"{SPEECH_START_TIME:.2f}s"
        )

        print(
            f"End time    : "
            f"{SPEECH_END_TIME:.2f}s"
        )

        print(
            f"Pre-roll    : "
            f"{PRE_ROLL_SECONDS:.2f}s"
        )

        print()

        print(
            "[LISTENER] "
            "In ascolto..."
        )

        # ====================================================
        # INPUT STREAM
        # ====================================================

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

                    audio = (
                        self.audio_queue.get(
                            timeout=1.0
                        )
                    )

                except queue.Empty:

                    if not self.watchdog.check():

                        print()

                        print(
                            "[WATCHDOG] "
                            "Audio stream timeout"
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
            "[EXIT] "
            "Listener stopped"
        )

    except Exception as e:

        print()

        print(
            f"[FATAL] {e}"
        )

        raise