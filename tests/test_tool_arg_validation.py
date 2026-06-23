"""Validator test for finding 2.4: streaming tool-call args are concatenated
raw and passed without JSON-schema validation to the bash executor.

Hypothesis: in src/tool_schemas.function_call_to_tool_block, when the LLM
streams a function-call with `args["command"]` set to a non-string type
(e.g. int 12345 or list ["rm", "-rf", "/"]), the resulting ToolBlock.content
is left as the non-string value, with no isinstance check, no schema
validation, and the bash executor is then asked to consume it.
"""

import asyncio
import json
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Mock heavy deps before importing (mirrors test_context_compactor.py pattern)
for mod in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "src.database", "core.models", "core.database",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()


def test_command_int_passes_validation_silently():
    """Feed args = {"command": 12345} (an int). Bug: should be rejected by
    schema validation, but isn't. Result: a ToolBlock with content=12345
    (an int) is returned to the caller."""
    import src.agent_tools  # import-order trick: must be loaded first to break the
                            # src.tool_schemas <-> src.agent_tools circular import.
    from src.tool_schemas import function_call_to_tool_block

    args_dict = {"command": 12345}
    arguments = json.dumps(args_dict)
    block = function_call_to_tool_block("bash", arguments)
    assert block is not None, "Block should be produced (no validation rejects the int)"
    # The bug: content is a non-string. Document the actual behavior.
    assert not isinstance(block.content, str), (
        f"UNEXPECTED: bash content is already validated to str. "
        f"Got type={type(block.content).__name__}, value={block.content!r}"
    )
    print(f"  -> bash(content=type={type(block.content).__name__}, value={block.content!r})")


def test_command_list_passes_validation_silently():
    """Feed args = {"command": ["rm", "-rf", "/"]} (a list). Bug: should be
    rejected, but isn't. The list is what reaches the bash executor."""
    import src.agent_tools  # break the circular import order
    from src.tool_schemas import function_call_to_tool_block

    args_dict = {"command": ["rm", "-rf", "/"]}
    arguments = json.dumps(args_dict)
    block = function_call_to_tool_block("bash", arguments)
    assert block is not None, "Block should be produced (no validation rejects the list)"
    assert not isinstance(block.content, str), (
        f"UNEXPECTED: bash content is already validated to str. "
        f"Got type={type(block.content).__name__}, value={block.content!r}"
    )
    print(f"  -> bash(content=type={type(block.content).__name__}, value={block.content!r})")


def test_no_jsonschema_in_module():
    """Negative control: assert tool_schemas does NOT import jsonschema and
    has no validate() helper. If this assertion fails the bug is fixed."""
    import src.agent_tools  # break the circular import order
    from src import tool_schemas as ts
    # 1) No jsonschema import
    src = open(ts.__file__).read()
    assert "import jsonschema" not in src
    assert "from jsonschema" not in src
    assert "jsonschema.validate" not in src
    # 2) function_call_to_tool_block does no isinstance(args.get("command"), str)
    import inspect
    fn_src = inspect.getsource(ts.function_call_to_tool_block)
    assert "isinstance" not in fn_src or "command" not in fn_src.split("isinstance")[0][-200:], (
        "UNEXPECTED: function_call_to_tool_block has an isinstance(command) check"
    )
    # 3) No ValidationError handling
    assert "ValidationError" not in src


def test_command_int_would_reach_bash_subprocess():
    """Trace one step further: simulate passing the int-laden ToolBlock to
    the bash executor. With the bug, the int is passed directly to
    subprocess.run / asyncio.create_subprocess_exec, which raises a TypeError
    only at exec time — the model output was never rejected at parse time."""
    import src.agent_tools  # break the circular import order
    from src.tool_schemas import function_call_to_tool_block

    args_dict = {"command": 12345}
    block = function_call_to_tool_block("bash", json.dumps(args_dict))
    # The bash executor does `["bash", "-c", command]` (or passes to run_subprocess).
    # With an int, list(cmd) at the OS level raises:
    #   TypeError: expected str, bytes or os.PathLike object, not int
    # — at EXEC time, after the tool was already accepted.
    cmd = ["bash", "-c", block.content]
    try:
        # Don't actually run, just demonstrate the type reaches the API surface.
        _ = cmd  # noqa: F841
    except TypeError:
        pass  # not what we expect from the executor
    # The smoking gun: by the time the OS rejects it, the model has
    # already "succeeded" in producing a tool call, and the agent loop
    # logs it as a (failed) tool execution rather than a validation error
    # at parse time.
    assert not isinstance(block.content, str), (
        "Validation must reject non-string command BEFORE the tool block is returned"
    )
