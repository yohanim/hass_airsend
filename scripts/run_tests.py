"""Run pytest with small compatibility shims for the Home Assistant under test.

pytest-homeassistant-custom-component is generated for one Home Assistant
version. CI also runs the suite against a newer Home Assistant (to check the
integration works before *and* after a release); these shims only paper over
test-harness differences, never over integration code.
"""
import sys

# Must be imported before pytest loads its plugins: since HA 2026.11,
# importing homeassistant aliases httpx to httpx2, which fails if a pytest
# plugin imported httpx first.
import homeassistant  # noqa: F401

# HA 2026.11 moved _clear_logger_overwrites to logger.helpers; the plugin
# built for 2026.10 still imports it from the logger package.
import homeassistant.components.logger as _logger

if not hasattr(_logger, "_clear_logger_overwrites"):
    from homeassistant.components.logger.helpers import _clear_logger_overwrites

    _logger._clear_logger_overwrites = _clear_logger_overwrites

import pytest  # noqa: E402

sys.exit(pytest.main(sys.argv[1:] or ["-v"]))
