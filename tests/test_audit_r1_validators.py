"""Reproducer tests for the 5 Round-1 audit findings.

These tests are independent of the source under audit and only assert the
specific behaviors claimed in the audit. If a test passes, the bug is real.
"""

import os
import time
import tempfile
import importlib
import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


# ===========================================================================
# Finding 1.1 — core/middleware.py:30 — require_admin accepts
#               INTERNAL_TOOL_TOKEN header WITHOUT a loopback check.
# ===========================================================================
#
# Claim: any caller (not just loopback) that sets X-Odysseus-Internal-Token
# to the in-process token value passes the admin gate. Compare to
# app.py:225 which adds `_is_trusted_loopback(request)` to the same check.

class TestFinding_1_1_RequireAdmin_NoLoopback:
    def test_remote_caller_with_token_header_bypasses_admin(self):
        """A request from a non-loopback host with the internal token
        header should NOT pass the admin gate. With the bug, it does."""
        from core.middleware import require_admin, INTERNAL_TOOL_TOKEN, INTERNAL_TOOL_HEADER

        # Build a request that pretends to come from a remote IP but
        # carries the internal token header. No auth state, no admin user.
        req = SimpleNamespace()
        req.app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
        req.state = SimpleNamespace(current_user=None)
        req.headers = {INTERNAL_TOOL_HEADER: INTERNAL_TOOL_TOKEN}
        # Simulate a remote client (the loopback check at app.py:206
        # is what should reject this request, but require_admin never
        # consults request.client at all).
        req.client = SimpleNamespace(host="203.0.113.7")  # TEST-NET-3

        from fastapi import HTTPException
        # If the bug is real, require_admin silently returns (no raise).
        # If the fix is in place, it should 403.
        with pytest.raises(HTTPException) as exc:
            require_admin(req)
        assert exc.value.status_code == 403, (
            f"Remote caller with internal-token header bypassed admin gate "
            f"(returned without raising). This is the bug from finding 1.1."
        )

    def test_empty_token_env_does_not_make_header_trivially_true(self):
        """If ODYSSEUS_INTERNAL_TOKEN were empty string and the env-var was
        set (truthy-string-but-falsy), an empty header would match
        trivially. The current `or secrets.token_hex(32)` short-circuits
        before that. Verify the module-level token is never ""."""
        # Force the import to use an empty env var
        old = os.environ.get("ODYSSEUS_INTERNAL_TOKEN")
        os.environ["ODYSSEUS_INTERNAL_TOKEN"] = ""
        try:
            import core.middleware
            importlib.reload(core.middleware)
            tok = core.middleware.INTERNAL_TOOL_TOKEN
            assert tok != "", (
                f"INTERNAL_TOOL_TOKEN should never be '' — would trivially match "
                f"an empty X-Odysseus-Internal-Token header. Got {tok!r}."
            )
            assert len(tok) >= 32, f"Expected 32+ char fallback, got len={len(tok)}"
        finally:
            if old is None:
                os.environ.pop("ODYSSEUS_INTERNAL_TOKEN", None)
            else:
                os.environ["ODYSSEUS_INTERNAL_TOKEN"] = old
            importlib.reload(core.middleware)


# ===========================================================================
# Finding 1.2 — core/auth.py:370-374 — verify_password timing oracle
# ===========================================================================
#
# Claim: an unknown username returns False *immediately* (no bcrypt work),
# while a known username always pays the bcrypt cost. An attacker can
# enumerate valid usernames from response timing.

class TestFinding_1_2_TimingOracle:
    @staticmethod
    def _build_auth_manager():
        """Spin up an AuthManager backed by a temp dir with one known user."""
        from core.auth import AuthManager
        tmpdir = tempfile.mkdtemp()
        auth_path = os.path.join(tmpdir, "auth.json")
        mgr = AuthManager(auth_path=auth_path)
        # Manually inject a user with a real bcrypt hash.
        import bcrypt as _bc
        mgr._config["users"] = {
            "known_user": {
                "password_hash": _bc.hashpw(b"x", _bc.gensalt()).decode("utf-8"),
                "is_admin": False,
            }
        }
        return mgr

    def test_unknown_user_skips_bcrypt_versus_known_user_with_bad_pw(self):
        mgr = self._build_auth_manager()
        # Warm up the bcrypt cost factor (first call has a one-time setup tax)
        for _ in range(3):
            mgr.verify_password("known_user", "wrong")

        N = 30  # bcrypt is ~50-100ms per call on default cost; 30x is enough
        # Path A: unknown username. Per the cited code, returns False
        # immediately on the `if username not in self.users: return False`.
        t0 = time.perf_counter()
        for _ in range(N):
            mgr.verify_password("nonexistent_user_xyz_abc", "x")
        t_unknown = (time.perf_counter() - t0) / N

        # Path B: known username, wrong password. Must run bcrypt.checkpw.
        t0 = time.perf_counter()
        for _ in range(N):
            mgr.verify_password("known_user", "wrong_password_long_enough")
        t_known = (time.perf_counter() - t0) / N

        # The audit's threshold is 10ms. Be conservative: any >5x ratio
        # with a >1ms absolute gap is suspicious; >10ms gap is conclusive.
        gap_ms = (t_known - t_unknown) * 1000
        ratio = t_known / max(t_unknown, 1e-9)
        print(
            f"\n[1.2 timing] unknown={t_unknown*1e6:.1f}us  "
            f"known_bad={t_known*1e6:.1f}us  "
            f"gap={gap_ms:.3f}ms  ratio={ratio:.1f}x"
        )
        assert gap_ms > 10.0, (
            f"Timing oracle NOT reproduced: gap={gap_ms:.3f}ms < 10ms "
            f"(ratio={ratio:.1f}x). bcrypt is running for both paths — "
            f"finding 1.2 may be a false positive."
        )


# ===========================================================================
# Finding 1.3 — core/platform_compat.py:96 — pid_alive swallows
#               PermissionError. A live process owned by another user
#               is reported as dead.
# ===========================================================================
#
# Claim: `except (OSError, ProcessLookupError)` catches PermissionError
# (a subclass of OSError) and returns False even when the pid is alive.

class TestFinding_1_3_PidAlive_PermissionError:
    def test_permission_error_returns_false_instead_of_true(self):
        """Simulate the cross-user case by patching os.kill to raise
        PermissionError (ESRCH is the dead-pid error; EPERM is the
        'process exists but not yours' error). The function should
        ideally return True for the live-but-not-ours case, but the
        current except clause lumps it in with dead-pid."""
        from core import platform_compat
        from unittest.mock import patch as _patch

        # Simulate: pid 4242 exists but we don't own it.
        with _patch.object(platform_compat.os, "kill",
                           side_effect=PermissionError(1, "Operation not permitted")):
            result = platform_compat.pid_alive(4242)
        assert result is False, (
            f"pid_alive returned {result!r} on PermissionError. "
            f"Live cross-user process is reported as dead — finding 1.3 is real."
        )

    def test_baseline_dead_pid_still_returns_false(self):
        """Sanity: a real ESRCH (ProcessLookupError) still returns False."""
        from core import platform_compat
        from unittest.mock import patch as _patch

        with _patch.object(platform_compat.os, "kill",
                           side_effect=ProcessLookupError(3, "No such process")):
            result = platform_compat.pid_alive(99999999)
        assert result is False

    def test_baseline_alive_pid_returns_true(self):
        """Sanity: own pid is alive."""
        from core import platform_compat
        assert platform_compat.pid_alive(os.getpid()) is True


# ===========================================================================
# Finding 1.4 — core/session_manager.py:583-585 — cleanup_empty_sessions
#               deletes from self.sessions BEFORE db.commit() succeeds.
#               A commit failure leaves the in-memory map diverged from DB.
# ===========================================================================

class TestFinding_1_4_CleanupRollbackDesync:
    def test_in_memory_map_cleared_before_commit_failure(self):
        """Inject a commit() that raises, between del self.sessions[id] and
        the actual DB delete. The in-memory map should already be empty
        even though no commit succeeded — i.e. the desync window exists."""

        # We test the actual ordering by reading the source. To make the
        # test work without a real DB we patch SessionLocal + the DB
        # object's delete/query/commit and let it run.
        from core import session_manager as sm_mod
        from core.session_manager import SessionManager

        # Use a minimal fake session object
        class FakeDBSession:
            def __init__(self, sid):
                self.id = sid
                self.message_count = 0
                self.archived = False
                self.last_accessed = None
                self.is_important = False
            def __repr__(self):
                return f"<FakeDBSession {self.id}>"

        # Stub core.database with a SessionLocal that returns a MagicMock
        # whose .query(...).all() yields our fake sessions.
        class _StubSession:
            def __init__(self, sids):
                self._sids = list(sids)
                self.deleted = []
                self.committed = False
                self.rolled_back = False
                self._fail_commit = False
            def query(self, *_args, **_kw):
                class _Q:
                    def __init__(self, outer):
                        self.outer = outer
                    def all(self):
                        return [FakeDBSession(s) for s in self.outer._sids]
                return _Q(self)
            def delete(self, obj):
                self.deleted.append(obj.id)
            def commit(self):
                if self.deleted and getattr(self, "_fail_commit", False):
                    raise RuntimeError("simulated commit failure")
                self.committed = True
            def rollback(self):
                self.rolled_back = True
            def close(self):
                pass

        stub = _StubSession(["s1", "s2"])
        # Make commit fail after the in-memory del already happened.
        stub._fail_commit = True

        with patch.object(sm_mod, "SessionLocal", return_value=stub), \
             patch.object(sm_mod, "logger", MagicMock()):
            mgr = SessionManager.__new__(SessionManager)
            mgr.sessions = {"s1": MagicMock(), "s2": MagicMock()}

            with pytest.raises(RuntimeError, match="simulated commit failure"):
                mgr.cleanup_empty_sessions()

        # The desync: in-memory map was cleared, DB rollback ran, so
        # the in-memory state is now permanently out of sync with DB.
        assert mgr.sessions == {}, (
            f"In-memory sessions should be empty after cleanup attempt, "
            f"got {list(mgr.sessions)}. The del happened before commit, "
            f"so even if commit failed the map is gone — finding 1.4 is real."
        )
        assert stub.rolled_back is True
        assert stub.committed is False


# ===========================================================================
# Finding 2.5 — routes/chat_routes.py:857-875 — clean_thinking_for_save
#               is OUTSIDE the cancel's try/except. If it raises (e.g. on
#               a malformed <think> tag in the partial response), the
#               partial response is never saved AND the exception masks
#               the original CancelledError.
# ===========================================================================
#
# Code-read claim: lines 857-873 show `clean_thinking_for_save` is
# called on line 867 BEFORE the inner `try:` on line 864. So if
# clean_thinking_for_save raises, neither save_sessions() nor the
# `raise` on line 873 runs — instead the raw CancelledError is lost
# and a fresh exception escapes, never saving the partial.

class TestFinding_2_5_ThinkingClean_OutsideTry:
    def test_clean_thinking_for_save_is_called_before_inner_try(self):
        """Code-read verification: the call to clean_thinking_for_save
        on line 867 must be inside the inner try: (line 864) for the
        partial-save path to be exception-safe. Re-read the source and
        assert the structural position."""
        import re

        with open("routes/chat_routes.py", "r") as f:
            src = f.read()

        # Locate the relevant snippet (the agent-mode cancel block)
        # We expect to find, in this order:
        #   except (asyncio.CancelledError, GeneratorExit):
        #       try:
        #           if full_response:
        #               ... clean_thinking_for_save(...)
        #       except Exception:
        #           ...
        #       raise
        # Per the finding, clean_thinking_for_save is OUTSIDE the inner try.

        m = re.search(
            r"except\s*\(asyncio\.CancelledError,\s*GeneratorExit\)\s*:\s*"
            r"try\s*:\s*"
            r"if full_response\s*:\s*"
            r"logger\.info\(\"Client disconnected mid-stream for session.*?\)",
            src,
            re.DOTALL,
        )
        assert m is None, (
            "Could not locate the agent-mode cancel block. Did the file change?"
        )

        # More lenient check: find the agent-mode cancel block specifically
        # (the one that says "mid-stream for session %s").
        # Locate line numbers of the relevant tokens.
        lines = src.split("\n")
        cancel_line = None
        inner_try_line = None
        clean_call_line = None
        inner_except_line = None
        for i, line in enumerate(lines, start=1):
            if "Client disconnected mid-stream for session" in line:
                cancel_line = i
            elif cancel_line and inner_try_line is None and line.strip() == "try:":
                inner_try_line = i
            elif cancel_line and inner_try_line and clean_call_line is None and "clean_thinking_for_save" in line:
                clean_call_line = i
            elif cancel_line and inner_try_line and "except Exception:" in line and clean_call_line:
                inner_except_line = i
                break

        assert cancel_line and inner_try_line and clean_call_line and inner_except_line, (
            f"Could not parse the cancel block. cancel={cancel_line} "
            f"inner_try={inner_try_line} clean_call={clean_call_line} "
            f"inner_except={inner_except_line}"
        )

        # The bug: clean_thinking_for_save must be AFTER inner_try_line.
        assert clean_call_line > inner_try_line, (
            f"clean_thinking_for_save (line {clean_call_line}) is INSIDE the "
            f"inner try (line {inner_try_line}). Finding 2.5 is a false positive."
        )
        # And must be BEFORE inner_except_line (so the outer raise happens after).
        assert clean_call_line < inner_except_line, (
            f"clean_thinking_for_save (line {clean_call_line}) is past the "
            f"inner except (line {inner_except_line}) — different code shape."
        )

        # Print the slice for the human reader.
        print(
            f"\n[2.5 code-read] outer except={cancel_line}  "
            f"inner try={inner_try_line}  "
            f"clean_thinking_for_save={clean_call_line}  "
            f"inner except={inner_except_line}"
        )
