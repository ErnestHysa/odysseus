## Validation skills manager owner scope

### Code trace

**services/memory/skills.py:366-446 (update_skill + delete_skill + read_skill_md):**
```python
366:    def update_skill(self, skill_id: str, updates: Dict) -> bool:
367:        """`skill_id` is the slug name. Allows updating any field plus
368:        renames if `name` changes (file is moved on disk)."""
369:        for path in self._iter_skill_files():                  # ← NO owner filter
370:            sk = self._read_skill(path)
371:            if not sk or sk.name != skill_id:                 # ← first match wins
372:                continue
373:            old_dir = os.path.dirname(path)
374:
375:            # Apply updates in a Skill-shape friendly way
376:            scalar_keys = (
377:                "description", "version", "category", "status", "confidence",
378:                "source", "teacher_model", "owner", "when_to_use",   # ← "owner" IS in here
379:                "body_extra",
380:            )
381:            for k in scalar_keys:
382:                if k in updates:
383:                    setattr(sk, k, updates[k])                # ← silently re-owns
...
420:            self._write_skill(sk)
421:            return True
422:        return False
423:
424:    def delete_skill(self, skill_id: str) -> bool:            # ← same first-match-wins, no owner arg
425:        for path in self._iter_skill_files():
426:            sk = self._read_skill(path)
427:            if not sk or sk.name != skill_id:
428:                continue
...
459:    def read_skill_md(self, name: str) -> Optional[str]:      # ← same first-match-wins
460:        for path in self._iter_skill_files():
461:            sk = self._read_skill(path)
462:            if sk and sk.name == name:
```

The method signature is `update_skill(self, skill_id, updates)` — no `owner=` parameter at all. Owner scoping is entirely the caller's responsibility.

**src/tool_implementations.py (caller lines):**
- 699-717 (edit) — calls `sm.load(owner=owner)`, finds a `match` against the caller's skills, then **calls `sm.update_skill(name, _skill_dump(sk_new))` with just the slug** (line 716). If two skills share a slug across categories on disk, the first one yielded by `os.walk` (alphabetical) gets hit, regardless of `match["owner"]`. Also: at line 714-715 the caller even *promotes* `updates["owner"]` to `attacker` if the incoming markdown set one — making the cross-user reassignment a single field away.
- 719-741 (patch) — does **not** look the skill up via `load(owner=owner)`; it goes straight to `sm.read_skill_md(name)` (first match on disk) and then `sm.update_skill(name, _skill_dump(sk_new))` (line 740). Whichever disk file matches the slug gets stomped.
- 743-754 (publish) — does call `sm.load(owner=owner)` first and resolves a `match`, so it is safer at the call site, but it still passes only the slug to `sm.update_skill(name, updates)` at line 753 and relies on slug uniqueness within the owner's filtered list.

**routes/skills_routes.py:1499 (HTTP route):**
```python
1490:        skills = skills_manager.load(owner=user)
1491:        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
...
1494:        _verify_owner(match, user)
...
1499:        ok = skills_manager.update_skill(match.get("name"), updates)
```
The HTTP route is owner-scoped — it filters via `sm.load(owner=user)` and `_verify_owner(match, user)` before calling `update_skill`. The HTTP path is safe **only** because the route resolves the match under the caller's owner first. The in-process callers do not all do that.

### Verdict: REAL
- one-line verdict: `SkillsManager.update_skill` is a slug-keyed, first-match-wins method that has no owner awareness whatsoever; the `owner` field is in its `scalar_keys` whitelist, so a caller can reassign ownership of a foreign user's skill file in a single call.
- evidence_for:
  - skills.py:366-422: the loop at 369-422 is `for path in self._iter_skill_files():` with only `sk.name != skill_id` as the filter — no `owner` check anywhere.
  - skills.py:376-380: `scalar_keys` tuple literally includes the string `"owner"`, and lines 381-383 `setattr(sk, k, updates[k])` will happily overwrite it.
  - skills.py:424-446 (`delete_skill`) and 459-468 (`read_skill_md`) are written the same way — slug-only, first-match-wins.
  - The test below demonstrates it concretely: two real disk files with the same slug but different owners; calling `update_skill("login-flow", {"owner": "attacker", "description": "pwned"})` (no owner arg) silently rewrites one of them with both the new description AND `owner: attacker`. The static-check test (`test_update_skill_scalar_keys_include_owner`) also passes, confirming `owner` is in the whitelist.
  - Multiple in-process callers in tool_implementations.py reach this method (716, 740, 753); the patch caller (740) does not even resolve a match under the caller's owner first.
- evidence_against:
  - The HTTP route at 1499 is owner-scoped, so the *web* path is safe.
  - `add_skill` (skills.py:338-340) auto-rewrites the slug to `base-2` on collision within the same root, so under the normal `add_skill` flow two users cannot end up with the same slug *in the same category directory*. The bug only becomes exploitable when (a) two skills share a slug across different category directories, (b) the legacy `data/skills.json` path is in play, or (c) files are dropped on disk out-of-band. All of these are real, not theoretical — categories are user-controllable, and the legacy loader is documented in the module docstring as still being consulted.

### Test result
```
============================= test session starts ==============================
platform darwin -- Python 3.12.8, pytest-9.0.3, pluggy-1.6.0
collected 2 items

tests/test_skills_manager_owner_isolation.py::test_update_skill_does_not_mutate_foreign_owned_skill FAILED [ 50%]
tests/test_skills_manager_owner_isolation.py::test_update_skill_scalar_keys_include_owner PASSED [100%]

=================================== FAILURES ===================================
____________ test_update_skill_does_not_mutate_foreign_owned_skill _____________

    def test_update_skill_does_not_mutate_foreign_owned_skill(tmp_path):
        ...
        alice_path = _write_skill_md(
            skills_root, category="alice-cat", name="login-flow",
            owner="alice", description="alice original",
        )
        bob_path = _write_skill_md(
            skills_root, category="bob-cat", name="login-flow",
            owner="bob", description="bob original",
        )
        ...
        result = sm.update_skill(
            "login-flow",
            {"owner": "attacker", "description": "pwned"},
        )
        ...
        assert "owner: attacker" not in after_alice, (
            "BUG: Alice's file was silently re-owned as 'attacker' by ..."
        )
>       assert "owner: attacker" not in after_bob, (
            "BUG: Bob's file was silently re-owned as 'attacker' by ..."
        )
E       AssertionError: BUG: Bob's file was silently re-owned as 'attacker' by
E       update_skill (cross-user ownership reassignment).
E       assert 'owner: attacker' not in '---
E       name: login-flow
E       description: pwned
E       version: 1.0.0
E       category: bob-cat
E       status: draft
E       confidence: 0.8
E       source: learned
E       owner: attacker                  ← BUG: reassigned from "bob" to "attacker"
E       created: "2026-01-01T00:00:00Z"
E       ---'

========================= 1 failed, 1 passed in 0.31s ==========================
```
- test file: `tests/test_skills_manager_owner_isolation.py`
- passing/failing: **1 failed, 1 passed** — the runtime test fails (bug reproduced), the static whitelist test passes (confirms `owner` is in `scalar_keys`).

### Recommended fix shape (DO NOT IMPLEMENT)
- Add an optional `owner: Optional[str] = None` parameter to `SkillsManager.update_skill`, `delete_skill`, and `read_skill_md`; when supplied, require `sk.owner == owner` for the matched skill (raise / return False if it doesn't match), and remove `"owner"` from the `scalar_keys` whitelist so a caller can never reassign ownership via the `updates` dict (ownership changes should be an explicit, audited operation). The in-process callers in `tool_implementations.py` (lines 716, 740, 753) should then be updated to always pass `owner=owner` (resolved from the prior `sm.load(owner=owner)` match) and to strip `owner` from the `updates` payload they hand to the manager.
