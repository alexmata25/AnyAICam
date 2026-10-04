"""Execute the real stream lookup body with SQLite and real encrypted credentials.
AST loading avoids starting the VMS/service threads during a unit test.
"""
import ast
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
import types
from unittest.mock import patch
import pytest
from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from appliance_protocol import encrypt_camera_credentials

@pytest.fixture
def stream_lookup(monkeypatch):
    db=sqlite3.connect(':memory:'); db.row_factory=sqlite3.Row
    db.executescript('CREATE TABLE cameras(id TEXT,camera_number INTEGER,onvif_endpoint TEXT,ip_address TEXT); CREATE TABLE camera_credentials(camera_id TEXT,encrypted_blob TEXT);')
    db.execute("INSERT INTO cameras VALUES('camera',1,'rtsp://192.168.253.10:8554/testcamera','192.168.253.10')")
    @contextmanager
    def connection(): yield db
    fake=types.ModuleType('partner_db'); fake.connection=connection
    monkeypatch.setitem(sys.modules,'partner_db',fake)
    monkeypatch.setenv('ANYAICAM_CAMERA_CREDENTIAL_KEY',Fernet.generate_key().decode())
    node=next(n for n in ast.parse((ROOT/'app/main.py').read_text(encoding='utf-8')).body if isinstance(n,ast.FunctionDef) and n.name=='_provisioned_camera_stream')
    ns={}; exec(compile(ast.Module(body=[node],type_ignores=[]),'app/main.py','exec'),ns)
    yield db,ns['_provisioned_camera_stream']
    db.close()

def test_anonymous_camera_stream_needs_no_credential_record(stream_lookup):
    db,lookup=stream_lookup
    assert lookup(1)=={'rtsp_url':'rtsp://192.168.253.10:8554/testcamera','username':'','password':''}
    assert db.execute('SELECT COUNT(*) FROM camera_credentials').fetchone()[0]==0

def test_real_credentials_remain_encrypted_and_are_used(stream_lookup):
    db,lookup=stream_lookup
    token=encrypt_camera_credentials('real-user','real-password')
    assert b'real-password' not in token
    db.execute('INSERT INTO camera_credentials VALUES(?,?)',('camera',token))
    assert lookup(1)['password']=='real-password'

def test_unreadable_credentials_do_not_become_anonymous(stream_lookup):
    db,lookup=stream_lookup
    db.execute("INSERT INTO camera_credentials VALUES('camera','not-decryptable')")
    assert lookup(1) is None

def test_absent_camera_has_no_stream(stream_lookup):
    _,lookup=stream_lookup
    assert lookup(2) is None

@pytest.mark.parametrize('uri',['','http://192.168.253.10/video','file:///etc/passwd'])
def test_non_rtsp_endpoint_cannot_stream(stream_lookup,uri):
    db,lookup=stream_lookup
    db.execute('UPDATE cameras SET onvif_endpoint=?',(uri,))
    assert lookup(1) is None
