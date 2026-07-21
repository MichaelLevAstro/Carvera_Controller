from kivy.properties import StringProperty
from kivy.uix.modalview import ModalView
from kivy.uix.recycleview import RecycleView
from kivy.uix.recycleview.views import RecycleDataViewBehavior
from kivy.uix.recycleboxlayout import RecycleBoxLayout
from kivy.uix.behaviors import FocusBehavior
from kivy.uix.recycleview.layout import LayoutSelectionBehavior

import threading
import logging
logger = logging.getLogger(__name__)

from carveracontroller.addons.probing.operations.OperationsBase import OperationsBase

class ProbingPreviewPopup(ModalView):
    title = StringProperty('Confirm')
    probe_preview_label = StringProperty('N/A')
    config: dict[str, float]
    gcode = StringProperty("")


    def __init__(self, controller, **kwargs):
        self.controller = controller
        # On the Z1 the operation is either a host-side G-code script
        # (z1_lines) or a live runner callable (z1_runner), set by
        # ProbingPopup.show_preview. Both None means the normal single-command path.
        self.z1_lines = None
        self.z1_runner = None
        super(ProbingPreviewPopup, self).__init__(**kwargs)

    def get_probe_switch_type(self):
        return 1
        # if self.cb_probe_normally_closed.active:
        #     return ProbingConstants.switch_type_nc
        #
        # if self.cb_probe_normally_open.active:
        #     return ProbingConstants.switch_type_no

    def  start_probing(self):
        if self.z1_runner is not None:
            # Z1: multi-step / compute operation, run in a background thread so
            # it can wait for probe results without blocking the UI.
            logger.debug("running Z1 probing runner")
            threading.Thread(target=self._run_z1_runner, daemon=True).start()
        elif self.z1_lines is not None:
            # Z1: stream the host-side probing script line by line.
            logger.debug("running Z1 probing script: %s", self.z1_lines)
            for line in self.z1_lines:
                self.controller.executeCommand(line + "\n")
        elif len(self.gcode) > 0:
            logger.debug("running gcode: " + self.gcode)
            self.controller.executeCommand(self.gcode + "\n")
        else:
            logger.error("no gcode")

    def _run_z1_runner(self):
        try:
            self.z1_runner()
        except Exception:
            logger.exception("Z1 probing runner failed")

class PopupMDI(RecycleView):

    def __init__(self, **kwargs):
        super(PopupMDI, self).__init__(**kwargs)