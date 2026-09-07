# KeyVoice

**KeyVoice** è un'applicazione Python per il riconoscimento vocale e l'elaborazione di comandi vocali. Utilizza il motore di riconoscimento vocale **Vosk** e il sistema di wake word **Porcupine** per attivare l'ascolto in background. È ideale per progetti di domotica, assistenti vocali o interfacce vocali personalizzate.

---

## 📖 Indice

1. [Descrizione del progetto](#descrizione-del-progetto)  
2. [Workflow del progetto](#workflow-del-progetto)  
3. [Requisiti](#requisiti)  
4. [Pre-configurazione](#pre-configurazione)  
5. [Installazione](#installazione)  
6. [Struttura del progetto](#struttura-del-progetto)  
7. [Avvio del servizio](#avvio-del-servizio)  
8. [Riconoscimento vocale](#riconoscimento-vocale)  
9. [Elaborazione del comando](#elaborazione-del-comando)  
10. [Esecuzione dell’azione](#esecuzione-dellazione)  
11. [Feedback vocale](#feedback-vocale)  
12. [Debug e test](#debug-e-test)  
13. [Contatti](#contatti)  

---

## 1. Descrizione del progetto

KeyVoice permette di:

- Attendere un **wake word** (es. "jarvis") per attivare il riconoscimento vocale.  
- Trascrivere l’audio in tempo reale con **Vosk**.  
- Analizzare i comandi vocali e determinare le azioni da eseguire.  
- Eseguire azioni automatizzate in base ai comandi riconosciuti.  
- Fornire un **feedback vocale** all’utente sull’azione completata.

---

## 2. Workflow del progetto

Il flusso operativo generale:

1. Ascolto del wake word  
2. Riconoscimento vocale  
3. Elaborazione del comando  
4. Esecuzione dell’azione  
5. Feedback vocale  

---

## 3. Requisiti

- Python 3.6 o superiore  
- Microfono funzionante  
- Connessione Internet per configurazione iniziale del wake word (Porcupine)  

---

## 4. Pre-configurazione

Prima dell’installazione, crea o modifica `config.json` nella root del progetto:

```json
{
  "porcupine": {
    "access_key": "INSERISCI_TUA_API_KEY_PORCUPINE",
    "keywords": ["jarvis"],
    "sensitivity": 0.7,
    "pre_buffer_seconds": 0.5,
    "wakeword_trim_ms": 300,
    "silence_threshold": 500,
    "silence_duration": 2
  },
  "vosk": {
    "model_path": "models/vosk-model-small-it-0.22",
    "sample_rate": 16000
  }
}
```

---

## 5. Installazione

Clona il repository e installa le dipendenze:

```bash
sudo apt update
sudo apt install libportaudio2 libportaudiocpp0 portaudio19-dev

sudo apt update
sudo apt install python3-pip python3-venv python3-dev libasound2-dev portaudio19-dev

sudo apt update
sudo apt install python3.10 python3.10-venv python3.10-dev



git clone https://github.com/katone82/KeyVoice.git
cd KeyVoice
#python3 -m venv keyvoiceenv
#source keyvoiceenv/bin/activate
prepara direttamente il virtualenv senza venv e source i due comandi sopra commentati
/bin/sh activate.sh


pip install --upgrade pip

pip uninstall pvporcupine
pip install pvporcupine==2.1.0  # The latest version often includes more ARM CPUs
pip install --upgrade pvporcupine

pip install -r requirements.txt
```

Scarica il modello Vosk per l’italiano da [qui](https://alphacephei.com/vosk/models) e inseriscilo nella cartella `models/`.

---

## 6. Struttura del progetto

```
KeyVoice/
├── src/
│   ├── keyvoice.py
│   ├── wake_word.py
│   ├── recognizer.py
│   ├── command_processor.py
│   ├── feedback.py
│   └── ...
├── models/
│   └── vosk-model-small-it-0.4/
├── config.json
├── requirements.txt
└── README.md
```

---

## 7. Avvio del servizio

Avvia KeyVoice con:

```bash
python3 src/keyvoice.py
```

---

### 7.1 Installazione del servizio

```bash
python3 create_services.py

#Scrivo file di servizio in: /etc/systemd/system/keyvoice.service
#File keyvoice.service creato. Copialo con sudo in /etc/systemd/system e abilita con:

sudo cp keyvoice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable keyvoice
sudo systemctl start keyvoice

```
per leggere i log adesso occorre accedere a journalctl

```bash
sudo journalctl -u keyvoice -f
```

## 8. Riconoscimento vocale

Dopo il rilevamento del wake word, KeyVoice utilizza **Vosk** per trascrivere l’audio in testo.

---

## 9. Elaborazione del comando

Il testo trascritto viene analizzato da `command_processor.py`, che determina l’azione da eseguire in base ai comandi riconosciuti.

---

## 10. Esecuzione dell’azione

Le azioni (es. accendi una luce, avvia una musica) vengono eseguite tramite funzioni definite o integrate con sistemi di domotica.

---

## 11. Feedback vocale

KeyVoice fornisce un feedback vocale all’utente sull’esito dell’azione eseguita (ad esempio, “Luce accesa!”).

---

## 12. Debug e test

Per attivare la modalità debug, modifica il campo `debug` in `config.json`:

```json
{
  ...
  "debug": true
}
```

Utilizza log e messaggi di console per verificare il comportamento durante lo sviluppo.

---

## 13. Contatti

- **Autore:** [Luca Santarelli](https://github.com/katone82)
- **Email:** lucasantarelli82@gmail.com  
- **GitHub:** [KeyVoice Repo](https://github.com/katone82/KeyVoice)

---

**Licenza:** MIT  

pip install -r requirements.txt


trip

modifica volume microfono
amixer cset numid=1 62259
62259 è circa il 95% di 65536.

vedi il log del service


#####
nuova card
cat /proc/asound/cards
0 [vc4hdmi0 ]: vc4-hdmi - vc4-hdmi-0 vc4-hdmi-0 1 [vc4hdmi1 ]: vc4-hdmi - vc4-hdmi-1 vc4-hdmi-1 2 [Array ]: USB-Audio - reSpeaker XVF3800 4-Mic Array Seeed Studio reSpeaker XVF3800 4-Mic Array at usb-xhci-hcd.1-1, high speed 4 [S4 ]: USB-Audio - Sound Blaster Play! 4 Generic Sound Blaster Play! 4 at usb-xhci-hcd.1-2, high speed

Adesso facciamo un passaggio fondamentale prima di modificare KeyVoice: dobbiamo vedere quanti canali e quali sample rate espone realmente il XVF3800.

1. Esegui
   arecord -D hw:2,0 --dump-hw-params /dev/null


Warning: Some sources (like microphones) may produce inaudible results
with 8-bit sampling. Use '-f' argument to increase resolution
e.g. '-f S16_LE'.
Recording WAVE '/dev/null' : Unsigned 8 bit, Rate 8000 Hz, Mono
HW Params of device "hw:2,0":
--------------------
ACCESS:  MMAP_INTERLEAVED RW_INTERLEAVED
FORMAT:  S16_LE
SUBFORMAT:  STD
SAMPLE_BITS: 16
FRAME_BITS: 32
CHANNELS: 2
RATE: 16000
PERIOD_TIME: [1000 1000000]
PERIOD_SIZE: [16 16000]
PERIOD_BYTES: [64 64000]
PERIODS: [2 1024]
BUFFER_TIME: [2000 2000000]
BUFFER_SIZE: [32 32000]
BUFFER_BYTES: [128 128000]
TICK_TIME: ALL
--------------------
arecord: set_params:1352: Sample format non available
Available formats:
- S16_LE

Configurazione rilevata

Il device:

hw:2,0

espone solo:

FORMAT:   S16_LE
CHANNELS: 2
RATE:     16000

Quindi abbiamo:

XVF3800
│
├── 16 bit
├── 16 kHz
└── 2 canali

devo capire quale canale e asr
arecord -D hw:1,0 -f S16_LE -r 16000 -c 2 -d 5 xvf3800_test.wav

quello che si sente meglio è asr
ffmpeg -i xvf3800_test.wav -map_channel 0.0.0 channel0.wav
ffmpeg -i xvf3800_test.wav -map_channel 0.0.0 channel0.wav

######### test respeaker
cd xvf3800-tool
chmod +x install.sh
./install.sh

Poi copia il xvf_host.py ufficiale in:

xvf3800-tool/vendor/xvf_host.py

e facciamo:

sudo .venv/bin/python xvf3800_tool.py status


sono rimasto a monitor segnali



##custom params
sudo .venv/bin/python xvf3800_tool.py telemetry
ReadCMD: cmdid: 203, resid: 33, payload: [0, 142, 14, 33, 64, 83, 121, 171, 64, 95, 22, 14, 64, 142, 14, 33, 64]
AEC_AZIMUTH_VALUES             (2.5165133476257324, 5.358560085296631, 2.2201154232025146, 2.5165133476257324)
ReadCMD: cmdid: 208, resid: 33, payload: [0, 250, 152, 196, 71, 0, 0, 0, 0, 136, 5, 118, 70, 250, 152, 196, 71]
AEC_SPENERGY_VALUES            (100657.953125, 0.0, 15745.3828125, 100657.953125)
DoA degrees: [144.2, 307.0, 127.2, 144.2]
Speech: [True, False, True, True]

#####verifica telemetria
sudo .venv/bin/python xvf3800_tool.py test --device hw:2,0 --seconds 20 --channels 2
[TEST] duration=20s device=hw:2,0 rate=16000 channels=2
[TEST] Speak normally from different directions during the recording.
arecord: main:831: audio open error: Device or resource busy
[TELEMETRY ERROR] Unknown status code: 68
[OK] audio:     /home/homeassistant/KeyVoice/xvf3800-tool/recordings/20260904_224733/xvf3800.wav
[OK] telemetry: /home/homeassistant/KeyVoice/xvf3800-tool/recordings/20260904_224733/telemetry.csv
[TEST] Now you have a real synchronized audio + DSP telemetry sample.






la card per riprodurre i suoni
aplay -D plughw:3,0 sounds/wake.wav

aplay -l
**** List of PLAYBACK Hardware Devices ****
card 0: vc4hdmi0 [vc4-hdmi-0], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
Subdevices: 1/1
Subdevice #0: subdevice #0
card 1: vc4hdmi1 [vc4-hdmi-1], device 0: MAI PCM i2s-hifi-0 [MAI PCM i2s-hifi-0]
Subdevices: 1/1
Subdevice #0: subdevice #0
card 2: Array [reSpeaker XVF3800 4-Mic Array], device 0: USB Audio [USB Audio]
Subdevices: 1/1
Subdevice #0: subdevice #0
card 3: S4 [Sound Blaster Play! 4], device 0: USB Audio [USB Audio]
Subdevices: 1/1
Subdevice #0: subdevice #0