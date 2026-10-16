"""The name a crawler reports itself as in its owner's device list."""

import secrets
from pathlib import Path

from mwmbl.crawler.env_vars import MWMBL_DEVICE_NAME

DEVICE_NAME_FILE = "device-name"


def get_device_name(data_path: Path) -> str:
    """Return this crawler's device name, generating and storing one on first run.

    Deliberately not platform.uname().node. Every submission is uploaded to public object
    storage under a path containing the user's name, and machine hostnames routinely carry
    their owner's real name ("jane-laptop"), so reporting one publishes it.

    A random id instead: stable across restarts because it is kept in the data directory,
    and meaningless to anyone but its owner. Set MWMBL_DEVICE_NAME to choose your own -
    worth doing if you run several crawlers and want to tell them apart in the UI.
    """
    if MWMBL_DEVICE_NAME:
        return MWMBL_DEVICE_NAME

    path = data_path / DEVICE_NAME_FILE
    if path.exists():
        stored = path.read_text().strip()
        if stored:
            return stored

    name = f"crawler-{secrets.token_hex(4)}"
    path.write_text(name)
    return name
