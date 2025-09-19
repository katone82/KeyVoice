import pvporcupine
import pyaudio
import struct
from collections import deque
import threading

ACCESS_KEY = ""
KEYWORDS = ["jarvis"]
SENSITIVITY = 0.7
PRE_BUFFER_SECONDS = 0.5
WAKEWORD_TRIM_MS = 300
SILENCE_THRESHOLD = 500
SILENCE_DURATION = 2

def porcupine_listener(audio_queue, stop_event: threading.Event):
    print("[PORCUPINE] Thread partito")
    try:
        porcupine = pvporcupine.create(
            access_key=ACCESS_KEY,
            keywords=KEYWORDS,
            sensitivities=[SENSITIVITY]
        )
        pa = pyaudio.PyAudio()
        stream = pa.open(
            rate=porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=porcupine.frame_length
        )
        print("[PORCUPINE] Listener pronto")
    except Exception as e:
        print(f"[PORCUPINE] Errore inizializzazione: {e}")
        return

    pre_buffer = deque(maxlen=int(PRE_BUFFER_SECONDS * porcupine.sample_rate))
    audio_buffer = []
    recording = False
    silence_counter = 0
    frame_duration = porcupine.frame_length / porcupine.sample_rate

    try:
        while not stop_event.is_set():
            pcm = stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            pre_buffer.extend(pcm_unpacked)

            # Wake word rilevata
            if porcupine.process(pcm_unpacked) >= 0 and not recording:
                print("[LISTENER] Wake word rilevata!")
                recording = True
                samples_to_trim = int((WAKEWORD_TRIM_MS / 1000) * porcupine.sample_rate)
                pre_buffer_list = list(pre_buffer)
                pre_buffer_list = pre_buffer_list[samples_to_trim:] if samples_to_trim < len(pre_buffer_list) else []
                audio_buffer.extend(pre_buffer_list)

            if recording:
                audio_buffer.extend(pcm_unpacked)
                if max(pcm_unpacked) < SILENCE_THRESHOLD:
                    silence_counter += frame_duration
                else:
                    silence_counter = 0

                if silence_counter >= SILENCE_DURATION:
                    print("[LISTENER] Fine registrazione, pausa rilevata")
                    # INVIO BUFFER ALLA CODA: copia indipendente
                    buffer_to_send = audio_buffer[:]  # nuova lista indipendente
                    audio_queue.put((buffer_to_send, porcupine.sample_rate))
                    print("[VOLK] Nuova registrazione: buffer inviato e azzerato")
                    # RESET COMPLETO DEL BUFFER
                    audio_buffer = []  # nuova lista vuota
                    recording = False
                    silence_counter = 0
                    pre_buffer.clear()  # reset pre-buffer
    finally:
        stop_event.set()
        stream.stop_stream()
        stream.close()
        pa.terminate()
        porcupine.delete()
        print("[PORCUPINE] Listener terminato")
