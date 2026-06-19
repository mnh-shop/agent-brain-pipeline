#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any

import yaml

PROVIDER_ENV = {
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_BASE_URL"),
    "opencode-go": ("OPENCODE_GO_API_KEY", "OPENCODE_GO_BASE_URL"),
    "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
}
VALID_ROTATION = {"fill_first", "round_robin", "least_used", "random"}


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a mapping")
    return data


def placeholders(value: str) -> bool:
    upper = value.upper()
    return not value or "PASTE_" in upper or "CHANGE_ME" in upper


def validate(cfg: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if cfg.get("version") != 1:
        errors.append("version must be 1")

    token = str(cfg.get("security", {}).get("internal_api_token", ""))
    if placeholders(token) or len(token) < 24:
        errors.append("security.internal_api_token must be a non-placeholder value of at least 24 characters")

    bots = cfg.get("telegram", {}).get("bots", {})
    orchestrator = bots.get("orchestrator", {})
    if orchestrator.get("enabled", False):
        bot_token = str(orchestrator.get("bot_token", ""))
        if placeholders(bot_token) or ":" not in bot_token:
            errors.append("telegram.bots.orchestrator.bot_token must be a valid BotFather-style token")
        if not orchestrator.get("allowed_user_ids"):
            errors.append("telegram.bots.orchestrator.allowed_user_ids must contain at least one numeric user ID")

    providers = cfg.get("providers", {})
    enabled_with_key = 0
    for name, provider in providers.items():
        rotation = provider.get("rotation", "fill_first")
        if rotation not in VALID_ROTATION:
            errors.append(f"providers.{name}.rotation must be one of {sorted(VALID_ROTATION)}")
        if provider.get("enabled", False):
            keys = [str(k) for k in provider.get("keys", []) if not placeholders(str(k))]
            if keys:
                enabled_with_key += 1
            else:
                errors.append(f"providers.{name} is enabled but has no usable key")
    if enabled_with_key == 0:
        errors.append("At least one enabled provider must have a usable API key")

    profiles = cfg.get("profiles", {})
    if not profiles.get("orchestrator", {}).get("enabled", False):
        errors.append("profiles.orchestrator must be enabled")
    if profiles.get("knowledge-writer", {}).get("enabled", False):
        wiki = cfg.get("wiki", {})
        if wiki.get("enabled") is not True:
            errors.append("wiki.enabled must be true when knowledge-writer is enabled")
        for key in ("vault_path", "raw_path", "wiki_path", "candidate_path", "canonical_path"):
            if key not in wiki or not wiki.get(key):
                errors.append(f"wiki.{key} must be configured when knowledge-writer is enabled")
        for key in ("auto_generate_after_audit", "auto_promote"):
            if key not in wiki:
                errors.append(f"wiki.{key} must be configured when knowledge-writer is enabled")
    for stage, stage_cfg in cfg.get("stages", {}).items():
        owner = stage_cfg.get("owner_profile")
        if owner not in profiles:
            errors.append(f"stages.{stage}.owner_profile references unknown profile {owner!r}")
        elif not profiles[owner].get("enabled", False):
            errors.append(f"stages.{stage}.owner_profile {owner!r} is disabled")

    primary = cfg.get("routing", {}).get("primary", {})
    primary_provider = primary.get("provider")
    primary_model = str(primary.get("model", ""))
    if primary_provider not in providers or not providers.get(primary_provider, {}).get("enabled"):
        errors.append("routing.primary.provider must reference an enabled provider")
    if placeholders(primary_model) or not primary_model:
        errors.append("routing.primary.model must be a concrete model ID")

    for item in cfg.get("routing", {}).get("fallbacks", []):
        provider = item.get("provider")
        if providers.get(provider, {}).get("enabled") and clean_keys(providers[provider]):
            model = str(item.get("model", ""))
            if placeholders(model) or model.startswith("SET_"):
                errors.append(f"Enabled fallback provider {provider!r} needs a concrete model ID")

    for profile_name, profile_cfg in profiles.items():
        override = profile_cfg.get("model")
        if override and profile_cfg.get("enabled"):
            provider = override.get("provider")
            if not providers.get(provider, {}).get("enabled") or not clean_keys(providers[provider]):
                errors.append(f"profiles.{profile_name}.model references disabled/unconfigured provider {provider!r}")

    refresh = cfg.get("maintenance", {}).get("refresh_interval_hours", 0)
    if not isinstance(refresh, (int, float)) or refresh <= 0:
        errors.append("maintenance.refresh_interval_hours must be positive")

    return errors


def clean_keys(provider: dict[str, Any]) -> list[str]:
    return [str(key) for key in provider.get("keys", []) if not placeholders(str(key))]


def env_lines(cfg: dict[str, Any], profile: str, telegram_enabled: bool) -> list[str]:
    lines = [
        f"PIPELINE_API_URL={cfg['api']['base_url_from_hermes']}",
        f"AGENT_BRAIN_API_TOKEN={cfg['security']['internal_api_token']}",
        f"HERMES_TIMEZONE={cfg.get('general', {}).get('timezone', 'America/Bogota')}",
    ]
    for name, provider in cfg.get("providers", {}).items():
        if name not in PROVIDER_ENV:
            continue
        keys = clean_keys(provider)
        if not keys:
            continue
        key_env, base_env = PROVIDER_ENV[name]
        lines.append(f"{key_env}={keys[0]}")
        base_url = str(provider.get("base_url", ""))
        if base_url:
            lines.append(f"{base_env}={base_url}")

    if telegram_enabled:
        bot = cfg["telegram"]["bots"].get(profile, cfg["telegram"]["bots"].get("orchestrator", {}))
        if bot.get("enabled"):
            lines.append(f"TELEGRAM_BOT_TOKEN={bot.get('bot_token', '')}")
            allowed = ",".join(str(x) for x in bot.get("allowed_user_ids", []))
            lines.append(f"TELEGRAM_ALLOWED_USERS={allowed}")
            if bot.get("home_channel"):
                lines.append(f"TELEGRAM_HOME_CHANNEL={bot['home_channel']}")
    if profile == "knowledge-writer":
        wiki = cfg.get("wiki", {})
        if wiki:
            lines.extend(
                [
                    f"WIKI_PATH={wiki.get('wiki_path', '/vault/wiki')}",
                    f"OBSIDIAN_VAULT_PATH={wiki.get('vault_path', '/vault')}",
                    f"WIKI_RAW_PATH={wiki.get('raw_path', '/vault/raw')}",
                    f"WIKI_CANDIDATE_PATH={wiki.get('candidate_path', '/vault/wiki/candidates')}",
                    f"WIKI_CANONICAL_PATH={wiki.get('canonical_path', '/vault/wiki/canonical')}",
                ]
            )
    return lines


def model_for_profile(cfg: dict[str, Any], profile: str) -> dict[str, str]:
    override = cfg["profiles"][profile].get("model")
    return override or cfg["routing"]["primary"]


def hermes_config(cfg: dict[str, Any], profile: str) -> dict[str, Any]:
    model = model_for_profile(cfg, profile)
    enabled_providers = cfg.get("providers", {})
    strategies = {
        name: p.get("rotation", "fill_first")
        for name, p in enabled_providers.items()
        if p.get("enabled") and clean_keys(p)
    }
    fallbacks = []
    for item in cfg.get("routing", {}).get("fallbacks", []):
        provider = item.get("provider")
        if enabled_providers.get(provider, {}).get("enabled") and clean_keys(enabled_providers[provider]):
            fallbacks.append({"provider": provider, "model": item.get("model")})

    return {
        "model": {"provider": model["provider"], "default": model["model"]},
        "fallback_providers": fallbacks,
        "credential_pool_strategies": strategies,
        "terminal": {"backend": "local"},
        "kanban": {"dispatch_in_gateway": True},
    }


def auth_json(cfg: dict[str, Any]) -> dict[str, Any]:
    pools: dict[str, list[dict[str, Any]]] = {}
    for provider, provider_cfg in cfg.get("providers", {}).items():
        if not provider_cfg.get("enabled"):
            continue
        entries = []
        for index, key in enumerate(clean_keys(provider_cfg)[1:], start=2):
            digest = hashlib.sha256(key.encode()).hexdigest()[:12]
            entries.append({
                "id": f"cfg-{provider}-{index}-{digest}",
                "label": f"runtime.yaml key {index}",
                "auth_type": "api_key",
                "priority": index - 1,
                "source": "manual",
                "access_token": key,
                "last_status": "unknown",
                "request_count": 0,
            })
        if entries:
            pools[provider] = entries
    return {"version": 1, "providers": {}, "credential_pool": pools}


def copy_profile_source(repo_root: Path, profile: str, destination: Path) -> None:
    source = repo_root / "profiles" / profile
    if not source.exists():
        raise FileNotFoundError(f"Missing profile distribution: {source}")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("SOUL.md", "skills"):
        src = source / name
        dst = destination / name
        if src.is_dir():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)


def write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def render(cfg: dict[str, Any], repo_root: Path) -> None:
    runtime = repo_root / ".runtime"
    hermes_root = runtime / "hermes"
    runtime.mkdir(parents=True, exist_ok=True)
    hermes_root.mkdir(parents=True, exist_ok=True)

    storage = cfg["storage"]
    # The operator edits one central file, but each container receives only the
    # subset it needs. The pipeline never receives Telegram or LLM credentials.
    pipeline_keys = (
        "version", "general", "security", "scm", "stages", "maintenance",
        "pipeline", "lint", "syntax", "codegraph", "codebase_memory", "embeddings", "lancedb", "storage", "api", "logging", "wiki",
    )
    pipeline_cfg = {key: cfg[key] for key in pipeline_keys if key in cfg}
    pipeline_config_path = runtime / "pipeline.yaml"
    write_secret(pipeline_config_path, yaml.safe_dump(pipeline_cfg, sort_keys=False))

    compose_env = {
        "HERMES_IMAGE": cfg["images"]["hermes"],
        "PIPELINE_IMAGE": cfg["images"]["pipeline"],
        "PIPELINE_PORT": str(cfg["api"]["public_host_port"]),
        "PIPELINE_CONFIG_PATH": str(pipeline_config_path.resolve()),
        "DATA_HOST_PATH": str((repo_root / storage["data_host_path"]).resolve()) if not Path(storage["data_host_path"]).is_absolute() else storage["data_host_path"],
        "HERMES_HOST_PATH": str((repo_root / storage["hermes_host_path"]).resolve()) if not Path(storage["hermes_host_path"]).is_absolute() else storage["hermes_host_path"],
        "OBSIDIAN_HOST_PATH": str((repo_root / storage["obsidian_host_path"]).resolve()) if not Path(storage["obsidian_host_path"]).is_absolute() else storage["obsidian_host_path"],
        "AGENT_BRAIN_API_TOKEN": cfg["security"]["internal_api_token"],
        "HERMES_TIMEZONE": cfg.get("general", {}).get("timezone", "America/Bogota"),
    }
    write_secret(runtime / "compose.env", "\n".join(f"{k}={v}" for k, v in compose_env.items()) + "\n")

    enabled = [name for name, value in cfg["profiles"].items() if value.get("enabled")]
    (runtime / "enabled-profiles.json").write_text(json.dumps(enabled, indent=2) + "\n", encoding="utf-8")

    # The root/default Hermes profile is the orchestrator.
    copy_profile_source(repo_root, "orchestrator", hermes_root)
    write_secret(hermes_root / ".env", "\n".join(env_lines(cfg, "orchestrator", True)) + "\n")
    (hermes_root / "config.yaml").write_text(yaml.safe_dump(hermes_config(cfg, "orchestrator"), sort_keys=False), encoding="utf-8")
    write_secret(hermes_root / "auth.json", json.dumps(auth_json(cfg), indent=2) + "\n")

    for profile in enabled:
        if profile == "orchestrator":
            continue
        dest = hermes_root / "profiles" / profile
        copy_profile_source(repo_root, profile, dest)
        write_secret(dest / ".env", "\n".join(env_lines(cfg, profile, profile in cfg.get("telegram", {}).get("bots", {}))) + "\n")
        (dest / "config.yaml").write_text(yaml.safe_dump(hermes_config(cfg, profile), sort_keys=False), encoding="utf-8")
        write_secret(dest / "auth.json", json.dumps(auth_json(cfg), indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    cfg = load(args.config)
    errors = validate(cfg)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        raise SystemExit(2)
    if not args.validate_only:
        render(cfg, args.root.resolve())
        print("Rendered .runtime/ from config/runtime.yaml")


if __name__ == "__main__":
    main()
