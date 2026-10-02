"""The cloud bounds what an appliance can store (2026-10-02): its software
version label and its camera discovery results are shown on customer pages,
so they are limited in size and characters at ingestion (the pages also
render them as text; see test_setup_hostile_appliance_values_browser.py)."""
import appliance_protocol as ap

HOSTILE = '<img src=x onerror="window.__pwned=1">'


def test_software_version_keeps_only_version_characters():
    assert "<" not in ap.sanitize_software_version(HOSTILE) and '"' not in ap.sanitize_software_version(HOSTILE)
    assert ap.sanitize_software_version("1.1.0-vms-310dcc85dc4b") == "1.1.0-vms-310dcc85dc4b"
    assert ap.sanitize_software_version("0.9.0 (310dcc8)") == "0.9.0 (310dcc8)"
    assert ap.sanitize_software_version(None) == "Unknown" and ap.sanitize_software_version("<>") == "Unknown"
    assert len(ap.sanitize_software_version("9" * 500)) == 80


def test_discovery_results_are_bounded():
    many = [{"manufacturer": "x" * 5000, "model": "m" + chr(0) + chr(27) + "z",
             "nested": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}, **{f"k{i}": i for i in range(200)}}] * 1000
    results = ap.sanitize_discovery_results(many)
    assert len(results) == ap.DISCOVERY_MAX_RESULTS
    first = results[0]
    assert len(first["manufacturer"]) == ap.DISCOVERY_MAX_TEXT and first["model"] == "mz"
    assert len(first) <= ap.DISCOVERY_MAX_KEYS + 2  # + the cloud's own id and name
    assert first["id"] == "candidate-1" and first["name"] == "Discovered camera 1"


def test_addressing_is_still_stripped():
    results = ap.sanitize_discovery_results([{"manufacturer": "Acme", **{key: "x" for key in ap.DISCOVERY_FORBIDDEN_KEYS}}])
    assert set(results[0]) == {"manufacturer", "id", "name"}
