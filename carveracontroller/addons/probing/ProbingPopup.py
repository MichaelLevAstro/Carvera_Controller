import logging

from kivy.clock import Clock
from kivy.uix.modalview import ModalView

from carveracontroller.translation import tr

from ... import Controller
from . import z1_probing
from .operations.Angle.AngleOperationType import AngleOperationType
from .operations.Angle.AngleSettings import AngleSettings
from .operations.Bore.BoreOperationType import BoreOperationType
from .operations.Bore.BoreSettings import BoreSettings
from .operations.Boss.BossOperationType import BossOperationType
from .operations.Boss.BossSettings import BossSettings
from .operations.Calibration.CalibrationOperationType import CalibrationOperationType
from .operations.Calibration.CalibrationSettings import CalibrationSettings
from .operations.ConfigUtils import get_machine_config_hint
from .operations.FourthAxis.FourthAxisOperationType import FourthAxisOperationType
from .operations.FourthAxis.FourthAxisSettings import FourthAxisSettings
from .operations.InsideCorner.InsideCornerOperationType import InsideCornerOperationType
from .operations.InsideCorner.InsideCornerSettings import InsideCornerSettings
from .operations.OperationsBase import OperationsBase
from .operations.OutsideCorner.OutsideCornerOperationType import OutsideCornerOperationType
from .operations.OutsideCorner.OutsideCornerSettings import OutsideCornerSettings
from .operations.ProbeTip.ProbeTipOperationType import ProbeTipOperationType
from .operations.ProbeTip.ProbeTipSettings import ProbeTipSettings
from .operations.SingleAxis.SingleAxisProbeOperationType import SingleAxisProbeOperationType
from .operations.SingleAxis.SingleAxisProbeSettings import SingleAxisProbeSettings
from .preview.ProbingPreviewPopup import ProbingPreviewPopup

logger = logging.getLogger(__name__)

from kivy.app import App


class ProbingPopup(ModalView):
    controller: Controller

    def __init__(self, controller, **kwargs):
        self.outside_corner_settings = None
        self.inside_corner_settings = None
        self.single_axis_settings = None
        self.bore_settings = None
        self.boss_settings = None
        self.angle_settings = None
        self.probeTipSettings = None
        self.calibration_settings = None
        self.fourth_axis_settings = None
        self._settings_panels = ()
        self.controller = controller

        self.preview_popup = ProbingPreviewPopup(controller)

        # wait on UI to finish loading
        Clock.schedule_once(self.delayed_bind, 0.1)

        super().__init__(**kwargs)

    def on_dismiss(self):
        App.get_running_app().root.restore_keyboard_jog_control()

    def allows_external_jog(self) -> bool:
        """Allow keyboard/pendant jog while the probing screen is open."""
        return self._is_open

    def delayed_bind(self, dt):
        self.outside_corner_settings = self.ids.outside_corner_settings
        self.inside_corner_settings = self.ids.inside_corner_settings
        self.single_axis_settings = self.ids.single_axis_settings
        self.bore_settings = self.ids.bore_settings
        self.boss_settings = self.ids.boss_settings
        self.calibration_settings = self.ids.calibration_settings_id
        self.angle_settings = self.ids.angle_settings
        self.probeTipSettings = self.ids.probeTipSettings
        self.fourth_axis_settings = self.ids.fourth_axis_settings
        self._settings_panels = (
            self.angle_settings,
            self.bore_settings,
            self.boss_settings,
            self.inside_corner_settings,
            self.outside_corner_settings,
            self.single_axis_settings,
            self.calibration_settings,
        )

    def open(self, *args, **kwargs):
        self.refresh_probe_tip_diameter_hints()
        super().open(*args, **kwargs)

    def delayed_bind_complete(self, dt):
        # self.angle_settings = self.ids.angle_settings
        # self.probeTipSettings = self.ids.probeTipSettings
        return

    def refresh_probe_tip_diameter_hints(self):
        hint = get_machine_config_hint("zprobe.probe_tip_diameter") or tr._("config")
        for settings in self._settings_panels:
            if settings and "ProbeTipDiameter" in settings.ids:
                settings.ids.ProbeTipDiameter.hint_text = hint

    def on_single_axis_probing_pressed(self, operation_key: str):
        cfg = self.single_axis_settings.get_config()
        the_op = SingleAxisProbeOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_inside_corner_probing_pressed(self, operation_key: str):
        cfg = self.inside_corner_settings.get_config()
        the_op = InsideCornerOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_outside_corner_probing_pressed(self, operation_key: str):
        cfg = self.outside_corner_settings.get_config()
        the_op = OutsideCornerOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_bore_probing_pressed(self, operation_key: str):
        cfg = self.bore_settings.get_config()
        the_op = BoreOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_boss_probing_pressed(self, operation_key: str):
        cfg = self.boss_settings.get_config()
        the_op = BossOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_angle_probing_pressed(self, operation_key: str):
        cfg = self.angle_settings.get_config()
        the_op = AngleOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_probeTip_probing_pressed(self, operation_key: str):
        cfg = self.probeTipSettings.get_config()
        the_op = ProbeTipOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_callibration_probing_pressed(self, operation_key: str):
        cfg = self.calibration_settings.get_config()
        the_op = CalibrationOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def on_fourth_axis_probing_pressed(self, operation_key: str):
        cfg = self.fourth_axis_settings.get_config()
        the_op = FourthAxisOperationType[operation_key].value
        self.show_preview(the_op, cfg)

    def show_preview(self, operation: OperationsBase, cfg):
        missing_definition = operation.get_missing_config(cfg)

        if missing_definition is None:
            gcode = operation.generate(cfg)
            app = App.get_running_app()
            # Multi-point bore centre (least-squares circle fit) replaces the
            # 4-point round-bore centering on ANY machine - it only uses G38.2 +
            # [PRB] + G10, which work on the raw and framed protocols alike.
            bore_runner = z1_probing.build_bore_center_runner(gcode, self.controller)
            if bore_runner is not None:
                self.preview_popup.z1_lines = None
                self.preview_popup.z1_runner = bore_runner
                self.preview_popup.gcode = gcode
                self.preview_popup.probe_preview_label = z1_probing.bore_preview_text(gcode)
            elif app is not None and app.model == "Z1":
                # The Z1's stock firmware lacks the M460-M469 probing macros, so
                # reproduce the operation on the host: either a fixed G-code
                # script, or a live runner for multi-step / compute operations.
                lines = z1_probing.translate(gcode)
                runner = None if lines is not None else z1_probing.build_runner(gcode, self.controller)
                self.preview_popup.z1_lines = lines
                self.preview_popup.z1_runner = runner
                if lines is not None:
                    self.preview_popup.gcode = gcode
                    self.preview_popup.probe_preview_label = "\n".join(lines)
                elif runner is not None:
                    self.preview_popup.gcode = gcode
                    self.preview_popup.probe_preview_label = z1_probing.preview_text(gcode)
                else:
                    self.preview_popup.gcode = ""
                    self.preview_popup.probe_preview_label = "This probing operation isn't available on the Z1."
            else:
                self.preview_popup.z1_lines = None
                self.preview_popup.z1_runner = None
                self.preview_popup.gcode = gcode
                self.preview_popup.probe_preview_label = gcode
        else:
            self.preview_popup.z1_lines = None
            self.preview_popup.z1_runner = None
            self.preview_popup.gcode = ""
            self.preview_popup.probe_preview_label = "Missing required parameter " + missing_definition.label

        self.preview_popup.open()

        Clock.schedule_once(lambda dt: self.link_shared_data_with_refresh(self.preview_popup), 0.1)

    def link_shared_data_with_refresh(self, popup):
        app = App.get_running_app()
        app.mdi_data.clear()
        try:
            popup.ids.manual_rvPopup.data = app.mdi_data
        except IndexError:
            logger.error("Recycle view layout change ignored")

        app.bind(mdi_data=lambda instance, value: self.on_mdi_data_changed(popup))

    def on_mdi_data_changed(self, popup):
        try:
            popup.ids.manual_rvPopup.refresh_from_data()
            Clock.schedule_once(lambda dt: self.scroll_to_bottom(popup.ids.manual_rvPopup), 0.01)
        except Exception as e:
            print("Popup refresh failed:", e)

    def scroll_to_bottom(self, rv):
        try:
            Clock.schedule_once(lambda dt: setattr(rv, "scroll_y", 0), 0.01)
        except Exception as e:
            print("Scroll failed:", e)
