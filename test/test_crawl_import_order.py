"""The standalone crawler has no database and no DJANGO_SETTINGS_MODULE in its environment.

mwmbl.crawl sets the settings module and calls django.setup() itself, so every mwmbl import
in it must come after that call - the mwmbl package reaches Django models (via
curated_domains) and raises ImproperlyConfigured if the app registry is not ready.
"""
import ast
from pathlib import Path

CRAWL_PATH = Path(__file__).parent.parent / "mwmbl" / "crawl.py"


def _django_setup_line(module: ast.Module) -> int:
    for node in ast.walk(module):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setup" and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "django"):
            return node.lineno
    raise AssertionError("mwmbl/crawl.py does not call django.setup()")


def test_mwmbl_imports_come_after_django_setup():
    module = ast.parse(CRAWL_PATH.read_text())
    setup_line = _django_setup_line(module)

    early_imports = [
        node.module for node in module.body
        if isinstance(node, ast.ImportFrom) and node.lineno < setup_line
        and node.module is not None and node.module.startswith("mwmbl")
    ]
    assert early_imports == []
