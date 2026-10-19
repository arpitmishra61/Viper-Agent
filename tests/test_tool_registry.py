"""Consistency checks across TOOL_DEFINITIONS, execute_tool and the guardrail tool sets."""

import inspect

import pytest
from tools import TOOL_DEFINITIONS, execute_tool
from tools.guardrails import EXTERNAL_TOOLS

NAMES = [d["function"]["name"] for d in TOOL_DEFINITIONS]


def test_tool_names_unique():
    assert len(NAMES) == len(set(NAMES))


def test_think_is_listed_first():
    assert NAMES[0] == "think"


@pytest.mark.parametrize("definition", TOOL_DEFINITIONS, ids=NAMES)
def test_definition_schema_well_formed(definition):
    assert definition["type"] == "function"
    fn = definition["function"]
    assert fn["description"].strip()
    params = fn["parameters"]
    assert params["type"] == "object"
    for req in params.get("required", []):
        assert req in params["properties"], f"{fn['name']}: required '{req}' not in properties"


@pytest.mark.parametrize("name", NAMES)
def test_every_defined_tool_is_dispatched(name):
    # execute_tool is a chain of `if name == "..."` — a defined-but-undispatched tool
    # would silently return "Unknown tool" at runtime.
    assert f'name == "{name}"' in inspect.getsource(execute_tool)


def test_unknown_tool():
    assert execute_tool("does_not_exist", {}) == "Unknown tool: does_not_exist"


def test_external_tools_are_real_tools():
    assert EXTERNAL_TOOLS <= set(NAMES)


def test_calculate_dispatch():
    assert execute_tool("calculate", {"expression": "2+3"}) == "5"
