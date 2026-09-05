from nc.config import Config


def test_role_adapters_round_trip(tmp_path):
    cfg = Config(home=tmp_path, adapter="codex", adapters={"critic": "claude"})
    cfg.save()
    loaded = Config.load(tmp_path)
    assert loaded.adapter_for("worker") == "codex"
    assert loaded.adapter_for("critic") == "claude"


def test_old_config_uses_global_adapter(tmp_path):
    (tmp_path / "config.json").write_text('{"adapter": "claude"}')
    cfg = Config.load(tmp_path)
    assert cfg.adapter_for("worker") == "claude"
    assert cfg.adapter_for("critic") == "claude"
