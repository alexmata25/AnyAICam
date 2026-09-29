(function () {
  'use strict';

  const CART_KEY = 'anyaicamHardwareCartV3';
  const LEGACY_CART_KEY = 'anyaicamUnifiedCartV1';

  const PRODUCTS = [
    {
      sku: 'AIC-CAM-5MP-DOME-001',
      kind: 'camera',
      name: 'VIVOTEK FD9380-HTV-V2 5MP Outdoor Dome AI Camera',
      model: 'VIVOTEK FD9380-HTV-V2',
      price: 349.99,
      image: 'vivotek-fd9380-hv-v2.png'
    },
    {
      sku: 'AIC-CAM-BULLET-IB9380',
      kind: 'camera',
      name: 'VIVOTEK IB9380-HTV-V2 5MP Outdoor Bullet AI Camera',
      model: 'VIVOTEK IB9380-HTV-V2',
      price: 349.99,
      image: 'vivotek-ib9380-htv-v2.png'
    },
    {
      sku: 'AIC-CAM-FISHEYE-FE9380',
      kind: 'camera',
      name: 'VIVOTEK FE9380-HV 5MP Fisheye Panoramic Camera',
      model: 'VIVOTEK FE9380-HV',
      price: 649.99,
      image: 'vivotek-fe9380-hv.png'
    },
    {
      sku: 'AIC-CAM-DUAL-MA9312',
      kind: 'camera',
      name: 'VIVOTEK MA9312-EHTV Dual-Directional 4K AI Camera',
      model: 'VIVOTEK MA9312-EHTV',
      price: 1799.99,
      image: 'vivotek-ma9312-ehtv.png'
    },
    {
      sku: 'AIC-POE-MOKER-8',
      kind: 'switch',
      name: 'MokerLink POE-F082G 8-Port PoE Switch',
      model: 'POE-F082G',
      price: 79.99,
      image: 'mokerlink-8-port-poe.jpg'
    },
    {
      sku: 'AIC-POE-MOKER-16',
      kind: 'switch',
      name: 'MokerLink POE-G162G 16-Port Gigabit PoE+ Switch',
      model: 'POE-G162G',
      price: 174.99,
      image: 'mokerlink-16-port-poe.jpg'
    },
    {
      sku: 'AIC-POE-MOKER-24',
      kind: 'switch',
      name: 'MokerLink POE-G244GS 24-Port Gigabit PoE+ Switch',
      model: 'POE-G244GS',
      price: 229.99,
      image: 'mokerlink-24-port-poe.jpg'
    },
    {
      sku: 'AIC-POE-MOKER-48',
      kind: 'switch',
      name: 'MokerLink POE-G482GS 48-Port Gigabit PoE Switch',
      model: 'POE-G482GS',
      price: 429.99,
      image: 'mokerlink-48-port-poe.jpg'
    }
  ];

  const BY_SKU = Object.fromEntries(PRODUCTS.map(product => [product.sku, product]));

  function clampQuantity(value) {
    const quantity = Number.parseInt(value, 10);
    if (!Number.isFinite(quantity)) return 0;
    return Math.max(0, Math.min(64, quantity));
  }

  function money(value) {
    return Number(value || 0).toLocaleString('en-US', {
      style: 'currency',
      currency: 'USD'
    });
  }

  function loadCart() {
    let cart = {};
    try {
      cart = JSON.parse(localStorage.getItem(CART_KEY) || '{}') || {};
    } catch (error) {
      cart = {};
    }

    const sourceItems = Array.isArray(cart.hardwareItems)
      ? cart.hardwareItems
      : Object.entries(cart).map(([sku, quantity]) => ({ sku, quantity }));
    const quantities = new Map();

    sourceItems.forEach(item => {
      if (!item || !BY_SKU[item.sku]) return;
      const quantity = clampQuantity(item.quantity);
      if (quantity > 0) quantities.set(item.sku, quantity);
    });

    // Migrate the obsolete single-camera cart once, then stop using it.
    if (cart.camera && clampQuantity(cart.camera.quantity) > 0 && !quantities.has('AIC-CAM-5MP-DOME-001')) {
      quantities.set('AIC-CAM-5MP-DOME-001', clampQuantity(cart.camera.quantity));
    }

    cart.hardwareItems = PRODUCTS
      .filter(product => quantities.has(product.sku))
      .map(product => ({
        sku: product.sku,
        kind: product.kind,
        name: product.name,
        quantity: quantities.get(product.sku),
        unitPrice: product.price,
        price: product.price,
        subtotal: Number((quantities.get(product.sku) * product.price).toFixed(2))
      }));

    // Delete the legacy object that forced every item to $199.
    delete cart.camera;

    saveCart(cart);
    return cart;
  }

  function saveCart(cart) {
    const items = Array.isArray(cart.hardwareItems) ? cart.hardwareItems : [];
    const quantities = Object.fromEntries(items.map(item => [item.sku, item.quantity]));
    localStorage.setItem(CART_KEY, JSON.stringify(quantities));
    localStorage.setItem(LEGACY_CART_KEY, JSON.stringify({ hardwareItems: items }));
  }

  function setQuantity(sku, quantity) {
    const product = BY_SKU[sku];
    if (!product) return;

    const cart = loadCart();
    const quantities = new Map(
      (cart.hardwareItems || []).map(item => [item.sku, clampQuantity(item.quantity)])
    );
    const nextQuantity = clampQuantity(quantity);

    if (nextQuantity > 0) quantities.set(sku, nextQuantity);
    else quantities.delete(sku);

    cart.hardwareItems = PRODUCTS
      .filter(item => quantities.has(item.sku))
      .map(item => ({
        sku: item.sku,
        kind: item.kind,
        name: item.name,
        quantity: quantities.get(item.sku),
        unitPrice: item.price,
        price: item.price,
        subtotal: Number((quantities.get(item.sku) * item.price).toFixed(2))
      }));

    saveCart(cart);
    renderAll();
    window.dispatchEvent(new CustomEvent('anyaicam:hardware-cart-updated', {
      detail: { hardwareItems: cart.hardwareItems }
    }));
  }

  function getQuantity(sku) {
    const cart = loadCart();
    const item = (cart.hardwareItems || []).find(row => row.sku === sku);
    return item ? clampQuantity(item.quantity) : 0;
  }

  function cardTemplate(product) {
    const quantity = getQuantity(product.sku);
    return `
      <article class="aic-catalog-card" data-sku="${product.sku}">
        <img src="${product.image}" alt="${product.name}" loading="lazy">
        <div class="aic-catalog-card-body">
          <p class="aic-product-kind">${product.kind === 'camera' ? 'Camera' : 'PoE switch'}</p>
          <h3>${product.name}</h3>
          <p class="aic-product-model">${product.model}</p>
          <p class="aic-product-price">${money(product.price)} each</p>
          <div class="aic-product-quantity">
            <button type="button" data-aic-minus="${product.sku}" aria-label="Decrease quantity">−</button>
            <input
              type="number"
              min="0"
              max="64"
              step="1"
              value="${quantity}"
              data-aic-quantity="${product.sku}"
              aria-label="${product.name} quantity">
            <button type="button" data-aic-plus="${product.sku}" aria-label="Increase quantity">+</button>
          </div>
          <p class="aic-product-line-total" data-aic-line-total="${product.sku}">
            ${quantity > 0 ? `${quantity} × ${money(product.price)} = ${money(quantity * product.price)}` : 'Not selected'}
          </p>
        </div>
      </article>`;
  }

  function renderCatalogs() {
    document.querySelectorAll('[data-aic-catalog]').forEach(container => {
      const kind = container.getAttribute('data-aic-catalog');
      container.innerHTML = PRODUCTS
        .filter(product => product.kind === kind)
        .map(cardTemplate)
        .join('');
    });
  }

  function renderSummary() {
    const cart = loadCart();
    const items = cart.hardwareItems || [];
    const total = items.reduce((sum, item) => sum + (clampQuantity(item.quantity) * Number(item.unitPrice || item.price || 0)), 0);

    document.querySelectorAll('[data-aic-hardware-summary]').forEach(container => {
      if (!items.length) {
        container.innerHTML = '<p>No camera or PoE hardware selected.</p><p><strong>Hardware total: $0.00</strong></p>';
        return;
      }

      const rows = items.map(item => `
        <div class="aic-cart-row">
          <span>${item.quantity} × ${item.name}</span>
          <strong>${money(item.quantity * item.unitPrice)}</strong>
        </div>`).join('');

      container.innerHTML = `
        ${rows}
        <div class="aic-cart-row aic-cart-total">
          <span>Hardware total</span>
          <strong>${money(total)}</strong>
        </div>`;
    });

    document.querySelectorAll('[data-aic-hardware-total], .aic-live-camera-total').forEach(element => {
      element.textContent = money(total);
    });
  }

  function bindEvents() {
    document.addEventListener('click', event => {
      const minus = event.target.closest('[data-aic-minus]');
      const plus = event.target.closest('[data-aic-plus]');

      if (minus) {
        const sku = minus.getAttribute('data-aic-minus');
        setQuantity(sku, getQuantity(sku) - 1);
      }

      if (plus) {
        const sku = plus.getAttribute('data-aic-plus');
        setQuantity(sku, getQuantity(sku) + 1);
      }
    });

    document.addEventListener('change', event => {
      const input = event.target.closest('[data-aic-quantity]');
      if (!input) return;
      setQuantity(input.getAttribute('data-aic-quantity'), input.value);
    });

    window.addEventListener('storage', renderAll);
  }

  function renderAll() {
    renderCatalogs();
    renderSummary();
  }

  document.addEventListener('DOMContentLoaded', () => {
    loadCart();
    bindEvents();
    renderAll();
  });

  window.AnyAICamHardwareCart = {
    products: PRODUCTS.slice(),
    load: loadCart,
    setQuantity,
    getItems: () => loadCart().hardwareItems || [],
    getTotal: () => (loadCart().hardwareItems || []).reduce(
      (sum, item) => sum + item.quantity * item.unitPrice,
      0
    )
  };
})();
