const LIKELY_COMPATIBLE_BRANDS = new Set([
  "hikvision",
  "dahua",
  "lts",
  "lorex",
  "axis",
  "reolink",
  "uniview",
  "tvt",
  "vivotek",
  "onvif",
]);

const form = document.querySelector("#compatibilityForm");
const typeInput = document.querySelector("#cameraType");
const brandInput = document.querySelector("#cameraBrand");
const modelInput = document.querySelector("#cameraModel");
const onvifInput = document.querySelector("#onvifStatus");
const countInput = document.querySelector("#cameraCount");
const message = document.querySelector("#compatibilityMessage");
const messageText = document.querySelector("#compatibilityMessageText");
const results = document.querySelector("#compatibilityResults");
const resultsBody = document.querySelector("#compatibilityResultsBody");
const orderButton = document.querySelector("#continueOrderButton");
const reviewButton = document.querySelector("#manualReviewButton");
const chatButtons = document.querySelectorAll("#openChatButton, #openChatHelpButton, #manualReviewButton");
let waitingForChatWidget = false;

function normalizeBrand(brand) {
  return (brand || "").trim().toLowerCase();
}

function getSystemTypeLabel(type) {
  if (type === "ip") return "IP / network cameras";
  if (type === "nvr") return "NVR with IP cameras";
  if (type === "analog") return "Analog / coax DVR cameras";
  return "Not sure";
}

function getStatus(brand, onvif, type) {
  const normalizedBrand = normalizeBrand(brand);

  if (type === "analog") return "Likely Compatible";

  if (type === "unknown" || !type) {
    return "Needs Review";
  }

  if (LIKELY_COMPATIBLE_BRANDS.has(normalizedBrand)) {
    return "Likely Compatible";
  }

  if (onvif === "yes") {
    return "Needs Review";
  }

  if (normalizedBrand === "unknown" || normalizedBrand === "other" || !normalizedBrand) {
    return "Needs Review";
  }

  return "Not Supported";
}

function getMessage(brand, status, onvif, type) {
  if (type === "analog") {
    return "Your analog/coax DVR system may connect to Videoloft cloud service. We will confirm the recorder brand and model and determine the correct activation path.";
  }

  if (type === "unknown" || !type) {
    return "We can help identify whether your system is analog, IP, or hybrid and confirm the best connection method before you order.";
  }

  if (status === "Likely Compatible") {
    return `Great news - your ${brand} camera or recorder system is likely compatible with Videoloft cloud service.`;
  }

  if (onvif === "yes") {
    return "Your device appears to support ONVIF. We may need to review the exact model before you order.";
  }

  if (status === "Needs Review") {
    return "We can help confirm your camera brand and model in chat before you order.";
  }

  return "We could not confirm compatibility from this brand. You can request a manual review.";
}

function makeCell(text) {
  const cell = document.createElement("td");
  cell.textContent = text;
  return cell;
}

function renderResult({ brand, model, onvif, count, status, type }) {
  const row = document.createElement("tr");
  const statusCell = document.createElement("td");
  const badge = document.createElement("span");

  badge.className = `camera-result-badge ${status.toLowerCase().replaceAll(" ", "-")}`;
  badge.textContent = status;
  statusCell.append(badge);

  row.append(
    makeCell(getSystemTypeLabel(type)),
    makeCell(brand),
    makeCell(model || "Unknown"),
    makeCell(onvif === "yes" ? "Yes" : onvif === "no" ? "No" : "Unknown"),
    makeCell(count || "1"),
    statusCell,
  );

  resultsBody.replaceChildren(row);
  results.hidden = false;
}

function openAvailableChat() {
  if (window.OpenWidget && typeof window.OpenWidget.call === "function") {
    window.OpenWidget.call("maximize");
    return true;
  }

  return false;
}

function openChat() {
  if (openAvailableChat()) {
    waitingForChatWidget = false;
    return;
  }

  waitingForChatWidget = true;

  window.setTimeout(() => {
    if (waitingForChatWidget && !openAvailableChat()) {
      window.location.href = "/support.html";
    }
    waitingForChatWidget = false;
  }, 1800);
}

function handleSubmit(event) {
  event.preventDefault();

  const brand = brandInput.value || "Unknown";
  const type = typeInput.value || "unknown";
  const model = modelInput.value.trim();
  const onvif = onvifInput.value;
  const count = countInput.value || "1";
  const status = getStatus(brand, onvif, type);

  renderResult({ brand, model, onvif, count, status, type });
  messageText.textContent = getMessage(brand, status, onvif, type);
  message.hidden = false;

  const likelyCompatible = status === "Likely Compatible";
  orderButton.hidden = !likelyCompatible;
  reviewButton.hidden = likelyCompatible;
}

form?.addEventListener("submit", handleSubmit);
chatButtons.forEach((button) => button.addEventListener("click", openChat));
document.addEventListener("openwidget-ready", () => {
  if (waitingForChatWidget) {
    openChat();
  }
});

