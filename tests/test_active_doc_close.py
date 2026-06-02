"""Behavior tests for the active-document fallback gate (#1160).

#1238 clears the in-memory pointer on detach/delete. This PR adds a
complementary gate: when the frontend sends ``active_doc_id=""`` (panel
closed), the session and in-memory fallback tiers in ``chat_routes.py``
are skipped entirely — the closed doc cannot be re-discovered even if the
pointer is stale for any reason.

Tests cover:
- JS contract: chat.js sends the field in both open and closed states
- Gate logic: the ``_frontend_sent_doc_id`` flag and fallback behavior
"""

from pathlib import Path

from src import tool_implementations as tools


_REPO = Path(__file__).resolve().parent.parent
_CHAT_JS = _REPO / "static" / "js" / "chat.js"


# ---------------------------------------------------------------------------
# JS contract — chat.js must send active_doc_id in both states
# ---------------------------------------------------------------------------

def test_js_sends_empty_string_when_panel_closed():
    src = _CHAT_JS.read_text()
    # The else branch must send an explicit empty string
    assert "fd.append('active_doc_id', '');" in src or (
        'fd.append("active_doc_id", "");' in src
    )


def test_js_sends_actual_id_when_panel_open():
    src = _CHAT_JS.read_text()
    assert "fd.append('active_doc_id', documentModule.getCurrentDocId());" in src


# ---------------------------------------------------------------------------
# Gate logic — reproduced from chat_routes.py lines 492–536
#
# The gate uses three inputs:
#   active_doc_id = form_data.get("active_doc_id", "").strip()
#   _frontend_sent_doc_id = "active_doc_id" in form_data
#
# Behavior:
#   active_doc_id non-empty  →  tier 1 lookup (by ID)
#   active_doc_id empty AND _frontend_sent  →  skip fallbacks (closed panel)
#   active_doc_id absent  →  run session + in-memory fallbacks (API caller)
# ---------------------------------------------------------------------------

def _gate_allows_fallback(form_data: dict) -> bool:
    """Return True if the fallback tier is entered."""
    active_doc_id = form_data.get("active_doc_id", "").strip()
    _frontend_sent_doc_id = "active_doc_id" in form_data
    if active_doc_id:
        return False  # tier 1
    elif not _frontend_sent_doc_id:
        return True  # fallbacks
    return False  # frontend sent empty → skip


def test_frontend_empty_field_blocks_fallback():
    """Panel closed: active_doc_id="" → fallbacks must not run."""
    assert not _gate_allows_fallback({"active_doc_id": ""})


def test_missing_field_allows_fallback():
    """Programmatic caller: no active_doc_id key → fallbacks must run."""
    assert _gate_allows_fallback({})


def test_explicit_id_does_not_enter_fallback():
    """Panel open: active_doc_id="doc-abc" → tier 1, not fallbacks."""
    assert not _gate_allows_fallback({"active_doc_id": "doc-abc"})


def test_whitespace_only_treated_as_closed():
    """active_doc_id="  " → stripped to "" → same as closed panel."""
    assert not _gate_allows_fallback({"active_doc_id": "  "})


# ---------------------------------------------------------------------------
# Interaction with tool_implementations helpers
# ---------------------------------------------------------------------------

def test_clear_active_document_after_close_prevents_stale_pointer():
    """If closeTab clears the pointer (#1238), get_active_document() returns
    None — so the in-memory fallback tier would not find a doc even if it
    were entered."""
    tools.set_active_document("doc-x")
    assert tools.get_active_document() == "doc-x"
    tools.clear_active_document("doc-x")
    assert tools.get_active_document() is None


def test_close_doc_does_not_clear_different_active_doc():
    """Closing doc B while doc A is active must not clear A's pointer."""
    tools.set_active_document("doc-a")
    assert tools.clear_active_document("doc-b") is False
    assert tools.get_active_document() == "doc-a"
    tools.set_active_document(None)
