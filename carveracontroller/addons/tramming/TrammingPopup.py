import threading
import logging

from kivy.clock import Clock
from kivy.properties import StringProperty, BooleanProperty
from kivy.uix.modalview import ModalView

from .TrammingRunner import TrammingRunner
# Imported so kv can resolve the <TrammingSettings> class used in the tabs.
from .TrammingSettings import TrammingSettings  # noqa: F401

logger = logging.getLogger(__name__)


class TrammingPopup(ModalView):
    status_text = StringProperty('Ready')
    running = BooleanProperty(False)

    def __init__(self, controller, **kwargs):
        self.controller = controller
        # Dedicated stop flag for the Stop button. controller.stop is the
        # streamIO shutdown flag and must NOT be reused here - the runner only
        # reads it so a tramming loop bails when the app is disconnecting.
        self._stop_event = threading.Event()
        self._thread = None
        super(TrammingPopup, self).__init__(**kwargs)

    def _settings_for(self, axis):
        return self.ids['settings_%s' % axis.lower()]

    def start_tramming(self, axis):
        if self.running:
            return
        cfg = self._settings_for(axis).get_config()
        try:
            minv = float(cfg['min'])
            maxv = float(cfg['max'])
        except (ValueError, KeyError):
            self.status_text = 'Set both Min and Max positions first.'
            return
        if abs(maxv - minv) < 1e-6:
            self.status_text = 'Min and Max must differ.'
            return
        try:
            feed = float(cfg.get('feed') or '500')
        except ValueError:
            feed = 500.0
        if feed <= 0:
            feed = 500.0
        try:
            loops = int(float(cfg.get('loops') or '0'))
        except ValueError:
            loops = 0
        if loops < 0:
            loops = 0

        self._stop_event.clear()
        runner = TrammingRunner(
            self.controller, axis, minv, maxv, feed, loops,
            self._stop_event, self._on_update, self._on_finish)
        self.running = True
        self.status_text = 'Tramming %s ...' % axis
        self._thread = threading.Thread(target=runner.run, daemon=True)
        self._thread.start()

    def stop_tramming(self):
        self._stop_event.set()
        self.status_text = 'Stopping after current move ...'

    def _on_update(self, loop_count):
        Clock.schedule_once(lambda dt: setattr(
            self, 'status_text', 'Completed %d loop(s) ...' % loop_count))

    def _on_finish(self, reason):
        msg = {
            'stopped': 'Stopped.',
            'done': 'Finished (loop limit reached).',
            'alarm': 'Stopped: machine alarm.',
            'timeout': 'Stopped: move timed out.',
            'error': 'Stopped: error (see log).',
        }.get(reason, 'Stopped.')

        def done(dt):
            self.running = False
            self.status_text = msg
        Clock.schedule_once(done)

    def on_dismiss(self):
        # Never leave a sweep running once the dialog is closed.
        self._stop_event.set()
