import pytest
from pipeline.urls import parse_repository_url


def test_github_short_url():
    repo = parse_repository_url("github.com/NousResearch/hermes-agent")
    assert repo.platform == "github"
    assert repo.namespace == "NousResearch"
    assert repo.name == "hermes-agent"
    assert repo.normalized == "https://github.com/NousResearch/hermes-agent.git"


def test_gitlab_nested_group():
    repo = parse_repository_url("https://gitlab.com/group/subgroup/project.git")
    assert repo.platform == "gitlab"
    assert repo.namespace == "group/subgroup"
    assert repo.name == "project"


def test_reject_other_host():
    with pytest.raises(ValueError):
        parse_repository_url("https://example.com/owner/repo")
