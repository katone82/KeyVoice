import json
import threading
import time
from queue import Empty, Queue

import numpy as np
import vosk
from scipy.signal import resample_poly


# ============================================================
# CONFIGURAZIONE
# ============================================================

VOSK_SAMPLE_RATE = 16_000


# ============================================================
# AUDIO UTILITIES
# ============================================================

def prepare_audio(
    audio_buffer,
    sample_rate: int
) -> np.ndarray:
    """
    Converte il buffer audio in PCM int16 mono a 16 kHz,
    pronto per Vosk.
    """

    audio_array = np.asarray(
        audio_buffer,
        dtype=np.int16
    )

    if audio_array.size == 0:
        return audio_array

    # ========================================================
    # RESAMPLE
    # ========================================================

    if sample_rate != VOSK_SAMPLE_RATE:

        audio_array = resample_poly(
            audio_array.astype(np.float32),
            VOSK_SAMPLE_RATE,
            sample_rate
        )

        audio_array = np.clip(
            audio_array,
            -32768,
            32767
        ).astype(np.int16)

    # ========================================================
    # NORMALIZZAZIONE
    # ========================================================

    max_val = np.max(
        np.abs(
            audio_array.astype(np.int32)
        )
    )

    if max_val > 0:

        gain = 32767.0 / max_val

        audio_array = np.clip(
            audio_array.astype(np.float32) * gain,
            -32768,
            32767
        ).astype(np.int16)

    return audio_array


# ============================================================
# VOSK LISTENER
# ============================================================

def vosk_listener(
    audio_queue: Queue,
    stop_event: threading.Event,
    config: dict,
    command_queue: Queue = None,
    ready_event=None
) -> None:
    """
    Thread Vosk.

    Ogni elemento ricevuto da audio_queue rappresenta
    UN comando completo già delimitato dal VAD.

    Vosk:
    - trascrive il buffer;
    - produce un risultato finale;
    - invia SOLO il risultato finale al fuzzy parser.

    Le trascrizioni parziali NON vengono mai inviate
    a command_queue.
    """

    model_path = config["model_path"]

    print(
        "[VOLK] Thread partito"
    )

    print(
        f"[VOLK] Caricamento modello da: "
        f"{model_path}"
    )

    # ========================================================
    # CARICAMENTO MODELLO
    # ========================================================

    try:

        model = vosk.Model(
            model_path
        )

        print(
            "[VOLK] Modello caricato, "
            "pronto all'ascolto"
        )

        if ready_event:
            ready_event.set()

    except Exception as exc:

        print(
            "[VOLK] ERRORE caricamento modello: "
            f"{exc}"
        )

        return

    # ========================================================
    # LOOP
    # ========================================================

    while not stop_event.is_set():

        try:

            audio_buffer, sample_rate = (
                audio_queue.get(
                    timeout=1
                )
            )

        except Empty:
            continue

        except Exception as exc:

            print(
                "[VOLK] Errore lettura audio_queue: "
                f"{exc}"
            )

            continue

        # ====================================================
        # VALIDAZIONE BUFFER
        # ====================================================

        if not audio_buffer:
            continue

        try:

            audio_array = prepare_audio(
                audio_buffer,
                sample_rate
            )

        except Exception as exc:

            print(
                "[VOLK] ERRORE preparazione audio: "
                f"{exc}"
            )

            continue

        if audio_array.size == 0:
            continue

        duration = (
            len(audio_array)
            / VOSK_SAMPLE_RATE
        )

        print(
            "[VOLK] "
            f"Elaborazione audio: {duration:.2f}s"
        )

        # ====================================================
        # PCM BYTES
        # ====================================================

        try:

            pcm_bytes = (
                audio_array.tobytes()
            )

        except Exception as exc:

            print(
                "[VOLK] ERRORE conversione PCM: "
                f"{exc}"
            )

            continue

        # ====================================================
        # NUOVO RECOGNIZER PER OGNI COMANDO
        # ====================================================

        recognizer = vosk.KaldiRecognizer(
            model,
            VOSK_SAMPLE_RATE
        )

        start_time = time.monotonic()

        try:

            # Passiamo TUTTO il comando al recognizer.
            #
            # Non ci interessa se AcceptWaveform restituisce
            # True o False: il buffer è già stato delimitato
            # dal nostro VAD.

            recognizer.AcceptWaveform(
                pcm_bytes
            )

            # =================================================
            # FINAL RESULT
            # =================================================

            result_json = (
                recognizer.FinalResult()
            )

            result = json.loads(
                result_json
            )

            text = result.get(
                "text",
                ""
            ).strip()

            elapsed = (
                time.monotonic()
                - start_time
            )

            # =================================================
            # NESSUN TESTO
            # =================================================

            if not text:

                print(
                    "[VOLK] "
                    "Nessun comando riconosciuto "
                    f"[decode: {elapsed:.3f}s]"
                )

                continue

            # =================================================
            # RISULTATO FINALE
            # =================================================

            print(
                "[VOLK] "
                f"Comando finale trascritto: "
                f"{text} "
                f"[Vosk decode time: "
                f"{elapsed:.3f}s]"
            )

            # =================================================
            # INVIO AL FUZZY
            # =================================================

            if command_queue is not None:

                command_queue.put(
                    text
                )

                print(
                    "[VOLK] "
                    "Comando inviato al fuzzy parser"
                )

        except json.JSONDecodeError as exc:

            print(
                "[VOLK] "
                f"ERRORE parsing risultato Vosk: "
                f"{exc}"
            )

        except Exception as exc:

            print(
                "[VOLK] "
                f"ERRORE riconoscimento: "
                f"{exc}"
            )

    # ========================================================
    # THREAD TERMINATO
    # ========================================================

    print(
        "[VOLK] Thread terminato"
    )