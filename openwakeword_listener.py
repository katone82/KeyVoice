import os
import queue
import subprocess
import threading
import time
import wave
from typing import List

import numpy as np
import sounddevice as sd
import webrtcvad

from openwakeword.model import Model


# ============================================================
# AUDIO CONFIGURATION
# ============================================================

# OpenWakeWord / Vosk lavorano a 16 kHz.
TARGET_SAMPLE_RATE = 16_000

# ReSpeaker XVF3800:
#   S16_LE
#   16 kHz
#   2 canali
DEVICE_SAMPLE_RATE = 16_000

# Il dispositivo espone 2 canali.
# Useremo esclusivamente Channel 0.
CHANNELS = 2

AUDIO_DTYPE = "int16"

# 80 ms a 16 kHz
OWW_FRAME_LENGTH = 1_280

# Anche il device lavora a 16 kHz,
# quindi non serve moltiplicare per 3.
DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH


# ============================================================
# SOUNDDEVICE
# ============================================================

# ReSpeaker XVF3800 come UNICO dispositivo di input.
#
# NON usare più:
#   Sound Blaster Play! 4
#
INPUT_DEVICE_NAME = "reSpeaker XVF3800 4-Mic Array"


# Massimo numero di chunk audio tenuti in attesa.
AUDIO_QUEUE_MAXSIZE = 50


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

# Sound Blaster Play! 4.
#
# Verificato con:
#
#   aplay -D plughw:4,0 xvf3800_test.wav
#
BEEP_DEVICE = "plughw:4,0"


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
# DEVICE SEARCH
# ============================================================

def find_input_device(
    device_name: str
) -> int:
    """
    Cerca un dispositivo sounddevice per nome.

    Deve avere almeno un canale di input.
    """

    devices = sd.query_devices()

    print(
        "[AUDIO] Dispositivi input disponibili:"
    )

    for index, device in enumerate(devices):

        max_input_channels = device.get(
            "max_input_channels",
            0
        )

        if max_input_channels <= 0:
            continue

        print(
            f"[AUDIO]   {index}: "
            f"{device['name']} "
            f"(IN={max_input_channels}, "
            f"RATE={device['default_samplerate']})"
        )

    wanted = device_name.lower()

    for index, device in enumerate(devices):

        if (
            device.get("max_input_channels", 0) > 0
            and wanted in device["name"].lower()
        ):

            print(
                "[AUDIO] Microfono selezionato: "
                f"{index} - {device['name']}"
            )

            return index

    raise RuntimeError(
        f"Dispositivo audio non trovato: {device_name}"
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
        1.5
    )

    # Se per questo numero di secondi non arriva più
    # nessun callback audio, ricreiamo InputStream.
    audio_watchdog_seconds = config.get(
        "audio_watchdog_seconds",
        3.0
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
        f"Audio watchdog: "
        f"{audio_watchdog_seconds}s"
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
    # WEBRTC VAD
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
    # INTERNAL AUDIO QUEUE
    #
    # Questa coda è SOLO tra callback sounddevice
    # e listener openWakeWord.
    #
    # Non è la audio_queue destinata a Vosk.
    # ========================================================

    mic_queue = queue.Queue(
        maxsize=AUDIO_QUEUE_MAXSIZE
    )

    # ========================================================
    # CALLBACK HEARTBEAT
    # ========================================================

    heartbeat_lock = threading.Lock()

    last_audio_callback = (
        time.monotonic()
    )

    callback_count = 0

    # ========================================================
    # CALLBACK SOUNDDEVICE
    # ========================================================

    def audio_callback(
        indata,
        frames,
        callback_time,
        status
    ):
        """
        Callback PortAudio.

        ReSpeaker XVF3800:

            Channel 0 = audio ASR pulito
            Channel 1 = secondo canale

        KeyVoice utilizza esclusivamente Channel 0.

        Il callback deve essere velocissimo:
        nessun VAD / OpenWakeWord / Vosk qui.
        """

        nonlocal last_audio_callback
        nonlocal callback_count

        if status:

            print(
                f"[AUDIO] Callback status: {status}"
            )

        with heartbeat_lock:

            last_audio_callback = (
                time.monotonic()
            )

            callback_count += 1

        # ====================================================
        # VALIDAZIONE INPUT
        # ====================================================

        if (
            indata.ndim != 2
            or indata.shape[1] < 2
        ):

            print(
                "[AUDIO] Formato inatteso: "
                f"shape={indata.shape}"
            )

            return

        # ====================================================
        # CHANNEL 0
        # ====================================================

        # Channel 0 = audio ASR pulito del XVF3800.
        #
        # Da:
        #
        #   [frame][channel]
        #
        # prendiamo:
        #
        #   [frame][0]
        #
        # ottenendo un array mono.

        chunk = np.array(
            indata[:, 0],
            dtype=np.int16,
            copy=True
        )

        try:

            mic_queue.put_nowait(
                chunk
            )

        except queue.Full:

            # Se il consumer rallenta,
            # scartiamo il frame più vecchio.

            try:

                mic_queue.get_nowait()

            except queue.Empty:

                pass

            try:

                mic_queue.put_nowait(
                    chunk
                )

            except queue.Full:

                pass

    # ========================================================
    # DEVICE
    # ========================================================

    try:

        input_device_index = (
            find_input_device(
                INPUT_DEVICE_NAME
            )
        )

        device_info = sd.query_devices(
            input_device_index,
            "input"
        )

        print(
            "[AUDIO] Device: "
            f"{device_info['name']}"
        )

        print(
            "[AUDIO] Default sample rate: "
            f"{device_info['default_samplerate']}"
        )

        print(
            "[AUDIO] Capture: "
            f"{DEVICE_SAMPLE_RATE} Hz"
        )

        print(
            "[AUDIO] Processing: "
            f"{TARGET_SAMPLE_RATE} Hz"
        )

        print(
            "[AUDIO] Channels: "
            f"{CHANNELS}"
        )

        print(
            "[AUDIO] Active channel: "
            "Channel 0"
        )

    except Exception as exc:

        print(
            "[AUDIO] "
            f"Errore ricerca microfono: {exc}"
        )

        stop_event.set()

        return

    # ========================================================
    # STREAM MANAGEMENT
    # ========================================================

    stream = None

    def clear_mic_queue() -> None:

        while True:

            try:

                mic_queue.get_nowait()

            except queue.Empty:

                return

    def start_audio_stream() -> None:

        nonlocal stream
        nonlocal last_audio_callback

        if stream is not None:

            try:

                stream.stop()

            except Exception:

                pass

            try:

                stream.close()

            except Exception:

                pass

            stream = None

        clear_mic_queue()

        print(
            "[AUDIO] Apertura InputStream..."
        )

        stream = sd.InputStream(
            device=input_device_index,
            samplerate=DEVICE_SAMPLE_RATE,
            blocksize=DEVICE_FRAME_LENGTH,
            channels=CHANNELS,
            dtype=AUDIO_DTYPE,
            callback=audio_callback
        )

        stream.start()

        with heartbeat_lock:

            last_audio_callback = (
                time.monotonic()
            )

        print(
            "[AUDIO] InputStream attivo"
        )

    def restart_audio_stream(
        reason: str
    ) -> bool:

        nonlocal stream

        print()

        print(
            "[AUDIO] =============================="
        )

        print(
            f"[AUDIO] Riavvio stream: {reason}"
        )

        print(
            "[AUDIO] =============================="
        )

        try:

            if stream is not None:

                try:

                    stream.abort()

                except Exception:

                    pass

                try:

                    stream.close()

                except Exception:

                    pass

                stream = None

            clear_mic_queue()

            time.sleep(
                0.3
            )

            start_audio_stream()

            print(
                "[AUDIO] Stream ripristinato"
            )

            return True

        except Exception as exc:

            print(
                "[AUDIO] "
                f"ERRORE riavvio stream: {exc}"
            )

            return False

    try:

        start_audio_stream()

    except Exception as exc:

        print(
            "[AUDIO] "
            f"Errore apertura InputStream: {exc}"
        )

        stop_event.set()

        return

    # ========================================================
    # STATE
    # ========================================================

    state = STATE_LISTENING

    audio_buffer: List[int] = []

    vad_buffer: List[int] = []

    wait_command_start = None

    command_start_time = None

    last_voice_time = None

    cooldown_until = None

    # ========================================================
    # STATE HELPERS
    # ========================================================

    def clear_command_buffers() -> None:

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

        clear_command_buffers()

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

        clear_command_buffers()

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

    # ========================================================
    # VAD
    # ========================================================

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
    # READ FROM CALLBACK QUEUE
    # ========================================================

    def get_next_audio_chunk():
        """
        Attende al massimo 250 ms.

        Il XVF3800 fornisce già:

            16 kHz
            S16_LE
            Channel 0

        Non viene effettuato alcun resampling.
        """

        try:

            audio_16k = mic_queue.get(
                timeout=0.25
            )

        except queue.Empty:

            return None

        return audio_16k

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

            # =================================================
            # AUDIO WATCHDOG
            # =================================================

            now = time.monotonic()

            with heartbeat_lock:

                callback_age = (
                    now
                    - last_audio_callback
                )

            if (
                callback_age
                >= audio_watchdog_seconds
            ):

                print(
                    "[AUDIO] "
                    "WATCHDOG: callback audio fermo "
                    f"da {callback_age:.2f}s"
                )

                if not restart_audio_stream(
                    "watchdog callback"
                ):

                    stop_event.set()

                    break

                enter_listening(
                    "audio stream recuperato"
                )

                continue

            # =================================================
            # GET AUDIO
            # =================================================

            pcm_np = get_next_audio_chunk()

            if pcm_np is None:

                continue

            now = time.monotonic()

            # =================================================
            # OPENWAKEWORD SEMPRE ALIMENTATO
            # =================================================

            try:

                prediction = model.predict(
                    pcm_np
                )

                score = prediction.get(
                    model_name,
                    0
                )

            except Exception as exc:

                print(
                    "[OPENWAKEWORD] "
                    f"Errore predict: {exc}"
                )

                continue

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

                play_beep()

                clear_command_buffers()

                wait_command_start = (
                    time.monotonic()
                )

                state = STATE_WAIT_COMMAND

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

                    command_start_time = now

                    last_voice_time = now

                    state = STATE_RECORDING

                    print(
                        "[LISTENER] "
                        "Inizio registrazione comando"
                    )

                    continue

                if wait_command_start is None:

                    enter_cooldown(
                        "WAIT non valido"
                    )

                    continue

                wait_elapsed = (
                    now
                    - wait_command_start
                )

                if (
                    wait_elapsed
                    >= vad_voice_start_timeout
                ):

                    print(
                        "[LISTENER] "
                        "Nessuna voce dopo wake word"
                    )

                    enter_cooldown(
                        "nessun comando"
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

                if (
                    command_start_time is None
                    or last_voice_time is None
                ):

                    enter_cooldown(
                        "stato recording non valido"
                    )

                    continue

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

                post_deadline = (
                    time.monotonic()
                    + 1.0
                )

                while (
                    len(post_buffer)
                    < post_samples
                    and not stop_event.is_set()
                    and time.monotonic()
                    < post_deadline
                ):

                    pcm_post = (
                        get_next_audio_chunk()
                    )

                    if pcm_post is None:

                        continue

                    # OpenWakeWord continua ad avanzare.

                    try:

                        model.predict(
                            pcm_post
                        )

                    except Exception:

                        pass

                    post_buffer.extend(
                        pcm_post.tolist()
                    )

                audio_buffer.extend(
                    post_buffer
                )

                # =============================================
                # COMMAND DURATION
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

                enter_cooldown(
                    "comando completato"
                )

                continue

            # =================================================
            # UNKNOWN STATE
            # =================================================

            print(
                "[LISTENER] "
                f"Stato sconosciuto: {state}"
            )

            enter_cooldown(
                "reset stato"
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

                stream.abort()

            except Exception:

                pass

            try:

                stream.close()

            except Exception:

                pass