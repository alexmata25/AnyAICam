const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "app", "main.py"), "utf8");
const marker = "const nativeFetch=window.fetch.bind(window);window.fetch=(input,options={{}})=>{{";
const start = source.indexOf(marker);
assert.notEqual(start, -1, "customer shell must contain its fetch/CSRF wrapper");
const end = source.indexOf("</script>", start);
assert.notEqual(end, -1, "fetch wrapper must terminate inside its script element");
// Execute the exact Python-f-string template after reducing only its escaped
// braces. This catches malformed shipped JavaScript that a duplicated test
// implementation cannot see.
const template = source.slice(start, end).replaceAll("{{", "{").replaceAll("}}", "}");
const calls = [];
const window = {
  fetch: (input, options = {}) => {
    calls.push({ input, options });
    return Promise.resolve({ ok: true });
  },
  addEventListener() {},
};
const document = {
  cookie: 'anyaicam_csrf="signed-token"',
  addEventListener() {},
  querySelector() { return null; },
};
const navigator = {};
const location = new URL("https://app.anyaicam.com/login");
const requestAnimationFrame = () => {};
const toastTimer = setTimeout;
const wrapped = new Function(
  "window", "document", "navigator", "location", "URL", "Request", "Headers",
  "requestAnimationFrame", "setTimeout", "clearTimeout", template,
);
wrapped(window, document, navigator, location, URL, Request, Headers, requestAnimationFrame, toastTimer, clearTimeout);

(async () => {
  await window.fetch("/read-only");
  assert.equal(calls.at(-1).input, "/read-only", "GET must still reach native fetch");
  assert.equal(calls.at(-1).options.headers, undefined, "GET must not receive a CSRF header");

  for (const input of ["/login", "login", new URL("/logout", location.href)]) {
    await window.fetch(input, { method: "POST" });
    assert.equal(new Headers(calls.at(-1).options.headers).get("X-CSRF-Token"), "signed-token", String(input));
  }

  const request = new Request(new URL("/logout", location.href), {
    method: "POST",
    headers: { Authorization: "Bearer fixture", "X-Test": "preserved" },
  });
  await window.fetch(request);
  const requestHeaders = new Headers(calls.at(-1).options.headers);
  assert.equal(requestHeaders.get("X-CSRF-Token"), "signed-token", "Request input receives CSRF");
  assert.equal(requestHeaders.get("Authorization"), "Bearer fixture", "Request headers are preserved");
  assert.equal(requestHeaders.get("X-Test"), "preserved", "Request custom headers are preserved");

  await window.fetch("https://example.invalid/login", { method: "POST" });
  assert.equal(new Headers(calls.at(-1).options.headers).get("X-CSRF-Token"), null, "cross-origin request gets no CSRF token");
  await window.fetch("//example.invalid/login", { method: "POST" });
  assert.equal(new Headers(calls.at(-1).options.headers).get("X-CSRF-Token"), null, "protocol-relative cross-origin request gets no CSRF token");
  console.log("actual-shell-fetch-wrapper=passed");
})().catch((error) => { console.error(error); process.exitCode = 1; });
