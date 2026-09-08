import ast
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
import partner_db
import partner_portal
from database_backend import override_target

@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.delenv('ANYAICAM_ADMIN_EMAIL', raising=False)
    monkeypatch.delenv('ANYAICAM_ADMIN_PASSWORD', raising=False)
    path=tmp_path/'accounts.db'
    with override_target(sqlite_path=path):
        partner_db.initialize_database()
        yield path

def test_first_admin_has_global_grant_and_hashed_password(target):
    user_id=partner_db.create_first_admin('Owner@example.test','Strong-test-password!')
    with partner_db.connection() as db:
        user=db.execute('SELECT * FROM partner_users WHERE id=?',(user_id,)).fetchone()
        grant=db.execute('SELECT * FROM identity_grants WHERE user_id=?',(user_id,)).fetchone()
    assert user['email']=='owner@example.test'
    assert user['password_hash']!='Strong-test-password!'
    assert partner_db.verify_password('Strong-test-password!',user['password_hash'])
    assert grant['scope_type']=='global' and grant['role']=='administrator'
    assert grant['scope_id'] is None and grant['revoked_at'] is None

def test_concurrent_first_admin_only_one_wins(target):
    def create(number):
        with override_target(sqlite_path=target):
            try:
                return partner_db.create_first_admin(f'owner{number}@example.test','Strong-test-password!')
            except partner_db.FirstAdminAlreadyExists:
                return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(create,range(4)))
    assert sum(value is not None for value in results)==1
    with partner_db.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM partner_users').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM identity_grants').fetchone()[0]==1

def test_failed_grant_rolls_back_account(target,monkeypatch):
    import appliance_identity
    def fail(*args,**kwargs): raise RuntimeError('test failure')
    monkeypatch.setattr(appliance_identity,'create_grant',fail)
    with pytest.raises(RuntimeError): partner_db.create_first_admin('a@example.test','Strong-test-password!')
    with partner_db.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM partner_users').fetchone()[0]==0

@pytest.mark.parametrize('role',['edge','combined','cloud'])
def test_anonymous_setup_routes_and_existing_account(target,monkeypatch,role):
    monkeypatch.setattr(partner_portal,'settings',SimpleNamespace(runtime_role=role))
    app=FastAPI()
    partner_portal.register_partner_routes(app,lambda title,active,content,scripts:content+scripts)
    # Execute the production authentication middleware, with authentication absent.
    source=Path('app/main.py').read_text(encoding='utf-8')
    node=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='authentication_middleware')
    node.decorator_list=[]
    namespace={'Request':object,'PUBLIC_PATH_PREFIXES':(), 'authenticated_user':lambda request:None}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'middleware','exec'),namespace)
    app.middleware('http')(namespace['authentication_middleware'])
    with TestClient(app) as client:
        assert client.get('/first-admin-setup').status_code==(404 if role=='cloud' else 200)
        response=client.post('/api/first-admin-setup',json={'email':'a@example.test','password':'Strong-test-password!'})
        assert response.status_code==(404 if role=='cloud' else 200)
        if role!='cloud':
            assert client.get('/first-admin-setup').status_code==404
            assert client.post('/api/first-admin-setup',json={}).status_code==404
    assert not any(getattr(route,'path','')=='/setup' for route in app.routes)

@pytest.mark.parametrize('email,password',[('bad','Strong-test-password!'),('a@b','short')])
def test_invalid_first_admin(target,email,password):
    with pytest.raises(ValueError): partner_db.create_first_admin(email,password)
