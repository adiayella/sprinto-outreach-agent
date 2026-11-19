import pytest

from sprinto_outreach.pipeline import Pipeline
from sprinto_outreach.settings import load_settings


@pytest.fixture
def pipe(tmp_path, monkeypatch):
    s = load_settings()
    s.mode = "simulation"
    s.demo_recipient = "me@example.com"
    monkeypatch.setattr("sprinto_outreach.settings.DATA_DIR", tmp_path)
    monkeypatch.setattr(type(s), "db_path", property(lambda self: tmp_path / "t.sqlite3"))
    return Pipeline(s)


