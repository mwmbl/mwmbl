"""
The crawler's self-reported device name.

Submissions are uploaded to world-readable object storage, so the name must not be the
machine hostname - see mwmbl.crawler.device.
"""

from pathlib import Path
from unittest.mock import patch

from mwmbl.crawler.device import DEVICE_NAME_FILE, get_device_name


def test_the_configured_name_wins(tmp_path: Path):
    with patch("mwmbl.crawler.device.MWMBL_DEVICE_NAME", "my-crawler"):
        assert get_device_name(tmp_path) == "my-crawler"
    assert not (tmp_path / DEVICE_NAME_FILE).exists()


def test_a_generated_name_is_stable_across_runs(tmp_path: Path):
    with patch("mwmbl.crawler.device.MWMBL_DEVICE_NAME", ""):
        first = get_device_name(tmp_path)
        second = get_device_name(tmp_path)

    assert first == second
    assert first.startswith("crawler-")
    assert (tmp_path / DEVICE_NAME_FILE).read_text() == first


def test_the_generated_name_does_not_leak_the_hostname(tmp_path: Path):
    import platform

    with patch("mwmbl.crawler.device.MWMBL_DEVICE_NAME", ""):
        name = get_device_name(tmp_path)

    assert platform.uname().node not in name


def test_an_empty_stored_name_is_replaced(tmp_path: Path):
    (tmp_path / DEVICE_NAME_FILE).write_text("  \n")

    with patch("mwmbl.crawler.device.MWMBL_DEVICE_NAME", ""):
        name = get_device_name(tmp_path)

    assert name.startswith("crawler-")
