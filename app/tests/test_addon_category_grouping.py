"""Commercial restructure confirmed 2026-09-21: Advanced Analytics and
Face Access are marketing groupings over the pre-existing, individually
priced analytics_entitlements.ANALYTICS_CATALOG keys -- not a new
pricing/entitlement architecture. AACO is a documented future add-on
with deliberately no catalog entry, price env var, or checkout/webhook
wiring yet."""
import analytics_entitlements as ae


def test_advanced_analytics_category_covers_exactly_the_four_existing_analytics():
    assert ae.ADDON_CATEGORIES["advanced_analytics"] == ("smart_motion", "people_counting", "lpr", "ppe")
    for key in ae.ADDON_CATEGORIES["advanced_analytics"]:
        assert key in ae.ANALYTIC_KEYS


def test_face_access_category_is_facial_recognition_alone():
    assert ae.ADDON_CATEGORIES["face_access"] == ("facial_recognition",)
    assert "facial_recognition" in ae.ANALYTIC_KEYS


def test_categories_do_not_overlap():
    advanced = set(ae.ADDON_CATEGORIES["advanced_analytics"])
    face = set(ae.ADDON_CATEGORIES["face_access"])
    assert advanced.isdisjoint(face)


def test_aaco_is_explicitly_not_sellable_and_has_no_catalog_entry():
    status = ae.aaco_product_status()
    assert status["sellable"] is False
    assert status["key"] not in ae.ANALYTIC_KEYS
    assert not any(item[0] == "aaco" for item in ae.ANALYTICS_CATALOG)
