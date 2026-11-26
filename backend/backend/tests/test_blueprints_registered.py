"""Every Blueprint defined under modules/ must be registered in app.py.

An unregistered blueprint answers 404 for all of its routes (this once took down the
ICU, clinical and OT modules). Run:  python -m pytest backend/backend/tests
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _defined_blueprints():
    found = {}
    for routes in (ROOT / "modules").glob("*/routes.py"):
        text = routes.read_text(encoding="utf-8")
        for var in re.findall(r"^(\w+)\s*=\s*Blueprint\(", text, flags=re.M):
            found[var] = routes.parent.name
    return found


def test_every_blueprint_is_registered():
    app_sources = [
        (ROOT / "app.py").read_text(encoding="utf-8"),
        *(p.read_text(encoding="utf-8") for p in (ROOT / "services").glob("*/app.py")),
    ]
    missing = [
        f"{module}.{var}"
        for var, module in _defined_blueprints().items()
        if not any(re.search(rf"register_blueprint\(\s*{var}\b", src) for src in app_sources)
    ]
    assert not missing, f"Blueprints defined but never registered in app.py or services/*/app.py: {missing}"


def test_blueprint_variables_are_uniquely_named():
    assert "bp" not in _defined_blueprints(), "Name blueprints <module>_bp, not a bare 'bp'"
