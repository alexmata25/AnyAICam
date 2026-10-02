"""WireGuard direct remote-connectivity -- cloud-side gateway (Phase B).

See docs/wireguard-remote-connectivity-plan.md Sec 3 for the full
architecture decision this package implements one slice of: the
WireGuard tunnel terminates HERE, in a small, trusted, non-browser
process -- never in the browser, never in the customer-facing portal
process itself (that process must never hold CAP_NET_ADMIN or a real
WireGuard private key).

This package is deliberately NOT imported by app/main.py and is NOT
wired into any deployed docker-compose service in this pass -- see
gateway_service.py's own module docstring for the placement decision
and exactly what "deploying this for real" would require, none of
which happens in this pass.
"""
