"""The canonical dispatcher speaks only the declared wire contract (``tui_gateway/contracts``)."""
from types import SimpleNamespace

import pytest

from gateway.session_authority import LiveSession, SessionAuthority
from gateway.session_controls import AuthorityConnection
from gateway.session_group_controls import GROUP_METHODS
from hermes_state import SessionDB
from hermes_state_runtime import admit_session_input, begin_runtime_epoch, claim_session_input, settle_session_input
from tui_gateway.contracts.registry import CANONICAL_METHODS, METHODS


@pytest.fixture
def owner(tmp_path):
    with SessionDB(db_path=tmp_path / 'state.db') as db:
        db.create_session('s', source='test')
        epoch = begin_runtime_epoch(db, instance_id='current')
        authority = SessionAuthority(SimpleNamespace(_draining=False), profile_id='owned',
                                     instance_id='current', db=db, epoch=epoch)
        authority.sessions['s'] = LiveSession(None, 'route')
        yield AuthorityConnection(authority, object(), {'user_id': 'owner'}), db


def test_every_canonical_verb_has_a_declared_contract(owner):
    """Each name the authority answers is a ``METHODS`` (shared with the sidecar) or
    ``CANONICAL_METHODS`` declaration, so it is rendered into the generated TypeScript and
    OpenRPC; and no canonical declaration outlives its handler."""
    connection, _ = owner
    served = set(connection.handlers()) | set(GROUP_METHODS) | {'profiles.list'}
    assert sorted(served - set(METHODS) - set(CANONICAL_METHODS)) == []
    assert sorted(set(CANONICAL_METHODS) - served) == []


@pytest.mark.asyncio
async def test_declared_params_gate_canonical_verbs_before_their_handlers(owner):
    """An unknown or missing key is ``4001 invalid_params`` naming the field, including verbs
    whose handlers only ``.get`` their keys; a well-formed call is unaffected."""
    connection, db = owner
    revision = db.get_session('s')['runtime_revision']
    mutate = {'session_id': 's', 'request_id': 'r1', 'expected_revision': revision,
              'operation': 'rename', 'payload': {'title': 'Declared'}}
    try:
        for method, params, field in (
                ('prompt.receipt', {'session_id': 's', 'admission_id': 'a', 'bogus': 1}, 'bogus'),
                ('prompt.cancel', {'session_id': 's'}, 'admission_id'),
                ('session.mutate', {**mutate, 'expected_revison': revision}, 'expected_revison')):
            response = await connection.dispatch({'id': 1, 'method': method, 'params': params})
            assert response['error']['code'] == 4001, (method, response)
            assert response['error']['data']['reason'] == 'invalid_params'
            assert field in response['error']['data']['fields'], (method, response)
        applied = await connection.dispatch({'id': 2, 'method': 'session.mutate', 'params': mutate})
        CANONICAL_METHODS['session.mutate'].result.model_validate(applied['result'])
        assert db.get_session('s')['title'] == 'Declared'
    finally:
        await connection.close()


@pytest.mark.asyncio
async def test_receipt_by_input_id_uses_exact_principal_and_session(owner):
    connection, db = owner
    authority = connection.authority
    db.create_session('other-session', source='test')
    authority.sessions['other-session'] = LiveSession(None, 'other-route')
    original = admit_session_input(db, epoch=authority.epoch, principal_id=connection.actor.subject,
                                   session_id='s', request_id='input-1', payload={'text': 'synthetic'})
    other = AuthorityConnection(authority, object(), {'user_id': 'other'})
    try:
        async def lookup(client, **params):
            return await client.dispatch({'id': 1, 'method': 'prompt.receipt', 'params': params})

        receipt = await lookup(connection, session_id='s', input_id='input-1')
        assert receipt['result']['admission_id'] == original['admission_id']
        assert receipt['result']['status'] == 'queued'
        assert (await lookup(other, session_id='s', input_id='input-1'))['error']['data']['reason'] == 'not_found'
        wrong_session = await lookup(connection, session_id='other-session', input_id='input-1')
        assert wrong_session['error']['data']['reason'] == 'not_found'
        for identity in ({}, {'input_id': []}, {'admission_id': 42},
                         {'input_id': 'input-1', 'admission_id': original['admission_id']}):
            error = (await lookup(connection, session_id='s', **identity))['error']
            assert error['data']['reason'] == 'invalid_params'

        claim = claim_session_input(db, epoch=authority.epoch, session_id='s')
        live = authority.sessions['s']
        live.settling[original['admission_id']] = claim
        settle_session_input(db, epoch=authority.epoch, admission_id=original['admission_id'],
                             generation=claim['generation'], outcome='interrupted')
        receipt = await lookup(connection, session_id='s', input_id='input-1')
        assert receipt['result']['status'] == 'started'
        del live.settling[original['admission_id']]
        receipt = await lookup(connection, session_id='s', input_id='input-1')
        assert (receipt['result']['status'], receipt['result']['outcome']) == ('terminal', 'interrupted')
    finally:
        await other.close()
        await connection.close()
