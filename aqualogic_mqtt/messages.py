import json
import logging
from aqualogic.core import AquaLogic
from aqualogic.states import States

class Messages:
    _identifier = None
    _discover_prefix = None
    _root = None
    _values_for_control_state_dict = None
    _ha_status_path = None
    _onoff = {False: "OFF", True: "ON"}
    
    def __init__(self, identifier, discover_prefix):
        self._identifier = identifier #TODO: Sanitize?
        self._discover_prefix = discover_prefix #TODO: Sanitize?
        self._root = f"{self._discover_prefix}/device/{self._identifier}"
        self._ha_status_path = f"{self._discover_prefix}/status" #TODO: Make path configurable

        self._values_for_control_state_dict = {
            States.CHECK_SYSTEM: { "key": "cs", "id": f"{ self._identifier }_binary_sensor_check_system", "name": "Check System" },
            States.LIGHTS: { "key": "l", "id": f"{ self._identifier }_switch_lights", "name": "Lights" },
            States.FILTER: { "key": "f", "id": f"{ self._identifier }_switch_filter", "name": "Filter" },
            States.AUX_1: { "key": "aux1", "id": f"{ self._identifier }_switch_aux_1", "name": "Aux 1" },
            States.AUX_2: { "key": "aux2", "id": f"{ self._identifier }_switch_aux_2", "name": "Aux 2" },
            States.SUPER_CHLORINATE: { "key": "sc", "id": f"{ self._identifier }_switch_super_chlorinate", "name": "Super Chlorinate" }
        }

    def get_subscription_topics(self):
        return [f"{self._discover_prefix}/device/{self._identifier}/+/set"]
    
    def get_discovery_topic(self):
        return f"{self._root}/config"
    
    def get_state_topic(self):
        return f"{self._root}/state"
    
    def _get_id_for_al_state(self, state): #TODO: Make public?
        return self._values_for_control_state_dict[state]["id"]

    def get_state_message(self, panel):
        return json.dumps({
            "t_a": panel.air_temp,
            "t_p": panel.pool_temp,
            "t_s": panel.spa_temp,
            "s_p": panel.pump_speed,
            "p_p": panel.pump_power,
            "c_p": panel.pool_chlorinator,
            "c_s": panel.spa_chlorinator,
            "salt": panel.salt_level,
            "cs": self._onoff[panel.get_state(States.CHECK_SYSTEM)],
            "l": self._onoff[panel.get_state(States.LIGHTS)],
            "f": self._onoff[panel.get_state(States.FILTER)],
            "aux1": self._onoff[panel.get_state(States.AUX_1)],
            "aux2": self._onoff[panel.get_state(States.AUX_2)],
            "sc": self._onoff[panel.get_state(States.SUPER_CHLORINATE)],
        })
    
    #TODO: ^ and v move out of this class, to divorce it from Aqualogic panel?

    def handle_message_on_topic(self, topic, msg, panel):
        if topic == self._ha_status_path and msg == "online": #TODO: Make configurable?
            return [(self.get_discovery_topic(), self.get_discovery_message())] 
        
        state_dict_filtered = { s:d for (s,d) in self._values_for_control_state_dict.items() if f"{self._root}/{d['id']}/set" == topic }
        logging.debug(f"{state_dict_filtered=}")
        for s in state_dict_filtered: # Really there will be only one...
            panel.set_state(s, True if msg == "ON" else False)
            return []

    def get_discovery_message(self):
        p =  {
            "dev": {
                "ids": self._identifier,
                "name": self._identifier,
                "mf": "Hayward",
                "mdl": "RS485", #TODO: Probably not.
                "sw": "0.0",
                "sn": self._identifier,
                "hw": "0.0"
            },
            "o": {
                "name":"aqualogic_mqtt",
                "sw": "0.0.1a",
                "url": "https://github.com/SphtKr/aqualogic_mqtt"
            },
            "cmps": {
                f"{ self._identifier }_sensor_air_temperature": {
                    "p": "sensor",
                    "dev_cla":"temperature",
                    "unit_of_meas":"°F",
                    "val_tpl":"{{ value_json.t_a }}",
                    "obj_id": f"{ self._identifier }_sensor_air_temperature",
                    "uniq_id": f"{ self._identifier }_sensor_air_temperature",
                    "name": "Air Temperature"
                },
                f"{ self._identifier }_sensor_pool_temperature": {
                    "p": "sensor",
                    "dev_cla": "temperature",
                    "unit_of_meas": "°F",
                    "val_tpl": "{{ value_json.t_p }}",
                    "obj_id": f"{ self._identifier }_sensor_pool_temperature",
                    "uniq_id": f"{ self._identifier }_sensor_pool_temperature",
                    "name": "Pool Temperature"
                },
                f"{ self._identifier }_sensor_spa_temperature": {
                    "p": "sensor",
                    "dev_cla": "temperature",
                    "unit_of_meas": "°F",
                    "val_tpl": "{{ value_json.t_s }}",
                    "obj_id": f"{ self._identifier }_sensor_spa_temperature",
                    "uniq_id": f"{ self._identifier }_sensor_spa_temperature",
                    "name": "Spa Temperature"
                },
                f"{ self._identifier }_binary_sensor_check_system": {
                    "p": "binary_sensor",
                    "dev_cla":"problem",
                    "val_tpl":"{{ value_json.cs }}",
                    "obj_id": f"{ self._identifier }_binary_sensor_check_system",
                    "uniq_id": f"{ self._identifier }_binary_sensor_check_system",
                    "name": "Check System"
                },
                f"{ self._identifier }_sensor_pool_chlorinator": {
                    "p": "sensor",
                    "unit_of_meas": "%",
                    "val_tpl": "{{ value_json.c_p }}",
                    "obj_id": f"{ self._identifier }_sensor_pool_chlorinator",
                    "uniq_id": f"{ self._identifier }_sensor_pool_chlorinator",
                    "name": "Pool Chlorinator"
                },
                f"{ self._identifier }_sensor_spa_chlorinator": {
                    "p": "sensor",
                    "unit_of_meas": "%",
                    "val_tpl": "{{ value_json.c_p }}",
                    "obj_id": f"{ self._identifier }_sensor_spa_chlorinator",
                    "uniq_id": f"{ self._identifier }_sensor_spa_chlorinator",
                    "name": "Spa Chlorinator"
                },
                f"{ self._identifier }_sensor_salt_level": {
                    "p": "sensor",
                    "unit_of_meas": "ppm",
                    "val_tpl": "{{ value_json.salt }}",
                    "obj_id": f"{ self._identifier }_sensor_salt_level",
                    "uniq_id": f"{ self._identifier }_sensor_salt_level",
                    "name": "Salt Level"
                }
            },
            "stat_t": self.get_state_topic(),
            "qos": 2
        }
        for s in [States.FILTER, States.LIGHTS, States.AUX_1, States.AUX_2, States.SUPER_CHLORINATE]:
            p['cmps'][self._get_id_for_al_state(s)] = {
                "p": "switch",
                "dev_cla": "light" if s == States.LIGHTS else "switch",
                "val_tpl":"{{ value_json." + self._values_for_control_state_dict[s]["key"] + " }}",
                "uniq_id": self._get_id_for_al_state(s),
                "obj_id": self._get_id_for_al_state(s),
                "name": self._values_for_control_state_dict[s]["name"], #TODO: Make method?
                "cmd_t": f"{self._root}/{self._get_id_for_al_state(s)}/set"
            }
        return json.dumps(p)
