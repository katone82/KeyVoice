"""
Listener MQTT per la notifica di fine timer da Home Assistant.

Il countdown vive interamente in HA (entità timer.keyvoice_N).
Quando un timer finisce, un'automazione HA pubblica un
messaggio su keyvoice/timer/finished con l'entity_id
interessato (vedi ha/keyvoice_timer_automation.yaml). Questo
modulo si limita ad ascoltare quel topic e a inoltrare
l'entity_id al chiamante.
"""

import json

import paho.mqtt.client as mqtt


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1883
DEFAULT_USERNAME = "zigbee2mqtt"
DEFAULT_PASSWORD = "doongle"

FINISHED_TOPIC = "keyvoice/timer/finished"


def avvia_listener_timer_mqtt(
    config: dict,
    on_finished
):
    """
    Connette al broker MQTT (stessa istanza usata da
    zigbee2mqtt/Home Assistant) e chiama on_finished(entity_id)
    per ogni notifica di fine timer ricevuta.

    Configurazione opzionale in config.json:

        "mqtt": {
            "host": "127.0.0.1",
            "port": 1883,
            "username": "zigbee2mqtt",
            "password": "..."
        }
    """

    mqtt_config = config.get(
        "mqtt",
        {}
    )

    host = mqtt_config.get(
        "host",
        DEFAULT_HOST
    )

    port = mqtt_config.get(
        "port",
        DEFAULT_PORT
    )

    username = mqtt_config.get(
        "username",
        DEFAULT_USERNAME
    )

    password = mqtt_config.get(
        "password",
        DEFAULT_PASSWORD
    )

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="keyvoice-timer-listener",
    )

    if username:
        client.username_pw_set(
            username,
            password
        )

    def _on_connect(
        client,
        userdata,
        flags,
        reason_code,
        properties=None,
    ):
        if reason_code == 0:
            print(
                "[TIMER-MQTT] Connesso al broker MQTT"
            )

            client.subscribe(
                FINISHED_TOPIC,
                qos=1
            )

        else:
            print(
                "[TIMER-MQTT] Connessione MQTT fallita: "
                f"{reason_code}"
            )

    def _on_disconnect(
        client,
        userdata,
        disconnect_flags,
        reason_code,
        properties=None,
    ):
        print(
            "[TIMER-MQTT] Disconnesso dal broker MQTT"
        )

    def _on_message(
        client,
        userdata,
        msg,
    ):
        try:
            payload = json.loads(
                msg.payload.decode("utf-8")
            )

            entity_id = payload.get(
                "entity_id"
            )

            if entity_id:
                on_finished(
                    entity_id
                )

        except Exception as exc:
            print(
                "[TIMER-MQTT] Errore parsing messaggio: "
                f"{exc}"
            )

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_message = _on_message

    try:
        client.connect(
            host,
            port,
            keepalive=60,
        )

        client.loop_start()

        print(
            "[TIMER-MQTT] Connessione a "
            f"{host}:{port} avviata"
        )

    except Exception:
        print(
            "[TIMER-MQTT] Impossibile connettersi "
            "al broker MQTT"
        )

    return client
