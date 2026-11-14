from app.config import gateway_token


def test_gateway_token_from_systemd_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("AGCORE_GATEWAY_TOKEN", raising=False)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    (tmp_path / "agcore_gateway_token").write_text("scoped-token\n")
    assert gateway_token() == "scoped-token"
