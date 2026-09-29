// Success message display
(function() {
  const params = new URLSearchParams(window.location.search);
  const statusMsg = params.get("sent");
  const actStatus = document.querySelector("#activationForm #formStatus");
  const supStatus = document.querySelector("#supportForm #formStatus");
  if (statusMsg === "success") {
    const msg = "Thanks! We received your request and will follow up shortly.";
    if (actStatus) actStatus.textContent = msg;
    if (supStatus) supStatus.textContent = msg;
  } else if (statusMsg === "saved") {
    const msg = "Request saved! We will contact you soon.";
    if (actStatus) actStatus.textContent = msg;
    if (supStatus) supStatus.textContent = msg;
  } else if (statusMsg === "fail") {
    const msg = "Something went wrong. Please email us directly at amata@anyaicam.com";
    if (actStatus) actStatus.textContent = msg;
    if (supStatus) supStatus.textContent = msg;
  }
})();

const heroVideo = document.querySelector("#heroVideo");
const heroVideoCaption = document.querySelector("#heroVideoCaption");
const heroVideoLink = document.querySelector("#heroVideoLink");
const videoChoices = document.querySelectorAll(".video-choice");
const mobileMenuButtons = document.querySelectorAll(".mobile-menu-toggle");

// Mobile menu toggle
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

// Dropdown toggle for mobile/touch devices
document.querySelectorAll('.site-nav .dropbtn').forEach(function(btn) {
  btn.addEventListener('click', function(e) {
    if (window.innerWidth <= 640) {
      e.preventDefault();
      var content = this.nextElementSibling;
      if (content && content.classList.contains('dropdown-content')) {
        var isVisible = content.style.display === 'block';
        document.querySelectorAll('.site-nav .dropdown-content').forEach(function(c) {
          c.style.display = '';
        });
        content.style.display = isVisible ? '' : 'block';
      }
    }
  });
});

// Video player switching
videoChoices.forEach((choice) => {
  if (choice.dataset.videoSrc === "how-it-works.mp4") {
    choice.textContent = "License Plate Recognition";
    choice.dataset.videoLabel = "Watch a license plate recognition example.";
  }
});

// Universal customer prices include a $4-per-camera margin.
const planPrices = {
  "2 day recording": "$8.19",
  "7 day recording": "$8.59",
  "14 day recording": "$9.19",
  "30 day recording": "$10.09",
};
const annualPlanPrices = {
  "2 day recording": "$88.45",
  "7 day recording": "$92.77",
  "14 day recording": "$99.25",
  "30 day recording": "$108.97",
};

videoChoices.forEach((choice) => {
  choice.addEventListener("click", () => {
    const videoSrc = choice.dataset.videoSrc;
    const videoType = choice.dataset.videoType;
    const videoLabel = choice.dataset.videoLabel;
    const youtubeContainer = document.getElementById("heroYoutubeContainer");

    videoChoices.forEach((button) => button.classList.remove("active"));
    choice.classList.add("active");

    if (videoType === "youtube") {
      if (heroVideo) heroVideo.style.display = "none";
      if (youtubeContainer) {
        youtubeContainer.style.display = "block";
        const iframe = youtubeContainer.querySelector("iframe");
        if (iframe) iframe.src = iframe.src.replace("&autoplay=0", "&autoplay=1").replace("?enablejsapi=1", "?enablejsapi=1&autoplay=1");
      }
      if (heroVideoCaption && videoLabel) {
        heroVideoCaption.textContent = videoLabel;
      }
      if (heroVideoLink) {
        heroVideoLink.href = "https://youtu.be/Xa0Od-JFnYs";
      }
      return;
    }

    if (heroVideo) heroVideo.style.display = "";
    if (youtubeContainer) youtubeContainer.style.display = "none";
    if (!heroVideo || !videoSrc) {
      return;
    }
    heroVideo.pause();
    while (heroVideo.firstChild) {
      heroVideo.removeChild(heroVideo.firstChild);
    }
    const source = document.createElement("source");
    source.src = videoSrc;
    source.type = "video/mp4";
    heroVideo.appendChild(source);
    heroVideo.load();
    if (heroVideoCaption && videoLabel) {
      heroVideoCaption.textContent = videoLabel;
    }
    if (heroVideoLink) {
      heroVideoLink.href = videoSrc;
    }
  });
});

// Smart Motion video toggle
const smartMotionBtn = document.getElementById("smartMotionVideoBtn");
const smartMotionContainer = document.getElementById("smartMotionVideoContainer");
let smartMotionIframe = document.getElementById("smartMotionIframe");

smartMotionBtn?.addEventListener("click", () => {
  if (smartMotionContainer.style.display === "none" || !smartMotionContainer.style.display) {
    smartMotionContainer.style.display = "block";
    smartMotionBtn.textContent = "Close Smart Motion Video";
    if (smartMotionIframe) {
      smartMotionIframe.src = "https://www.youtube.com/embed/aTBdazIvs40?enablejsapi=1&autoplay=1";
    }
  } else {
    smartMotionContainer.style.display = "none";
    smartMotionBtn.textContent = "Watch Smart Motion Video";
    if (smartMotionIframe) {
      smartMotionIframe.src = "https://www.youtube.com/embed/aTBdazIvs40?enablejsapi=1";
    }
  }
});



/* Canonical LTS hardware catalog shared by public and partner experiences. */
window.AICHardwareCatalog = window.AICHardwareCatalog || {};
[
  {
    "sku": "AIC-CAM-LTS-LXIP1142",
    "kind": "camera",
    "name": "LTS LXIP1142W-28MA — Pro-X 4MP Starlight Turret IP Camera",
    "short": "4MP · 2.8 mm lens · built-in microphone · PoE · IP67",
    "price": 114.99
  },
  {
    "sku": "AIC-CAM-LTS-CMIP3382",
    "kind": "camera",
    "name": "LTS CMIP3382WI-28SDL — Platinum Plus 8MP Active-Deterrence Turret",
    "short": "8MP / 4K · active deterrence · smart detection · PoE",
    "price": 249.99
  },
  {
    "sku": "AIC-CAM-LTS-CMIP3C42",
    "kind": "camera",
    "name": "LTS CMIP3C42WI-28SDL — 4MP Color 24/7 Active-Deterrence Turret",
    "short": "4MP Color 24/7 · hybrid illumination · active deterrence · PoE",
    "price": 299.99
  },
  {
    "sku": "AIC-CAM-LTS-CMIP3C82",
    "kind": "camera",
    "name": "LTS CMIP3C82WI-28SDL — 8MP Color 24/7 Active-Deterrence Turret",
    "short": "8MP / 4K Color 24/7 · hybrid illumination · red/blue strobes · PoE",
    "price": 409.99
  },
  {
    "sku": "AIC-CAM-LTS-CMHT1722",
    "kind": "camera",
    "name": "LTS CMHT1722-28LS — 2MP HD-TVI Active-Deterrence Turret",
    "short": "2MP analog HD-TVI · dual light · audio over coax · DVR required",
    "price": 114.99
  },
  {
    "sku": "AIC-CAM-LTS-CMHT1752",
    "kind": "camera",
    "name": "LTS CMHT1752-28LS — 5MP HD-TVI Dual-Light Turret",
    "short": "5MP analog HD-TVI · dual light · 2.8 mm lens · DVR required",
    "price": 129.99
  },
  {
    "sku": "AIC-CAM-LTS-CMHT1782",
    "kind": "camera",
    "name": "LTS CMHT1782-28LF — Platinum Plus 8MP HD-TVI Turret",
    "short": "8MP analog HD-TVI · 2.8 mm lens · DVR required",
    "price": 149.99
  },
  {
    "sku": "AIC-POE-LTS-8",
    "kind": "poe",
    "name": "LTS VSPOE-SW802 — 8-Port PoE Switch with 2 Uplink Ports",
    "short": "8 powered PoE ports · 2 uplink ports",
    "price": 159.99
  },
  {
    "sku": "AIC-POE-LTS-16",
    "kind": "poe",
    "name": "LTS VSPOE-SW1602 — 16-Port PoE Switch with 2 Combo Ports",
    "short": "16 powered PoE ports · 2 combo uplink ports",
    "price": 329.99
  },
  {
    "sku": "AIC-POE-LTS-24",
    "kind": "poe",
    "name": "LTS VSPOE-SW2402 — Pro-VS 24-Port PoE Switch with 2 Combo Ports",
    "short": "24 powered PoE ports · 2 combo uplink ports",
    "price": 429.99
  }
].forEach(function (product) { window.AICHardwareCatalog[product.sku] = product; });
