const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "..", "app", "main.py"), "utf8");
assert(source.includes("token=token.replace(/^\"(.*)\"$/,'$1')"));
assert(source.includes("'X-CSRF-Token':token"));

const document = { cookie: 'anyaicam_csrf="signed-token"' };
let capturedHeader = null;
const nativeFetch = (_input, options) => {
  capturedHeader = options.headers["X-CSRF-Token"];
  return Promise.resolve({ ok: true });
};
const window = { fetch: nativeFetch };

window.fetch = (input, options = {}) => {
  const method = (options.method || "GET").toUpperCase();
  const sameOrigin = typeof input === "string"
    ? !input.startsWith("http://") && !input.startsWith("https://")
    : input.url.startsWith(location.origin);
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

window.fetch("/login", { method: "POST" }).then(() => {
  assert.equal(capturedHeader, "signed-token");
  assert(!capturedHeader.startsWith('"'));
  assert(!capturedHeader.endsWith('"'));
  console.log(`browser-header=${capturedHeader}`);
});
