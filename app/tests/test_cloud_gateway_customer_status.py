"""Synthetic camera mappings and entitlement non-inference tests."""
import unittest
from app import cloud_gateway_customer_status as status


class CloudGatewayCustomerStatusTests(unittest.TestCase):
    def setUp(self):
        self.appliance = {'id':'a','customer_id':'customer-a','gateway_edition':'cloud_gateway'}
        self.cameras = [dict(id='c1',name='Front',camera_number=1,device_key='device-1',customer_id='customer-a',appliance_id='a',status='active'),
                        dict(id='foreign',name='Other',camera_number=2,device_key='device-2',customer_id='customer-b',appliance_id='a',status='active')]
        self.queries = []
    def rows(self, sql, args):
        self.queries.append((sql,args)); return self.cameras
    def get(self, verified=True, customer='customer-a'):
        return status.camera_statuses(customer,self.appliance,edition_verified=verified,rows=self.rows)
    def test_scoped_camera_never_infers_paid_entitlement_or_upload_health(self):
        result = self.get()
        self.assertEqual(self.queries[0][1],('customer-a','a'))
        self.assertEqual(result,[{'camera_id':'c1','name':'Front','mapping_status':'identified',
                                  'entitlement_status':'unavailable','event_upload_status':'held'}])
        self.assertEqual(self.get(customer='customer-b'),[])
        self.assertEqual(self.get(verified=False),[])
    def test_duplicate_or_stale_mapping_is_never_identified(self):
        self.cameras.append({**self.cameras[0], 'id':'c2', 'name':'Duplicate'})
        self.assertEqual([c['mapping_status'] for c in self.get()],['ambiguous','ambiguous'])
        self.cameras.pop(); self.cameras[0]['appliance_id']='other-appliance'
        self.assertEqual(self.get(),[])
        self.cameras[0]['appliance_id']='a'; self.cameras[0]['camera_number']=None
        self.assertEqual(self.get()[0]['mapping_status'],'pending')
    def test_other_editions_and_revoked_verification_do_not_read_cameras(self):
        self.appliance['gateway_edition']='full_vms'
        self.assertEqual(self.get(),[]); self.assertEqual(self.queries,[])
        self.appliance['gateway_edition']='cloud_gateway'
        self.assertEqual(self.get(verified=False),[]); self.assertEqual(self.queries,[])

    def test_mapping_rechecks_device_keys_on_other_owned_appliances(self):
        self.cameras.append({**self.cameras[0], 'id':'c2', 'appliance_id':'different-box', 'camera_number':2})
        result=self.get()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['mapping_status'],'ambiguous')
        self.assertEqual(self.queries[1][1],('customer-a',))
        self.cameras[-1]['customer_id']='customer-b'
        self.assertEqual(self.get()[0]['mapping_status'],'identified')

    def test_unconfigured_or_out_of_range_camera_is_pending(self):
        self.cameras[0]['status']='pending'
        self.assertEqual(self.get()[0]['mapping_status'],'pending')
        self.cameras[0]['status']='active'; self.cameras[0]['camera_number']=10001
        self.assertEqual(self.get()[0]['mapping_status'],'pending')


if __name__ == '__main__': unittest.main()
