#!/usr/bin/env python3

import time
import threading
import queue
import subprocess
import collections
import wave

import numpy as np
import sounddevice as sd
from openwakeword.model import Model


# ============================================================
# AUDIO
# ============================================================

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000

CHANNELS = 2

INPUT_DEVICE_NAME = "reSpeaker XVF3800 4-Mic Array"

BLOCK_MS = 30
BLOCK_SIZE = int(TARGET_SAMPLE_RATE * BLOCK_MS / 1000)


# ============================================================
# WAKE WORD
# ============================================================

WAKEWORD_MODEL = "hey_jarvis"
WAKEWORD_THRESHOLD = 0.35


# ============================================================
# SPEECH GATE
# ============================================================

# Rapporto RMS / noise floor necessario per considerare
# un frame come possibile inizio della voce.
SPEECH_START_RATIO = 2.5

# Rapporto RMS / noise floor usato durante la registrazione.
# Più basso per non perdere parti deboli della frase.
SPEECH_END_RATIO = 1.5

# Tempo minimo di voce continua prima di iniziare la registrazione.
SPEECH_START_TIME = 0.12

# Silenzio necessario per terminare il comando.
SPEECH_END_TIME = 0.50

# Audio mantenuto prima dell'inizio effettivo della voce.
PRE_ROLL_SECONDS = 0.30


# ============================================================
# NOISE FLOOR
# ============================================================

# Calibrazione iniziale.
NOISE_LEARN_SECONDS = 2.0

# Numero di blocchi utilizzati per calcolare il noise floor.
NOISE_WINDOW_SECONDS = 2.0

# Durante il normale ascolto, un frame viene considerato
# rumore solamente se non supera questa proporzione
# rispetto al noise floor corrente.
NOISE_MAX_RATIO = 1.5

# Evita che il noise floor cambi troppo rapidamente.
NOISE_UPDATE_ALPHA = 0.02


# ============================================================
# POST WAKEWORD
# ============================================================

# Tempo durante il quale ignoriamo completamente l'audio
# dopo la wake word.
#
# Serve principalmente per eliminare:
#
#   wake word
#   beep
#   riverberi
#   transienti
#
POST_WAKE_IGNORE_SECONDS = 0.40


# ============================================================
# COMMAND
# ============================================================

MAX_COMMAND_SECONDS = 6.0

# Tempo massimo durante il quale aspettiamo che l'utente
# inizi effettivamente a parlare dopo la wake word.
WAIT_COMMAND_TIMEOUT = 4.0


# ============================================================
# AUDIO WATCHDOG
# ============================================================

AUDIO_WATCHDOG_SECONDS = 3.0


# ============================================================
# BEEP
# ============================================================

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

        self.samples = collections.deque(
            maxlen=int(NOISE_WINDOW_SECONDS / (BLOCK_MS / 1000))
        )

        self.calibration_samples = []

        self.calibrating = True

        self.calibration_start = time.monotonic()


    # --------------------------------------------------------
    # RMS
    # --------------------------------------------------------

    def rms(self, audio):

        if len(audio) == 0:
            return 0.0

        x = audio.astype(np.float32)

        return float(
            np.sqrt(
                np.mean(x * x) + 1e-12
            )
        )


    # --------------------------------------------------------
    # CALIBRATION
    # --------------------------------------------------------

    def calibration_update(self, rms):

        if rms <= 0:
            return False

        self.calibration_samples.append(rms)

        elapsed = time.monotonic() - self.calibration_start

        if elapsed >= NOISE_LEARN_SECONDS:

            if self.calibration_samples:

                # Mediana = molto più resistente ai picchi
                # rispetto alla semplice media.
                self.noise_floor = float(
                    np.median(self.calibration_samples)
                )

            else:

                self.noise_floor = rms

            self.calibrating = False

            print()
            print(
                f"[CAL] Noise floor iniziale: "
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

        ratio = rms / self.noise_floor

        # IMPORTANTE:
        #
        # Se il segnale è molto più forte del rumore,
        # probabilmente è voce/transiente.
        #
        # Non dobbiamo inserirlo nel noise floor.
        if ratio > NOISE_MAX_RATIO:

            return

        # Conserviamo il campione per poter seguire lentamente
        # eventuali variazioni dell'ambiente.
        self.samples.append(rms)

        if not self.samples:
            return

        median_noise = float(
            np.median(self.samples)
        )

        self.noise_floor = (
            (1.0 - NOISE_UPDATE_ALPHA)
            * self.noise_floor
            +
            NOISE_UPDATE_ALPHA
            * median_noise
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

        self.audio_queue = queue.Queue(maxsize=100)

        self.command_audio = []

        self.state_start = time.monotonic()

        self.watchdog = AudioWatchdog()

        self.speech_gate = AdaptiveSpeechGate()

        self.speech_candidate_start = None

        self.speech_start_time = None

        self.last_speech_time = None

        self.cooldown_start = None

        self.post_wake_ignore_until = 0

        print()
        print("[INIT] Loading OpenWakeWord...")

        self.oww = Model(
            wakeword_models=[WAKEWORD_MODEL]
        )

        print("[INIT] OpenWakeWord loaded")

        self.device = self.find_input_device()

        print(
            f"[INIT] Input device: {self.device}"
        )


    # ========================================================
    # FIND DEVICE
    # ========================================================

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
            f"Input device not found: "
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

            self.audio_queue.put_nowait(audio)

        except queue.Full:

            pass


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
    # WAKE WORD
    # ========================================================

    def detect_wakeword(self, audio):

        if len(audio) == 0:

            return False

        channel = audio[:, 0]

        pcm = np.asarray(
            channel,
            dtype=np.float32
        )

        try:

            prediction = self.oww.predict(pcm)

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
    # STATE
    # ========================================================

    def set_state(self, state):

        if state != self.state:

            print(
                f"[STATE] "
                f"{self.state} -> {state}"
            )

        self.state = state

        self.state_start = time.monotonic()


    # ========================================================
    # RESET COMMAND
    # ========================================================

    def reset_command(self):

        self.command_audio = []

        self.speech_candidate_start = None

        self.speech_start_time = None

        self.last_speech_time = None


    # ========================================================
    # LISTENING
    # ========================================================

    def process_listening(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )


        # ----------------------------------------------------
        # CALIBRATION
        # ----------------------------------------------------

        if self.speech_gate.calibrating:

            finished = (
                self.speech_gate.calibration_update(
                    rms
                )
            )

            elapsed = (
                time.monotonic()
                - self.speech_gate.calibration_start
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
        #
        # NON aggiorniamo il noise floor prima della wakeword.
        #
        # In questo modo il frame che contiene la wakeword
        # non può contaminare immediatamente il rumore.
        #

        if self.detect_wakeword(audio):

            print()

            self.reset_command()

            # ------------------------------------------------
            # BLOCCO POST WAKEWORD
            # ------------------------------------------------
            #
            # Ignoriamo il beep e i transienti.
            #

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

    def process_wait_command(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        now = time.monotonic()


        # ----------------------------------------------------
        # POST-WAKE IGNORE
        # ----------------------------------------------------

        if now < self.post_wake_ignore_until:

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
        # NOISE FLOOR
        # ----------------------------------------------------

        if self.speech_gate.noise_floor is None:

            noise = 0.0

        else:

            noise = (
                self.speech_gate.noise_floor
            )


        print(
            f"[WAIT] "
            f"RMS={rms:.1f} "
            f"noise={noise:.1f} "
            f"ratio={ratio:.2f}",
            end="\r"
        )


        # ----------------------------------------------------
        # SPEECH CANDIDATE
        # ----------------------------------------------------

        if self.speech_gate.is_speech(
            rms,
            SPEECH_START_RATIO
        ):

            if (
                self.speech_candidate_start
                is None
            ):

                self.speech_candidate_start = now

            candidate_time = (
                now
                - self.speech_candidate_start
            )


            # ------------------------------------------------
            # CONFERMA VOCE
            # ------------------------------------------------

            if candidate_time >= SPEECH_START_TIME:

                print()

                print(
                    f"[SPEECH] "
                    f"Speech confirmed "
                    f"({candidate_time:.2f}s)"
                )


                # ------------------------------------------------
                # PRE-ROLL
                # ------------------------------------------------
                #
                # Il pre-roll viene costruito soltanto con
                # l'audio successivo al post-wake ignore.
                #
                # In questo modo il beep non entra nel comando.
                #

                self.command_audio = [
                    audio.copy()
                ]

                self.speech_start_time = now

                self.last_speech_time = now

                self.set_state(
                    RECORDING
                )

                return

        else:

            # Segnale insufficiente:
            # annulliamo il candidato.

            self.speech_candidate_start = None


        # ----------------------------------------------------
        # TIMEOUT
        # ----------------------------------------------------

        if (
            now - self.state_start
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


    # ========================================================
    # RECORDING
    # ========================================================

    def process_recording(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        now = time.monotonic()


        # ----------------------------------------------------
        # SAVE AUDIO
        # ----------------------------------------------------

        self.command_audio.append(
            audio.copy()
        )


        # ----------------------------------------------------
        # SPEECH / SILENCE
        # ----------------------------------------------------

        if ratio >= SPEECH_END_RATIO:

            self.last_speech_time = now


        silence_time = (
            now
            - self.last_speech_time
        )


        print(
            f"[REC] "
            f"RMS={rms:.1f} "
            f"ratio={ratio:.2f} "
            f"silence={silence_time:.2f}s",
            end="\r"
        )


        # ----------------------------------------------------
        # END OF COMMAND
        # ----------------------------------------------------

        if silence_time >= SPEECH_END_TIME:

            print()

            print(
                f"[SPEECH] "
                f"End detected "
                f"(silence={silence_time:.2f}s)"
            )

            self.finish_command()

            return


        # ----------------------------------------------------
        # MAX COMMAND TIME
        # ----------------------------------------------------

        command_time = (
            now
            - self.speech_start_time
        )

        if command_time >= MAX_COMMAND_SECONDS:

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
        # CONCAT AUDIO
        # ----------------------------------------------------

        audio = np.concatenate(
            self.command_audio,
            axis=0
        )


        # ----------------------------------------------------
        # PRE-ROLL
        # ----------------------------------------------------
        #
        # Manteniamo massimo PRE_ROLL_SECONDS prima
        # dell'inizio effettivo della voce.
        #
        # Attenzione:
        # speech_start_time è un timestamp reale, quindi
        # qui limitiamo semplicemente la quantità iniziale
        # mantenuta nel buffer.
        #

        max_samples = int(
            (
                PRE_ROLL_SECONDS
                + MAX_COMMAND_SECONDS
            )
            * TARGET_SAMPLE_RATE
        )


        if len(audio) > max_samples:

            audio = audio[-max_samples:]


        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

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


        # ----------------------------------------------------
        # SAVE WAV
        # ----------------------------------------------------

        filename = (
            "/tmp/keyvoice_command.wav"
        )


        try:

            # Solo canale 0
            mono = np.asarray(
                audio[:, 0],
                dtype=np.int16
            )

            with wave.open(
                filename,
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
                f"[RECORD] "
                f"Saved: {filename}"
            )


        except Exception as e:

            print(
                f"[RECORD] "
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
        print("================================================")
        print(" KeyVoice Wake Word Listener")
        print("================================================")
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
        print(
            f"Start time  : {SPEECH_START_TIME}s"
        )
        print(
            f"End time    : {SPEECH_END_TIME}s"
        )
        print(
            f"Noise cal   : {NOISE_LEARN_SECONDS}s"
        )
        print(
            f"Post wake   : {POST_WAKE_IGNORE_SECONDS}s"
        )
        print(
            f"Pre-roll    : {PRE_ROLL_SECONDS}s"
        )
        print("================================================")
        print()


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

        print(
            f"[FATAL] {e}"
        )

        raise