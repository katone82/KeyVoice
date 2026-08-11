import os
import subprocess
import threading
import time
import wave
from typing import List

import numpy as np
import pyaudio
import webrtcvad
from scipy.signal import resample_poly

from openwakeword.model import Model


# ============================================================
# AUDIO CONFIGURATION
# ============================================================

TARGET_SAMPLE_RATE = 16_000
DEVICE_SAMPLE_RATE = 48_000

INPUT_DEVICE_INDEX = 0

CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16

# 80 ms a 16 kHz
OWW_FRAME_LENGTH = 1_280

# 80 ms a 48 kHz
DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH * 3


# ============================================================
# LISTENER STATES
# ============================================================

STATE_LISTENING = "listening"
STATE_WAIT_COMMAND = "wait_command"
STATE_RECORDING = "recording"
STATE_COOLDOWN = "cooldown"


# ============================================================
# BEEP
# ============================================================

BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"
BEEP_DEVICE = "plughw:3,0"


def play_beep() -> None:
    if not os.path.exists(BEEP_FILE):
        print(
            f"[BEEP] File non trovato: {BEEP_FILE}"
        )
        return

    try:
        subprocess.run(
            [
                "aplay",
                "-q",
                "-D",
                BEEP_DEVICE,
                BEEP_FILE
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2
        )

    except subprocess.TimeoutExpired:
        print(
            "[BEEP] Timeout riproduzione"
        )

    except Exception as exc:
        print(
            f"[BEEP] Errore: {exc}"
        )


# ============================================================
# AUDIO CONVERSION
# ============================================================

def convert_48k_to_16k(
    pcm_bytes: bytes
) -> np.ndarray:

    audio_48k = np.frombuffer(
        pcm_bytes,
        dtype=np.int16
    )

    audio_16k = resample_poly(
        audio_48k.astype(np.float32),
        up=1,
        down=3
    )

    return np.clip(
        audio_16k,
        -32768,
        32767
    ).astype(np.int16)


# ============================================================
# AUDIO READ
# ============================================================

def read_audio_chunk(
    stream: pyaudio.Stream
) -> np.ndarray:

    pcm = stream.read(
        DEVICE_FRAME_LENGTH,
        exception_on_overflow=False
    )

    return convert_48k_to_16k(
        pcm
    )


# ============================================================
# DEBUG AUDIO
# ============================================================

def save_debug_audio(
    buffer: List[int],
    sample_rate: int
) -> None:

    debug_dir = "debug_audio"

    os.makedirs(
        debug_dir,
        exist_ok=True
    )

    timestamp = time.strftime(
        "%Y%m%d_%H%M%S"
    )

    filename = os.path.join(
        debug_dir,
        f"command_{timestamp}.wav"
    )

    audio_data = np.asarray(
        buffer,
        dtype=np.int16
    )

    with wave.open(
        filename,
        "wb"
    ) as wav_file:

        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(
            sample_rate
        )

        wav_file.writeframes(
            audio_data.tobytes()
        )

    print(
        f"[DEBUG] Audio salvato: {filename}"
    )


# ============================================================
# OPENWAKEWORD LISTENER
# ============================================================

def openwakeword_listener(
    audio_queue,
    stop_event: threading.Event,
    config: dict
) -> None:

    # ========================================================
    # CONFIG
    # ========================================================

    model_name = config.get(
        "model",
        "hey_jarvis"
    )

    threshold = config.get(
        "threshold",
        0.35
    )

    save_debug = config.get(
        "save_debug_audio",
        False
    )

    vad_mode = config.get(
        "vad_mode",
        2
    )

    vad_voice_start_timeout = config.get(
        "vad_voice_start_timeout",
        1.5
    )

    vad_voice_end_sec = config.get(
        "vad_voice_end_sec",
        0.6
    )

    max_command_seconds = config.get(
        "max_command_seconds",
        6.0
    )

    min_command_seconds = config.get(
        "min_command_seconds",
        0.4
    )

    post_buffer_seconds = config.get(
        "post_buffer_seconds",
        0.1
    )

    wakeword_cooldown_sec = config.get(
        "wakeword_cooldown_sec",
        1.2
    )

    print(
        "[OPENWAKEWORD] Thread partito"
    )

    print(
        f"[OPENWAKEWORD] Modello: {model_name}"
    )

    print(
        f"[OPENWAKEWORD] Threshold: {threshold}"
    )

    print(
        f"[OPENWAKEWORD] VAD mode: {vad_mode}"
    )

    print(
        "[OPENWAKEWORD] "
        f"Timeout inizio comando: "
        f"{vad_voice_start_timeout}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Timeout silenzio: "
        f"{vad_voice_end_sec}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Durata massima comando: "
        f"{max_command_seconds}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Cooldown: "
        f"{wakeword_cooldown_sec}s"
    )

    # ========================================================
    # MODEL
    # ========================================================

    try:
        model = Model(
            wakeword_models=[
                model_name
            ],
            inference_framework="onnx"
        )

        print(
            "[OPENWAKEWORD] Modello caricato"
        )

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore caricamento modello: {exc}"
        )

        stop_event.set()
        return

    # ========================================================
    # MICROPHONE
    # ========================================================

    audio = pyaudio.PyAudio()
    stream = None

    try:
        device_info = (
            audio.get_device_info_by_index(
                INPUT_DEVICE_INDEX
            )
        )

        print(
            "[OPENWAKEWORD] Microfono: "
            f"{device_info['name']}"
        )

        print(
            "[OPENWAKEWORD] "
            f"Device index: {INPUT_DEVICE_INDEX}"
        )

        print(
            "[OPENWAKEWORD] "
            f"Capture audio: "
            f"{DEVICE_SAMPLE_RATE} Hz"
        )

        print(
            "[OPENWAKEWORD] "
            f"Processing audio: "
            f"{TARGET_SAMPLE_RATE} Hz"
        )

        stream = audio.open(
            rate=DEVICE_SAMPLE_RATE,
            channels=CHANNELS,
            format=AUDIO_FORMAT,
            input=True,
            input_device_index=INPUT_DEVICE_INDEX,
            frames_per_buffer=DEVICE_FRAME_LENGTH
        )

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore apertura microfono: {exc}"
        )

        audio.terminate()
        stop_event.set()

        return

    print(
        "[OPENWAKEWORD] Listener pronto"
    )

    # ========================================================
    # VAD
    # ========================================================

    vad = webrtcvad.Vad()

    vad.set_mode(
        vad_mode
    )

    vad_frame_ms = 30

    vad_frame_length = int(
        TARGET_SAMPLE_RATE
        * vad_frame_ms
        / 1000
    )

    # ========================================================
    # BUFFERS
    # ========================================================

    audio_buffer: List[int] = []
    vad_buffer: List[int] = []

    # ========================================================
    # STATE
    # ========================================================

    state = STATE_LISTENING

    wait_command_start = None
    command_start_time = None
    last_voice_time = None
    cooldown_until = None

    # ========================================================
    # HELPERS
    # ========================================================

    def clear_buffers() -> None:
        audio_buffer.clear()
        vad_buffer.clear()

    def enter_listening(
        reason: str = ""
    ) -> None:

        nonlocal state
        nonlocal wait_command_start
        nonlocal command_start_time
        nonlocal last_voice_time
        nonlocal cooldown_until

        clear_buffers()

        wait_command_start = None
        command_start_time = None
        last_voice_time = None
        cooldown_until = None

        state = STATE_LISTENING

        if reason:

            print(
                "[LISTENER] "
                f"In ascolto wake word "
                f"({reason})"
            )

        else:

            print(
                "[LISTENER] "
                "In ascolto wake word"
            )

    def enter_cooldown(
        reason: str
    ) -> None:

        nonlocal state
        nonlocal wait_command_start
        nonlocal command_start_time
        nonlocal last_voice_time
        nonlocal cooldown_until

        clear_buffers()

        wait_command_start = None
        command_start_time = None
        last_voice_time = None

        cooldown_until = (
            time.monotonic()
            + wakeword_cooldown_sec
        )

        state = STATE_COOLDOWN

        print(
            "[LISTENER] "
            f"Cooldown ({reason})"
        )

    def process_vad_frames() -> bool:

        speech_detected = False

        while (
            len(vad_buffer)
            >= vad_frame_length
        ):

            frame = vad_buffer[
                :vad_frame_length
            ]

            del vad_buffer[
                :vad_frame_length
            ]

            frame_bytes = np.asarray(
                frame,
                dtype=np.int16
            ).tobytes()

            try:

                is_speech = vad.is_speech(
                    frame_bytes,
                    TARGET_SAMPLE_RATE
                )

            except Exception as exc:

                print(
                    f"[VAD] Errore: {exc}"
                )

                is_speech = True

            if is_speech:
                speech_detected = True

        return speech_detected

    # ========================================================
    # INITIAL STATE
    # ========================================================

    enter_listening(
        "avvio"
    )

    # ========================================================
    # MAIN LOOP
    # ========================================================

    try:

        while not stop_event.is_set():

            pcm_np = read_audio_chunk(
                stream
            )

            now = time.monotonic()

            # =================================================
            # COOLDOWN
            # =================================================

            if state == STATE_COOLDOWN:

                if (
                    cooldown_until is not None
                    and now >= cooldown_until
                ):

                    enter_listening(
                        "cooldown terminato"
                    )

                continue

            # =================================================
            # LISTENING
            # =================================================

            if state == STATE_LISTENING:

                prediction = model.predict(
                    pcm_np
                )

                score = prediction.get(
                    model_name,
                    0
                )

                if score >= 0.05:

                    print(
                        "[OPENWAKEWORD] "
                        f"score={score:.3f}"
                    )

                if score < threshold:
                    continue

                print()

                print(
                    "[LISTENER] "
                    "Wake word rilevata! "
                    f"score={score:.3f}"
                )

                print()

                # =============================================
                # BEEP
                # =============================================

                play_beep()

                # =============================================
                # ENTER WAIT COMMAND
                # =============================================

                clear_buffers()

                wait_command_start = (
                    time.monotonic()
                )

                state = (
                    STATE_WAIT_COMMAND
                )

                print(
                    "[LISTENER] "
                    "Attendo comando..."
                )

                continue

            # =================================================
            # WAIT COMMAND
            # =================================================

            if state == STATE_WAIT_COMMAND:

                audio_buffer.extend(
                    pcm_np.tolist()
                )

                vad_buffer.extend(
                    pcm_np.tolist()
                )

                voice_detected = (
                    process_vad_frames()
                )

                if voice_detected:

                    now = time.monotonic()

                    command_start_time = now
                    last_voice_time = now

                    state = (
                        STATE_RECORDING
                    )

                    print(
                        "[LISTENER] "
                        "Inizio registrazione comando"
                    )

                    continue

                # Sicurezza
                if wait_command_start is None:

                    enter_listening(
                        "stato WAIT non valido"
                    )

                    continue

                elapsed_wait = (
                    now
                    - wait_command_start
                )

                if (
                    elapsed_wait
                    >= vad_voice_start_timeout
                ):

                    print(
                        "[LISTENER] "
                        "Nessuna voce dopo wake word"
                    )

                    # Qui NON serve cooldown:
                    # non abbiamo registrato alcun comando.
                    enter_listening(
                        "timeout comando"
                    )

                continue

            # =================================================
            # RECORDING
            # =================================================

            if state == STATE_RECORDING:

                audio_buffer.extend(
                    pcm_np.tolist()
                )

                vad_buffer.extend(
                    pcm_np.tolist()
                )

                speech_detected = (
                    process_vad_frames()
                )

                if speech_detected:
                    last_voice_time = now

                # =============================================
                # STATE SAFETY
                # =============================================

                if (
                    command_start_time is None
                    or last_voice_time is None
                ):

                    print(
                        "[LISTENER] "
                        "Stato registrazione "
                        "non valido"
                    )

                    enter_listening(
                        "reset sicurezza"
                    )

                    continue

                # =============================================
                # TIMERS
                # =============================================

                command_elapsed = (
                    now
                    - command_start_time
                )

                silence_elapsed = (
                    now
                    - last_voice_time
                )

                silence_timeout = (
                    silence_elapsed
                    >= vad_voice_end_sec
                )

                command_timeout = (
                    command_elapsed
                    >= max_command_seconds
                )

                if (
                    not silence_timeout
                    and not command_timeout
                ):

                    continue

                # =============================================
                # END COMMAND
                # =============================================

                if command_timeout:

                    print(
                        "[LISTENER] "
                        "Timeout massimo comando "
                        f"({command_elapsed:.2f}s)"
                    )

                else:

                    print(
                        "[LISTENER] "
                        "Fine registrazione "
                        f"(silenzio "
                        f"{silence_elapsed:.2f}s)"
                    )

                # =============================================
                # POST BUFFER
                # =============================================

                post_samples = int(
                    post_buffer_seconds
                    * TARGET_SAMPLE_RATE
                )

                post_buffer: List[int] = []

                while (
                    len(post_buffer)
                    < post_samples
                    and not stop_event.is_set()
                ):

                    pcm_post = read_audio_chunk(
                        stream
                    )

                    post_buffer.extend(
                        pcm_post.tolist()
                    )

                audio_buffer.extend(
                    post_buffer
                )

                # =============================================
                # DURATION
                # =============================================

                duration = (
                    len(audio_buffer)
                    / TARGET_SAMPLE_RATE
                )

                print(
                    "[LISTENER] "
                    f"Audio comando: "
                    f"{duration:.2f}s"
                )

                # =============================================
                # SEND TO VOSK
                # =============================================

                min_samples = int(
                    min_command_seconds
                    * TARGET_SAMPLE_RATE
                )

                if (
                    len(audio_buffer)
                    >= min_samples
                ):

                    buffer_to_send = list(
                        audio_buffer
                    )

                    audio_queue.put(
                        (
                            buffer_to_send,
                            TARGET_SAMPLE_RATE
                        )
                    )

                    print(
                        "[LISTENER] "
                        "Buffer inviato a Vosk"
                    )

                    if save_debug:

                        save_debug_audio(
                            buffer_to_send,
                            TARGET_SAMPLE_RATE
                        )

                else:

                    print(
                        "[LISTENER] "
                        "Audio troppo corto, ignoro"
                    )

                # =============================================
                # FORCE COOLDOWN
                # =============================================

                enter_cooldown(
                    "comando completato"
                )

                continue

            # =================================================
            # UNKNOWN STATE SAFETY
            # =================================================

            print(
                "[LISTENER] "
                f"Stato sconosciuto: {state}"
            )

            enter_listening(
                "reset stato sconosciuto"
            )

    # ========================================================
    # ERROR
    # ========================================================

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore listener: {exc}"
        )

        stop_event.set()

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        print(
            "[OPENWAKEWORD] "
            "Chiusura listener"
        )

        if stream is not None:

            try:

                if stream.is_active():
                    stream.stop_stream()

            except Exception:
                pass

            try:

                stream.close()

            except Exception:
                pass

        audio.terminate()