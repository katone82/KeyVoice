# Timer vocale

Il countdown dei timer vive in Home Assistant (dominio
`timer`), non in KeyVoice. KeyVoice si occupa solo di:

1. riconoscere il comando vocale (avvio/cancellazione, durata,
   nome libero);
2. chiedere a HA di avviare/cancellare il timer via REST
   (`timer.start` / `timer.cancel`);
3. ascoltare via MQTT quando HA segnala che un timer è finito,
   e dare il feedback sonoro;
4. annunciare a voce la creazione del timer (es. "timer torta
   di dieci minuti creato") tramite sintesi vocale
   (espeak-ng o Piper, vedi sotto).

## Setup Home Assistant

1. Aggiungere le 5 entità timer del pool: vedi
   [ha/keyvoice_timer_helpers.yaml](../ha/keyvoice_timer_helpers.yaml)
   (`timer.keyvoice_1` .. `timer.keyvoice_5`).
2. Aggiungere l'automazione che notifica KeyVoice a fine
   timer: vedi
   [ha/keyvoice_timer_automation.yaml](../ha/keyvoice_timer_automation.yaml).

Il numero di timer paralleli è fisso a 5 (`TIMER_POOL_SIZE` in
`fuzzy_parser.py`): per cambiarlo, aggiornare sia la costante
sia il numero di entità in `keyvoice_timer_helpers.yaml`.

## Comandi vocali

Avvio (con o senza nome libero):

    timer di dieci minuti
    crea un timer di dieci minuti
    crea un timer torta di dieci minuti
    crea un timer chiamato torta di dieci minuti
    avvia un timer per la pizza di due ore

Cancellazione (per nome se più timer sono attivi, altrimenti
generica) — spegne anche subito la suoneria di un timer già
scaduto, se in corso:

    cancella il timer
    cancella il timer torta
    ferma timer chiamato torta
    annulla timer
    stop timer
    spegni timer

Durate riconosciute: 1-60 minuti, 1-12 ore (vedi
`TIMER_MAX_MINUTI` / `TIMER_MAX_ORE` in `vosk_listener.py`).

## Perché il nome funziona anche se la grammatica Vosk è chiusa

La grammatica Vosk (`build_vosk_grammar()`) è a vocabolario
chiuso: qualunque parola non elencata diventa `[unk]`, senza
modo di recuperarne il testo. Il nome del timer è per
definizione imprevedibile, quindi non può stare nella
grammatica.

Per questo, quando la frase riconosciuta con la grammatica
contiene la parola "timer" (o è del tutto `[unk]`),
`vosk_listener.py` rifà la decodifica sullo stesso audio con un
secondo recognizer SENZA grammatica (dettatura libera), e usa
quel testo per riconoscere nome e durata. Per tutti gli altri
comandi (domotica) il comportamento resta quello di prima,
invariato.

## Setup sintesi vocale (annuncio "timer creato")

Due motori intercambiabili (`command_feedback.tts_engine` in
config.json), stesso comportamento per il resto del sistema.

### espeak-ng (default, zero setup extra)

Un solo pacchetto apt, voce robotica ma affidabile:

    sudo apt install espeak-ng

### Piper (voce neurale, molto più naturale, sempre leggero)

Pensato apposta per Raspberry Pi (è il motore TTS locale usato
da Home Assistant Assist).

1. Installare il pacchetto Python (nel venv di KeyVoice):

       pip install piper-tts

2. Scaricare una voce italiana. `download_voices` NON accetta
   un prefisso di lingua per elencare le voci (dà errore
   "did not match pattern") — va passato l'id completo
   `<lingua>-<nome>-<qualità>`. Al momento esistono solo due
   voci italiane:

       python -m piper.download_voices it_IT-paola-medium
       python -m piper.download_voices it_IT-riccardo-x_low

   `paola-medium` è nettamente la scelta migliore (voce
   femminile, qualità media — su Pi 4 resta in tempo reale);
   `riccardo` è solo in qualità `x_low`, più simile a espeak-ng.
   L'elenco aggiornato delle voci di tutte le lingue è su
   [huggingface.co/rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).

   Il comando scarica sia il file `.onnx` che `.onnx.json` di
   configurazione (servono entrambi, stesso nome, stessa
   cartella), di norma in `~/.local/share/piper-voices/` —
   segna il percorso completo del file `.onnx`, serve al passo
   successivo.

3. Configurare in config.json (vedi sotto) `tts_engine: "piper"`
   e `piper_model` con il percorso assoluto del file `.onnx`
   scaricato.

Se il modello configurato manca, KeyVoice lo segnala all'avvio
(`[SOUND] ATTENZIONE: tts_engine=piper ma piper_model non
trovato`) e l'annuncio viene semplicemente saltato finché non
lo si corregge — il resto del timer continua a funzionare.

## Configurazione opzionale in config.json

Broker MQTT usato per la notifica di fine timer (default: lo
stesso broker locale di zigbee2mqtt), e sintesi vocale (default:
espeak-ng, italiano, 150 parole/min):

    {
      "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "username": "zigbee2mqtt",
        "password": "..."
      },
      "command_feedback": {
        "tts_engine": "espeak",
        "tts_voice": "it",
        "tts_speed": 150
      }
    }

Per usare Piper invece di espeak-ng, sostituire la sezione
`command_feedback` con (il JSON non ammette commenti, quindi
niente `tts_voice`/`tts_speed` qui: sono specifici di
espeak-ng):

    {
      "command_feedback": {
        "tts_engine": "piper",
        "piper_model": "/home/homeassistant/.local/share/piper-voices/it_IT-paola-medium.onnx"
      }
    }
