"""Prototype: AnyAiCam Base Price + Partner Markup pricing model.

NOT wired into any live route. NOT imported by main.py or any
production code path. This is a standalone, self-contained module
built to prove the new commercial model works before any of it is
applied to production, per explicit instruction. It intentionally has
zero dependency on main.py, partner_db, or `/app` -- it can be tested
anywhere plain Python 3 runs, no container required.

Three distinct price concepts (channel-conflict-safe design, per
explicit instruction -- see the accompanying report's channel-conflict
section for the full rationale):

1. `anyaicam_base_price` -- the minimum AnyAiCam must receive.
   INTERNAL/channel-pricing information. Never displayed on
   AnyAiCam.com. Shown to partners (as their floor) and to AnyAiCam
   staff (as their own accounting), never to an end customer.
2. `standard_retail_price` -- the normal published AnyAiCam.com price.
   Always >= anyaicam_base_price, with real headroom (never "barely
   above" the base -- a thin margin here is what would let AnyAiCam's
   own direct price undercut every partner and defeat the whole
   channel). This is what a DIRECT AnyAiCam sale charges, and what a
   partner sees as a suggested default.
3. `customer_price` -- whatever a partner actually quotes their
   customer (or, for a direct sale, the standard retail price). Must
   be >= anyaicam_base_price. May be below, at, or above
   standard_retail_price -- a partner discounting toward (but never
   below) the base price is fine for AnyAiCam's required revenue; it
   simply means that partner is competing on price rather than markup.

Design (see the accompanying report, Part B, for the full rationale):

- Every plan has one authoritative `anyaicam_base_price`. Centralized
  in the *_BASE_PRICE dicts below; a real implementation would source
  this from pricing_config.json's own
  `partner.plan_terms[key].anyaicam_base_price` field instead of a
  Python dict, but the *shape* and every downstream calculation here
  is exactly what that migration would consume.
- A partner chooses any customer_price >= anyaicam_base_price.
- partner_earnings = customer_price - anyaicam_base_price.
- anyaicam_owed = anyaicam_base_price, always, regardless of markup.
- AWS cost and AnyAiCam's own margin are never part of the customer-
  or partner-facing return value -- only base_price, standard_retail,
  customer_price, and partner_earnings are ever exposed outward.
- PRICING_VERSION tags every quote so a later base-price change can
  never silently alter what an already-created quote/snapshot says --
  see calculate_customer_quote()'s own docstring.
- Partner attribution reuses the REAL, already-existing
  customers.partner_id column (confirmed live in production this
  session: the one real test customer already carries
  partner_id='anyaicam-primary') -- ANYAICAM_DIRECT_PARTNER_ID below
  is that same convention, not a new concept. A direct AnyAiCam sale
  is simply a sale attributed to AnyAiCam's own partner record; the
  "partner earnings" on a direct sale is AnyAiCam's own extra margin,
  which naturally stays with AnyAiCam rather than being paid out
  anywhere -- no separate direct-sale code path is needed at the
  ledger level, only at the UI/price-selection level (a direct sale
  always uses standard_retail_price, never a partner-chosen price).
"""

from dataclasses import dataclass
from datetime import datetime

PRICING_VERSION = "2026.08-baseprice-v1"

# Matches the real, already-existing customers.partner_id convention
# (confirmed live in production this session) for AnyAiCam's own
# direct sales -- not a new identifier being introduced.
ANYAICAM_DIRECT_PARTNER_ID = "anyaicam-primary"

# ============================================================ centralized base-price source
# In a real migration this dict is replaced by reading
# pricing_config.json's own partner.plan_terms[key].anyaicam_base_price
# field via load_pricing() -- the shape (resolution -> retention ->
# dollar amount) is identical, so calculate_customer_quote() below
# would need zero changes, only its data source swapped.

LOCAL_RECORDING_BASE_PRICE = {
    "2mp": 2.00,
    "4mp": 3.00,
    "8mp": 4.50,
}

MOTION_CLOUD_BASE_PRICE = {
    "2mp": {"2": 2.00, "7": 2.00, "14": 2.00, "30": 2.00},
    "4mp": {"2": 3.00, "7": 3.00, "14": 3.00, "30": 3.00},
    "8mp": {"2": 4.50, "7": 4.50, "14": 4.50, "30": 4.50},
}

CONTINUOUS_CLOUD_BASE_PRICE = {
    "2mp": {"2": 3.00, "7": 8.50, "14": 16.50, "30": 35.00},
    "4mp": {"2": 5.00, "7": 15.50, "14": 30.00, "30": 64.00},
    "8mp": {"2": 8.50, "7": 28.00, "14": 54.50, "30": 115.50},
}

# Standard published AnyAiCam.com retail -- what a DIRECT AnyAiCam sale
# charges, and what a partner sees as a suggested default. Reuses this
# session's earlier "example customer price" numbers, which already
# carry real headroom above the base prices above -- exactly the
# margin partners need for the channel to make sense. Deliberately
# never barely-above-base: see the module docstring's channel-conflict
# note on why a thin gap here would let AnyAiCam's own direct price
# undercut every partner.
LOCAL_RECORDING_STANDARD_RETAIL = {
    "2mp": 4.99,
    "4mp": 6.99,
    "8mp": 10.99,
}

MOTION_CLOUD_STANDARD_RETAIL = {
    "2mp": {"2": 4.99, "7": 5.99, "14": 6.99, "30": 8.99},
    "4mp": {"2": 6.99, "7": 8.49, "14": 9.99, "30": 12.99},
    "8mp": {"2": 10.99, "7": 12.99, "14": 15.49, "30": 19.99},
}

CONTINUOUS_CLOUD_STANDARD_RETAIL = {
    "2mp": {"2": 4.99, "7": 14.00, "14": 27.00, "30": 56.50},
    "4mp": {"2": 8.00, "7": 25.00, "14": 49.00, "30": 103.50},
    "8mp": {"2": 14.00, "7": 45.00, "14": 88.50, "30": 188.00},
}

PRODUCTS = ("local", "motion", "continuous")


class BasePriceError(ValueError):
    """Raised whenever a proposed customer price would give AnyAiCam
    less than its required base price. Never silently clamped or
    corrected -- the caller (a real quote-creation route) must handle
    this exactly like any other validation failure."""


def get_base_price(product: str, resolution: str, retention_days: str | int | None = None) -> float:
    """The single function every consumer (quote generation,
    activation, the admin pricing screen, reporting) should call for
    "what must AnyAiCam receive for this plan" -- centralizing this
    one lookup is what makes a future "+$1 across every plan" change
    a data edit to the three dicts above, never a code change here or
    in any caller."""
    resolution = resolution.lower()
    if product == "local":
        if resolution not in LOCAL_RECORDING_BASE_PRICE:
            raise ValueError(f"Unknown resolution for Local Recording: {resolution!r}")
        return LOCAL_RECORDING_BASE_PRICE[resolution]
    if product in ("motion", "continuous"):
        table = MOTION_CLOUD_BASE_PRICE if product == "motion" else CONTINUOUS_CLOUD_BASE_PRICE
        retention_key = str(retention_days)
        if resolution not in table or retention_key not in table[resolution]:
            raise ValueError(f"Unknown resolution/retention for {product}: {resolution!r}/{retention_key!r}")
        return table[resolution][retention_key]
    raise ValueError(f"Unknown product: {product!r}. Must be one of {PRODUCTS}.")


def get_standard_retail_price(product: str, resolution: str, retention_days: str | int | None = None) -> float:
    """The published AnyAiCam.com price -- what a direct sale charges,
    and what a partner sees as a suggested default. Never the floor
    itself (get_base_price() is the floor); this is deliberately a
    SEPARATE lookup so the two can never be accidentally conflated by
    a caller reading from the wrong table."""
    resolution = resolution.lower()
    if product == "local":
        if resolution not in LOCAL_RECORDING_STANDARD_RETAIL:
            raise ValueError(f"Unknown resolution for Local Recording: {resolution!r}")
        return LOCAL_RECORDING_STANDARD_RETAIL[resolution]
    if product in ("motion", "continuous"):
        table = MOTION_CLOUD_STANDARD_RETAIL if product == "motion" else CONTINUOUS_CLOUD_STANDARD_RETAIL
        retention_key = str(retention_days)
        if resolution not in table or retention_key not in table[resolution]:
            raise ValueError(f"Unknown resolution/retention for {product}: {resolution!r}/{retention_key!r}")
        return table[resolution][retention_key]
    raise ValueError(f"Unknown product: {product!r}. Must be one of {PRODUCTS}.")


@dataclass(frozen=True)
class CustomerQuote:
    """The complete, authoritative result of a quote calculation.
    Every field here is safe to show to a partner. base_price is
    intentionally the ONLY AnyAiCam-side number ever included --
    AWS cost and AnyAiCam's own net/margin never appear anywhere in
    this object, matching the explicit requirement that partners
    never need to understand AnyAiCam's internal cost structure."""

    product: str
    resolution: str
    retention_days: str | None
    base_price: float
    standard_retail_price: float
    customer_price: float
    partner_earnings: float
    anyaicam_owed: float
    attributed_partner_id: str
    pricing_version: str
    quoted_at: str


def calculate_customer_quote(
    product: str,
    resolution: str,
    customer_price: float,
    retention_days: str | int | None = None,
    partner_id: str = ANYAICAM_DIRECT_PARTNER_ID,
    now: datetime | None = None,
) -> CustomerQuote:
    """The one function a real /api/partner/calculate-equivalent route
    would call. Raises BasePriceError if customer_price is below the
    required base price -- never clamps it up silently, and never
    lets a below-floor quote through with a wrong number.

    `partner_id` defaults to AnyAiCam's own direct-sale attribution
    (ANYAICAM_DIRECT_PARTNER_ID) -- a real caller creating a quote on
    behalf of an actual partner always passes that partner's own id.
    This mirrors the real, already-existing customers.partner_id
    column exactly; no new attribution concept is introduced.

    `now` is optional and test-only (mirrors this session's own
    established now= pattern for deterministic timestamp assertions);
    production call sites never pass it.

    The returned CustomerQuote's `pricing_version` field is what makes
    historical-record protection work: once a quote is created, this
    object (or its serialized form) is what gets stored -- verbatim,
    forever. A LATER change to the base-price/retail tables or
    PRICING_VERSION has zero effect on an already-returned
    CustomerQuote, because nothing here re-reads live state after this
    call returns; the caller is responsible for persisting this
    object's fields (never re-deriving them later from the
    then-current pricing tables)."""
    base_price = get_base_price(product, resolution, retention_days)
    standard_retail = get_standard_retail_price(product, resolution, retention_days)
    if customer_price < base_price:
        raise BasePriceError(
            f"Customer price ${customer_price:.2f} is below AnyAiCam's required base price "
            f"${base_price:.2f} for {product}/{resolution}"
            + (f"/{retention_days}d" if retention_days is not None else "") + "."
        )
    partner_earnings = round(customer_price - base_price, 2)
    return CustomerQuote(
        product=product,
        resolution=resolution,
        retention_days=str(retention_days) if retention_days is not None else None,
        base_price=round(base_price, 2),
        standard_retail_price=round(standard_retail, 2),
        customer_price=round(customer_price, 2),
        partner_earnings=partner_earnings,
        anyaicam_owed=round(base_price, 2),
        attributed_partner_id=partner_id,
        pricing_version=PRICING_VERSION,
        quoted_at=(now or datetime.now()).isoformat(),
    )


def customer_facing_view(quote: CustomerQuote) -> dict:
    """What a CUSTOMER is allowed to see -- explicitly excludes
    base_price, partner_earnings, and anyaicam_owed. Only the
    product/plan info and the customer's own agreed price. A real
    customer-facing route should build its response through this
    function, never by hand-picking fields from CustomerQuote (which
    would risk a future field addition to CustomerQuote silently
    leaking through)."""
    return {
        "product": quote.product,
        "resolution": quote.resolution,
        "retention_days": quote.retention_days,
        "price": quote.customer_price,
    }


def partner_facing_view(quote: CustomerQuote) -> dict:
    """What a PARTNER is allowed to see: their own earnings, the
    AnyAiCam base price they must clear (their floor), AND the
    standard retail price (a suggested default/reference point) --
    explicit per instruction that partners should see both. Never AWS
    cost or AnyAiCam's own internal margin -- those never exist as
    fields on CustomerQuote at all, so there's nothing for this
    function to accidentally leak."""
    return {
        "product": quote.product,
        "resolution": quote.resolution,
        "retention_days": quote.retention_days,
        "anyaicam_base_price": quote.base_price,
        "standard_retail_price": quote.standard_retail_price,
        "customer_price": quote.customer_price,
        "partner_earnings": quote.partner_earnings,
    }


def public_website_view(product: str, resolution: str, retention_days: str | int | None = None) -> dict:
    """What AnyAiCam.com is allowed to display: ONLY the standard
    retail price. Never the internal base price -- this is the
    channel-conflict guardrail. Deliberately does not accept or return
    a CustomerQuote at all (no quote/partner/customer context exists
    yet at the point someone is just browsing the public pricing
    page), so there is no base_price field anywhere nearby that a
    future refactor could accidentally start rendering."""
    return {
        "product": product,
        "resolution": resolution,
        "retention_days": str(retention_days) if retention_days is not None else None,
        "price": round(get_standard_retail_price(product, resolution, retention_days), 2),
    }


def calculate_direct_sale_quote(product: str, resolution: str, retention_days: str | int | None = None, now: datetime | None = None) -> CustomerQuote:
    """AnyAiCam selling direct, with no partner involved. Always
    charges the standard retail price -- never discounted to the base
    price or below what a partner would see as their own floor -- so
    AnyAiCam's own direct channel can never undercut a partner's
    minimum viable price. Attributed to ANYAICAM_DIRECT_PARTNER_ID,
    the same real customers.partner_id convention already in
    production. The resulting "partner_earnings" on this quote is
    AnyAiCam's own extra margin above its required base -- it simply
    isn't paid out anywhere, since AnyAiCam is both the base-price
    recipient and the (non-)partner here."""
    standard_retail = get_standard_retail_price(product, resolution, retention_days)
    return calculate_customer_quote(
        product, resolution, customer_price=standard_retail, retention_days=retention_days,
        partner_id=ANYAICAM_DIRECT_PARTNER_ID, now=now,
    )


@dataclass(frozen=True)
class ServiceCharge:
    """A partner-provided line item that is explicitly NOT subscription
    markup: installation, support, warranty, maintenance, networking,
    monitoring, or any other itemized service. Deliberately a
    completely separate type from CustomerQuote -- there is no shared
    base class, no shared field names beyond the plain dollar amounts,
    and no code path that folds a ServiceCharge into
    CustomerQuote.partner_earnings. This mirrors the already-existing
    real distinction in calculate_partner_quote() between
    monthly_recurring_profit (subscription) and installation_profit/
    hardware_profit (separate line items) -- this prototype keeps that
    same separation, generalized to any named service type rather than
    only installation/hardware."""

    service_type: str
    partner_price: float
    anyaicam_cost: float


def calculate_service_charge(service_type: str, partner_price: float, anyaicam_cost: float = 0.0) -> ServiceCharge:
    """A partner charging for installation/support/warranty/maintenance/
    networking/monitoring/etc. This has no required-base-price floor
    at all by default (anyaicam_cost defaults to 0.0 -- AnyAiCam has
    no stake in most such services unless it explicitly chooses to,
    e.g. reselling a warranty it underwrites itself) -- it is
    structurally impossible for this function to be mistaken for a
    subscription quote, since ServiceCharge and CustomerQuote share no
    fields."""
    return ServiceCharge(service_type=service_type, partner_price=round(partner_price, 2), anyaicam_cost=round(anyaicam_cost, 2))


# ============================================================ Partner attribution: QR/link/code checkout, hijack protection, transfers
#
# The preferred flow (per explicit instruction) is that the CUSTOMER
# completes checkout directly with AnyAiCam -- a partner never has to
# purchase on the customer's behalf. What a partner hands the customer
# is an ATTRIBUTION TOKEN (a QR code, referral link, or partner code
# all resolve to the same underlying object below -- the transport
# mechanism differs, the attribution logic does not). Once a
# customer/account relationship is established, attribution is
# persisted server-side (on the customer/quote record, reusing the
# real, already-existing customers.partner_id column) -- never left to
# live only in a browser cookie, exactly as instructed.

@dataclass(frozen=True)
class AttributionToken:
    """A partner-specific QR code / referral link / partner code is all
    the same object underneath -- only the transport differs (a QR
    encodes the same token a link carries as a query parameter, which
    is the same string a partner could read aloud as a "code"). One
    validation path serves all three, so there is no divergence to
    keep in sync."""

    token: str
    partner_id: str
    created_at: str
    expires_at: str | None
    revoked: bool = False

    def is_valid(self, now: datetime | None = None) -> bool:
        if self.revoked:
            return False
        if self.expires_at is None:
            return True
        return (now or datetime.now()).isoformat() <= self.expires_at


def generate_attribution_token(partner_id: str, token: str, valid_days: int | None = 90, now: datetime | None = None) -> AttributionToken:
    """Issue a new token for a partner. `token` is passed in rather than
    generated here -- a real implementation would use a cryptographically
    random, unguessable string (e.g. secrets.token_urlsafe()); this
    prototype keeps token generation itself out of scope and focuses on
    the attribution/resolution logic that consumes one. `valid_days=None`
    means the token never expires by date (still revocable)."""
    created = now or datetime.now()
    expires = None
    if valid_days is not None:
        from datetime import timedelta
        expires = (created + timedelta(days=valid_days)).isoformat()
    return AttributionToken(token=token, partner_id=partner_id, created_at=created.isoformat(), expires_at=expires)


@dataclass(frozen=True)
class AttributionDecision:
    """The result of resolving who a customer is attributed to, plus
    WHY -- this `reason` string is exactly the attribution audit trail
    requested: every resolution (not just transfers) produces one of
    these, and a real implementation persists every one, not only the
    final state, so a later dispute has a full history to point to."""

    resolved_partner_id: str
    previous_partner_id: str | None
    reason: str
    decided_at: str


def resolve_attribution(
    existing_partner_id: str | None,
    presented_token: AttributionToken | None = None,
    now: datetime | None = None,
) -> AttributionDecision:
    """The single function every checkout/quote-creation path calls to
    decide who a customer is attributed to. Never raises and never
    blocks checkout -- an expired or invalid token silently falls back
    to AnyAiCam-direct attribution rather than stopping the sale,
    matching the explicit preference that the customer always be able
    to complete checkout themselves.

    Cases (matching the explicit list requested):

    - Brand new customer (existing_partner_id is None), valid token
      -> attributed to that partner ("partner QR/link -> customer
      checkout").
    - Brand new customer, no token or an invalid/expired/revoked one
      -> attributed to ANYAICAM_DIRECT_PARTNER_ID ("AnyAiCam direct
      customer with no partner" / "expired or invalid attribution
      links or codes").
    - Existing customer currently attributed to AnyAiCam-direct (never
      actually partner-owned) and a valid token from a real partner
      arrives -> reattributed to that partner. Direct attribution is
      not a protected relationship to hijack-guard against, since no
      partner relationship existed yet.
    - Existing customer already attributed to a REAL partner (not
      AnyAiCam-direct) -> that attribution is preserved NO MATTER WHAT
      token is presented, including a different partner's valid token
      ("preventing one partner from hijacking another partner's
      existing customer"). The only way to move a customer off an
      established partner is the explicit, admin-mediated
      transfer_customer_attribution() below -- never a token.
    - "Customer leaves and later returns directly to AnyAiCam.com":
      if a quote/customer record already exists with a real partner_id
      (established server-side, not just a cookie that expired when
      they left), calling this again with that existing_partner_id
      hits the "preserved" case above -- the attribution survives the
      customer's browsing session ending.
    - Renewals / plan changes / camera count changes / cancel-then-
      reactivate: all of these call this function with the customer's
      current existing_partner_id and (normally) no new token -- they
      all fall into the "preserved" case, so recurring partner
      earnings continue uninterrupted across any of these events
      without needing separate handling per event type."""
    decided_at = (now or datetime.now()).isoformat()
    token_valid = presented_token is not None and presented_token.is_valid(now)

    if existing_partner_id is None:
        if token_valid:
            return AttributionDecision(presented_token.partner_id, None, "new_customer_attributed_via_token", decided_at)
        reason = "new_customer_no_token_direct" if presented_token is None else "invalid_token_defaulted_to_direct"
        return AttributionDecision(ANYAICAM_DIRECT_PARTNER_ID, None, reason, decided_at)

    if existing_partner_id == ANYAICAM_DIRECT_PARTNER_ID:
        if token_valid:
            return AttributionDecision(presented_token.partner_id, existing_partner_id, "reattributed_from_direct_to_partner", decided_at)
        return AttributionDecision(existing_partner_id, existing_partner_id, "remains_direct", decided_at)

    # existing_partner_id is a real, already-established partner:
    # protected, regardless of what token (if any) is presented.
    if token_valid and presented_token.partner_id != existing_partner_id:
        return AttributionDecision(existing_partner_id, existing_partner_id, "hijack_attempt_blocked_existing_partner_protected", decided_at)
    return AttributionDecision(existing_partner_id, existing_partner_id, "existing_attribution_preserved", decided_at)


def transfer_customer_attribution(
    current_partner_id: str,
    new_partner_id: str,
    approved_by: str,
    reason: str,
    now: datetime | None = None,
) -> AttributionDecision:
    """The ONLY way an established partner attribution changes --
    always an explicit, admin-mediated action (never a token, never
    automatic). `approved_by` and `reason` are required and become part
    of the audit trail -- a real implementation persists this exact
    record (old partner, new partner, who approved it, why, when)
    rather than merely updating customers.partner_id in place."""
    if not approved_by:
        raise ValueError("Customer transfers require an approving admin identity.")
    if not reason:
        raise ValueError("Customer transfers require an explicit reason.")
    decided_at = (now or datetime.now()).isoformat()
    return AttributionDecision(new_partner_id, current_partner_id, f"admin_transfer_approved_by:{approved_by}:{reason}", decided_at)


# ============================================================ Discounts and promotions: centrally controlled, explicit economic treatment
#
# A partner calling calculate_customer_quote() above can NEVER cause
# AnyAiCam to receive less than anyaicam_base_price -- that function
# already raises BasePriceError for any customer_price below base, and
# nothing about that check is bypassable by a partner. The ONLY path
# that can ever put a real dollar amount below base_price is the
# explicit, admin-only path below -- and even then, WHO absorbs the
# shortfall must be stated on the Promotion object itself, never
# inferred from the retail price.

@dataclass(frozen=True)
class Promotion:
    """An admin-created, admin-approved subsidy. `admin_approved` must
    be True for calculate_promotional_quote() to ever allow a
    below-base sale -- a Promotion object with admin_approved=False is
    inert by design (e.g. a draft awaiting sign-off), not merely a
    convention callers are trusted to check themselves.

    `absorbed_by` is the explicit, stored economic treatment: who pays
    for the gap between the discounted customer_price and the required
    base_price. 'anyaicam' means AnyAiCam's own net receipt drops below
    its normal base price for this sale. 'partner' means AnyAiCam still
    receives its full base_price and the partner's own earnings absorb
    the shortfall (may go negative -- the partner owes for the privilege
    of running the promotion). 'shared' splits the shortfall by
    `partner_share` (0.0-1.0, the fraction the partner absorbs)."""

    promotion_id: str
    absorbed_by: str  # 'anyaicam' | 'partner' | 'shared'
    admin_approved: bool
    created_by: str
    partner_share: float | None = None  # required, 0.0-1.0, only when absorbed_by == 'shared'

    def __post_init__(self):
        if self.absorbed_by not in ("anyaicam", "partner", "shared"):
            raise ValueError(f"Unknown absorbed_by: {self.absorbed_by!r}. Must be 'anyaicam', 'partner', or 'shared'.")
        if self.absorbed_by == "shared":
            if self.partner_share is None or not (0.0 <= self.partner_share <= 1.0):
                raise ValueError("Shared promotions require partner_share between 0.0 and 1.0.")


@dataclass(frozen=True)
class PromotionalQuote:
    """Everything CustomerQuote has, plus the explicit subsidy
    accounting a promoted sale requires: how much was subsidized, who
    absorbed it, and under which promotion_id -- so an accounting
    system never has to infer the treatment from the difference between
    customer_price and standard_retail_price."""

    product: str
    resolution: str
    retention_days: str | None
    base_price: float
    standard_retail_price: float
    customer_price: float
    anyaicam_net: float
    subsidized_amount: float
    absorbed_by: str | None
    partner_earnings: float
    attributed_partner_id: str
    promotion_id: str | None
    pricing_version: str
    quoted_at: str


def calculate_promotional_quote(
    product: str,
    resolution: str,
    customer_price: float,
    retention_days: str | int | None = None,
    partner_id: str = ANYAICAM_DIRECT_PARTNER_ID,
    promotion: Promotion | None = None,
    now: datetime | None = None,
) -> PromotionalQuote:
    """The ONLY function in this module that can ever return a sale
    priced below anyaicam_base_price -- and only when an explicit,
    admin-approved Promotion is passed. Without a Promotion (or with an
    unapproved one), this behaves exactly like calculate_customer_quote:
    BasePriceError below base_price, AnyAiCam always receives its full
    base_price, no ambiguity.

    Enforces: AnyAiCam's net required amount >= base_price UNLESS an
    admin-approved promotion explicitly subsidizes the difference --
    and even then, the shortfall is charged to whoever the Promotion
    says absorbs it, never silently split by an inferred formula."""
    base_price = get_base_price(product, resolution, retention_days)
    standard_retail = get_standard_retail_price(product, resolution, retention_days)
    quoted_at = (now or datetime.now()).isoformat()

    if customer_price >= base_price:
        return PromotionalQuote(
            product=product, resolution=resolution,
            retention_days=str(retention_days) if retention_days is not None else None,
            base_price=round(base_price, 2), standard_retail_price=round(standard_retail, 2),
            customer_price=round(customer_price, 2), anyaicam_net=round(base_price, 2),
            subsidized_amount=0.0, absorbed_by=None,
            partner_earnings=round(customer_price - base_price, 2),
            attributed_partner_id=partner_id,
            promotion_id=promotion.promotion_id if promotion else None,
            pricing_version=PRICING_VERSION, quoted_at=quoted_at,
        )

    if promotion is None or not promotion.admin_approved:
        raise BasePriceError(
            f"Customer price ${customer_price:.2f} is below AnyAiCam's required base price "
            f"${base_price:.2f} for {product}/{resolution}"
            + (f"/{retention_days}d" if retention_days is not None else "")
            + " and no admin-approved promotion authorizes the difference."
        )

    shortfall = round(base_price - customer_price, 2)
    if promotion.absorbed_by == "anyaicam":
        anyaicam_net = round(customer_price, 2)
        partner_earnings = 0.0
    elif promotion.absorbed_by == "partner":
        anyaicam_net = round(base_price, 2)
        partner_earnings = round(customer_price - base_price, 2)  # negative: partner absorbs the shortfall
    else:  # 'shared', validated by Promotion.__post_init__
        partner_shortfall = round(shortfall * promotion.partner_share, 2)
        anyaicam_shortfall = round(shortfall - partner_shortfall, 2)
        anyaicam_net = round(base_price - anyaicam_shortfall, 2)
        partner_earnings = round(-partner_shortfall, 2)

    return PromotionalQuote(
        product=product, resolution=resolution,
        retention_days=str(retention_days) if retention_days is not None else None,
        base_price=round(base_price, 2), standard_retail_price=round(standard_retail, 2),
        customer_price=round(customer_price, 2), anyaicam_net=anyaicam_net,
        subsidized_amount=shortfall, absorbed_by=promotion.absorbed_by,
        partner_earnings=partner_earnings, attributed_partner_id=partner_id,
        promotion_id=promotion.promotion_id,
        pricing_version=PRICING_VERSION, quoted_at=quoted_at,
    )
