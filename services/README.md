# Timer vocale

Il countdown dei timer vive in Home Assistant (dominio
`timer`), non in KeyVoice. KeyVoice si occupa solo di:

1. riconoscere il comando vocale (avvio/cancellazione, durata,
   nome libero);
2. chiedere a HA di avviare/cancellare il timer via REST
   (`timer.start` / `timer.cancel`);
3. ascoltare via MQTT quando HA segnala che un timer è finito,
   e dare il feedback sonoro.

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
generica):

    cancella il timer
    cancella il timer torta
    ferma timer chiamato torta
    annulla timer
    stop timer

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

## Configurazione opzionale in config.json

Broker MQTT usato per la notifica di fine timer (default: lo
stesso broker locale di zigbee2mqtt):

    {
      "mqtt": {
        "host": "127.0.0.1",
        "port": 1883,
        "username": "zigbee2mqtt",
        "password": "..."
      }
    }
