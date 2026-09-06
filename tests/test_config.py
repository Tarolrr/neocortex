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


def test_planner_defaults_to_heaviest_model_regardless_of_order():
    for models in (
        {"worker": "gpt-5.6-luna", "critic": "gpt-6-astra"},
        {"critic": "gpt-6-astra", "worker": "gpt-5.6-luna"},
    ):
        cfg = Config(models=models)
        assert cfg.model_for("planner") == "gpt-6-astra"
        assert cfg.model_for("worker") == "gpt-5.6-luna"
        assert cfg.model_for("critic") == "gpt-6-astra"
        cfg.models["planner"] = "gpt-5.6-luna"
        assert cfg.model_for("planner") == "gpt-5.6-luna"


def test_unknown_models_keep_configuration_order():
    cfg = Config(models={"worker": "custom-first", "critic": "custom-second"})
    assert cfg.model_for("planner") == "custom-first"


def test_planner_limits_defaults_and_round_trip(tmp_path):
    cfg = Config(home=tmp_path)
    assert cfg.planner_max_queued == 5
    assert cfg.planner_max_pending_proposals == 1
    cfg.planner_max_queued = 2
    cfg.planner_max_pending_proposals = 0
    cfg.save()
    loaded = Config.load(tmp_path)
    assert loaded.planner_max_queued == 2
    assert loaded.planner_max_pending_proposals == 0
