"""Suite-wide env hygiene: point AGENTS_HOST_DIR at a per-test tmp dir so
agents-relay tests never leak devices or vault tokens into ~/.agents."""
import pytest


@pytest.fixture(autouse=True)
def _isolated_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    (host / "workspaces" / "default").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AGENTS_HOST_DIR", str(host))
    monkeypatch.delenv("AGENTS_WORKSPACE", raising=False)
    monkeypatch.delenv("AGENTS_RELAY_DIR", raising=False)
    monkeypatch.delenv("AGENTS_STORE_DB", raising=False)
    monkeypatch.delenv("AGENTS_USERS_FILE", raising=False)
