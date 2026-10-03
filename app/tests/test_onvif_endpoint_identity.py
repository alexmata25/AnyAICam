import pytest
from onvif_discovery import parse_probe_response, probe

CAMERA='414e5941-4943-414d-8123-123456789abc'
MESSAGE='aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

def response(address='urn:uuid:'+CAMERA, message=MESSAGE, prefix='w', discovery='http://schemas.xmlsoap.org/ws/2005/04/discovery', addressing='http://schemas.xmlsoap.org/ws/2004/08/addressing'):
 return (f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:{prefix}="{addressing}" xmlns:d="{discovery}">'
         f'<s:Header><{prefix}:MessageID>uuid:{message}</{prefix}:MessageID></s:Header><s:Body><d:ProbeMatches><d:ProbeMatch>'
         f'<{prefix}:EndpointReference><{prefix}:Address>{address}</{prefix}:Address></{prefix}:EndpointReference>'
         '<d:XAddrs>http://192.168.253.10:8080/onvif/device_service</d:XAddrs></d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>')

def test_message_uuid_never_becomes_device_identity():
 assert parse_probe_response(response())['device_key']==CAMERA

@pytest.mark.parametrize('address', ['', 'urn:uuid:not-a-uuid', 'https://example.test/'+CAMERA])
def test_missing_or_invalid_endpoint_cannot_fall_back_to_message_uuid(address):
 assert parse_probe_response(response(address=address)) is None

@pytest.mark.parametrize('addressing,discovery', [
 ('http://schemas.xmlsoap.org/ws/2004/08/addressing','http://schemas.xmlsoap.org/ws/2005/04/discovery'),
 ('http://www.w3.org/2005/08/addressing','http://docs.oasis-open.org/ws-dd/ns/discovery/2009/01')])
def test_namespaces_are_independent_of_xml_prefix(addressing, discovery):
 assert parse_probe_response(response(prefix='addr',addressing=addressing,discovery=discovery))['device_key']==CAMERA

def test_different_message_ids_deduplicate_to_one_camera():
 devices=probe(send_probe=lambda:[response(),response(message='bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')],tcp_probe=lambda *_:True,allowed_network='192.168.253.0/24')
 assert len(devices)==1 and devices[0]['device_key']==CAMERA

def test_invalid_xml_is_ignored():
 assert parse_probe_response('<d:ProbeMatch>') is None

def test_identity_and_xaddrs_are_not_combined_across_matches():
 data=response().replace('<d:XAddrs>','</d:ProbeMatch><d:ProbeMatch><d:XAddrs>')
 assert parse_probe_response(data) is None
