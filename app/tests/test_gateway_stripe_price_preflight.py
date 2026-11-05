"""Synthetic Stripe catalog validation; no credentials or provider access."""
import copy
import unittest
from app.gateway_stripe_price_preflight import validate_gateway_prices


class GatewayPricePreflightTests(unittest.TestCase):
    def setUp(self):
        def price(pid,product):
            return {'id':pid,'product':product,'livemode':False,'active':True,'currency':'usd',
                    'unit_amount':999,'type':'recurring','billing_scheme':'per_unit',
                    'recurring':{'interval':'month','interval_count':1,'usage_type':'licensed'}}
        self.data={'/v1/prices/price_local':price('price_local','prod_local'),
                   '/v1/prices/price_cloud':price('price_cloud','prod_cloud'),
                   '/v1/products/prod_local':{'id':'prod_local','livemode':False,'active':True,'metadata':{'anyaicam_product':'local_gateway_remote'}},
                   '/v1/products/prod_cloud':{'id':'prod_cloud','livemode':False,'active':True,'metadata':{'anyaicam_product':'cloud_gateway'}}}
        self.reads=[]

    def run_check(self, **kwargs):
        def fetch(path,description):
            self.reads.append(path)
            return copy.deepcopy(self.data[path])
        return validate_gateway_prices(**{'local_price_id':'price_local','cloud_price_id':'price_cloud',
                                         'key_is_test':True,'fetch':fetch,**kwargs})

    def test_validates_both_prices_and_distinct_products(self):
        self.assertEqual(self.run_check(),{'ready':True,'reason':'verified_test_catalog'})
        self.assertEqual(len(self.reads),4)

    def test_live_unknown_or_same_ids_never_fetch(self):
        for kwargs in ({'key_is_test':False},{'key_is_test':None},{'cloud_price_id':'price_local'}):
            self.assertFalse(self.run_check(**kwargs)['ready'])
        self.assertEqual(self.reads,[])

    def test_bad_catalog_is_rejected(self):
        for path,key,value in [('/v1/prices/price_local','livemode',True),
                               ('/v1/prices/price_local','active',False),
                               ('/v1/prices/price_cloud','currency','eur'),
                               ('/v1/prices/price_cloud','unit_amount',1000),
                               ('/v1/products/prod_cloud','metadata',{'anyaicam_product':'local_gateway_remote'}),
                               ('/v1/products/prod_cloud','livemode',True)]:
            previous=self.data[path][key]; self.data[path][key]=value
            with self.subTest(path=path,key=key): self.assertFalse(self.run_check()['ready'])
            self.data[path][key]=previous
        self.data['/v1/prices/price_cloud']['recurring']['interval']='year'
        self.assertFalse(self.run_check()['ready'])
        self.data['/v1/prices/price_cloud']['recurring']['interval']='month'
        self.data['/v1/prices/price_cloud']['recurring']['usage_type']='metered'
        self.assertFalse(self.run_check()['ready'])

    def test_provider_failure_is_not_success(self):
        self.assertEqual(self.run_check(fetch=lambda *args:(_ for _ in ()).throw(TimeoutError())),
                         {'ready':False,'reason':'provider_unavailable'})


if __name__ == '__main__': unittest.main()
