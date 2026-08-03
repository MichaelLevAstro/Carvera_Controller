from __future__ import annotations

import json
import logging
import os

from kivy.app import App
from kivy.utils import platform as kivy_platform

logger = logging.getLogger(__name__)


class ConfigUtils:
    LEGACY_CONFIG_DIR = os.path.expanduser("~/.kivy/")

    @staticmethod
    def get_config_dir() -> str:
        # python-for-android never sets HOME, so "~" resolves to /data on Android,
        # which the app cannot write to - every save failed silently. The private
        # app data directory survives app restarts and updates on both mobile OSes.
        if kivy_platform in ("android", "ios"):
            app = App.get_running_app()
            if app is not None:
                return app.user_data_dir
        return ConfigUtils.LEGACY_CONFIG_DIR

    @staticmethod
    def save_config(config: dict, filename: str):
        try:
            config_dir = ConfigUtils.get_config_dir()
            os.makedirs(config_dir, exist_ok=True)  # Ensure directory exists
            file_path = os.path.join(config_dir, filename)
            with open(file_path, "w") as f:
                json.dump(config, f, indent=4)
            logger.info(f"Configuration saved to {file_path}")
        except Exception as e:
            logger.error(f"Error saving configuration: {e}")

    @staticmethod
    def load_config(filename: str) -> dict:
        config_dir = ConfigUtils.get_config_dir()
        file_path = os.path.join(config_dir, filename)
        if not os.path.exists(file_path) and config_dir != ConfigUtils.LEGACY_CONFIG_DIR:
            # Settings saved before the move to the app data directory.
            file_path = os.path.join(ConfigUtils.LEGACY_CONFIG_DIR, filename)
        if os.path.exists(file_path):
            try:
                with open(file_path) as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Error loading configuration: {e}")
        return {}  # Return an empty dictionary if loading fails or file doesn't exist


def _format_hint_value(val) -> str:
    try:
        f = float(val)
        if f == int(f):
            return str(int(f))
        return "%g" % f
    except (ValueError, TypeError):
        return str(val).strip()


def _get_setting_list() -> dict:
    from kivy.app import App

    return App.get_running_app().root.setting_list


def get_machine_config_hint(config_key: str) -> str | None:
    try:
        val = _get_setting_list().get(config_key)
        if val is not None and str(val).strip():
            return _format_hint_value(val)
    except Exception:
        pass
    return None
