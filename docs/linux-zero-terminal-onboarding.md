# Linux zero-terminal onboarding (2026-10-08)

Linux 1.2.4 (`3b2317e`) onboarded a customer through a terminal:
`anyaicam-setup` asked for a Cloud ID + activation token, and
`anyaicam-setup --claim` printed a claim code for someone to relay into the
portal. This replaces that customer-facing step with a browser flow. The
secure mechanisms underneath -- headless claim (`2ceb084`), device secret,
single-use proof, credential issue and retry recovery -- are unchanged.

## Customer experience

1. Install AnyAiCam Linux. The installer now points the agent at the AnyAiCam
   cloud by default and adds **AnyAiCam Setup** to the desktop.
2. At the next desktop sign-in (or from the applications menu) **AnyAiCam
   Setup** opens in the browser: `http://127.0.0.1:8790/`, served by the
   appliance agent on the appliance itself.
3. **Link this appliance** → the browser goes to
   `https://app.anyaicam.com/claim#code=…`. The customer signs in (or creates
   an account); the claim page finds the appliance by itself and the customer
   confirms the site (one deliberate click).
4. The appliance completes enrollment on its own; the claim page waits for it
   and continues to **Discover cameras** (`/customer/setup?step=4`).
5. Discover → select cameras, enter username/password when asked → the cloud
   validates and provisions them through the appliance (existing setup steps
   4–7) → **Confirm and open dashboard**.

At no point does the customer see, copy, type or relay a Cloud ID, activation
token or claim code.

## Pieces

| Where | Change |
|---|---|
| `appliance-agent/anyaicam_agent/link_server.py` (new) | The setup page. Loopback only; `GET /`, `GET /status` (for the desktop launcher), `POST /link`. |
| `appliance-agent/anyaicam_agent/headless_claim.py` | `link_code()`: opens or resumes THIS appliance's claim with its own device secret (the cloud issues a fresh code for the same session; an expired session is followed). One lock with the step loop. |
| `appliance-agent/anyaicam_agent/service.py` | Starts the page for the agent's lifetime and shares the headless claim with it (`ANYAICAM_LINK_PORT`, default 8790, `0` = off). A busy port is a warning, never fatal. |
| `installer/14-desktop-setup.sh` (new) | `/usr/local/bin/anyaicam-setup-page`, XDG autostart (only while not linked) and menu entry; refuses symlinks; removed by `uninstall.sh`. |
| `installer/09-identity.sh` | Without `--portal-url` the agent gets `https://app.anyaicam.com`, unless `agent.env` already names a valid https cloud. |
| `installer/install.sh` | Calls the desktop setup; the final message points to **AnyAiCam Setup** instead of a terminal command. |
| `app/appliance_claims.py` | `/claim` also accepts `#code=` (kept in the tab, never shown); the claim page looks the appliance up, keeps the code out of the page, and after confirmation waits for the appliance then continues to Discover cameras. |
| `app/partner_workspace.py` | `/customer/setup?step=N` (only once the customer has an appliance). |

`anyaicam-setup` (interactive and `--claim`) stays for support and
technicians; customers are no longer directed to it.

## Security

* **Device possession is unchanged in strength.** Only a person at the
  appliance itself can start a link, as with the terminal before: the page is
  bound to `127.0.0.1`; the `Host` header must be the loopback address (DNS
  rebinding); `POST /link` needs the page's per-process random token
  (constant-time compare), and a same-origin `Origin` / `Sec-Fetch-Site` when
  the browser sends them; 6 links a minute.
* **The code never leaves the owner's browser tab** except to the cloud's own
  claim APIs (POST bodies): it is only in the redirect's URL fragment (never
  sent to a server, removed from the address bar and history by `/claim`),
  in `sessionStorage` once (removed on read), and in a JS variable. It is not
  in any page, log or file (`claim_state.json` holds the session id and
  device secret as before, never the code). Each click issues a fresh code
  for the same session; earlier links stop working.
* **Explicit consent stays.** The claim page never confirms by itself; the
  customer chooses a site of their own account and clicks Confirm (a site of
  another account is refused, 403).
* **Unchanged:** `claim/begin` / `status` / `complete`, device secret, proof
  encryption and single use, credential recovery, rate limits, the label QR
  flow, Cloud ID + activation token, the terminal `--claim` flow.
* Page headers: `no-store`, `X-Frame-Options: DENY`, CSP
  `default-src 'none'; form-action 'self' <portal>; frame-ancestors 'none'`,
  `Referrer-Policy: same-origin` (not `no-referrer`, which would make the
  browser send `Origin: null` on the page's own POST).

## Tests

* `appliance-agent/tests/test_zero_terminal_link.py` (21): link_code open /
  resume / reboot / expired / proof pending / foreign portal / cloud errors /
  already provisioned / lock with the step loop; the page: loopback only, no
  code or identifiers on the page, redirect only in the fragment, never
  logged, DNS rebinding, token / Origin / Sec-Fetch-Site, rate limit, cloud
  unreachable, linked state, `/status`, wiring, port off / busy.
  `tests/conftest.py` keeps the page off for every other agent test.
* `app/tests/test_zero_terminal_onboarding_e2e.py` (8): real cloud routes
  (uvicorn) + real agent claim and page: the full customer flow; reboot
  before confirm; reboot after confirm before redemption; link clicked twice;
  cloud unreachable then retry; expired claim replaced; a linked appliance
  never opens another claim; another account's site refused.
* `app/tests/test_zero_terminal_claim_pages.py` (4),
  `test_zero_terminal_setup_step.py` (2): `/claim#code=`, hidden code,
  auto-lookup, wait-then-Discover, explicit confirm; `?step=`.
* `installer/tests/test_desktop_setup_installer.py` (7): files, launcher
  autostart only while unlinked, menu always, symlink refusal (Linux),
  uninstall, wiring; `test_claim_label_installer.py`: portal default rules.

## Not in this change

* Installing the package itself still uses the existing installer command;
  a double-click `.deb` (branch `codex/linux-deb-packaging`) would remove that
  last step.
* No deployment: the cloud part needs the normal blue/green deploy; the Linux
  part ships in the next installer release.
