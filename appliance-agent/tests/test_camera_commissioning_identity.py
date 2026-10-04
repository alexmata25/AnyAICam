"""Camera commissioning without fabricated credentials, ports or MACs."""
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
from anyaicam_agent import discovery, provisioning
from anyaicam_agent.camera_binding import (
    CameraBindingStore, DiscoveredCameraStore, auto_bind_discovered_cameras,
    reconcile_cloud_cameras, stable_device_key,
)

UUID = 'urn:uuid:414e5941-4943-414d-8123-123456789abc'
OTHER = 'urn:uuid:414e5941-4943-414d-8123-123456789abd'

def camera(**kw):
    return dict(device_key=UUID, ip='192.168.253.10', mac_address='Unknown', **kw)

def cloud(key=UUID, id='cam-1', number=1):
    return dict(id=id, device_key=key, camera_number=number)

@pytest.mark.parametrize('port', [554, 8554, 10554])
def test_verify_uses_discovered_port_even_when_anonymous(port):
    with patch.object(provisioning, 'locate_device', return_value=camera(rtsp_support=True, rtsp_port=port)), patch.object(provisioning, 'verify_rtsp_credentials', return_value=(True, 'ok')) as verify:
        assert provisioning.verify_device(UUID, None)[0]
    verify.assert_called_once_with('192.168.253.10', port, '', '', path=provisioning.DEFAULT_RTSP_STREAM_PATH)

def test_onvif_uri_selects_real_port_path_and_query():
    with patch.object(provisioning, 'locate_device', return_value=camera(rtsp_support=True, rtsp_port=8554, onvif_support=True)), patch.object(provisioning, 'resolve_media_uri', return_value={'status':'resolved', 'rtsp_uri':'rtsp://192.168.253.10:10554/testcamera?profile=1'}), patch.object(provisioning, 'verify_rtsp_credentials', return_value=(True,'ok')) as verify:
        assert provisioning.verify_device(UUID, {'username':'real-user','password':'real-secret'})[0]
    verify.assert_called_once_with('192.168.253.10', 10554, 'real-user','real-secret', path='/testcamera?profile=1')

@pytest.mark.parametrize('uri', ['rtsp://192.168.253.11:8554/testcamera','rtsp://user:secret@192.168.253.10/testcamera','http://192.168.253.10/testcamera','rtsp://192.168.253.10:0/testcamera','rtsp://192.168.253.10:65536/testcamera','rtsp://192.168.253.10/testcamera#fragment'])
def test_untrusted_endpoint_never_receives_credentials(uri):
    with patch.object(provisioning, 'locate_device', return_value=camera(rtsp_support=True, rtsp_uri=uri)), patch.object(provisioning, 'verify_rtsp_credentials') as verify:
        assert not provisioning.verify_device(UUID, {'username':'real-user','password':'real-secret'})[0]
    verify.assert_not_called()

@pytest.mark.parametrize('port', [None, 0, -1, 65536, True, '8554'])
def test_invalid_explicit_port_does_not_fall_back(port):
    with patch.object(provisioning,'locate_device',return_value=camera(rtsp_support=True,rtsp_port=port)), patch.object(provisioning,'verify_rtsp_credentials') as verify:
        assert not provisioning.verify_device(UUID,None)[0]
    verify.assert_not_called()

def test_onvif_authentication_cannot_be_skipped():
    with patch.object(provisioning,'locate_device',return_value=camera(rtsp_support=True,onvif_support=True)), patch.object(provisioning,'resolve_media_uri',return_value={'status':'auth_required'}), patch.object(provisioning,'verify_rtsp_credentials') as verify:
        assert not provisioning.verify_device(UUID,None)[0]
    verify.assert_not_called()

def test_anonymous_binding_uses_actual_uuid_and_survives_ip_change(tmp_path):
    store=CameraBindingStore(tmp_path/'bindings.json')
    assert auto_bind_discovered_cameras([cloud()],[camera()],store)==['cam-1']
    binding=store.bindings()[0]
    assert binding['device_key']==UUID
    assert 'mac_address' not in binding
    moved=camera(); moved['ip']='192.168.253.11'
    assert auto_bind_discovered_cameras([cloud()],[moved],store)==[]
    reader=Mock(); reader.status.return_value={'online':True,'recording':True}
    result=reconcile_cloud_cameras([cloud()],[moved],store.bindings(),reader)
    assert result[0]['online'] and result[0]['recording']
    assert 'ip' not in result[0] and 'mac_address' not in result[0]

@pytest.mark.parametrize('key', ['ip-123456','mac-123456','Unknown','urn:uuid:1111','urn:uuid:00000000-0000-0000-0000-000000000000'])
def test_unstable_or_nil_identity_cannot_replace_mac(key,tmp_path):
    store=CameraBindingStore(tmp_path/'bindings.json')
    candidate=camera(); candidate['device_key']=key
    assert auto_bind_discovered_cameras([cloud(key)],[candidate],store)==[]
    assert store.bindings()==[]
    with pytest.raises(ValueError): store.bind('cam-1',1,device_key=key)

def test_same_uuid_cannot_bind_two_active_cloud_cameras(tmp_path):
    store=CameraBindingStore(tmp_path/'bindings.json')
    assert auto_bind_discovered_cameras([cloud(),cloud(id='cam-2',number=2)],[camera()],store)==['cam-1']
    with pytest.raises(ValueError): store.bind('cam-2',2,device_key=UUID)
    with pytest.raises(ValueError): store.bind('cam-2',1,device_key=OTHER)

def test_wrong_uuid_never_matches_by_ip(tmp_path):
    store=CameraBindingStore(tmp_path/'bindings.json'); store.bind('cam-1',1,device_key=OTHER)
    reader=Mock()
    assert reconcile_cloud_cameras([cloud()],[camera()],store.bindings(),reader)[0]['last_error']=='binding_device_key_mismatch'
    reader.status.assert_not_called()

def test_duplicate_uuid_is_rejected_before_and_after_discovery_merge(tmp_path):
    one=camera(); two=camera(); two['ip']='192.168.253.11'; two['device_key']=UUID.upper()
    store=CameraBindingStore(tmp_path/'bindings.json'); discovered=DiscoveredCameraStore(tmp_path/'discovered.json')
    assert auto_bind_discovered_cameras([cloud()],[one,two],store)==[]
    discovered.save_scan([one,two])
    assert len(discovered.cameras())==1 and discovered.cameras()[0]['identity_ambiguous']
    assert auto_bind_discovered_cameras([cloud()],discovered.cameras(),store)==[]
    discovered.save_scan([one])
    assert auto_bind_discovered_cameras([cloud()],discovered.cameras(),store)==['cam-1']

def test_scan_records_the_open_nonstandard_port():
    import ipaddress
    with patch.object(discovery,'_onvif_probe',return_value={}), patch.object(discovery,'arp_table',return_value={}), patch.object(discovery,'_port',side_effect=lambda ip,port,timeout=.25: port==8554):
        result=discovery.scan([ipaddress.ip_network('192.168.253.10/32')])
    assert result[0]['rtsp_port']==8554


def test_uuid_cannot_override_an_established_real_mac(tmp_path):
    store=CameraBindingStore(tmp_path/'bindings.json')
    store.bind('cam-1',1,'02:00:00:00:00:01',device_key=UUID)
    impostor=camera(); impostor['mac_address']='02:00:00:00:00:02'
    assert auto_bind_discovered_cameras([cloud()],[impostor],store)==[]
    assert store.bindings()[0]['mac_address']=='02:00:00:00:00:01'
    reader=Mock()
    assert reconcile_cloud_cameras([cloud()],[impostor],store.bindings(),reader)[0]['last_error']=='camera_not_discovered'
    reader.status.assert_not_called()
