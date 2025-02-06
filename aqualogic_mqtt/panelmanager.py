import time
import logging

logger = logging.getLogger(__name__)

# At present PanelManager only keeps track of system messages, though
# it may expand to handle all aqualogic.panel concerns in the future.
class PanelManager:
    _timeout = None
    _exp_s = None
    _last_text_update = None

    def __init__(self, connect_timeout:(int), message_exp_seconds:(int)):
        self._last_text_update = time.time()
        self._timeout = connect_timeout
        self._exp_s = message_exp_seconds
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
    
    def get_last_update_age(self):
        return time.time() - self._last_text_update
    
    def is_updating(self):
        return (time.time() - self._last_text_update) < self._timeout

    # This is a method with the same name/sig as one in aqualogic.web.WebServer. This
    # allows 1: monkey-patching this class into aqualogic to allow the process loop to
    # function without its web server running, 2: us to pick up activity and screen
    # updates from the panel (e.g. to determine if the connection is lost).
    def text_updated(self, str):
        self._last_text_update = time.time()
        logger.debug(f"text_updated: {str}")
        return
