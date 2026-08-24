import json

import pytest

from gateway.session_context import (
    clear_project_topic_creator,
    set_project_topic_creator,
)
from tools.project_tools import project_topic_create
from tools.registry import registry


def test_registry_exposes_async_project_topic_create_under_kanban():
    entry = registry.get_entry("project_topic_create")
    assert entry is not None
    assert entry.toolset == "kanban"
    assert entry.is_async is True
    assert entry.schema["parameters"]["required"] == ["name"]


def test_project_topic_create_registry_async_dispatch():
    async def creator(**kwargs):
        return {"success": True, **kwargs}

    token = set_project_topic_creator(creator)
    try:
        result = json.loads(registry.dispatch(
            "project_topic_create", {"name": " Alpha ", "workdir": "", "status": "active"}
        ))
    finally:
        clear_project_topic_creator(token)

    assert result == {
        "success": True,
        "name": "Alpha",
        "workdir": None,
        "status": "active",
    }


@pytest.mark.asyncio
async def test_project_topic_create_fails_closed_without_management_callback():
    result = json.loads(await project_topic_create("Alpha"))
    assert result["success"] is False
    assert "management Topic" in result["error"]


@pytest.mark.asyncio
async def test_project_topic_create_validates_name_before_callback():
    called = False

    async def creator(**kwargs):
        nonlocal called
        called = True
        return {"success": True}

    token = set_project_topic_creator(creator)
    try:
        result = json.loads(await project_topic_create("   "))
    finally:
        clear_project_topic_creator(token)
    assert result == {"success": False, "error": "name is required"}
    assert called is False
