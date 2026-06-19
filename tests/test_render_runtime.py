from pathlib import Path
import importlib.util
import yaml


def load_renderer():
    path = Path(__file__).parents[1] / "scripts" / "render_runtime.py"
    spec = importlib.util.spec_from_file_location("render_runtime", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def test_example_structure_has_expected_profiles():
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    assert cfg["maintenance"]["refresh_interval_hours"] == 36
    assert cfg["stages"]["syntax"]["owner_profile"] == "syntax-analyst"
    assert cfg["stages"]["structure"]["owner_profile"] == "structure-analyst"
    assert cfg["stages"]["semantics"]["owner_profile"] == "semantic-analyst"


def test_validation_rejects_placeholders():
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    renderer = load_renderer()
    errors = renderer.validate(cfg)
    assert errors
    assert any("internal_api_token" in error for error in errors)


def test_render_writes_key_pool_and_fallbacks(tmp_path):
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["security"]["internal_api_token"] = "a" * 32
    cfg["telegram"]["bots"]["orchestrator"]["bot_token"] = "123456:TEST_TOKEN"
    cfg["telegram"]["bots"]["orchestrator"]["allowed_user_ids"] = [123456789]
    cfg["providers"]["openrouter"]["keys"] = ["sk-or-v1-first", "sk-or-v1-second"]
    cfg["providers"]["deepseek"]["enabled"] = True
    cfg["providers"]["deepseek"]["keys"] = ["sk-deepseek-first"]
    cfg["routing"]["fallbacks"] = [{"provider": "deepseek", "model": "deepseek-chat"}]
    cfg["storage"]["data_host_path"] = str(tmp_path / "data")
    cfg["storage"]["hermes_host_path"] = str(tmp_path / "hermes")
    cfg["storage"]["obsidian_host_path"] = str(tmp_path / "vault")

    # Render against a temporary repository root containing the profile sources.
    work = tmp_path / "repo"
    work.mkdir()
    (work / "profiles").symlink_to(root / "profiles", target_is_directory=True)
    renderer = load_renderer()
    assert renderer.validate(cfg) == []
    renderer.render(cfg, work)

    env_text = (work / ".runtime" / "hermes" / ".env").read_text()
    assert "OPENROUTER_API_KEY=sk-or-v1-first" in env_text
    assert "DEEPSEEK_API_KEY=sk-deepseek-first" in env_text
    auth = __import__("json").loads((work / ".runtime" / "hermes" / "auth.json").read_text())
    assert auth["credential_pool"]["openrouter"][0]["access_token"] == "sk-or-v1-second"
    rendered = yaml.safe_load((work / ".runtime" / "hermes" / "config.yaml").read_text())
    assert rendered["credential_pool_strategies"]["openrouter"] == "round_robin"
    assert rendered["fallback_providers"] == [{"provider": "deepseek", "model": "deepseek-chat"}]

    pipeline_cfg = yaml.safe_load((work / ".runtime" / "pipeline.yaml").read_text())
    assert "telegram" not in pipeline_cfg
    assert "providers" not in pipeline_cfg
    assert pipeline_cfg["syntax"]["max_symbol_bytes"] == 262144
    assert pipeline_cfg["lint"]["fail_severities"] == ["error", "fatal"]
    assert pipeline_cfg["scm"]["github_token"] == ""
    assert (work / ".runtime" / "pipeline.yaml").stat().st_mode & 0o777 == 0o600
