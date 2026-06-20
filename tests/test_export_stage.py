from __future__ import annotations

from pathlib import Path

import yaml

from pipeline.config import get_config
from pipeline.stages import export


def _prepare_config(tmp_path: Path) -> Path:
    root = Path(__file__).parents[1]
    cfg = yaml.safe_load((root / "config" / "runtime.example.yaml").read_text())
    cfg["storage"]["data_dir"] = str(tmp_path / "data")
    cfg["storage"]["obsidian_dir"] = str(tmp_path / "vault")
    config_path = tmp_path / "runtime.yaml"
    config_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return config_path


def test_export_stage_reads_split_reports_and_wiki_state(tmp_path, monkeypatch):
    config_path = _prepare_config(tmp_path)
    monkeypatch.setenv("AGENT_BRAIN_CONFIG", str(config_path))
    get_config.cache_clear()
    monkeypatch.setattr(export, "refresh_kanban", lambda vault=None: None)

    snapshot = tmp_path / "snapshots" / "commit" / "snapshot"
    snapshot.mkdir(parents=True)
    artifacts = snapshot.parent
    (artifacts / "integrity-report.json").write_text('{"passed": true}', encoding="utf-8")
    (artifacts / "normalization-report.json").write_text('{"passed": true, "file_count": 7, "unit_count": 12}', encoding="utf-8")
    (artifacts / "lint-report.json").write_text('{"passed": true}', encoding="utf-8")
    (artifacts / "syntax-report.json").write_text('{"passed": true, "symbol_unit_count": 9}', encoding="utf-8")
    (artifacts / "codegraph-report.json").write_text('{"passed": true}', encoding="utf-8")
    (artifacts / "codebase-memory-report.json").write_text('{"passed": true}', encoding="utf-8")
    (artifacts / "retrieval-report.json").write_text('{"passed": true}', encoding="utf-8")
    (artifacts / "vector-report.json").write_text('{"passed": true}', encoding="utf-8")
    (artifacts / "audit-report.json").write_text('{"passed": true}', encoding="utf-8")

    report = export.run(
        {
            "run_id": "INGEST-TEST",
            "source_id": "github-example-repo",
            "repository_url": "https://github.com/example/repo.git",
            "commit_sha": "deadbeef",
            "snapshot_path": str(snapshot),
            "wiki_state": "awaiting_wiki_agent",
        }
    )

    note = Path(report["source_note"]).read_text(encoding="utf-8")
    assert "- Integrity: passed" in note
    assert "- Normalize: passed" in note
    assert "- Lint: passed" in note
    assert "- Codebase-Memory: passed" in note
    assert "- Wiki state: awaiting_wiki_agent" in note
    assert "- Files: 7" in note
    assert "- Searchable units: 9" in note
