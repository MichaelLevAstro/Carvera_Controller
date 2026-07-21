from kivy.uix.boxlayout import BoxLayout
from kivy.properties import StringProperty

from carveracontroller.addons.probing.operations.ConfigUtils import ConfigUtils


class TrammingSettings(BoxLayout):
    """Per-axis min/max/feed/loops settings for a tramming sweep.

    One instance is used per axis tab (X, Y, Z, A). Each axis persists to its
    own file so the tabs never overwrite each other's values. The kv text
    fields reference ``root.axis`` so they re-read the correct file when the
    parent assigns the axis after construction.
    """
    axis = StringProperty('X')

    _defaults = {'min': '', 'max': '', 'feed': '500', 'loops': '0'}

    def __init__(self, **kwargs):
        self.config = None
        self._loaded_axis = None
        super(TrammingSettings, self).__init__(**kwargs)

    def _filename(self):
        return "tramming-%s.json" % self.axis

    def _ensure_loaded(self):
        # Reload whenever the axis changed so a reused widget can't serve or
        # save another axis's values.
        if self.config is None or self._loaded_axis != self.axis:
            self.config = ConfigUtils.load_config(self._filename())
            self._loaded_axis = self.axis

    def get_setting(self, key: str) -> str:
        self._ensure_loaded()
        if key in self.config:
            return str(self.config[key])
        return self._defaults.get(key, '')

    def setting_changed(self, key: str, value: str):
        self._ensure_loaded()
        self.config[key] = value
        ConfigUtils.save_config(self.config, self._filename())

    def get_config(self):
        self._ensure_loaded()
        return {key: self.get_setting(key) for key in self._defaults}
