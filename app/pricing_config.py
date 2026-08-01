import json
import math
from copy import deepcopy
from pathlib import Path

CONFIG_FILE = Path('/app/recordings/pricing_config.json')

DEFAULT_PRICING = {
    'version': 2,
    'currency': 'USD',
    'trial_days': 30,
    'annual_discount_percent': 10,
    'plans': {
        '2mp': {
            'label': '2MP / 1080p',
            'motion': {'2': 7.99, '7': 8.09, '14': 8.39, '30': None},
            'continuous': {'2': None, '7': 9.79, '14': 10.89, '30': 11.99},
        },
        '4mp': {
            'label': '4MP',
            'motion': {'2': 8.19, '7': 8.59, '14': 9.19, '30': 10.09},
            'continuous': {'2': 9.49, '7': 11.09, '14': 13.29, '30': 14.79},
        },
        '8mp': {
            'label': '8MP / 4K',
            'motion': {'2': 8.49, '7': 9.09, '14': 9.89, '30': 11.29},
            'continuous': {'2': 9.99, '7': 12.39, '14': 15.69, '30': 17.59},
        },
    },
    'addons': {
        'smart_motion': {'label': 'Smart Motion', 'price': 1.79},
        'people_counting': {'label': 'People Counting', 'price': 10.99},
        'lpr': {'label': 'License Plate Recognition', 'price': 18.99},
        'ppe': {'label': 'Construction PPE Monitoring', 'price': 17.99},
    },
    'conflicts': {
        '2mp.motion.30': {
            'label': '2MP / 1080p - Motion - 30 days',
            'options': [10.99, 8.99],
            'sources': ['Visible pricing table', 'JavaScript calculator'],
            'confirmed': False,
        },
        '2mp.continuous.2': {
            'label': '2MP / 1080p - Continuous - 2 days',
            'options': [10.99, 8.99],
            'sources': ['Visible pricing table', 'JavaScript pricing object'],
            'confirmed': False,
        },
    },
    'partner': {
        'pricing_mode': 'fixed',
        'percentage_discount': 0,
        'map_enabled': False,
        'volume_tiers': [
            {'key': '1-4', 'label': '1–4 cameras', 'min': 1, 'max': 4, 'discount_percent': 0},
            {'key': '5-8', 'label': '5–8 cameras', 'min': 5, 'max': 8, 'discount_percent': 0},
            {'key': '9-16', 'label': '9–16 cameras', 'min': 9, 'max': 16, 'discount_percent': 0},
            {'key': '17-32', 'label': '17–32 cameras', 'min': 17, 'max': 32, 'discount_percent': 0},
            {'key': '33-64', 'label': '33–64 cameras', 'min': 33, 'max': 64, 'discount_percent': 0},
            {'key': '65+', 'label': '65 or more cameras', 'min': 65, 'max': 128, 'discount_percent': 0},
        ],
        'plan_terms': {},
        'addon_terms': {},
        'hardware': {'retail_price': 0, 'partner_price': None, 'partner_cost': None},
        'installation': {'retail_price': 0, 'partner_price': None, 'partner_cost': None},
    },
}


def _merge(base: dict, saved: dict) -> dict:
    for key, value in saved.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load_pricing() -> dict:
    config = deepcopy(DEFAULT_PRICING)
    try:
        if CONFIG_FILE.exists():
            _merge(config, json.loads(CONFIG_FILE.read_text(encoding='utf-8')))
    except (OSError, json.JSONDecodeError):
        pass
    _ensure_partner_terms(config)
    return config


def _ensure_partner_terms(config: dict) -> None:
    partner = config.setdefault('partner', deepcopy(DEFAULT_PRICING['partner']))
    plan_terms = partner.setdefault('plan_terms', {})
    for resolution, plan in config['plans'].items():
        for recording in ('motion', 'continuous'):
            for retention, retail in plan[recording].items():
                key = f'{resolution}.{recording}.{retention}'
                record = plan_terms.setdefault(key, {})
                record['retail_monthly_price'] = retail
                record.setdefault('partner_monthly_price', None)
                record.setdefault('partner_cost', None)
                record.setdefault('suggested_retail_price', retail)
                record.setdefault('minimum_advertised_price', None)
                record.setdefault('map_enabled', False)
    addon_terms = partner.setdefault('addon_terms', {})
    for key, addon in config['addons'].items():
        record = addon_terms.setdefault(key, {})
        record['retail_monthly_price'] = addon['price']
        record.setdefault('partner_monthly_price', None)
        record.setdefault('partner_cost', None)
        record.setdefault('suggested_retail_price', addon['price'])
        record.setdefault('minimum_advertised_price', None)
        record.setdefault('map_enabled', False)


def public_pricing(config: dict | None = None) -> dict:
    config = config or load_pricing()
    return {
        'version': config['version'], 'currency': config['currency'],
        'trial_days': config['trial_days'],
        'annual_discount_percent': config['annual_discount_percent'],
        'plans': config['plans'], 'addons': config['addons'],
        'conflicts': {key: {'label': item['label'], 'confirmed': item.get('confirmed', False)} for key, item in config.get('conflicts', {}).items()},
    }


def _partner_unit_price(retail: float, terms: dict, quantity: int, config: dict) -> float:
    partner = config['partner']; mode = partner.get('pricing_mode', 'fixed')
    if mode == 'fixed':
        value = terms.get('partner_monthly_price')
        if value is None:
            raise ValueError('Partner pricing is not configured for this selection.')
        return float(value)
    discount = float(partner.get('percentage_discount', 0))
    if mode == 'volume':
        tier = next((item for item in partner['volume_tiers'] if int(item['min']) <= quantity <= int(item['max'])), None)
        if tier is None:
            raise ValueError('No partner volume tier is configured for this camera quantity.')
        discount = float(tier.get('discount_percent', 0))
    return round(retail * (1 - discount / 100), 2)


def calculate_partner_quote(selection: dict, config: dict | None = None) -> dict:
    config = config or load_pricing(); retail_quote = calculate_quote(selection, config)
    key = f'{retail_quote["resolution"]}.{retail_quote["recording"]}.{retail_quote["retention_days"]}'
    plan_terms = config['partner']['plan_terms'][key]
    partner_cloud = _partner_unit_price(retail_quote['per_camera_cloud'], plan_terms, retail_quote['quantity'], config)
    partner_addons = 0.0; partner_cost_addons = 0.0
    for addon_key in retail_quote['addons']:
        terms = config['partner']['addon_terms'][addon_key]
        retail = float(config['addons'][addon_key]['price'])
        partner_addons += _partner_unit_price(retail, terms, retail_quote['quantity'], config)
        partner_cost_addons += float(terms.get('partner_cost') or 0)
    partner_cost_cloud = float(plan_terms.get('partner_cost') or 0)
    default_sell = retail_quote['per_camera_total']
    sell_per_camera = float(selection.get('selling_price_per_camera', default_sell))
    map_values = [float(item['minimum_advertised_price']) for item in [plan_terms] + [config['partner']['addon_terms'][a] for a in retail_quote['addons']] if item.get('map_enabled') and item.get('minimum_advertised_price') is not None]
    minimum_sell = sum(map_values) if map_values else 0
    if sell_per_camera < default_sell:
        raise ValueError('Partner selling price may equal or exceed the public retail price, but cannot be lower.')
    if sell_per_camera < minimum_sell:
        raise ValueError('Selling price is below the configured minimum advertised price.')
    quantity = retail_quote['quantity']; wholesale_unit = round(partner_cloud + partner_addons, 2)
    cost_unit = round(partner_cost_cloud + partner_cost_addons, 2)
    monthly_revenue = round(sell_per_camera * quantity, 2)
    monthly_partner_charge = round(wholesale_unit * quantity, 2)
    recurring_profit = round(monthly_revenue - monthly_partner_charge, 2)
    margin = round(recurring_profit / monthly_revenue * 100, 2) if monthly_revenue else 0
    install_sell = float(selection.get('installation_sell_price', config['partner']['installation']['retail_price']))
    install_cost = float(config['partner']['installation'].get('partner_cost') or 0)
    installation_profit = round(install_sell - install_cost, 2)
    hardware_sell = float(selection.get('hardware_sell_price', config['partner']['hardware']['retail_price']))
    hardware_charge = float(config['partner']['hardware'].get('partner_price') or 0)
    hardware_profit = round(hardware_sell - hardware_charge, 2)
    return {**retail_quote, 'selling_price_per_camera': round(sell_per_camera, 2), 'partner_price_per_camera': wholesale_unit,
            'partner_cost_per_camera': cost_unit, 'partner_profit_per_camera': round(sell_per_camera - wholesale_unit, 2),
            'partner_margin_percent': margin, 'monthly_customer_revenue': monthly_revenue,
            'monthly_partner_charge': monthly_partner_charge, 'monthly_recurring_profit': recurring_profit,
            'installation_profit': installation_profit, 'hardware_profit': hardware_profit,
            'first_year_profit': round(recurring_profit * 12 + installation_profit + hardware_profit, 2),
            'volume_tier': next((tier['label'] for tier in config['partner']['volume_tiers'] if tier['min'] <= quantity <= tier['max']), ''),
            'partner_confidential': True}


def save_pricing(config: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(config, indent=2), encoding='utf-8')
    temporary.replace(CONFIG_FILE)


def calculate_quote(selection: dict, config: dict | None = None) -> dict:
    config = config or load_pricing()
    resolution = str(selection.get('resolution', '2mp')).lower()
    recording = str(selection.get('recording', 'motion')).lower()
    retention = str(selection.get('retention', '2'))
    quantity = int(selection.get('quantity', 1))
    if quantity < 1 or quantity > 128:
        raise ValueError('Camera quantity must be between 1 and 128.')
    try:
        per_camera_cloud = config['plans'][resolution][recording][retention]
    except KeyError as error:
        raise ValueError('That resolution, recording mode, or retention option is unavailable.') from error
    conflict_key = f'{resolution}.{recording}.{retention}'
    conflict = config.get('conflicts', {}).get(conflict_key)
    if per_camera_cloud is None or (conflict and not conflict.get('confirmed')):
        raise ValueError(f'{conflict.get("label", conflict_key)} requires administrator price confirmation.')
    selected_addons = [item for item in selection.get('addons', []) if item in config['addons']]
    per_camera_analytics = round(sum(float(config['addons'][item]['price']) for item in selected_addons), 2)
    cloud_subtotal = round(float(per_camera_cloud) * quantity, 2)
    analytics_subtotal = round(per_camera_analytics * quantity, 2)
    monthly_total = round(cloud_subtotal + analytics_subtotal, 2)
    annual_before_discount = round(monthly_total * 12, 2)
    discount_percent = float(config.get('annual_discount_percent', 10))
    annual_discount = round(annual_before_discount * discount_percent / 100, 2)
    annual_total = round(annual_before_discount - annual_discount, 2)
    return {
        'resolution': resolution,
        'resolution_label': config['plans'][resolution]['label'],
        'recording': recording,
        'retention_days': int(retention),
        'quantity': quantity,
        'addons': selected_addons,
        'per_camera_cloud': round(float(per_camera_cloud), 2),
        'per_camera_analytics': per_camera_analytics,
        'per_camera_total': round(float(per_camera_cloud) + per_camera_analytics, 2),
        'cloud_subtotal': cloud_subtotal,
        'analytics_subtotal': analytics_subtotal,
        'monthly_total': monthly_total,
        'annual_before_discount': annual_before_discount,
        'annual_discount_percent': discount_percent,
        'annual_discount': annual_discount,
        'annual_total': annual_total,
        'trial_days': int(config.get('trial_days', 30)),
        'estimate_only': True,
    }
