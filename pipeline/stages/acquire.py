from __future__ import annotations

import os
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.config import data_dir, get_config
from pipeline.db import connect, snapshot_identity, upsert_snapshot, update_run
from pipeline.urls import RepositoryURL, parse_repository_url
from pipeline.util import read_json, run_command, sha256_file, temporary_askpass, utc_now, write_json


def _auth(repo: RepositoryURL, cfg: dict[str, Any]):
    if repo.platform == "github":
        token = str(cfg.get("scm", {}).get("github_token", ""))
        return ("x-access-token", token) if token else ("", "")
    token = str(cfg.get("scm", {}).get("gitlab_token", ""))
    return ("oauth2", token) if token else ("", "")


def _git_env(repo: RepositoryURL, cfg: dict[str, Any]):
    username, password = _auth(repo, cfg)
    if not password:
        return None
    return temporary_askpass(username, password)


def _default_branch(mirror: Path, timeout: int, env: dict[str, str] | None) -> str:
    symbolic = run_command(["git", "--git-dir", str(mirror), "symbolic-ref", "HEAD"], env=env, timeout=timeout, check=False)
    if symbolic.returncode == 0 and symbolic.stdout.strip().startswith("refs/heads/"):
        return symbolic.stdout.strip().removeprefix("refs/heads/")
    for candidate in ("main", "master"):
        result = run_command(["git", "--git-dir", str(mirror), "show-ref", "--verify", f"refs/heads/{candidate}"], env=env, timeout=timeout, check=False)
        if result.returncode == 0:
            return candidate
    refs = run_command(["git", "--git-dir", str(mirror), "for-each-ref", "--format=%(refname:short)", "refs/heads"], env=env, timeout=timeout)
    first = next((line.strip() for line in refs.stdout.splitlines() if line.strip()), None)
    if not first:
        raise RuntimeError("Repository has no branches")
    return first



def _record_source_and_run(
    *,
    repo: RepositoryURL,
    branch: str,
    commit_sha: str,
    extracted_dir: Path,
    cfg: dict[str, Any],
    run_id: str,
    requested_ref: str | None,
) -> None:
    refresh_hours = float(cfg["maintenance"].get("refresh_interval_hours", 36))
    jitter = float(cfg["maintenance"].get("refresh_jitter_hours", 12))
    next_hours = max(1, refresh_hours + random.uniform(-jitter, jitter))
    next_refresh = (datetime.now(timezone.utc) + timedelta(hours=next_hours)).isoformat()
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO sources(source_id,platform,repository_url,namespace,name,repository_name,default_branch,latest_commit,last_ingested_at,next_refresh_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id) DO UPDATE SET
              repository_url=excluded.repository_url,
              default_branch=excluded.default_branch,
              latest_commit=excluded.latest_commit,
              last_ingested_at=excluded.last_ingested_at,
              next_refresh_at=excluded.next_refresh_at,
              updated_at=excluded.updated_at
            """,
            (repo.source_id, repo.platform, repo.normalized, repo.namespace, repo.name, repo.name, branch, commit_sha, now, next_refresh, now, now),
        )
    update_run(
        run_id,
        source_id=repo.source_id,
        commit_sha=commit_sha,
        resolved_branch=branch,
        requested_ref=requested_ref,
        snapshot_path=str(extracted_dir),
    )


def _raw_snapshot_is_valid(raw_dir: Path, mirror: Path, timeout: int, env: dict[str, str]) -> tuple[bool, dict[str, Any] | None]:
    manifest_path = raw_dir / "source-manifest.json"
    if not manifest_path.exists():
        return False, None
    try:
        manifest = read_json(manifest_path)
        for name, expected in manifest.get("checksums", {}).items():
            target = raw_dir / name
            if not target.exists() or sha256_file(target) != expected:
                return False, None
        bundle = raw_dir / "repository.bundle"
        verified = run_command(
            ["git", "--git-dir", str(mirror), "bundle", "verify", str(bundle)],
            env=env, timeout=timeout, check=False,
        )
        if verified.returncode != 0:
            return False, None
        return True, manifest
    except Exception:
        return False, None

def run(run: dict[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    repo = parse_repository_url(run["repository_url"])
    timeout = int(cfg["scm"].get("clone_timeout_seconds", 1800))
    base = data_dir()
    source_root = base / "sources" / repo.host / Path(repo.namespace) / repo.name
    mirror = source_root / "mirror.git"
    source_root.mkdir(parents=True, exist_ok=True)

    auth_ctx = _git_env(repo, cfg)
    if auth_ctx:
        context = auth_ctx
    else:
        from contextlib import nullcontext
        context = nullcontext({"GIT_TERMINAL_PROMPT": "0"})

    with context as env:
        if mirror.exists():
            run_command(["git", "--git-dir", str(mirror), "remote", "set-url", "origin", repo.normalized], env=env, timeout=timeout)
            run_command(["git", "--git-dir", str(mirror), "fetch", "--prune", "--tags", "origin"], env=env, timeout=timeout)
        else:
            run_command(["git", "clone", "--mirror", repo.normalized, str(mirror)], env=env, timeout=timeout)

        branch = run["requested_ref"] or _default_branch(mirror, timeout, env)
        ref_candidates = [branch, f"refs/heads/{branch}", f"refs/remotes/origin/{branch}", f"refs/tags/{branch}"]
        commit_sha = None
        for ref in ref_candidates:
            resolved = run_command(["git", "--git-dir", str(mirror), "rev-parse", f"{ref}^{{commit}}"], env=env, timeout=timeout, check=False)
            if resolved.returncode == 0:
                commit_sha = resolved.stdout.strip()
                break
        if not commit_sha:
            raise RuntimeError(f"Could not resolve ref {branch!r}")

        snapshot_root = source_root / "snapshots" / commit_sha
        raw_dir = snapshot_root / "raw"
        extracted_dir = snapshot_root / "extracted"
        raw_dir.mkdir(parents=True, exist_ok=True)

        bundle = raw_dir / "repository.bundle"
        mirror_archive = raw_dir / "mirror.git.tar.zst"
        archive = raw_dir / "source.tar.zst"

        # Raw artifacts are commit-addressed and never overwritten. A refresh that
        # resolves to the same commit verifies and reuses the existing raw set.
        valid_existing, existing_manifest = _raw_snapshot_is_valid(raw_dir, mirror, timeout, env)
        if valid_existing and existing_manifest:
            if extracted_dir.exists():
                shutil.rmtree(extracted_dir)
            extracted_dir.mkdir(parents=True)
            extract_command = f"zstd -q -dc {shlex_quote(str(archive))} | tar -xf - -C {shlex_quote(str(extracted_dir))}"
            run_command(["sh", "-c", extract_command], env=env, timeout=timeout)
            _record_source_and_run(
                repo=repo, branch=branch, commit_sha=commit_sha, extracted_dir=extracted_dir,
                cfg=cfg, run_id=run["run_id"], requested_ref=run.get("requested_ref"),
            )
            return {
                **existing_manifest,
                "run_id": run["run_id"],
                "reused_snapshot": True,
                "original_run_id": existing_manifest.get("created_by_run_id"),
            }

        # A partial/corrupt raw directory is quarantined rather than silently reused.
        if any(raw_dir.iterdir()):
            quarantine = source_root / "quarantine" / f"{commit_sha}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
            quarantine.parent.mkdir(parents=True, exist_ok=True)
            raw_dir.replace(quarantine)
            raw_dir.mkdir(parents=True, exist_ok=True)
            bundle = raw_dir / "repository.bundle"
            mirror_archive = raw_dir / "mirror.git.tar.zst"
            archive = raw_dir / "source.tar.zst"

        if extracted_dir.exists():
            shutil.rmtree(extracted_dir)
        extracted_dir.mkdir(parents=True)
        run_command(["git", "--git-dir", str(mirror), "bundle", "create", str(bundle), "--all"], env=env, timeout=timeout)

        lfs_result = None
        if cfg.get("scm", {}).get("fetch_git_lfs", True):
            lfs_result = run_command(["git", "--git-dir", str(mirror), "lfs", "fetch", "--all", "origin"], env=env, timeout=timeout, check=False)

        mirror_command = f"tar -C {shlex_quote(str(source_root))} -cf - mirror.git | zstd -q -T0 -10 -o {shlex_quote(str(mirror_archive))}"
        run_command(["sh", "-c", mirror_command], env=env, timeout=timeout)

        # Archive the exact selected commit without analysis modifications. Shell pipe is intentional and contains no secrets.
        archive_command = f"git --git-dir={shlex_quote(str(mirror))} archive --format=tar {shlex_quote(commit_sha)} | zstd -q -T0 -19 -o {shlex_quote(str(archive))}"
        run_command(["sh", "-c", archive_command], env=env, timeout=timeout)
        extract_command = f"zstd -q -dc {shlex_quote(str(archive))} | tar -xf - -C {shlex_quote(str(extracted_dir))}"
        run_command(["sh", "-c", extract_command], env=env, timeout=timeout)

        bundle_verify = run_command(["git", "--git-dir", str(mirror), "bundle", "verify", str(bundle)], env=env, timeout=timeout)

    checksums = {
        "repository.bundle": sha256_file(bundle),
        "mirror.git.tar.zst": sha256_file(mirror_archive),
        "source.tar.zst": sha256_file(archive),
    }
    (raw_dir / "checksums.sha256").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in checksums.items()), encoding="utf-8"
    )
    preserved_bytes = sum((raw_dir / name).stat().st_size for name in checksums)
    maximum = int(cfg.get("scm", {}).get("max_repository_bytes", 10 * 1024**3))
    if preserved_bytes > maximum:
        raise RuntimeError(f"Preserved repository artifacts exceed configured maximum: {preserved_bytes} > {maximum}")
    manifest = {
        "schema_version": 1,
        "created_by_run_id": run["run_id"],
        "source_id": repo.source_id,
        "platform": repo.platform,
        "host": repo.host,
        "repository_url": repo.normalized,
        "namespace": repo.namespace,
        "name": repo.name,
        "repository_name": repo.name,
        "branch_or_ref": branch,
        "requested_ref": run.get("requested_ref"),
        "resolved_branch": branch,
        "commit_sha": commit_sha,
        "acquired_at": utc_now(),
        "mirror_path": str(mirror),
        "bundle_path": str(bundle),
        "mirror_archive_path": str(mirror_archive),
        "archive_path": str(archive),
        "git_lfs_fetch": {
            "attempted": lfs_result is not None,
            "returncode": lfs_result.returncode if lfs_result else None,
            "stderr_tail": lfs_result.stderr[-4000:] if lfs_result else "",
        },
        "snapshot_path": str(extracted_dir),
        "checksums": checksums,
        "preserved_bytes": preserved_bytes,
        "bundle_verify": bundle_verify.stdout.strip(),
    }
    manifest_path = raw_dir / "source-manifest.json"
    write_json(manifest_path, manifest)

    snapshot_record = {
        "snapshot_id": snapshot_identity(repo.source_id, commit_sha, run.get("requested_ref")),
        "source_id": repo.source_id,
        "platform": repo.platform,
        "repository_url": repo.normalized,
        "namespace": repo.namespace,
        "repository_name": repo.name,
        "requested_ref": run.get("requested_ref"),
        "resolved_branch": branch,
        "commit_sha": commit_sha,
        "path": str(extracted_dir),
        "raw_path": str(raw_dir),
        "bundle_path": str(bundle),
        "archive_path": str(archive),
        "mirror_archive_path": str(mirror_archive),
        "schema_version": 1,
        "pipeline_version": "0.1.0",
        "generator_name": "acquire",
        "generator_version": "1",
    }
    upsert_snapshot(snapshot_record)

    _record_source_and_run(
        repo=repo, branch=branch, commit_sha=commit_sha, extracted_dir=extracted_dir,
        cfg=cfg, run_id=run["run_id"], requested_ref=run.get("requested_ref"),
    )
    return manifest


def shlex_quote(value: str) -> str:
    import shlex
    return shlex.quote(value)
