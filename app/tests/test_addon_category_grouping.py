"""Commercial restructure confirmed 2026-09-21, corrected same day: Advanced
Analytics is ONE customer-billed, recurring addon_key that grants FOUR
internal analytic_keys (smart_motion/people_counting/lpr/ppe) together from
a single Stripe Price -- not four separately purchasable add-ons. Face
Access (facial_recognition) remains its own separate addon_key. This
bundling now lives directly in ANALYTICS_CATALOG's per-row analytic_keys
tuple (see analytics_entitlements.py's module docstring) rather than in a
separate ADDON_CATEGORIES grouping layered on top of a one-key-per-row
catalog. AACO is a documented future add-on with deliberately no catalog
entry, price env var, or checkout/webhook wiring yet."""
import analytics_entitlements as ae


def test_advanced_analytics_addon_grants_exactly_the_four_existing_analytics():
    entry = next(item for item in ae.ANALYTICS_CATALOG if item[0] == "advanced_analytics")
    _, _, analytic_keys, _ = entry
    assert set(analytic_keys) == {"smart_motion", "people_counting", "lpr", "ppe"}
    for key in analytic_keys:
        assert key in ae.ANALYTIC_KEYS


def test_facial_recognition_addon_grants_facial_recognition_alone():
    entry = next(item for item in ae.ANALYTICS_CATALOG if item[0] == "facial_recognition")
    _, _, analytic_keys, _ = entry
    assert analytic_keys == ("facial_recognition",)
    assert "facial_recognition" in ae.ANALYTIC_KEYS


def test_advanced_analytics_and_facial_recognition_addons_do_not_overlap():
    advanced = set(next(item for item in ae.ANALYTICS_CATALOG if item[0] == "advanced_analytics")[2])
    face = set(next(item for item in ae.ANALYTICS_CATALOG if item[0] == "facial_recognition")[2])
    assert advanced.isdisjoint(face)


def test_every_other_catalog_entry_is_a_single_analytic_key_addon():
    """Every addon_key except advanced_analytics grants exactly one
    analytic_key, and that analytic_key equals its own addon_key -- the
    2026-09-21 correction only changed advanced_analytics's shape."""
    for addon_key, _label, analytic_keys, _env_var in ae.ANALYTICS_CATALOG:
        if addon_key == "advanced_analytics":
            continue
        assert analytic_keys == (addon_key,)


def test_aaco_is_explicitly_not_sellable_and_has_no_catalog_entry():
    status = ae.aaco_product_status()
    assert status["sellable"] is False
    assert status["key"] not in ae.ADDON_KEYS
    assert status["key"] not in ae.ANALYTIC_KEYS
    assert not any(item[0] == "aaco" for item in ae.ANALYTICS_CATALOG)
