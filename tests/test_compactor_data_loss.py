"""Validator test for finding 2.3: split_point off-by-system-messages.

Hypothesis: in src/context_compactor.maybe_compact, `split_point` is the index
into the SYSTEM-STRIPPED `convo_msgs`, but `_update_session_history` slices
`session.history` (SYSTEM-INCLUSIVE) with that same split_point, dropping the
leading system messages.

Builds a session whose `history` interleaves two system messages through the
conversation so that an off-by-system-messages slicing error is observable.
"""

import asyncio
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Mock heavy deps before importing the module (mirrors test_context_compactor.py)
for mod in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "src.database", "core.models", "core.database",
]:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

# Re-import core.models under the MagicMock so the `from core.models import
# ChatMessage` at module load succeeds with a usable class.
import importlib
core_models = sys.modules["core.models"]
if not hasattr(core_models, "ChatMessage") or isinstance(core_models.ChatMessage, MagicMock):
    class ChatMessage:
        def __init__(self, role, content, metadata=None):
            self.role = role
            self.content = content
            self.metadata = metadata or {}
    core_models.ChatMessage = ChatMessage

# Now safe to import the compactor
from src import context_compactor as cc


def _make_session(history):
    s = MagicMock()
    s.history = list(history)            # store a copy
    s.id = None                          # bypass the manager-replace path
    del s.replace_messages               # not used in this path
    return s


def _ensure_compactor_runs():
    """Stub the LLM call and context-length lookup so the compactor runs to
    completion deterministically without an actual model endpoint."""
    cc.get_context_length = MagicMock(return_value=100_000)
    cc.estimate_tokens = MagicMock(side_effect=lambda msgs: max(1, sum(len(str(m.get("content", ""))) for m in msgs) // 2))
    # Force a single deterministic summary, regardless of model.
    cc.llm_call_async = AsyncMock(return_value="DETERMINISTIC-SUMMARY")
    cc.resolve_endpoint = MagicMock(return_value=("", "", {}))


def test_split_point_drops_leading_system_messages():
    """A system message placed BEFORE all conversation turns must survive a
    compact() call. With the suspected bug, `session.history[split_point:]`
    chops off the first system message because `split_point` is computed
    against the system-stripped list."""
    # Force the compact path: the function returns early if convo < 4.
    history = [
        {"role": "system", "content": "system_A — the prefix we must keep"},
        {"role": "user", "content": "user_1"},
        {"role": "assistant", "content": "assistant_1"},
        {"role": "system", "content": "system_B — midstream system note"},
        {"role": "user", "content": "user_2"},
        {"role": "assistant", "content": "assistant_2"},
    ]
    session = _make_session(history)
    messages = list(history)
    _ensure_compactor_runs()

    # Drive a big enough pct to enter the compact path
    with patch.object(cc, "estimate_tokens", return_value=999_999):
        new_messages, _ctx, was_compacted = asyncio.run(
            cc.maybe_compact(
                session=session,
                endpoint_url="http://fake",
                model="fake-model",
                messages=messages,
            )
        )

    assert was_compacted is True, "Compactor should have run for this large history"

    # Assertion 1: the system_A content is preserved somewhere in new_messages
    new_contents = [m.get("content", "") for m in new_messages]
    assert any("system_A" in c for c in new_contents), (
        f"BUG: leading system message was dropped. new_messages={new_messages!r}"
    )

    # Assertion 2: session.history still contains the leading system message.
    # After `_update_session_history`, the new history is [summary, ...recent].
    # `recent` was `session.history[split_point:]`. split_point here is 3
    # (len(convo_msgs)//2 with 5 convo msgs), and `session.history` has 6
    # entries. So `recent` is session.history[3:] =
    #   [system_B, user_2, assistant_2]
    # The CORRECT slicing (split_point + len(system_msgs_prefix)) would have
    # kept more of the conversation. The bug manifests as the system_A
    # content (a real system prompt) being silently dropped.
    hist_contents = [getattr(m, "content", None) or m.get("content", "") for m in session.history]
    assert any("system_A" in c for c in hist_contents), (
        f"BUG: system_A was dropped from session.history. "
        f"session.history={session.history!r}"
    )


def test_split_point_off_by_system_count():
    """Concrete demo of the off-by-N: with 1 leading system and 5 convo msgs,
    split_point=2 (convo_msgs//2), session.history[2:] drops the system msg
    AND the first user/assistant turn."""
    history = [
        {"role": "system", "content": "system_A"},            # index 0
        {"role": "user", "content": "user_1"},                # index 1
        {"role": "assistant", "content": "assistant_1"},      # index 2
        {"role": "user", "content": "user_2"},                # index 3
        {"role": "assistant", "content": "assistant_2"},      # index 4
        {"role": "user", "content": "user_3"},                # index 5
    ]
    session = _make_session(history)
    _ensure_compactor_runs()

    with patch.object(cc, "estimate_tokens", return_value=999_999):
        asyncio.run(
            cc.maybe_compact(
                session=session,
                endpoint_url="http://fake",
                model="fake-model",
                messages=list(history),
            )
        )

    # The suspected bug: `recent_history = session.history[2:]` would
    # be [assistant_1, user_2, assistant_2, user_3] (assistant_1 instead
    # of system_A/user_1). The CORRECT behavior is to keep all 6 original
    # messages in the recent half plus the summary prefix.
    # The presence of the summary msg plus the recent slice must STILL
    # contain system_A (it's part of the original history that survives
    # the compact — the summary just takes the place of older msgs).
    hist_contents = [getattr(m, "content", None) or m.get("content", "") for m in session.history]

    # system_A should appear in either:
    # (a) the recent half that was kept, OR
    # (b) the summary content (since the OLDER half was summarized).
    # Since system_A is at index 0 and len(convo_msgs)=5, split_point=2
    # means the older half is convo_msgs[:2] = [user_1, assistant_1],
    # which gets summarized. system_A is NOT in the older half.
    # So it MUST survive in the recent half — i.e. in session.history.
    assert "system_A" in hist_contents, (
        f"BUG: system_A was dropped from session.history. "
        f"history_contents={hist_contents}"
    )
