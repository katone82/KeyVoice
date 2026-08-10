import os
import struct
import threading
import time
import wave
from collections import deque

import numpy as np
import pyaudio
import webrtcvad

from openwakeword.model import Model


def openwakeword_listener(
        audio_queue,
        stop_event: threading.Event,
        config: dict):

    SAMPLE_RATE = 16000

    MODEL = config.get("model", "hey_jarvis")
    THRESHOLD = config.get("threshold", 0.35)

    PRE_BUFFER_SECONDS = config.get("pre_buffer_seconds", 0.3)
    POST_BUFFER_SECONDS = config.get("post_buffer_seconds", 0.1)

    SAVE_DEBUG_AUDIO = config.get("save_debug_audio", False)

    VAD_VOICE_START_TIMEOUT = config.get(
        "vad_voice_start_timeout",
        1.5
    )

    VAD_VOICE_END_SEC = config.get(
        "vad_voice_end_sec",
        0.4
    )

    # openWakeWord lavora bene con chunk multipli di 80 ms
    FRAME_LENGTH = 1280

    print("[OPENWAKEWORD] Thread partito")
    print(f"[OPENWAKEWORD] Modello: {MODEL}")
    print(f"[OPENWAKEWORD] Threshold: {THRESHOLD}")

    # ==============================
    # MODELLO OPENWAKEWORD
    # ==============================

    try:

        model = Model(
            wakeword_models=[MODEL],
            inference_framework="onnx"
        )

        print("[OPENWAKEWORD] Modello caricato")

    except Exception as e:

        print(
            f"[OPENWAKEWORD] "
            f"Errore caricamento modello: {e}"
        )

        stop_event.set()
        return

    # ==============================
    # MICROFONO
    # ==============================

    pa = pyaudio.PyAudio()

    try:

        stream = pa.open(
            rate=SAMPLE_RATE,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=FRAME_LENGTH
        )

    except Exception as e:

        print(
            f"[OPENWAKEWORD] "
            f"Errore apertura microfono: {e}"
        )

        pa.terminate()
        stop_event.set()

        return

    print("[OPENWAKEWORD] Listener pronto")

    # ==============================
    # VAD
    # ==============================

    vad = webrtcvad.Vad()

    # 0 = permissivo
    # 3 = aggressivo
    vad.set_mode(3)

    vad_frame_ms = 30

    vad_frame_length = int(
        SAMPLE_RATE * vad_frame_ms / 1000
    )

    vad_buffer = []

    voice_inactive_frames = 0

    max_voice_inactive_frames = int(
        VAD_VOICE_END_SEC /
        (vad_frame_ms / 1000)
    )

    # ==============================
    # BUFFER
    # ==============================

    pre_buffer = deque(
        maxlen=int(
            PRE_BUFFER_SECONDS *
            SAMPLE_RATE
        )
    )

    recording = False

    audio_buffer = []

    # ==============================
    # DEBUG AUDIO
    # ==============================

    def save_debug_audio(buffer):

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

        with wave.open(
                filename,
                "wb") as wf:

            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)

            wf.writeframes(
                np.array(
                    buffer,
                    dtype=np.int16
                ).tobytes()
            )

        print(
            f"[DEBUG] Audio salvato: {filename}"
        )

    # ==============================
    # LOOP
    # ==============================

    try:

        while not stop_event.is_set():

            pcm = stream.read(
                FRAME_LENGTH,
                exception_on_overflow=False
            )

            pcm_np = np.frombuffer(
                pcm,
                dtype=np.int16
            )

            # ----------------------
            # ASCOLTO WAKE WORD
            # ----------------------

            if not recording:

                pre_buffer.extend(
                    pcm_np.tolist()
                )

                prediction = model.predict(
                    pcm_np
                )

                score = prediction.get(
                    MODEL,
                    0
                )

                # debug utile inizialmente
                if score >= 0.05:

                    print(
                        f"[OPENWAKEWORD] "
                        f"score={score:.3f}"
                    )

                if score >= THRESHOLD:

                    print(
                        "\n"
                        "[LISTENER] "
                        "Wake word rilevata! "
                        f"score={score:.3f}"
                        "\n"
                    )

                    play_beep()

                    recording = True

                    audio_buffer.clear()
                    vad_buffer.clear()

                    voice_inactive_frames = 0

                    # Non includiamo la wake word
                    pre_buffer.clear()

                    # ----------------------
                    # ATTENDI INIZIO COMANDO
                    # ----------------------

                    voice_detected = False

                    start_time = time.time()

                    while (
                        not voice_detected
                        and
                        time.time() - start_time
                        < VAD_VOICE_START_TIMEOUT
                        and
                        not stop_event.is_set()
                    ):

                        pcm_wait = stream.read(
                            FRAME_LENGTH,
                            exception_on_overflow=False
                        )

                        pcm_wait_np = np.frombuffer(
                            pcm_wait,
                            dtype=np.int16
                        )

                        audio_buffer.extend(
                            pcm_wait_np.tolist()
                        )

                        vad_buffer.extend(
                            pcm_wait_np.tolist()
                        )

                        while (
                            len(vad_buffer)
                            >= vad_frame_length
                        ):

                            frame = (
                                vad_buffer[
                                    :vad_frame_length
                                ]
                            )

                            del vad_buffer[
                                :vad_frame_length
                            ]

                            frame_bytes = (
                                np.array(
                                    frame,
                                    dtype=np.int16
                                ).tobytes()
                            )

                            try:

                                is_speech = (
                                    vad.is_speech(
                                        frame_bytes,
                                        SAMPLE_RATE
                                    )
                                )

                            except Exception as e:

                                print(
                                    f"[VAD] Errore: {e}"
                                )

                                is_speech = True

                            if is_speech:

                                voice_detected = True
                                break

                    if not voice_detected:

                        print(
                            "[LISTENER] "
                            "Nessuna voce dopo "
                            "wake word"
                        )

                        recording = False

                        audio_buffer.clear()
                        vad_buffer.clear()

                        continue

            # ----------------------
            # REGISTRA COMANDO
            # ----------------------

            if recording:

                audio_buffer.extend(
                    pcm_np.tolist()
                )

                vad_buffer.extend(
                    pcm_np.tolist()
                )

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

                    frame_bytes = np.array(
                        frame,
                        dtype=np.int16
                    ).tobytes()

                    try:

                        is_speech = vad.is_speech(
                            frame_bytes,
                            SAMPLE_RATE
                        )

                    except Exception as e:

                        print(
                            f"[VAD] Errore: {e}"
                        )

                        is_speech = True

                    if is_speech:

                        voice_inactive_frames = 0

                    else:

                        voice_inactive_frames += 1

                # ----------------------
                # FINE COMANDO
                # ----------------------

                if (
                    voice_inactive_frames
                    >= max_voice_inactive_frames
                ):

                    print(
                        "[LISTENER] "
                        "Fine registrazione "
                        "(VAD)"
                    )

                    # piccolo buffer finale
                    post_samples = int(
                        POST_BUFFER_SECONDS *
                        SAMPLE_RATE
                    )

                    post_buffer = []

                    while (
                        len(post_buffer)
                        < post_samples
                        and
                        not stop_event.is_set()
                    ):

                        pcm_post = stream.read(
                            FRAME_LENGTH,
                            exception_on_overflow=False
                        )

                        pcm_post_np = (
                            np.frombuffer(
                                pcm_post,
                                dtype=np.int16
                            )
                        )

                        post_buffer.extend(
                            pcm_post_np.tolist()
                        )

                    audio_buffer.extend(
                        post_buffer
                    )

                    duration = (
                        len(audio_buffer) /
                        SAMPLE_RATE
                    )

                    print(
                        "[LISTENER] "
                        f"Audio comando: "
                        f"{duration:.2f}s"
                    )

                    # minimo 0.4 sec
                    min_samples = int(
                        0.4 * SAMPLE_RATE
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
                                SAMPLE_RATE
                            )
                        )

                        print(
                            "[LISTENER] "
                            "Buffer inviato a Vosk"
                        )

                        if SAVE_DEBUG_AUDIO:

                            save_debug_audio(
                                buffer_to_send
                            )

                    else:

                        print(
                            "[LISTENER] "
                            "Audio troppo corto, "
                            "ignoro"
                        )

                    audio_buffer.clear()
                    vad_buffer.clear()
                    pre_buffer.clear()

                    voice_inactive_frames = 0
                    recording = False

    finally:

        print(
            "[OPENWAKEWORD] "
            "Chiusura listener"
        )

        stream.stop_stream()
        stream.close()

        pa.terminate()


# ==============================
# BEEP
# ==============================

try:

    import simpleaudio as sa

    def play_beep():

        frequency = 1000
        duration = 0.15
        sample_rate = 44100

        t = np.linspace(
            0,
            duration,
            int(
                sample_rate *
                duration
            ),
            False
        )

        tone = np.sin(
            frequency *
            2 *
            np.pi *
            t
        )

        audio = (
            tone *
            32767
        ).astype(
            np.int16
        )

        play_obj = sa.play_buffer(
            audio,
            1,
            2,
            sample_rate
        )

        play_obj.wait_done()


except ImportError:

    def play_beep():

        print(
            "[WARN] "
            "simpleaudio non disponibile"
        )