const registerForm = document.querySelector("#partnerRegisterForm");
const loginForm = document.querySelector("#partnerLoginForm");
const leadForm = document.querySelector("#partnerLeadForm");
const registerStatus = document.querySelector("#registerStatus");
const loginStatus = document.querySelector("#loginStatus");
const leadStatus = document.querySelector("#leadStatus");
const activePartnerName = document.querySelector("#activePartnerName");
const activePartnerId = document.querySelector("#activePartnerId");
const leadTableBody = document.querySelector("#leadTableBody");
const mobileMenuButtons = document.querySelectorAll(".mobile-menu-toggle");

mobileMenuButtons.forEach((button) => {
  const header = button.closest(".site-header");
  const nav = header?.querySelector(".site-nav");

  button.addEventListener("click", () => {
    const isOpen = header?.classList.toggle("menu-open") || false;
    button.setAttribute("aria-expanded", String(isOpen));
  });

  nav?.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => {
      header?.classList.remove("menu-open");
      button.setAttribute("aria-expanded", "false");
    });
  });
});

const partnerKey = "anyaicamPartners";
const activePartnerKey = "anyaicamActivePartner";
const leadKey = "anyaicamPartnerLeads";

const partnerPrices = {
  "2MP": {
    "2 day recording": "$8.00",
    "7 day recording": "$8.25",
    "14 day recording": "$8.75",
    "30 day recording": "$9.00",
  },
  "4MP": {
    "2 day recording": "$9.00",
    "7 day recording": "$9.25",
    "14 day recording": "$9.75",
    "30 day recording": "$10.00",
  },
  "8MP": {
    "2 day recording": "$10.00",
    "7 day recording": "$10.25",
    "14 day recording": "$10.75",
    "30 day recording": "$11.00",
  },
};

const readJson = (key) => JSON.parse(localStorage.getItem(key) || "[]");
const writeJson = (key, value) => localStorage.setItem(key, JSON.stringify(value));
const makePartnerId = (email) => `P-${btoa(email).replace(/=+$/g, "").slice(0, 8).toUpperCase()}`;
const makeLeadId = () => {
  const date = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  const random = Math.random().toString(36).slice(2, 8).toUpperCase();
  return `AIC-LEAD-${date}-${random}`;
};

function getActivePartner() {
  return JSON.parse(localStorage.getItem(activePartnerKey) || "null");
}

function setActivePartner(partner) {
  localStorage.setItem(activePartnerKey, JSON.stringify(partner));
  renderActivePartner();
}

function renderActivePartner() {
  const partner = getActivePartner();

  if (!partner) {
    activePartnerName.textContent = "No partner logged in";
    activePartnerId.textContent = "Register or log in before submitting a customer lead.";
    return;
  }

  activePartnerName.textContent = partner.name;
  activePartnerId.textContent = `Partner ID: ${partner.id} | ${partner.email}`;
}

function renderLeads() {
  const leads = readJson(leadKey);

  if (!leads.length) {
    leadTableBody.innerHTML = '<tr><td colspan="5">No partner leads submitted yet.</td></tr>';
    return;
  }

  leadTableBody.innerHTML = leads
    .map(
      (lead) => `
        <tr>
          <td>${lead.customerName}</td>
          <td>${lead.partnerName}</td>
          <td>${lead.cameraCount}</td>
          <td>${lead.resolution}, ${lead.duration}, ${lead.price}/camera</td>
          <td>${lead.date}</td>
        </tr>
      `
    )
    .join("");
}

registerForm?.addEventListener("submit", (event) => {
  event.preventDefault();

  const data = new FormData(registerForm);
  const email = String(data.get("partnerEmail") || "").trim().toLowerCase();
  const partners = readJson(partnerKey);

  if (partners.some((partner) => partner.email === email)) {
    registerStatus.textContent = "This partner email is already registered. Please log in.";
    return;
  }

  const partner = {
    id: makePartnerId(email),
    name: String(data.get("partnerName") || "").trim(),
    email,
    password: String(data.get("partnerPassword") || ""),
  };

  partners.push(partner);
  writeJson(partnerKey, partners);
  setActivePartner(partner);

  registerStatus.textContent = "Partner registered and logged in.";
  registerForm.reset();
});

loginForm?.addEventListener("submit", (event) => {
  event.preventDefault();

  const data = new FormData(loginForm);
  const email = String(data.get("loginEmail") || "").trim().toLowerCase();
  const password = String(data.get("loginPassword") || "");
  const partner = readJson(partnerKey).find(
    (item) => item.email === email && item.password === password
  );

  if (!partner) {
    loginStatus.textContent = "Partner login not found. Check the email and password.";
    return;
  }

  setActivePartner(partner);
  loginStatus.textContent = "Partner logged in.";
  loginForm.reset();
});

leadForm?.addEventListener("submit", (event) => {
  event.preventDefault();

  const partner = getActivePartner();

  if (!partner) {
    leadStatus.textContent = "Please register or log in before submitting a customer lead.";
    return;
  }

  const data = new FormData(leadForm);
  const resolution = data.get("customerCameraResolution") || "";
  const duration = data.get("customerRecordingDuration") || "";
  const price = partnerPrices[resolution]?.[duration] || "Confirm during activation";
  const lead = {
    leadId: makeLeadId(),
    partnerId: partner.id,
    partnerName: partner.name,
    partnerEmail: partner.email,
    customerName: String(data.get("customerName") || ""),
    customerEmail: String(data.get("customerEmail") || ""),
    customerPhone: String(data.get("customerPhone") || ""),
    cameraCount: String(data.get("customerCameraCount") || ""),
    resolution,
    duration,
    price,
    brand: String(data.get("customerBrand") || ""),
    adapterNeeded: String(data.get("customerAdapterNeeded") || ""),
    notes: String(data.get("customerNotes") || ""),
    date: new Date().toLocaleDateString(),
  };

  const leads = readJson(leadKey);
  leads.push(lead);
  writeJson(leadKey, leads);
  renderLeads();

  const subject = "ANY AI CAM Partner Customer Lead";
  const body = [
    "New ANY AI CAM partner customer lead:",
    "Security note: Do not ask for camera passwords, payment card details, or private access codes by email.",
    "",
    `Lead ID: ${lead.leadId}`,
    `Partner Name: ${lead.partnerName}`,
    `Partner ID: ${lead.partnerId}`,
    `Partner Email: ${lead.partnerEmail}`,
    "",
    `Customer Name: ${lead.customerName}`,
    `Customer Email: ${lead.customerEmail}`,
    `Customer Phone: ${lead.customerPhone}`,
    `Number of Cameras: ${lead.cameraCount}`,
    `Camera Megapixel: ${lead.resolution}`,
    `Recording Duration: ${lead.duration}`,
    `Price Per Camera / Month: ${lead.price}`,
    `Camera/NVR Brand: ${lead.brand}`,
    `Adapter / Software Path: ${lead.adapterNeeded}`,
    `Notes: ${lead.notes}`,
    "",
    "Next step: Review lead, send exact pricing, and include the secure payment link.",
  ].join("\n");

  leadStatus.textContent = "Lead saved under this partner and email is ready to send.";
  window.location.href = `mailto:amata@anyaicam.com?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  leadForm.reset();
});

renderActivePartner();
renderLeads();

