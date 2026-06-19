from pipeline.stages.semantics import _project_from_index


def test_project_from_index_reads_direct_project():
    assert _project_from_index({"status": "indexed", "project": "tmp-repository"}, "fallback") == "tmp-repository"


def test_project_from_index_reads_nested_project():
    value = {"result": {"content": [{"project_name": "nested-project"}]}}
    assert _project_from_index(value, "fallback") == "nested-project"


def test_project_from_index_falls_back():
    assert _project_from_index({"status": "indexed"}, "fallback") == "fallback"
