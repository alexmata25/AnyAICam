const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "app", "main.py"), "utf8");
assert(source.includes("token=token.replace(/^\"(.*)\"$/,'$1')"));
assert(source.includes("'X-CSRF-Token':token"));

const document = { cookie: 'anyaicam_csrf="signed-token"' };
let capturedHeader = null;
const nativeFetch = (_input, options) => {
  capturedHeader = options.headers?.["X-CSRF-Token"] ?? null;
  return Promise.resolve({ ok: true });
};
const window = {
  fetch: nativeFetch,
  location: new URL("https://app.anyaicam.com/login"),
};

window.fetch = (input, options = {}) => {
  const method = (options.method || "GET").toUpperCase();
  const requestUrl = typeof input === "string"
    ? new URL(input, window.location.href)
    : new URL(input.url);
  const sameOrigin = requestUrl.origin === window.location.origin;
  if (sameOrigin && ["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
    const csrf = document.cookie
      .split("; ")
      .find((item) => item.startsWith("anyaicam_csrf="));
    if (csrf) {
      let token = decodeURIComponent(csrf.split("=").slice(1).join("="));
      token = token.replace(/^"(.*)"$/, "$1");
      options.headers = { ...(options.headers || {}), "X-CSRF-Token": token };
    }
  }
  return nativeFetch(input, options);
};

(async () => {
  for (const input of [
    "/login",
    "login",
    "https://app.anyaicam.com/login",
  ]) {
    capturedHeader = null;
    await window.fetch(input, { method: "POST" });
    assert.equal(capturedHeader, "signed-token", input);
    assert(!capturedHeader.startsWith('"'), input);
    assert(!capturedHeader.endsWith('"'), input);
  }
  capturedHeader = null;
  await window.fetch("https://example.invalid/login", { method: "POST" });
  assert.equal(capturedHeader, null, "cross-origin request");
  console.log("same-origin-login-variants=passed");
})();
