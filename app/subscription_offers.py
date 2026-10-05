"""What My subscription shows and sells under billing v2 (2026-10-05).

Read from the authoritative sources, never invented:

* Plan inclusions -- per_camera_billing.PLANS[...]["features"] plus its
  retention fields. Feature gating already follows them
  (analytics_entitlements.account_wide_feature_active): AI Local and Hybrid
  include person/vehicle detection, Smart Motion, AI event search,
  intelligent notifications, core AACO and supported Talk Down (with AAC
  Voice Call); Hybrid adds cloud services and 14-day cloud EVENT retention.
* Advanced analytics -- People Counting (advanced rules and reports), LPR and
  PPE are not part of any plan in the code: only the analytics packages in
  pricing_catalog.ADDONS grant them. They are shown as ONE optional section,
  each package with exactly what it adds, and a package never offered (or
  sold, see addon_offer) when everything it grants is already included or
  already on the account through another package -- no feature is sold
  twice.
* Premium products -- Face Access (per door, by size) is unchanged.

Nothing here changes a price, a Stripe Price ID or an existing entitlement:
packages a customer already holds stay active and shown, whatever their plan.
"""
from __future__ import annotations

import os
from html import escape

import pricing_catalog

# Customer-facing names and contents of the analytics packages.
ANALYTICS_FEATURE_LABELS = {
    "people_counting": "People Counting (advanced rules and reports)",
    "lpr": "License Plate Recognition (LPR)",
    "ppe": "PPE detection",
    "smart_motion": "Smart Motion",
}
PACKAGE_NAMES = {
    "ai_essentials": "People Counting",
    "ai_professional": "People Counting + PPE",
    "vehicle_intelligence": "License Plate Recognition (LPR)",
    "advanced_analytics": "Complete analytics: People Counting + LPR + PPE",
    "talk_down": "Talk Down / two-way audio and AAC Voice Call",
}
# Plan features shown with customer wording (keys from per_camera_billing.FEATURE_LABELS).
PLAN_FEATURE_WORDING = {
    "supported_talk_down": "Talk Down / two-way audio and AAC Voice Call on supported cameras",
    "remote_viewing_where_supported": "Remote viewing where supported",
}
# Features a plan's gating treats as included (analytics_entitlements.account_wide_feature_active).
V2_INCLUDED_GRANTS = {"talk_down": "supported_talk_down", "voice_call": "supported_talk_down",
                      "smart_motion": "smart_motion", "aaco": "core_aaco_retrieval"}


def plan_included_grants(plan_key: str | None) -> set:
    """Add-on grant keys a v2 plan already includes."""
    import per_camera_billing
    features = set((per_camera_billing.PLANS.get(plan_key or "") or {}).get("features") or [])
    return {grant for grant, feature in V2_INCLUDED_GRANTS.items() if feature in features}


def addon_offer(customer_id: str, addon_key: str, plan_key: str | None = None) -> dict:
    """Whether this package may be offered/sold to this account now:
    'active' (held), 'included' (the plan includes all it grants), 'covered'
    (other active packages already grant all of it), or 'available'."""
    import analytics_entitlements as analytics
    held = set(analytics.active_addon_keys(customer_id))
    if addon_key in held:
        return {"state": "active"}
    grants = set(analytics._grants_for(addon_key))
    if grants and grants <= plan_included_grants(plan_key):
        return {"state": "included", "reason": "Included with your plan."}
    already = {grant for grant in grants if analytics.feature_granted_by_other_addon(customer_id, grant, excluding=addon_key)}
    if grants and already == grants:
        holder = next((key for key in held if grants <= set(analytics._grants_for(key))), None)
        name = PACKAGE_NAMES.get(holder or "", "a package you already have")
        return {"state": "covered", "reason": f"Already included in your {name} package."}
    if already:
        # Buying it would charge again for analytics the account already has.
        names = ", ".join(sorted(ANALYTICS_FEATURE_LABELS.get(grant, grant) for grant in already))
        return {"state": "overlaps", "reason": f"Includes {names}, which you already have. To switch packages, contact AnyAiCam support."}
    return {"state": "available"}


def plan_inclusions_html(plan_key: str, payload: dict | None = None) -> str:
    """'Included with your plan' rows for a v2 plan, from PLANS."""
    import per_camera_billing
    plan = per_camera_billing.PLANS[plan_key]
    rows = []
    for feature in plan["features"]:
        label = PLAN_FEATURE_WORDING.get(feature) or per_camera_billing.FEATURE_LABELS.get(feature, feature)
        rows.append((label, "Included"))
    local_days = (payload or {}).get("local_retention_days") or plan["local_retention_days"]
    rows.append((f"Local recording retention: {local_days} days standard", "Included"))
    if plan["cloud_event_storage"]:
        rows.append((f"Cloud EVENT retention: {plan['cloud_event_retention_days']} days (event clips, not continuous recording)", "Included"))
    html ="".join(f'<div class="health-row" data-included="{escape(label, quote=True)}"><span>{escape(label)}</span>'
                   f'<span class="pill">{escape(state)}</span></div>' for label, state in rows)
    if not plan["cloud_event_storage"]:
        html += ('<div class="health-row"><span>Cloud event storage<br><span class="health-detail">Available with Hybrid</span></span>'
                 '<span class="health-detail">Not included</span></div>')
    html += ("<p class=\"health-detail\">Longer local retention (14 or 30 days) can be set up when your appliance's storage "
             "supports it; ask AnyAiCam support. It is not a separate plan.</p>")
    return html


def plan_comparison_html() -> str:
    """The three plans side by side, for a customer choosing one."""
    import per_camera_billing
    keys = list(per_camera_billing.PLANS)
    features = []
    for key in keys:
        for feature in per_camera_billing.PLANS[key]["features"]:
            if feature not in features:
                features.append(feature)
    head = "".join(f'<th scope="col">{escape(per_camera_billing.PLANS[k]["display_name"])}<br>'
                   f'<span class="health-detail">${per_camera_billing.PLANS[k]["monthly_cents_per_camera"] / 100:.2f}/camera/mo</span></th>'
                   for k in keys)
    body = ""
    for feature in features:
        label = PLAN_FEATURE_WORDING.get(feature) or per_camera_billing.FEATURE_LABELS.get(feature, feature)
        cells = "".join("<td>✓</td>" if feature in per_camera_billing.PLANS[k]["features"] else '<td aria-label="Not included">—</td>'
                        for k in keys)
        body += f"<tr><th scope=\"row\">{escape(label)}</th>{cells}</tr>"
    body += ("<tr><th scope=\"row\">Local recording retention (standard)</th>"
             + "".join(f"<td>{per_camera_billing.PLANS[k]['local_retention_days']} days</td>" for k in keys) + "</tr>")
    body += ("<tr><th scope=\"row\">Cloud EVENT retention</th>"
             + "".join(f"<td>{per_camera_billing.PLANS[k]['cloud_event_retention_days']} days</td>" if per_camera_billing.PLANS[k]["cloud_event_storage"]
                       else "<td>—</td>" for k in keys) + "</tr>")
    return ('<div style="overflow-x:auto"><table class="plan-compare" id="v2-plan-compare" style="width:100%;border-collapse:collapse;font-size:14px">'
            f'<thead><tr><th></th>{head}</tr></thead><tbody>{body}</tbody></table></div>')


def _price_text(addon: dict | None) -> str:
    if not addon or addon["monthly_cents"] is None:
        return ""
    per = " per site" if addon["unit"] == "per_site" else ""
    return f"${addon['monthly_cents'] / 100:.2f}/mo{per}"


def analytics_rows_html(customer_id: str, plan_key: str | None, is_owner: bool, site_count: int) -> str:
    """The optional advanced-analytics packages and (where the plan does not
    include it) Talk Down, each with what it adds and one honest action."""
    import analytics_entitlements as analytics
    held = set(analytics.active_addon_keys(customer_id))
    active_features = set(analytics.get_active_analytics_for_customer(customer_id))
    rendered = []
    for key, _label, grants, env_var in analytics.ANALYTICS_CATALOG:
        if key == "facial_recognition" or key.startswith("face_access_"):
            continue
        addon = pricing_catalog.find_addon(key)
        # Same recognition as before: a held package, an older purchase recorded
        # under its own key, or the old Advanced Analytics shape.
        is_active = (key in held or key in active_features
                     or (key == "advanced_analytics" and {"people_counting", "lpr", "ppe"} <= active_features))
        if not is_active and (not addon or addon["monthly_cents"] is None):
            continue  # no approved price (e.g. Cloud Overflow): not shown as something to buy
        offer = {"state": "active"} if is_active else addon_offer(customer_id, key, plan_key)
        if offer["state"] == "included":
            continue  # shown under "Included with your plan", never as an Add
        name = PACKAGE_NAMES.get(key, _label)
        adds = [ANALYTICS_FEATURE_LABELS.get(g) for g in grants if g in ANALYTICS_FEATURE_LABELS]
        detail = " · ".join(text for text in (_price_text(addon), f"Adds: {', '.join(adds)}" if adds else "",
                                               f"{site_count} sites" if key == "talk_down" and site_count > 1 else "",
                                               "Included with AI Local and Hybrid" if key == "talk_down" and plan_key == "basic_local" else "")
                            if text)
        if offer["state"] == "active":
            included_now = key == "talk_down" and "talk_down" in plan_included_grants(plan_key)
            action = ('<span class="pill">Active</span>' if not included_now else
                      '<span class="health-detail" style="max-width:300px;text-align:right">Active (earlier add-on). Talk Down is '
                      'now included with your plan.</span>')
        elif offer["state"] in ("covered", "overlaps"):
            action = f'<span class="health-detail" style="max-width:300px;text-align:right">{escape(offer["reason"])}</span>'
        elif not os.environ.get(env_var, "").strip() or not analytics.checkout_item(key, customer_id)["sellable"]:
            action = '<span class="pending-badge" aria-disabled="true">Not available yet</span>'
        elif is_owner:
            quantity = site_count if addon["unit"] == "per_site" else 1
            action = (f'<button class="ghost-button addon-buy-button" data-addon-key="{escape(key, quote=True)}" '
                      f'data-quantity="{quantity}">Add</button>')
        else:
            action = '<span class="health-detail">Not purchased</span>'
        rendered.append(f'<div class="health-row" data-addon-row="{escape(key, quote=True)}"><span>{escape(name)}<br>'
                        f'<span class="health-detail">{escape(detail)}</span></span>{action}</div>')
    return "".join(rendered)
