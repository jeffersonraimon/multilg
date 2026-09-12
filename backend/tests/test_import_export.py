import tempfile
import pytest
from fastapi.testclient import TestClient
from app.main import app
import app.main as main_module
from app.database import Database

@pytest.fixture
def client(monkeypatch):
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        monkeypatch.setenv("LG_DATABASE_PATH", tmp.name)
        monkeypatch.setenv("LG_SECRET_KEY", "secret-key-with-more-than-16-chars")
        main_module.db = Database()
        with TestClient(app) as test_client:
            yield test_client

def test_export_import_flow(client):
    # 1. Create a telnet LG
    sample_lg = {
        "name": "LG Teste Export",
        "protocol": "telnet",
        "enabled": True,
        "config": {
            "host": "10.0.0.1",
            "port": 23,
            "username": "admin",
            "password": "secretpassword",
            "username_prompt": r"(?i)username[: ]*$",
            "password_prompt": r"(?i)password[: ]*$",
            "prompt_regex": r"[>#]\s*$",
            "pre_commands": [],
            "commands": {"ping": "ping {target}"},
            "commands_v6": {},
            "quit_command": "exit",
            "timeout": 20
        }
    }
    create_res = client.post("/api/looking-glasses", json=sample_lg)
    assert create_res.status_code == 201

    # 2. Export LGs
    export_res = client.get("/api/looking-glasses/export")
    assert export_res.status_code == 200
    data = export_res.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["name"] == "LG Teste Export"
    assert data[0]["config"]["password"] == "secretpassword"

    # 3. Import LGs in batch (1 matching existing name, 1 new)
    import_payload = [
        data[0],
        {
            "name": "LG Teste Import 2",
            "protocol": "telnet",
            "enabled": False,
            "config": {
                "host": "10.0.0.2",
                "port": 23,
                "commands": {"ping": "ping {target}"}
            }
        }
    ]
    import_res = client.post("/api/looking-glasses/import", json=import_payload)
    assert import_res.status_code == 201
    import_data = import_res.json()
    assert import_data["imported"] == 2
    assert import_data["created"] == 1
    assert import_data["updated"] == 1

    # 4. Verify list count (should be 2 instead of duplicated 3)
    list_res = client.get("/api/looking-glasses")
    assert list_res.status_code == 200
    assert len(list_res.json()) == 2

def test_import_validation_error(client):
    invalid_payload = [
        {"name": "Invalid LG", "protocol": "telnet", "config": {}}
    ]
    import_res = client.post("/api/looking-glasses/import", json=invalid_payload)
    assert import_res.status_code == 422
    assert "Erro de validação" in import_res.json()["detail"]
