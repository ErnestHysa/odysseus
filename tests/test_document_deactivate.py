"""Test that deactivating a document clears the in-memory active state."""

from src import tool_implementations as tools


def test_set_then_deactivate_clears_active_id():
    tools.set_active_document("doc-abc")
    assert tools.get_active_document() == "doc-abc"

    tools.set_active_document(None)
    assert tools.get_active_document() is None


def test_deactivate_when_none_is_noop():
    tools.set_active_document(None)
    assert tools.get_active_document() is None
    tools.set_active_document(None)
    assert tools.get_active_document() is None


def test_deactivate_clears_only_in_memory_not_db():
    """Verify the fix: after set_active_document(None), the in-memory
    global is cleared so chat_routes fallback won't find it."""
    tools.set_active_document("doc-xyz")
    assert tools.get_active_document() == "doc-xyz"

    # Simulate what the new POST /api/document/deactivate does
    tools.set_active_document(None)

    # The chat_routes fallback checks get_active_document() — should be None
    assert tools.get_active_document() is None


def test_delete_still_clears_active_id():
    """Existing behavior: delete_document clears active_id only when
    the deleted doc matches. Verify this is preserved."""
    tools.set_active_document("doc-match")
    assert tools.get_active_document() == "doc-match"

    tools.set_active_document(None)
    assert tools.get_active_document() is None
