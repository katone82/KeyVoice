# ============================================================
# DEBUG LOGGING CONDIVISO
# ============================================================
#
# Un solo interruttore per tutto KeyVoice (wake word listener,
# Vosk, fuzzy parser). Con False vedi solo le azioni vere e
# proprie: wake word rilevata, comando catturato, testo
# trascritto, comando riconosciuto/non riconosciuto, comando
# inviato a Home Assistant. Con True torna tutta la diagnostica
# dettagliata (telemetria XVF, score periodici, dettagli di
# costruzione grammatica/matching, ecc.).
#
# Il valore reale viene impostato da run_service.py leggendo la
# chiave "debug_logging" da config/config.json, PRIMA di avviare
# i thread. Il default qui sotto vale solo per l'esecuzione
# standalone dei singoli script (es. `python3
# xvf3800_wakeword_listener.py` da solo, senza passare da
# run_service.py).
#
# IMPORTANTE per chi importa questo modulo: usare sempre
#     import debug_config
#     ... debug_config.DEBUG_LOGGING ...
# e MAI
#     from debug_config import DEBUG_LOGGING
# Quest'ultima copia il valore al momento dell'import: se
# run_service.py lo aggiorna dopo (leggendo config.json), i
# moduli che l'hanno già importato con "from...import" continuano
# a vedere il vecchio valore. Con "import debug_config" e accesso
# tramite attributo, invece, si legge sempre il valore corrente.

DEBUG_LOGGING = False