#!/usr/bin/env python3

import os
import sys
import time
import threading
import queue
import subprocess
import collections
import wave
import math

import numpy as np
import sounddevice as sd

from openwakeword.model import Model


# ============================================================
# PATH
# ============================================================

# Directory dello script
SCRIPT_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

# Path di xvf_host.py.
#
# Default:
#     ./vendor/xvf_host.py
#
# Il path è relativo alla directory dello script,
# NON alla directory corrente della shell.
XVF_HOST_PATH = "./xvf3800-tool/vendor/xvf_host.py"


# Risoluzione assoluta del path
XVF_HOST_PATH = os.path.abspath(
    os.path.join(
        SCRIPT_DIR,
        XVF_HOST_PATH
    )
)


if not os.path.isfile(XVF_HOST_PATH):
    raise FileNotFoundError(
        "xvf_host.py non trovato: "
        f"{XVF_HOST_PATH}"
    )


# Aggiungiamo al PYTHONPATH la directory che contiene
# xvf_host.py.
XVF_HOST_DIR = os.path.dirname(
    XVF_HOST_PATH
)

if XVF_HOST_DIR not in sys.path:
    sys.path.insert(
        0,
        XVF_HOST_DIR
    )


try:
    import xvf_host
except Exception as e:
    print(
        "[XVF] Impossibile importare "
        f"xvf_host.py da {XVF_HOST_PATH}: {e}"
    )
    raise

# ============================================================
# AUDIO
# ============================================================

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000

CHANNELS = 2

INPUT_DEVICE_NAME = (
    "reSpeaker XVF3800 4-Mic Array"
)

BLOCK_MS = 30

BLOCK_SIZE = int(
    TARGET_SAMPLE_RATE
    * BLOCK_MS
    / 1000
)


# ============================================================
# WAKE WORD
# ============================================================

WAKEWORD_MODEL = "hey_jarvis"

WAKEWORD_THRESHOLD = 0.35


# ============================================================
# XVF3800 DIRECTION GATE
# ============================================================

# Frequenza di aggiornamento della telemetria XVF3800.
#
# NON leggiamo xvf_host ad ogni blocco audio da 30 ms.
# Il controllo USB sarebbe inutilmente pesante.
#
# 100 ms = 10 letture al secondo.
XVF_POLL_INTERVAL = 0.10


# Rapporto minimo tra beam dominante e secondo beam.
#
# Esempio:
#
#   beam 1 = 100
#   beam 2 = 20
#
# ratio = 5.0
#
# Una direzione molto dominante è più probabilmente la
# sorgente vocale desiderata.
DIRECTION_MIN_DOMINANCE = 1.50


# Speech energy minima del beam dominante.
#
# Il valore esatto dipende dal firmware/configurazione
# del XVF3800.
#
# Non usiamo questo valore come soglia assoluta per
# iniziare il comando: lo usiamo insieme a dominanza
# e stabilità.
DIRECTION_MIN_ENERGY = 1.0


# Numero di aggiornamenti consecutivi necessari per
# considerare stabile la direzione.
#
# 3 x 100 ms = circa 300 ms.
DIRECTION_CONFIRMATIONS = 3


# Tolleranza angolare durante il comando.
#
# La persona può muovere leggermente la testa senza
# perdere il lock.
DIRECTION_ANGLE_TOLERANCE = 35.0


# Durante la registrazione permettiamo una perdita
# temporanea della direzione prima di considerarla
# silenzio.
DIRECTION_LOST_GRACE_SECONDS = 0.18


# ============================================================
# SPEECH GATE
# ============================================================

# Questi valori rimangono come secondo livello di sicurezza.
#
# Il gate principale diventa direzionale.
#
# Il RMS serve per evitare che un frame completamente
# insignificante venga considerato voce solamente perché
# il DSP mantiene una telemetria residua.

SPEECH_START_RATIO = 2.5

SPEECH_END_RATIO = 1.5


# Tempo minimo durante il quale devono essere presenti
# contemporaneamente:
#
#   - energia audio
#   - direzione stabile
#
SPEECH_START_TIME = 0.24


# Silenzio direzionale necessario per terminare.
SPEECH_END_TIME = 0.50


# ============================================================
# NOISE FLOOR
# ============================================================

NOISE_LEARN_SECONDS = 2.0

NOISE_WINDOW_SECONDS = 2.0

NOISE_MAX_RATIO = 1.5

NOISE_UPDATE_ALPHA = 0.02


# ============================================================
# POST WAKEWORD
# ============================================================

POST_WAKE_IGNORE_SECONDS = 0.40


# ============================================================
# COMMAND
# ============================================================

MAX_COMMAND_SECONDS = 6.0

WAIT_COMMAND_TIMEOUT = 4.0


# ============================================================
# PRE ROLL
# ============================================================

PRE_ROLL_SECONDS = 0.30


# ============================================================
# AUDIO WATCHDOG
# ============================================================

AUDIO_WATCHDOG_SECONDS = 3.0


# ============================================================
# COOLDOWN
# ============================================================

COOLDOWN_SECONDS = 0.80


# ============================================================
# BEEP
# ============================================================

BEEP_FILE = (
    "/home/homeassistant/KeyVoice/sounds/wake.wav"
)

BEEP_DEVICE = "plughw:4,0"


# ============================================================
# OUTPUT
# ============================================================

COMMAND_WAV = (
    "/tmp/keyvoice_command.wav"
)


# ============================================================
# STATES
# ============================================================

LISTENING = "LISTENING"
WAIT_COMMAND = "WAIT_COMMAND"
RECORDING = "RECORDING"
COOLDOWN = "COOLDOWN"


# ============================================================
# HELPERS
# ============================================================

def normalize_angle(angle):
    """
    Normalizza un angolo in [0, 360).
    """
    return angle % 360.0


def angular_distance(a, b):
    """
    Distanza angolare minima tra due angoli.

    Esempio:

        350° e 10° -> 20°
    """
    diff = abs(
        normalize_angle(a)
        - normalize_angle(b)
    )

    return min(
        diff,
        360.0 - diff
    )


# ============================================================
# ADAPTIVE SPEECH GATE
# ============================================================

class AdaptiveSpeechGate:

    def __init__(self):

        self.noise_floor = None

        self.samples = collections.deque(
            maxlen=int(
                NOISE_WINDOW_SECONDS
                / (BLOCK_MS / 1000)
            )
        )

        self.calibration_samples = []

        self.calibrating = True

        self.calibration_start = (
            time.monotonic()
        )

    # --------------------------------------------------------
    # RMS
    # --------------------------------------------------------

    def rms(self, audio):

        if len(audio) == 0:
            return 0.0

        x = audio.astype(
            np.float32
        )

        return float(
            np.sqrt(
                np.mean(x * x)
                + 1e-12
            )
        )

    # --------------------------------------------------------
    # CALIBRATION
    # --------------------------------------------------------

    def calibration_update(self, rms):

        if rms <= 0:
            return False

        self.calibration_samples.append(
            rms
        )

        elapsed = (
            time.monotonic()
            - self.calibration_start
        )

        if elapsed >= NOISE_LEARN_SECONDS:

            if self.calibration_samples:

                values = np.asarray(
                    self.calibration_samples,
                    dtype=np.float32
                )

                # Usiamo un percentile basso invece
                # della mediana pura.
                #
                # In una stanza reale possono esserci
                # transienti durante la calibrazione.
                #
                # Il percentile 30 rappresenta meglio
                # il livello di fondo.
                self.noise_floor = float(
                    np.percentile(
                        values,
                        30
                    )
                )

            else:

                self.noise_floor = rms

            # Protezione contro noise floor troppo basso.
            self.noise_floor = max(
                self.noise_floor,
                50.0
            )

            self.calibrating = False

            print()

            print(
                "[CAL] Noise floor iniziale: "
                f"{self.noise_floor:.1f}"
            )

            return True

        return False

    # --------------------------------------------------------
    # UPDATE NOISE
    # --------------------------------------------------------

    def update_noise(self, rms):

        if rms <= 0:
            return

        if self.noise_floor is None:

            self.noise_floor = rms

            return

        ratio = (
            rms
            / self.noise_floor
        )

        # Non impariamo voce o transienti forti.
        if ratio > NOISE_MAX_RATIO:
            return

        self.samples.append(
            rms
        )

        if not self.samples:
            return

        values = np.asarray(
            self.samples,
            dtype=np.float32
        )

        median_noise = float(
            np.median(values)
        )

        self.noise_floor = (
            (1.0 - NOISE_UPDATE_ALPHA)
            * self.noise_floor
            +
            NOISE_UPDATE_ALPHA
            * median_noise
        )

        self.noise_floor = max(
            self.noise_floor,
            50.0
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

        return (
            rms
            / self.noise_floor
        )

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
# XVF3800 TELEMETRY
# ============================================================

class XVF3800Telemetry:

    def __init__(self):

        self.lock = threading.Lock()

        self.running = False

        self.thread = None

        self.azimuths = []

        self.energies = []

        self.dominant_beam = None

        self.dominant_angle = None

        self.dominant_energy = 0.0

        self.second_energy = 0.0

        self.dominance_ratio = 0.0

        self.last_update = 0.0

        self.error_count = 0

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    def start(self):

        print(
            "[XVF] Avvio telemetry thread"
        )

        self.running = True

        self.thread = threading.Thread(
            target=self._worker,
            daemon=True
        )

        self.thread.start()

    # --------------------------------------------------------
    # STOP
    # --------------------------------------------------------

    def stop(self):

        self.running = False

        if self.thread is not None:

            self.thread.join(
                timeout=1.0
            )

    # --------------------------------------------------------
    # READ DEVICE
    # --------------------------------------------------------

    def _read(self):

        device = None

        try:

            device = xvf_host.find()

            if not device:

                raise RuntimeError(
                    "XVF3800 non trovato"
                )

            azimuths = device.read(
                "AEC_AZIMUTH_VALUES"
            )

            energies = device.read(
                "AEC_SPENERGY_VALUES"
            )

            return (
                list(azimuths),
                list(energies)
            )

        finally:

            if device is not None:

                try:
                    device.close()
                except Exception:
                    pass

    # --------------------------------------------------------
    # WORKER
    # --------------------------------------------------------

    def _worker(self):

        while self.running:

            started = time.monotonic()

            try:

                azimuths, energies = (
                    self._read()
                )

                if (
                    not azimuths
                    or not energies
                ):
                    raise RuntimeError(
                        "Telemetry vuota"
                    )

                count = min(
                    len(azimuths),
                    len(energies)
                )

                azimuths = azimuths[:count]
                energies = energies[:count]

                degrees = [
                    normalize_angle(
                        math.degrees(
                            value
                        )
                    )
                    for value in azimuths
                ]

                energies = [
                    float(value)
                    for value in energies
                ]

                dominant = max(
                    range(len(energies)),
                    key=lambda i:
                    energies[i]
                )

                dominant_energy = (
                    energies[dominant]
                )

                ordered = sorted(
                    energies,
                    reverse=True
                )

                second_energy = (
                    ordered[1]
                    if len(ordered) > 1
                    else 0.0
                )

                ratio = (
                    dominant_energy
                    /
                    max(
                        second_energy,
                        1.0
                    )
                )

                with self.lock:

                    self.azimuths = (
                        degrees
                    )

                    self.energies = (
                        energies
                    )

                    self.dominant_beam = (
                        dominant
                    )

                    self.dominant_angle = (
                        degrees[dominant]
                    )

                    self.dominant_energy = (
                        dominant_energy
                    )

                    self.second_energy = (
                        second_energy
                    )

                    self.dominance_ratio = (
                        ratio
                    )

                    self.last_update = (
                        time.monotonic()
                    )

            except Exception as e:

                self.error_count += 1

                if self.error_count <= 5:

                    print(
                        f"[XVF] Telemetry error: {e}"
                    )

            elapsed = (
                time.monotonic()
                - started
            )

            sleep_time = max(
                0.01,
                XVF_POLL_INTERVAL
                - elapsed
            )

            time.sleep(
                sleep_time
            )

    # --------------------------------------------------------
    # SNAPSHOT
    # --------------------------------------------------------

    def snapshot(self):

        with self.lock:

            return {
                "azimuths":
                    list(self.azimuths),

                "energies":
                    list(self.energies),

                "dominant_beam":
                    self.dominant_beam,

                "dominant_angle":
                    self.dominant_angle,

                "dominant_energy":
                    self.dominant_energy,

                "second_energy":
                    self.second_energy,

                "dominance_ratio":
                    self.dominance_ratio,

                "last_update":
                    self.last_update
            }

    # --------------------------------------------------------
    # DIRECTION VALID
    # --------------------------------------------------------

    def valid_direction(self):

        with self.lock:

            if (
                self.dominant_beam
                is None
            ):
                return False

            if (
                self.dominant_angle
                is None
            ):
                return False

            if (
                self.dominant_energy
                < DIRECTION_MIN_ENERGY
            ):
                return False

            if (
                self.dominance_ratio
                < DIRECTION_MIN_DOMINANCE
            ):
                return False

            return True

    # --------------------------------------------------------
    # MATCH LOCKED DIRECTION
    # --------------------------------------------------------

    def matches_direction(
        self,
        locked_angle
    ):

        if locked_angle is None:
            return False

        with self.lock:

            if (
                self.dominant_angle
                is None
            ):
                return False

            if (
                self.dominant_energy
                < DIRECTION_MIN_ENERGY
            ):
                return False

            if (
                self.dominance_ratio
                < DIRECTION_MIN_DOMINANCE
            ):
                return False

            distance = angular_distance(
                self.dominant_angle,
                locked_angle
            )

            return (
                distance
                <= DIRECTION_ANGLE_TOLERANCE
            )


# ============================================================
# AUDIO WATCHDOG
# ============================================================

class AudioWatchdog:

    def __init__(self):

        self.last_audio = (
            time.monotonic()
        )

    def update(self):

        self.last_audio = (
            time.monotonic()
        )

    def check(self):

        return (
            time.monotonic()
            - self.last_audio
            <= AUDIO_WATCHDOG_SECONDS
        )


# ============================================================
# LISTENER
# ============================================================

class WakeWordListener:

    def __init__(self):

        self.state = LISTENING

        self.audio_queue = queue.Queue(
            maxsize=100
        )

        self.command_audio = []

        self.state_start = (
            time.monotonic()
        )

        self.watchdog = (
            AudioWatchdog()
        )

        self.speech_gate = (
            AdaptiveSpeechGate()
        )

        self.xvf = (
            XVF3800Telemetry()
        )

        # ----------------------------------------------------
        # SPEECH DIRECTION
        # ----------------------------------------------------

        self.locked_direction = None

        self.direction_candidate = None

        self.direction_confirmations = 0

        self.direction_last_valid = None

        # ----------------------------------------------------
        # SPEECH STATE
        # ----------------------------------------------------

        self.speech_candidate_start = None

        self.speech_start_time = None

        self.last_speech_time = None

        self.cooldown_start = None

        self.post_wake_ignore_until = 0

        # ----------------------------------------------------
        # PRE ROLL
        # ----------------------------------------------------

        self.pre_roll_buffer = collections.deque(
            maxlen=int(
                PRE_ROLL_SECONDS
                * TARGET_SAMPLE_RATE
                / BLOCK_SIZE
            )
        )

        print()

        print(
            f"[INIT] XVF host: {XVF_HOST_PATH}"
        )

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

        self.device = (
            self.find_input_device()
        )

        print(
            f"[INIT] Input device: "
            f"{self.device}"
        )

        # ----------------------------------------------------
        # XVF
        # ----------------------------------------------------

        self.xvf.start()

    # ========================================================
    # FIND DEVICE
    # ========================================================

    def find_input_device(self):

        devices = sd.query_devices()

        for index, device in enumerate(
            devices
        ):

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
    # CALLBACK
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

            pass

    # ========================================================
    # BEEP
    # ========================================================

    def play_beep(self):

        if not os.path.exists(
            BEEP_FILE
        ):

            print(
                f"[BEEP] File non trovato: "
                f"{BEEP_FILE}"
            )

            return

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
    # WAKE WORD
    # ========================================================

    def detect_wakeword(
        self,
        audio
    ):

        if len(audio) == 0:
            return False

        channel = audio[:, 0]

        pcm = np.asarray(
            channel,
            dtype=np.float32
        )

        try:

            prediction = (
                self.oww.predict(
                    pcm
                )
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

        if (
            score
            >= WAKEWORD_THRESHOLD
        ):

            print()

            print(
                f"[WAKE] "
                f"{WAKEWORD_MODEL} "
                f"score={score:.3f}"
            )

            return True

        return False

    # ========================================================
    # STATE
    # ========================================================

    def set_state(
        self,
        state
    ):

        if state != self.state:

            print(
                f"[STATE] "
                f"{self.state} -> "
                f"{state}"
            )

        self.state = state

        self.state_start = (
            time.monotonic()
        )

    # ========================================================
    # RESET COMMAND
    # ========================================================

    def reset_command(self):

        self.command_audio = []

        self.speech_candidate_start = (
            None
        )

        self.speech_start_time = (
            None
        )

        self.last_speech_time = (
            None
        )

        self.locked_direction = (
            None
        )

        self.direction_candidate = (
            None
        )

        self.direction_confirmations = 0

        self.direction_last_valid = (
            None
        )

        self.pre_roll_buffer.clear()

    # ========================================================
    # DIRECTION LOCK
    # ========================================================

    def update_direction_lock(self):

        snapshot = (
            self.xvf.snapshot()
        )

        angle = (
            snapshot["dominant_angle"]
        )

        energy = (
            snapshot["dominant_energy"]
        )

        ratio = (
            snapshot["dominance_ratio"]
        )

        if (
            angle is None
            or energy < DIRECTION_MIN_ENERGY
            or ratio < DIRECTION_MIN_DOMINANCE
        ):

            self.direction_candidate = (
                None
            )

            self.direction_confirmations = (
                0
            )

            return False

        # ----------------------------------------------------
        # First direction
        # ----------------------------------------------------

        if (
            self.direction_candidate
            is None
        ):

            self.direction_candidate = (
                angle
            )

            self.direction_confirmations = (
                1
            )

            return False

        # ----------------------------------------------------
        # Same direction
        # ----------------------------------------------------

        distance = angular_distance(
            angle,
            self.direction_candidate
        )

        if (
            distance
            <= DIRECTION_ANGLE_TOLERANCE
        ):

            self.direction_confirmations += (
                1
            )

        else:

            # Nuova possibile direzione.
            self.direction_candidate = (
                angle
            )

            self.direction_confirmations = (
                1
            )

        # ----------------------------------------------------
        # Confirm
        # ----------------------------------------------------

        if (
            self.direction_confirmations
            >= DIRECTION_CONFIRMATIONS
        ):

            self.locked_direction = (
                self.direction_candidate
            )

            self.direction_last_valid = (
                time.monotonic()
            )

            print(
                f"[DIRECTION] "
                f"Locked at "
                f"{self.locked_direction:.1f}° "
                f"(energy={energy:.1f}, "
                f"ratio={ratio:.2f})"
            )

            return True

        return False

    # ========================================================
    # DIRECTION ACTIVE
    # ========================================================

    def direction_is_active(self):

        if self.locked_direction is None:

            return False

        now = time.monotonic()

        if (
            self.xvf.matches_direction(
                self.locked_direction
            )
        ):

            self.direction_last_valid = now

            return True

        if (
            self.direction_last_valid
            is not None
            and
            now
            - self.direction_last_valid
            <= DIRECTION_LOST_GRACE_SECONDS
        ):

            return True

        return False

    # ========================================================
    # LISTENING
    # ========================================================

    def process_listening(
        self,
        audio
    ):

        channel = audio[:, 0]

        rms = (
            self.speech_gate.rms(
                channel
            )
        )

        # ----------------------------------------------------
        # CALIBRATION
        # ----------------------------------------------------

        if (
            self.speech_gate.calibrating
        ):

            finished = (
                self.speech_gate
                .calibration_update(
                    rms
                )
            )

            elapsed = (
                time.monotonic()
                -
                self.speech_gate
                .calibration_start
            )

            print(
                f"[CAL] "
                f"RMS={rms:.1f} "
                f"elapsed={elapsed:.1f}s",
                end="\r"
            )

            return

        # ----------------------------------------------------
        # WAKEWORD FIRST
        # ----------------------------------------------------

        if self.detect_wakeword(
            audio
        ):

            print()

            self.reset_command()

            self.post_wake_ignore_until = (
                time.monotonic()
                + POST_WAKE_IGNORE_SECONDS
            )

            print(
                f"[GATE] "
                f"Ignore post-wake: "
                f"{POST_WAKE_IGNORE_SECONDS:.2f}s"
            )

            self.play_beep()

            self.set_state(
                WAIT_COMMAND
            )

            return

        # ----------------------------------------------------
        # NOISE UPDATE
        # ----------------------------------------------------

        self.speech_gate.update_noise(
            rms
        )

    # ========================================================
    # WAIT COMMAND
    # ========================================================

    def process_wait_command(
        self,
        audio
    ):

        channel = audio[:, 0]

        rms = (
            self.speech_gate.rms(
                channel
            )
        )

        ratio = (
            self.speech_gate.ratio(
                rms
            )
        )

        now = time.monotonic()

        # ----------------------------------------------------
        # POST WAKE IGNORE
        # ----------------------------------------------------

        if (
            now
            < self.post_wake_ignore_until
        ):

            remaining = (
                self.post_wake_ignore_until
                - now
            )

            print(
                f"[WAIT] "
                f"post-wake ignore "
                f"{remaining:.2f}s",
                end="\r"
            )

            return

        # ----------------------------------------------------
        # DIRECTION
        # ----------------------------------------------------

        direction_valid = (
            self.xvf.valid_direction()
        )

        if direction_valid:

            self.update_direction_lock()

        # ----------------------------------------------------
        # PRE ROLL
        # ----------------------------------------------------

        self.pre_roll_buffer.append(
            audio.copy()
        )

        # ----------------------------------------------------
        # AUDIO ENERGY
        # ----------------------------------------------------

        energy_valid = (
            self.speech_gate.is_speech(
                rms,
                SPEECH_START_RATIO
            )
        )

        # ----------------------------------------------------
        # FINAL START CONDITION
        # ----------------------------------------------------
        #
        # Per iniziare il comando devono essere vere
        # entrambe:
        #
        #   1. audio sufficientemente forte
        #   2. direzione XVF stabile
        #

        direction_ready = (
            self.locked_direction
            is not None
        )

        if (
            energy_valid
            and direction_ready
            and self.direction_is_active()
        ):

            if (
                self.speech_candidate_start
                is None
            ):

                self.speech_candidate_start = (
                    now
                )

            candidate_time = (
                now
                - self.speech_candidate_start
            )

            if (
                candidate_time
                >= SPEECH_START_TIME
            ):

                print()

                snapshot = (
                    self.xvf.snapshot()
                )

                print(
                    "[SPEECH] "
                    f"Speech confirmed "
                    f"({candidate_time:.2f}s)"
                )

                print(
                    "[DIRECTION] "
                    f"{self.locked_direction:.1f}° "
                    f"energy="
                    f"{snapshot['dominant_energy']:.1f} "
                    f"ratio="
                    f"{snapshot['dominance_ratio']:.2f}"
                )

                # ------------------------------------------------
                # PRE ROLL REALE
                # ------------------------------------------------

                self.command_audio = list(
                    self.pre_roll_buffer
                )

                self.command_audio.append(
                    audio.copy()
                )

                self.speech_start_time = now

                self.last_speech_time = now

                self.set_state(
                    RECORDING
                )

                return

        else:

            self.speech_candidate_start = (
                None
            )

        # ----------------------------------------------------
        # TIMEOUT
        # ----------------------------------------------------

        if (
            now
            - self.state_start
            >= WAIT_COMMAND_TIMEOUT
        ):

            print()

            print(
                "[WAIT] "
                "Command timeout"
            )

            self.set_state(
                LISTENING
            )

            self.reset_command()

    # ========================================================
    # RECORDING
    # ========================================================

    def process_recording(
        self,
        audio
    ):

        channel = audio[:, 0]

        rms = (
            self.speech_gate.rms(
                channel
            )
        )

        ratio = (
            self.speech_gate.ratio(
                rms
            )
        )

        now = time.monotonic()

        # ----------------------------------------------------
        # DIRECTION
        # ----------------------------------------------------

        direction_active = (
            self.direction_is_active()
        )

        # ----------------------------------------------------
        # AUDIO FROM LOCKED DIRECTION
        # ----------------------------------------------------
        #
        # Questa è la parte fondamentale.
        #
        # L'audio viene comunque mantenuto temporalmente
        # continuo, ma se il DSP indica che la sorgente
        # non proviene dalla direzione della voce,
        # quel frame viene sostituito con silenzio.
        #
        # In questo modo:
        #
        #   voce nostra       -> conserva
        #   rumore laterale   -> silenzio
        #   rumore posteriore -> silenzio
        #

        if direction_active:

            accepted_audio = (
                audio.copy()
            )

        else:

            accepted_audio = np.zeros_like(
                audio
            )

        self.command_audio.append(
            accepted_audio
        )

        # ----------------------------------------------------
        # SPEECH ACTIVITY
        # ----------------------------------------------------
        #
        # La fine della frase viene determinata dalla
        # combinazione:
        #
        #   - direzione XVF
        #   - energia audio
        #
        # Non basta più un rumore forte proveniente
        # da un'altra direzione.
        #

        directional_speech = (
            direction_active
            and
            ratio >= SPEECH_END_RATIO
        )

        if directional_speech:

            self.last_speech_time = now

        silence_time = (
            now
            - self.last_speech_time
        )

        snapshot = (
            self.xvf.snapshot()
        )

        angle = (
            snapshot["dominant_angle"]
        )

        energy = (
            snapshot["dominant_energy"]
        )

        dom_ratio = (
            snapshot["dominance_ratio"]
        )

        angle_text = (
            f"{angle:.0f}°"
            if angle is not None
            else "---"
        )

        print(
            f"[REC] "
            f"RMS={rms:.0f} "
            f"ratio={ratio:.2f} "
            f"dir={angle_text} "
            f"energy={energy:.0f} "
            f"dom={dom_ratio:.2f} "
            f"silence={silence_time:.2f}s",
            end="\r"
        )

        # ----------------------------------------------------
        # END OF COMMAND
        # ----------------------------------------------------

        if (
            silence_time
            >= SPEECH_END_TIME
        ):

            print()

            print(
                "[SPEECH] "
                f"End detected "
                f"(directional silence="
                f"{silence_time:.2f}s)"
            )

            self.finish_command()

            return

        # ----------------------------------------------------
        # MAX COMMAND
        # ----------------------------------------------------

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

    # ========================================================
    # FINISH COMMAND
    # ========================================================

    def finish_command(self):

        if not self.command_audio:

            self.set_state(
                LISTENING
            )

            return

        # ----------------------------------------------------
        # CONCAT
        # ----------------------------------------------------

        audio = np.concatenate(
            self.command_audio,
            axis=0
        )

        # ----------------------------------------------------
        # LIMIT
        # ----------------------------------------------------

        max_samples = int(
            (
                PRE_ROLL_SECONDS
                + MAX_COMMAND_SECONDS
                + SPEECH_END_TIME
            )
            * TARGET_SAMPLE_RATE
        )

        if (
            len(audio)
            > max_samples
        ):

            audio = audio[
                -max_samples:
            ]

        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

        duration = (
            len(audio)
            / TARGET_SAMPLE_RATE
        )

        print()

        print(
            "[RECORD] "
            f"Command audio: "
            f"{duration:.2f}s"
        )

        print(
            "[RECORD] "
            f"Locked direction: "
            f"{self.locked_direction}"
        )

        # ----------------------------------------------------
        # SAVE WAV
        # ----------------------------------------------------

        try:

            mono = np.asarray(
                audio[:, 0],
                dtype=np.int16
            )

            with wave.open(
                COMMAND_WAV,
                "wb"
            ) as wf:

                wf.setnchannels(1)

                wf.setsampwidth(2)

                wf.setframerate(
                    TARGET_SAMPLE_RATE
                )

                wf.writeframes(
                    mono.tobytes()
                )

            print(
                "[RECORD] "
                f"Saved: {COMMAND_WAV}"
            )

        except Exception as e:

            print(
                "[RECORD] "
                f"Save error: {e}"
            )

        print(
            "[RECORD] "
            "Ready for Vosk processing"
        )

        # ----------------------------------------------------
        # COOLDOWN
        # ----------------------------------------------------

        self.set_state(
            COOLDOWN
        )

        self.cooldown_start = (
            time.monotonic()
        )

    # ========================================================
    # COOLDOWN
    # ========================================================

    def process_cooldown(
        self,
        audio
    ):

        if (
            time.monotonic()
            - self.cooldown_start
            >= COOLDOWN_SECONDS
        ):

            self.reset_command()

            self.set_state(
                LISTENING
            )

    # ========================================================
    # PROCESS AUDIO
    # ========================================================

    def process_audio(
        self,
        audio
    ):

        if self.state == LISTENING:

            self.process_listening(
                audio
            )

        elif (
            self.state
            == WAIT_COMMAND
        ):

            self.process_wait_command(
                audio
            )

        elif (
            self.state
            == RECORDING
        ):

            self.process_recording(
                audio
            )

        elif (
            self.state
            == COOLDOWN
        ):

            self.process_cooldown(
                audio
            )

    # ========================================================
    # RUN
    # ========================================================

    def run(self):

        print()

        print(
            "================================================"
        )

        print(
            " KeyVoice Wake Word Listener"
        )

        print(
            " XVF3800 Directional Speech Gate"
        )

        print(
            "================================================"
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
            f"{SPEECH_START_TIME}s"
        )

        print(
            f"End time    : "
            f"{SPEECH_END_TIME}s"
        )

        print(
            f"Direction   : "
            f"{DIRECTION_ANGLE_TOLERANCE}°"
        )

        print(
            f"Dominance   : "
            f"{DIRECTION_MIN_DOMINANCE}x"
        )

        print(
            f"Post wake   : "
            f"{POST_WAKE_IGNORE_SECONDS}s"
        )

        print(
            f"Pre-roll    : "
            f"{PRE_ROLL_SECONDS}s"
        )

        print(
            "================================================"
        )

        print()

        try:

            with sd.InputStream(

                device=self.device,

                samplerate=DEVICE_SAMPLE_RATE,

                channels=CHANNELS,

                dtype="int16",

                blocksize=BLOCK_SIZE,

                callback=self.audio_callback,

                latency="low"

            ):

                print(
                    "[LISTENER] "
                    "Calibrazione rumore..."
                )

                while True:

                    try:

                        audio = (
                            self.audio_queue.get(
                                timeout=1.0
                            )
                        )

                    except queue.Empty:

                        if not self.watchdog.check():

                            print(
                                "[WATCHDOG] "
                                "Audio stream timeout"
                            )

                        continue

                    self.process_audio(
                        audio
                    )

        finally:

            print()

            print(
                "[XVF] "
                "Stopping telemetry..."
            )

            self.xvf.stop()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        listener = (
            WakeWordListener()
        )

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