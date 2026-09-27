"""Offline research tools for Micro Gold futures."""

import logging


# The CLI installs real handlers; library use and tests stay silent by default.
logging.getLogger(__name__).addHandler(logging.NullHandler())
