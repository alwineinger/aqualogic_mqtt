import threading
import logging
import sys
import ssl
from time import sleep

import paho.mqtt.client as mqtt

from aqualogic.core import AquaLogic
from aqualogic.states import States

from .messages import Messages

# Monkey-patch broken serial method in Aqualogic
def _patched_write_to_serial(self, data):
    self._serial.write(data)
    self._serial.flush()
AquaLogic._write_to_serial = _patched_write_to_serial
# Monkey-patch class property into _web so we can avoid running the web server without an error
class _WebDummy:
    def text_updated(self, str):
        return
AquaLogic._web = _WebDummy()

logging.basicConfig(level=logging.DEBUG)

class Client:
    _panel = None
    _paho_client = None
    _panel_thread = None
    _identifier = None
    _discover_prefix = None
    _formatter = None

    def __init__(self, identifier="aqualogic", discover_prefix="homeassistant"):
        self._identifier = identifier
        self._discover_prefix = discover_prefix

        self._formatter = Messages(identifier, discover_prefix)

        self._panel = AquaLogic(web_port=0)
        self._paho_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        self._paho_client.on_message = self._on_message
        self._paho_client.on_connect = self._on_connect

    # Respond to panel events
    def _panel_changed(self, panel):
        logging.debug(f"_panel_changed called... Publishing to {self._formatter.get_state_topic()}...")
        msg = self._formatter.get_state_message(panel)
        logging.debug(msg)
        self._paho_client.publish(self._formatter.get_state_topic(), msg)

    # Respond to MQTT events    
    def _on_message(self, client, userdata, msg):
        logging.debug(f"_on_message called for topic {msg.topic} with payload {msg.payload}")
        new_messages = self._formatter.handle_message_on_topic(msg.topic, str(msg.payload.decode("utf-8")), self._panel)
        for t, m in new_messages:
            self._paho_client.publish(t, m)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        logging.debug("_on_connect called")
        sub_topics = self._formatter.get_subscription_topics()
        for topic in sub_topics:
            self._paho_client.subscribe(topic)
        logging.debug(f"Publishing to {self._formatter.get_discovery_topic()}...")
        logging.debug(self._formatter.get_discovery_message())
        self._paho_client.publish(self._formatter.get_discovery_topic(), self._formatter.get_discovery_message())
        ...

    def connect_panel(self, source):
        if ':' in source:
            s_host, s_port = source.split(':')
            self._panel.connect(s_host, int(s_port))
        else:
            self._panel.connect_serial(source)
        ...

    def connect_mqtt(self, dest:(str), port:(int)=1883, keepalive=60):
        host = dest
        if dest is not None:
            if ':' in dest:
                host, port = dest.split(':')
                port = int(port)
            else:
                host = dest
        self._paho_client.tls_set(cert_reqs=ssl.CERT_NONE)
        r = self._paho_client.connect(host, port, keepalive)
        logging.debug(f"Connected to {host}:{port} with result {r}")

    def loop_forever(self):
        try:
            #self._paho_client.loop_start()
            self._panel_thread = threading.Thread(target=self._panel.process, args=[self._panel_changed])
            self._panel_thread.start()
            self._paho_client.loop_forever()
            #while True:
            #    sleep(1)
        finally:
            #self._paho_client.loop_stop()
            pass
        
        

if __name__ == "__main__":
    autodisc_prefix = None
    source = None
    dest = None
    if len(sys.argv) >= 3 and len(sys.argv) < 5:
        print('Connecting to {}...'.format(sys.argv[1]))
        source = sys.argv[1]
        dest = sys.argv[2]
        if len(sys.argv) == 4 and not sys.argv[3] == '':
            autodisc_prefix = sys.argv[3]
    else:
        print('Usage: python -m aqualogic_mqtt.client [/serial/path|tcphost:port] [mqttdest] [autodiscover_prefix]')
        quit()

    mqtt_client = Client()
    mqtt_client.connect_mqtt(dest=dest)
    mqtt_client.connect_panel(source)
    mqtt_client.loop_forever()

    
