import time
from types import SimpleNamespace
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
import pytest
import live_playlist
from local_live_hls import local_playlist,segment_path
from database_backend import override_target
from partner_db import initialize_database,connection

@pytest.fixture
def local(tmp_path,monkeypatch):
    monkeypatch.setattr(live_playlist,'get_configured_signer',lambda:None)
    monkeypatch.setattr(live_playlist,'settings',SimpleNamespace(runtime_role='edge'))
    monkeypatch.delenv('ANYAICAM_CLOUDFRONT_URL',raising=False)
    monkeypatch.delenv('ANYAICAM_CLOUDFRONT_KEY_PAIR_ID',raising=False)
    monkeypatch.setattr(live_playlist,'partner_identity',lambda r:{'role':'customer_owner','customer_id':'cust','email':'owner@example.test'})
    with override_target(sqlite_path=tmp_path/'local.db'):
        initialize_database()
        with connection() as db:
            db.execute("INSERT INTO partners(id,name,created_at) VALUES('partner','Partner','2026-01-01')")
            db.execute("INSERT INTO customers(id,partner_id,name,email,status,created_at) VALUES('cust','partner','Customer','a@example.test','active','2026-01-01')")
            db.execute("INSERT INTO sites(id,customer_id,name,created_at) VALUES('site','cust','Site','2026-01-01')")
            db.execute("INSERT INTO partner_users(id,email,role,customer_id,password_hash,created_at) VALUES('user','owner@example.test','customer_owner','cust','x','2026-01-01')")
            for appliance in ['local','remote']:
                db.execute("INSERT INTO appliances(id,customer_id,site_id,cloud_id,created_at) VALUES(?,'cust','site',?,'2026-01-01')",(appliance,appliance))
                db.execute("INSERT INTO cameras(id,customer_id,site_id,appliance_id,camera_number,name,created_at) VALUES(?,'cust','site',?,1,'Camera','2026-01-01')",(appliance,appliance))
        (tmp_path/'camera1_000001.ts').write_bytes(b'synthetic local stream')
        (tmp_path/'camera1.m3u8').write_text('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2.0,\ncamera1_000001.ts\n')
        identity={'appliance_id':'local','cloud_id':'local','credential':'test-only'}
        app=FastAPI()
        live_playlist.register_live_playlist_routes(app,hls_folder=tmp_path,local_identity=lambda:identity)
        with TestClient(app) as client: yield client,tmp_path,identity

def test_local_authorized_playlist_and_segment(local):
    client,path,_identity=local
    response=client.get('/api/customer/cameras/local/live/playlist.m3u8')
    assert response.status_code==200 and '/live/segments/camera1_000001.ts' in response.text
    response=client.get('/api/customer/cameras/local/live/segments/camera1_000001.ts')
    assert response.status_code==200 and response.content==b'synthetic local stream'
    assert response.headers['cache-control']=='no-store'

@pytest.mark.parametrize('suffix',['playlist.m3u8','segments/camera1_000001.ts'])
def test_same_slot_different_appliance_is_denied(local,suffix):
    client,path,_identity=local
    assert client.get('/api/customer/cameras/remote/live/'+suffix).status_code==404

@pytest.mark.parametrize('role',['cloud','invalid'])
def test_local_route_deployment_restriction(local,monkeypatch,role):
    client,path,_identity=local
    monkeypatch.setattr(live_playlist,'settings',SimpleNamespace(runtime_role=role))
    for suffix in ['playlist.m3u8','segments/camera1_000001.ts']:
        assert client.get('/api/customer/cameras/local/live/'+suffix).status_code==503

def test_cloud_configuration_does_not_fall_back(local,monkeypatch):
    client,path,_identity=local
    monkeypatch.setenv('ANYAICAM_CLOUDFRONT_URL','https://cdn.example.test')
    assert client.get('/api/customer/cameras/local/live/playlist.m3u8').status_code==503
    assert client.get('/api/customer/cameras/local/live/segments/camera1_000001.ts').status_code==503

@pytest.mark.parametrize('name',['camera10_000001.ts','camera100_000001.ts','camera11.ts','../camera1_1.ts','camera1_1.ts:secret'])
def test_exact_camera_prefix_and_path(local,name):
    _client,path,_identity=local
    with pytest.raises(HTTPException): segment_path(path,1,name)

def test_playlist_rejects_foreign_segments_and_uri_tags(local):
    _client,path,_identity=local
    (path/'camera10_000001.ts').write_bytes(b'foreign')
    (path/'camera1.m3u8').write_text('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://bad.example/key"\n#EXTINF:2.0,\ncamera10_000001.ts\n')
    text=local_playlist(path,1,'local')
    assert 'camera10' not in text and 'https://' not in text and '#EXTINF' not in text

def test_unauthenticated_and_other_customer(local,monkeypatch):
    client,path,_identity=local
    for identity in [None,{'role':'customer_owner','customer_id':'other','email':'owner@example.test'}]:
        monkeypatch.setattr(live_playlist,'partner_identity',lambda r:identity)
        for suffix in ['playlist.m3u8','segments/camera1_000001.ts']:
            assert client.get('/api/customer/cameras/local/live/'+suffix).status_code in (403,404)

def test_missing_and_stale_manifest(local):
    _client,path,_identity=local
    assert '#EXTINF' not in local_playlist(path,2,'missing')
    import os
    os.utime(path/'camera1.m3u8',(0,0))
    assert '#EXTINF' not in local_playlist(path,1,'local')

def test_viewer_requires_camera_permission(local,monkeypatch):
    client,path,_identity=local
    monkeypatch.setattr(live_playlist,'partner_identity',lambda r:{'role':'customer_viewer','customer_id':'cust','email':'owner@example.test'})
    assert client.get('/api/customer/cameras/local/live/segments/camera1_000001.ts').status_code==403


def test_viewer_with_live_permission_can_read_only_the_authorized_camera(local,monkeypatch):
    client,path,_identity=local
    with connection() as db:
        db.execute(
            "INSERT INTO customer_camera_permissions(user_id,camera_id,can_live,can_playback) VALUES('user','local',1,0)"
        )
    monkeypatch.setattr(live_playlist,'partner_identity',lambda r:{'role':'customer_viewer','customer_id':'cust','email':'owner@example.test'})
    assert client.get('/api/customer/cameras/local/live/playlist.m3u8').status_code==200
    assert client.get('/api/customer/cameras/local/live/segments/camera1_000001.ts').status_code==200
    assert client.get('/api/customer/cameras/remote/live/playlist.m3u8').status_code==403


def test_cold_start_playlist_is_valid_empty_manifest(local):
    client,path,_identity=local
    (path/'camera1.m3u8').unlink()
    response=client.get('/api/customer/cameras/local/live/playlist.m3u8')
    assert response.status_code==200
    assert response.headers['content-type'].startswith('application/vnd.apple.mpegurl')
    assert response.text.startswith('#EXTM3U\n')
    assert '#EXTINF' not in response.text


@pytest.mark.parametrize('identity_update',[
    {'appliance_id':'remote'},
    {'cloud_id':'remote'},
    {'credential':''},
])
def test_appliance_identity_mismatch_is_denied(local,identity_update):
    client,_path,identity=local
    identity.update(identity_update)
    for suffix in ['playlist.m3u8','segments/camera1_000001.ts']:
        assert client.get('/api/customer/cameras/local/live/'+suffix).status_code in (404,503)


@pytest.mark.parametrize('encoded_name',[
    '..%2Fcamera1_000001.ts',
    '%2e%2e%2fcamera1_000001.ts',
    'camera10_000001.ts',
    'camera1.m3u8',
])
def test_segment_endpoint_rejects_traversal_and_non_segment_files(local,encoded_name):
    client,_path,_identity=local
    response=client.get('/api/customer/cameras/local/live/segments/'+encoded_name)
    assert response.status_code==404


def test_main_wires_portable_folder_exact_segment_names_and_local_identity():
    source=(__import__('pathlib').Path(__file__).parents[2]/'app'/'main.py').read_text(encoding='utf-8')
    assert 'ANYAICAM_HLS_FOLDER' in source
    assert 'camera{camera_number}_%09d.ts' in source
    assert 'local_identity=lambda: own_appliance_identity()' in source
