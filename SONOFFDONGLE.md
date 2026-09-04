lsusb
Bus 004 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 003 Device 004: ID 1a86:55d4 QinHeng Electronics SONOFF Zigbee 3.0 USB Dongle Plus V2
Bus 003 Device 002: ID 2886:001a Seeed Technology Co., Ltd. reSpeaker XVF3800 4-Mic Array
Bus 003 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 002 Device 001: ID 1d6b:0003 Linux Foundation 3.0 root hub
Bus 001 Device 002: ID 041e:3274 Creative Technology, Ltd Sound Blaster Play! 4
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
katone@raspyha:~ $ ls -l /dev/serial/by-id/
total 0
lrwxrwxrwx 1 root root 13 Sep  3 23:04 usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_20230329111745-if00 -> ../../ttyACM0

####poi env ha
docker ps
systemctl status home-assistant --no-pager
systemctl status mosquitto --no-pager
CONTAINER ID   IMAGE                                          COMMAND                  CREATED         STATUS         PORTS     NAMES
cc97ed5e6a38   ghcr.io/home-assistant/home-assistant:stable   "/init"                  3 weeks ago     Up 8 minutes             homeassistant
a169e5ada844   alexxit/go2rtc:latest                          "/sbin/tini -- go2rt…"   11 months ago   Up 8 minutes             go2rtc
999fdc18381b   bentel-absoluta:local                          "/__cacert_entrypoin…"   11 months ago   Up 8 minutes             bentel-absoluta
Unit home-assistant.service could not be found.
● mosquitto.service - Mosquitto MQTT Broker
Loaded: loaded (/lib/systemd/system/mosquitto.service; enabled; preset: enabled)
Active: active (running) since Wed 2026-09-02 12:42:42 CEST; 1 day 10h ago
Docs: man:mosquitto.conf(5)
man:mosquitto(8)
Process: 917 ExecStartPre=/bin/mkdir -m 740 -p /var/log/mosquitto (code=exited, status=0/SUCCESS)
Process: 920 ExecStartPre=/bin/chown mosquitto /var/log/mosquitto (code=exited, status=0/SUCCESS)
Process: 924 ExecStartPre=/bin/mkdir -m 740 -p /run/mosquitto (code=exited, status=0/SUCCESS)
Process: 925 ExecStartPre=/bin/chown mosquitto /run/mosquitto (code=exited, status=0/SUCCESS)
Main PID: 926 (mosquitto)
Tasks: 1 (limit: 4757)
CPU: 132ms
CGroup: /system.slice/mosquitto.service
└─926 /usr/sbin/mosquitto -c /etc/mosquitto/mosquitto.conf

Sep 02 12:42:40 raspyha systemd[1]: Starting mosquitto.service - Mosquitto MQTT Broker...
Sep 02 12:42:42 raspyha systemd[1]: Started mosquitto.service - Mosquitto MQTT Broker.



#utente dedicato
sudo mosquitto_passwd /etc/mosquitto/passwd zigbee2mqtt
password: doongle

sudo systemctl restart mosquitto



Crea la directory
sudo mkdir -p /opt/zigbee2mqtt/data
sudo chown -R $USER:$USER /opt/zigbee2mqtt
2. Creiamo il docker-compose.yml
   nano /opt/zigbee2mqtt/docker-compose.yml

Inserisci:

services:
zigbee2mqtt:
container_name: zigbee2mqtt
image: koenkk/zigbee2mqtt:latest
restart: unless-stopped

    volumes:
      - /opt/zigbee2mqtt/data:/app/data

    ports:
      - "8080:8080"

    devices:
      - /dev/serial/by-id/usb-ITEAD_SONOFF_Zigbee_3.0_USB_Dongle_Plus_V2_20230329111745-if00:/dev/ttyACM0

    environment:
      - TZ=Europe/Rome

Salva con:

CTRL+O
INVIO
CTRL+X
3. Configuriamo Zigbee2MQTT

Ora:

nano /opt/zigbee2mqtt/data/configuration.yaml

Metti:

homeassistant:
enabled: true

mqtt:
server: mqtt://192.168.1.50:1883
user: zigbee2mqtt
password: "LA_PASSWORD_CHE_HAI_IMPOSTATO"

serial:
port: /dev/ttyACM0
adapter: ember

frontend:
enabled: true
port: 8080

advanced:
channel: 11
network_key: GENERATE
pan_id: GENERATE
ext_pan_id: GENERATE

permit_join: false

Nota importante: il tuo ZBDongle-E usa il firmware Ember, quindi adapter: ember è la scelta corretta.

4. Avviamo Zigbee2MQTT
   cd /opt/zigbee2mqtt
   docker compose up -d

Controlliamo:

docker ps

Dovresti vedere:

zigbee2mqtt
homeassistant
go2rtc
bentel-absoluta

Poi:

docker logs zigbee2mqtt --tail 50
5. Interfaccia Zigbee2MQTT

Dal PC apri:

http://192.168.1.50:8080

Dovrebbe comparire l'interfaccia di Zigbee2MQTT.

Per ora però non fare il pairing di nessun dispositivo.

Quando hai eseguito:

docker compose up -d

mandami l'output di:

docker logs zigbee2mqtt --tail 50




####aggiusta rete
sudo ip link set wlan0 mtu 1492

finito abbiamo la rete 

New network formed!
PAN ID: 14844
Channel: 11


data da docker logs --tail 50 zigbee2mqtt dopo il docker compose up -d
