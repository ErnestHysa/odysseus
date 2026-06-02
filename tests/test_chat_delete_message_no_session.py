"""Issue #1428 — clicking X on a client-side-only chat bubble should remove it
from the DOM, even when there is no current chat session.

Repro from the issue: send a message without a model selected. The chat
shows the "No chat session active" assistant bubble. Clicking the X in the
footer does nothing.

Root cause: ``deleteMessage()`` in ``static/js/chat.js`` early-exited when
``getCurrentSessionId()`` returned null. The "No chat session active" bubble
is created with no ``dbId`` (it was never persisted), so the existing
fallback path that removes the DOM-only bubbles was unreachable.

The fix: drop the ``if (!sessionId) return;`` early-exit and delegate the
pair-finding logic to ``computeDeleteTargets()`` in
``static/js/chatDeleteTargets.js``. The fallback at the bottom of
``deleteMessage()`` then handles the DOM-only delete.

This test pins both:
  1. ``computeDeleteTargets()`` returns the right indices/dbIds for the
     message-list shapes that ``deleteMessage()`` actually sees.
  2. The static ``deleteMessage()`` source no longer early-exits on a
     missing session id.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HELPER = _REPO / "static" / "js" / "chatDeleteTargets.js"
_CHAT_JS = _REPO / "static" / "js" / "chat.js"
_HAS_NODE = shutil.which("node") is not None


# Wire format: array of [classes: list[str], dbId: str|null].
def _call(messages, clicked_index):
    """Invoke computeDeleteTargets in node and return the JSON result."""
    js = (
        "import { computeDeleteTargets } from '"
        + _HELPER.as_posix()
        + "';\n"
        "function makeMsg([classes, dbId]) {\n"
        "  const cls = new Set(classes);\n"
        "  return {\n"
        "    classList: { contains: (c) => cls.has(c) },\n"
        "    dataset: dbId ? { dbId } : {},\n"
        "  };\n"
        "}\n"
        f"const msgs = {json.dumps(messages)}.map(makeMsg);\n"
        f"console.log(JSON.stringify(computeDeleteTargets(msgs, {clicked_index})));\n"
    )
    proc = subprocess.run(
        ["node", "--input-type=module"],
        input=js,
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


# The exact scenario from the issue screenshot: a lone assistant bubble
# with no dbId, no user partner. computeDeleteTargets should return
# aiIndex pointing at the clicked bubble, no user partner, and empty
# msgIds (so the fallback path will just remove the DOM).
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_lone_assistant_bubble_no_db_id_no_user_partner():
    result = _call([[["msg", "msg-ai"], None]], 0)
    assert result == {"userIndex": -1, "aiIndex": 0, "msgIds": []}


# User bubble with no AI partner (sent a message, no model selected, no
# response was streamed). Clicking X should target the user bubble.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_user_bubble_no_ai_partner():
    result = _call([[["msg", "msg-user"], None]], 0)
    assert result == {"userIndex": 0, "aiIndex": -1, "msgIds": []}


# User + AI pair, both have dbIds — clicking on the user bubble should
# return both indices and both dbIds.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_user_ai_pair_with_db_ids_clicked_user():
    messages = [
        [["msg", "msg-user"], "db-user-1"],
        [["msg", "msg-ai"], "db-ai-1"],
    ]
    result = _call(messages, 0)
    assert result == {"userIndex": 0, "aiIndex": 1, "msgIds": ["db-user-1", "db-ai-1"]}


# AI clicked — should walk back to find the user partner.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_ai_bubble_clicked_finds_preceding_user():
    messages = [
        [["msg", "msg-user"], "db-user-1"],
        [["msg", "msg-ai"], "db-ai-1"],
    ]
    result = _call(messages, 1)
    assert result == {"userIndex": 0, "aiIndex": 1, "msgIds": ["db-user-1", "db-ai-1"]}


# AI bubble has no user partner (the issue scenario). msgIds should be
# empty so the fallback removes the DOM without an API call.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_lone_ai_bubble_no_user_partner_no_db_id():
    result = _call([[["msg", "msg-ai"], None]], 0)
    assert result == {"userIndex": -1, "aiIndex": 0, "msgIds": []}


# User + AI pair, neither has a dbId (optimistic renders before the
# message_saved event fires). msgIds should be empty so the DOM-only
# fallback fires — no API call to a not-yet-saved message.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_pair_without_db_ids_yields_empty_msg_ids():
    messages = [
        [["msg", "msg-user"], None],
        [["msg", "msg-ai"], None],
    ]
    result = _call(messages, 0)
    assert result == {"userIndex": 0, "aiIndex": 1, "msgIds": []}


# User + AI pair with a continuation bubble. Clicking the continuation
# should resolve to the main AI bubble's user/AI pair, not include the
# continuation's own dbId.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_continuation_bubble_walks_back_to_main_ai():
    messages = [
        [["msg", "msg-user"], "db-user-1"],
        [["msg", "msg-ai"], "db-ai-1"],
        [["msg", "msg-ai", "msg-continuation"], "db-ai-1-cont"],
    ]
    result = _call(messages, 2)
    assert result == {"userIndex": 0, "aiIndex": 1, "msgIds": ["db-user-1", "db-ai-1"]}


# Out-of-range index returns null so the caller can early-exit.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_out_of_range_index_returns_null():
    result = _call([[["msg", "msg-user"], "db-user-1"]], 5)
    assert result is None


# Negative index returns null.
@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_negative_index_returns_null():
    result = _call([[["msg", "msg-user"], "db-user-1"]], -1)
    assert result is None


# Structural regression: the early-exit guard in deleteMessage() must be
# gone, otherwise the issue is back. Reading the actual source is the
# only honest check — the early-exit line used to be the very first
# thing after getCurrentSessionId(), so a surviving
# ``if (!sessionId) return;`` (or any ``if (!sessionId) return``) means
# the fix was lost.
def test_delete_message_no_early_exit_on_missing_session():
    src = _CHAT_JS.read_text()
    start = src.find("export async function deleteMessage(")
    assert start > 0, "deleteMessage not found in chat.js"
    end = src.find("export async function", start + 1)
    body = src[start:end] if end > 0 else src[start:]
    assert "if (!sessionId) return" not in body, (
        "deleteMessage() still early-exits on missing sessionId — "
        "this is the bug fixed by #1428. The early-exit was reachable "
        "for any client-side-only bubble (e.g. 'No chat session active' "
        "error messages) and made the delete-X button silently fail."
    )
