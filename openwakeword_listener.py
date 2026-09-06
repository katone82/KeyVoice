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

    TARGET_SAMPLE_RATE = 16_000
    DEVICE_SAMPLE_RATE = 16_000

    CHANNELS = 2
    ACTIVE_CHANNEL = 0

    AUDIO_DTYPE = "int16"

    # 40 ms

    OWW_FRAME_LENGTH = 640
    DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH

    INPUT_DEVICE_NAME = "reSpeaker XVF3800 4-Mic Array"

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

    BEEP_DEVICE = "plughw:4,0"

    def play_beep() -> None:

    ```
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

        print("[BEEP] Timeout riproduzione")

    except Exception as exc:

        print(f"[BEEP] Errore: {exc}")
    ```

    # ============================================================

    # DEVICE SEARCH

    # ============================================================

    def find_input_device(
    device_name: str
    ) -> int:

    ```
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
    ```

    # ============================================================

    # DEBUG AUDIO

    # ============================================================

    def save_debug_audio(
    buffer: List[int],
    sample_rate: int
    ) -> None:

    ```
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
        wav_file.setframerate(sample_rate)

        wav_file.writeframes(
            audio_data.tobytes()
        )

    print(
        f"[DEBUG] Audio salvato: {filename}"
    )
    ```

    # ============================================================

    # ADAPTIVE SPEECH GATE

    # ============================================================

    class AdaptiveSpeechGate:
    """
    Speech gate basato sull'energia RMS del Channel 0.

    ```
    Non usa una soglia assoluta.

    Prima costruisce il rumore di fondo:

        noise_floor

    Successivamente confronta:

        current_energy / noise_floor

    con una soglia relativa.

    Il gate ha isteresi:

        START -> soglia più alta
        END   -> soglia più bassa

    Questo evita continui ON/OFF vicino alla soglia.
    """

    def __init__(
        self,
        sample_rate: int,
        noise_learning_seconds: float = 2.0,
        start_ratio: float = 2.5,
        end_ratio: float = 1.5,
        min_voice_seconds: float = 0.12,
        min_silence_seconds: float = 0.50,
    ):

        self.sample_rate = sample_rate

        self.noise_learning_seconds = (
            noise_learning_seconds
        )

        self.start_ratio = start_ratio
        self.end_ratio = end_ratio

        self.min_voice_seconds = (
            min_voice_seconds
        )

        self.min_silence_seconds = (
            min_silence_seconds
        )

        self.energy_samples = []

        self.noise_floor = None

        self.voice_since = None
        self.silence_since = None

        self.speech = False

    # --------------------------------------------------------
    # RESET
    # --------------------------------------------------------

    def reset(self) -> None:

        self.energy_samples.clear()

        self.noise_floor = None

        self.voice_since = None
        self.silence_since = None

        self.speech = False

    # --------------------------------------------------------
    # ENERGY
    # --------------------------------------------------------

    @staticmethod
    def calculate_energy(
        pcm: np.ndarray
    ) -> float:

        if len(pcm) == 0:
            return 0.0

        audio = pcm.astype(
            np.float32
        )

        rms = np.sqrt(
            np.mean(
                audio * audio
            )
        )

        return float(rms)

    # --------------------------------------------------------
    # INITIAL NOISE CALIBRATION
    # --------------------------------------------------------

    def update_noise_floor(
        self,
        energy: float
    ) -> bool:

        if self.noise_floor is not None:

            return True

        self.energy_samples.append(
            energy
        )

        required_samples = int(
            self.noise_learning_seconds
            * self.sample_rate
            / DEVICE_FRAME_LENGTH
        )

        if len(self.energy_samples) < required_samples:

            return False

        # Mediana molto più robusta
        # rispetto alla media in presenza
        # di qualche rumore improvviso.

        self.noise_floor = max(
            float(
                np.median(
                    self.energy_samples
                )
            ),
            1.0
        )

        print(
            "[DSP] Noise floor iniziale: "
            f"{self.noise_floor:.2f}"
        )

        return True

    # --------------------------------------------------------
    # PROCESS
    # --------------------------------------------------------

    def process(
        self,
        pcm: np.ndarray,
        now: float
    ) -> bool:

        energy = self.calculate_energy(
            pcm
        )

        # ----------------------------------------------------
        # NOISE CALIBRATION
        # ----------------------------------------------------

        if not self.update_noise_floor(
            energy
        ):

            return False

        # ----------------------------------------------------
        # SLOW NOISE FLOOR UPDATE
        # ----------------------------------------------------

        if not self.speech:

            alpha = 0.02

            self.noise_floor = (
                (1.0 - alpha)
                * self.noise_floor
                + alpha
                * max(energy, 1.0)
            )

        # ----------------------------------------------------
        # RELATIVE ENERGY
        # ----------------------------------------------------

        ratio = (
            energy
            / max(
                self.noise_floor,
                1.0
            )
        )

        # ----------------------------------------------------
        # START SPEECH
        # ----------------------------------------------------

        if not self.speech:

            if ratio >= self.start_ratio:

                self.silence_since = None

                if self.voice_since is None:

                    self.voice_since = now

                elapsed = (
                    now
                    - self.voice_since
                )

                if (
                    elapsed
                    >= self.min_voice_seconds
                ):

                    self.speech = True

                    self.voice_since = None

                    print(
                        "[DSP] VOCE ON "
                        f"energy={energy:.1f} "
                        f"floor={self.noise_floor:.1f} "
                        f"ratio={ratio:.2f}"
                    )

                    return True

            else:

                self.voice_since = None

            return False

        # ----------------------------------------------------
        # END SPEECH
        # ----------------------------------------------------

        if ratio <= self.end_ratio:

            self.voice_since = None

            if self.silence_since is None:

                self.silence_since = now

            elapsed = (
                now
                - self.silence_since
            )

            if (
                elapsed
                >= self.min_silence_seconds
            ):

                self.speech = False

                self.silence_since = None

                print(
                    "[DSP] VOCE OFF "
                    f"energy={energy:.1f} "
                    f"floor={self.noise_floor:.1f} "
                    f"ratio={ratio:.2f}"
                )

                return False

        else:

            self.silence_since = None

        return True

    # --------------------------------------------------------
    # DEBUG
    # --------------------------------------------------------

    def debug_info(
        self,
        pcm: np.ndarray
    ) -> str:

        energy = self.calculate_energy(
            pcm
        )

        if self.noise_floor is None:

            return (
                f"energy={energy:.1f} "
                "floor=CALIBRATING"
            )

        ratio = (
            energy
            / max(
                self.noise_floor,
                1.0
            )
        )

        return (
            f"energy={energy:.1f} "
            f"floor={self.noise_floor:.1f} "
            f"ratio={ratio:.2f} "
            f"speech={self.speech}"
        )
    ```

    # ============================================================

    # OPENWAKEWORD LISTENER

    # ============================================================

    def openwakeword_listener(
    audio_queue,
    stop_event: threading.Event,
    config: dict
    ) -> None:

    ```
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

    debug_wakeword = config.get(
        "debug_wakeword",
        False
    )

    # --------------------------------------------------------
    # SPEECH GATE
    # --------------------------------------------------------

    noise_learning_seconds = config.get(
        "noise_learning_seconds",
        2.0
    )

    speech_start_ratio = config.get(
        "speech_start_ratio",
        2.5
    )

    speech_end_ratio = config.get(
        "speech_end_ratio",
        1.5
    )

    speech_start_seconds = config.get(
        "speech_start_seconds",
        0.12
    )

    speech_end_seconds = config.get(
        "speech_end_seconds",
        0.50
    )

    # --------------------------------------------------------
    # TIMEOUT
    # --------------------------------------------------------

    voice_start_timeout = config.get(
        "voice_start_timeout",
        1.5
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
        "[DSP] "
        f"Noise learning: "
        f"{noise_learning_seconds}s"
    )

    print(
        "[DSP] "
        f"Speech start ratio: "
        f"{speech_start_ratio}"
    )

    print(
        "[DSP] "
        f"Speech end ratio: "
        f"{speech_end_ratio}"
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
    #
    # Manteniamo VAD solo come informazione secondaria.
    # NON decide più l'inizio/fine registrazione.
    # ========================================================

    vad = webrtcvad.Vad()

    vad.set_mode(
        config.get(
            "vad_mode",
            2
        )
    )

    vad_frame_ms = 30

    vad_frame_length = int(
        TARGET_SAMPLE_RATE
        * vad_frame_ms
        / 1000
    )

    vad_buffer: List[int] = []

    # ========================================================
    # INTERNAL AUDIO QUEUE
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
    # AUDIO CALLBACK
    # ========================================================

    def audio_callback(
        indata,
        frames,
        callback_time,
        status
    ):

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

        if (
            indata.ndim != 2
            or indata.shape[1] <= ACTIVE_CHANNEL
        ):

            print(
                "[AUDIO] Formato inatteso: "
                f"shape={indata.shape}"
            )

            return

        chunk = np.array(
            indata[:, ACTIVE_CHANNEL],
            dtype=np.int16,
            copy=True
        )

        try:

            mic_queue.put_nowait(
                chunk
            )

        except queue.Full:

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
            "[AUDIO] Capture: "
            f"{DEVICE_SAMPLE_RATE} Hz"
        )

        print(
            "[AUDIO] Channels: "
            f"{CHANNELS}"
        )

        print(
            "[AUDIO] Active channel: "
            f"{ACTIVE_CHANNEL}"
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

            time.sleep(0.3)

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
    # SPEECH GATE
    # ========================================================

    speech_gate = AdaptiveSpeechGate(
        sample_rate=TARGET_SAMPLE_RATE,
        noise_learning_seconds=noise_learning_seconds,
        start_ratio=speech_start_ratio,
        end_ratio=speech_end_ratio,
        min_voice_seconds=speech_start_seconds,
        min_silence_seconds=speech_end_seconds
    )

    # ========================================================
    # STATE
    # ========================================================

    state = STATE_LISTENING

    audio_buffer: List[int] = []

    wait_command_start = None
    command_start_time = None

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
        nonlocal cooldown_until

        clear_command_buffers()

        wait_command_start = None
        command_start_time = None
        cooldown_until = None

        speech_gate.reset()

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
        nonlocal cooldown_until

        clear_command_buffers()

        wait_command_start = None
        command_start_time = None

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
    # SECONDARY WEBRTC VAD
    # ========================================================

    def process_webrtc_vad(
        pcm_np: np.ndarray
    ) -> bool:

        vad_buffer.extend(
            pcm_np.tolist()
        )

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

                if vad.is_speech(
                    frame_bytes,
                    TARGET_SAMPLE_RATE
                ):

                    speech_detected = True

            except Exception:

                pass

        return speech_detected

    # ========================================================
    # READ AUDIO
    # ========================================================

    def get_next_audio_chunk():

        try:

            return mic_queue.get(
                timeout=0.25
            )

        except queue.Empty:

            return None

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
            # WATCHDOG
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
            # AUDIO
            # =================================================

            pcm_np = get_next_audio_chunk()

            if pcm_np is None:

                continue

            now = time.monotonic()

            # =================================================
            # OPENWAKEWORD
            # =================================================

            try:

                prediction = model.predict(
                    pcm_np
                )

                score = prediction.get(
                    model_name,
                    0.0
                )

            except Exception as exc:

                print(
                    "[OPENWAKEWORD] "
                    f"Errore predict: {exc}"
                )

                continue

            if (
                debug_wakeword
                and score >= 0.05
            ):

                print(
                    "[OPENWAKEWORD] "
                    f"score={score:.3f}"
                )

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

                # Nuova calibrazione del rumore
                # dopo il beep.

                speech_gate.reset()

                wait_command_start = (
                    time.monotonic()
                )

                state = STATE_WAIT_COMMAND

                print(
                    "[LISTENER] "
                    "Attendo voce..."
                )

                continue

            # =================================================
            # WAIT COMMAND
            # =================================================

            if state == STATE_WAIT_COMMAND:

                audio_buffer.extend(
                    pcm_np.tolist()
                )

                # VAD solo diagnostico.

                vad_result = (
                    process_webrtc_vad(
                        pcm_np
                    )
                )

                speech_detected = (
                    speech_gate.process(
                        pcm_np,
                        now
                    )
                )

                if speech_detected:

                    command_start_time = now

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
                    >= voice_start_timeout
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

                vad_result = (
                    process_webrtc_vad(
                        pcm_np
                    )
                )

                speech_detected = (
                    speech_gate.process(
                        pcm_np,
                        now
                    )
                )

                if debug_wakeword:

                    print(
                        "[DSP] "
                        f"{speech_gate.debug_info(pcm_np)} "
                        f"webrtc={vad_result}"
                    )

                # ------------------------------------------------
                # COMANDO TERMINATO
                #
                # speech_gate diventa False solo quando
                # l'energia rimane sotto la soglia di release
                # per speech_end_seconds.
                # ------------------------------------------------

                if not speech_detected:

                    print(
                        "[LISTENER] "
                        "Fine registrazione: "
                        "silenzio DSP"
                    )

                    break_recording = True

                else:

                    break_recording = False

                # ------------------------------------------------
                # MAX COMMAND TIME
                # ------------------------------------------------

                if command_start_time is None:

                    enter_cooldown(
                        "stato recording non valido"
                    )

                    continue

                command_elapsed = (
                    now
                    - command_start_time
                )

                if (
                    command_elapsed
                    >= max_command_seconds
                ):

                    print(
                        "[LISTENER] "
                        "Timeout massimo comando "
                        f"({command_elapsed:.2f}s)"
                    )

                    break_recording = True

                if not break_recording:

                    continue

                # =================================================
                # POST BUFFER
                # =================================================

                post_samples = int(
                    post_buffer_seconds
                    * TARGET_SAMPLE_RATE
                )

                post_buffer: List[int] = []

                post_deadline = (
                    time.monotonic()
                    \+ 1.0
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

                # =================================================
                # COMMAND DURATION
                # =================================================

                duration = (
                    len(audio_buffer)
                    / TARGET_SAMPLE_RATE
                )

                print(
                    "[LISTENER] "
                    f"Audio comando: "
                    f"{duration:.2f}s"
                )

                # =================================================
                # SEND TO VOSK
                # =================================================

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

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore listener: {exc}"
        )

        stop_event.set()

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
    ```
