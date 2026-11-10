"""Read-only, fail-closed validation for configured Gateway Stripe prices.

The caller supplies the existing Stripe reader and independently checks key
mode. This module does not load configuration or contact Stripe by itself.
"""
from urllib.parse import quote


def validate_gateway_prices(*, local_price_id, cloud_price_id, key_is_test, fetch):
    """Return readiness only when both canonical test Prices prove the catalog.

    Product metadata must explicitly classify the server-owned Stripe Product;
    generic camera plans and browser-provided metadata are not evidence.
    """
    prices=(('local_gateway_remote',local_price_id),('cloud_gateway',cloud_price_id))
    if key_is_test is not True:
        return {'ready':False,'reason':'test_credential_required'}
    if (any(not isinstance(pid,str) or not pid.startswith('price_') or pid != pid.strip() for _,pid in prices)
            or local_price_id == cloud_price_id):
        return {'ready':False,'reason':'invalid_or_duplicate_price_ids'}
    product_ids=set()
    try:
        for classification,price_id in prices:
            price=fetch('/v1/prices/'+quote(price_id,safe=''),'Gateway test Price')
            if not isinstance(price,dict): return {'ready':False,'reason':'invalid_price_response'}
            recurring=price.get('recurring')
            if (price.get('id') != price_id or price.get('livemode') is not False or price.get('active') is not True
                    or price.get('currency') != 'usd' or type(price.get('unit_amount')) is not int
                    or price['unit_amount'] != 999 or price.get('type') != 'recurring' or price.get('billing_scheme') != 'per_unit'
                    or not isinstance(recurring,dict) or recurring.get('interval') != 'month'
                    or recurring.get('usage_type') != 'licensed'
                    or type(recurring.get('interval_count')) is not int or recurring['interval_count'] != 1):
                return {'ready':False,'reason':'price_contract_mismatch'}
            product_id=price.get('product')
            if not isinstance(product_id,str) or not product_id.startswith('prod_') or product_id in product_ids:
                return {'ready':False,'reason':'product_identity_invalid'}
            product_ids.add(product_id)
            product=fetch('/v1/products/'+quote(product_id,safe=''),'Gateway test Product')
            metadata=product.get('metadata') if isinstance(product,dict) else None
            if (not isinstance(product,dict) or product.get('id') != product_id or product.get('livemode') is not False
                    or product.get('active') is not True or not isinstance(metadata,dict)
                    or metadata.get('anyaicam_product') != classification):
                return {'ready':False,'reason':'product_classification_mismatch'}
    except Exception:
        return {'ready':False,'reason':'provider_unavailable'}
    return {'ready':True,'reason':'verified_test_catalog'}
