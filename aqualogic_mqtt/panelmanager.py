import time

# At present PanelManager only keeps track of system messages, though
# it may expand to handle all aqualogic.panel concerns in the future.
class PanelManager:
    _exp_s = None

    def __init__(self, exp_seconds:(int)):
        self._exp_s = exp_seconds
        self._registry = {}

    def observe_system_message(self, message:(str)):
        if message is None:
            return
        message = message.strip(' \x00')
        now = time.time()
        self._registry[message] = now
        exp = now - self._exp_s
        self._registry = { k:v for k,v in self._registry.items() if v > exp }

    def get_system_messages(self):
        return sorted(self._registry.keys())