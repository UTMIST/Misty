from uuid import UUID

import pytest

from contracts.directory import DirectoryUnavailable
from contracts.visibility import Actor, DENY, SEE_ALL


class _Key:
    def __init__(self, scopes):
        self._scopes = set(scopes)

    def has_scope(self, s):
        return "admin" in self._scopes or s in self._scopes


class _Dir:
    def __init__(self, teams=frozenset(), down=False):
        self._teams, self._down = teams, down

    def get_active_team_ids(self, pid):
        if self._down:
            raise DirectoryUnavailable("down")
        return self._teams


def _resolve(dep, **overrides):
    """Call a dependency function directly with keyword overrides."""
    return dep(**overrides)


def test_read_context_actor_resolves_teams():
    from src.api.authz import read_context

    T1 = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    ctx = read_context(
        actor_id=UUID(int=1), key=_Key(["docs:read"]), directory=_Dir(frozenset({T1}))
    )
    assert isinstance(ctx, Actor)
    assert ctx.team_ids == frozenset({T1})


def test_read_context_directory_down_degrades_to_empty_teams():
    from src.api.authz import read_context

    ctx = read_context(actor_id=UUID(int=1), key=_Key(["docs:read"]), directory=_Dir(down=True))
    assert isinstance(ctx, Actor)
    assert ctx.team_ids == frozenset()


def test_read_context_no_actor_read_all_is_see_all():
    from src.api.authz import read_context

    ctx = read_context(actor_id=None, key=_Key(["docs:read:all"]), directory=_Dir())
    assert ctx is SEE_ALL


def test_read_context_no_actor_plain_read_is_deny():
    from src.api.authz import read_context

    ctx = read_context(actor_id=None, key=_Key(["docs:read"]), directory=_Dir())
    assert ctx is DENY


def test_read_context_no_actor_no_read_scope_403():
    from fastapi import HTTPException
    from src.api.authz import read_context

    with pytest.raises(HTTPException) as ei:
        read_context(actor_id=None, key=_Key(["docs:write"]), directory=_Dir())
    assert ei.value.status_code == 403


def test_read_context_actor_without_read_scope_403():
    from fastapi import HTTPException
    from src.api.authz import read_context

    with pytest.raises(HTTPException) as ei:
        read_context(actor_id=UUID(int=1), key=_Key(["act-as-user"]), directory=_Dir())
    assert ei.value.status_code == 403


def test_read_context_actor_with_read_scope_ok():
    from src.api.authz import read_context

    ctx = read_context(actor_id=UUID(int=1), key=_Key(["docs:read"]), directory=_Dir())
    assert isinstance(ctx, Actor)


def test_read_context_actor_with_read_all_scope_ok():
    from src.api.authz import read_context

    ctx = read_context(actor_id=UUID(int=1), key=_Key(["docs:read:all"]), directory=_Dir())
    assert isinstance(ctx, Actor)


def test_write_context_no_actor_is_see_all():
    from src.api.authz import write_context

    assert write_context(actor_id=None, directory=_Dir()) is SEE_ALL


def test_write_context_actor_resolves():
    from src.api.authz import write_context

    ctx = write_context(actor_id=UUID(int=1), directory=_Dir())
    assert isinstance(ctx, Actor)
