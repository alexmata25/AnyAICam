// Shared customer VMS push. No Firebase credentials or customer data are bundled.
const status = document.getElementById('vms-push-status');
const enable = document.getElementById('vms-push-enable');
const devices = document.getElementById('vms-push-devices');
let firebaseModules;
async function sdk() {
  if (!firebaseModules) firebaseModules = Promise.all([
    import('https://www.gstatic.com/firebasejs/12.0.0/firebase-app.js'),
    import('https://www.gstatic.com/firebasejs/12.0.0/firebase-messaging.js')
  ]);
  return firebaseModules;
}
async function listDevices() {
  const response = await fetch('/api/mobile/push/devices');
  if (!response.ok) throw new Error('Sign in to manage your push devices.');
  const data = await response.json();
  devices.replaceChildren();
  for (const d of data.devices) {
    const row = document.createElement('p');
    const label = document.createElement('span');
    label.textContent = `${d.platform} · ${d.enabled ? 'Enrolled' : 'Needs reconnect (open this page on that device)'} · ${d.id.slice(0, 8)} `;
    const remove = document.createElement('button');
    remove.type = 'button'; remove.className = 'ghost-button'; remove.textContent = 'Disconnect';
    remove.addEventListener('click', async () => {
      try {
        const r = await fetch(`/api/mobile/push/devices/${encodeURIComponent(d.id)}`, {method: 'DELETE'});
        if (!r.ok) throw new Error('Could not disconnect this device.');
        // Server revocation is authoritative, even if this browser is offline later.
        await listDevices(); status.textContent = 'Device disconnected.';
      } catch (e) { status.textContent = e.message; }
    });
    row.append(label, remove); devices.append(row);
  }
  return data.devices;
}
async function enroll(config) {
  const [appSdk, messagingSdk] = await sdk();
  if (!await messagingSdk.isSupported()) throw new Error('Push is not supported in this browser. On iPhone or iPad, use a supported Home Screen web app.');
  const app = appSdk.getApps().find(a => a.name === 'anyaicam-push') || appSdk.initializeApp(config.firebase, 'anyaicam-push');
  const registration = await navigator.serviceWorker.register('/mobile-push-sw.js', {scope: '/mobile-push/'});
  if (!registration.active) await new Promise((resolve, reject) => {
    const worker = registration.installing || registration.waiting;
    const timeout = setTimeout(() => reject(new Error('Push setup timed out. Please try again.')), 15000);
    if (!worker) { clearTimeout(timeout); reject(new Error('Push worker unavailable.')); return; }
    const checkState = () => {
      if (worker.state === 'activated') { clearTimeout(timeout); resolve(); }
      if (worker.state === 'redundant') { clearTimeout(timeout); reject(new Error('Push worker could not start.')); }
    };
    worker.addEventListener('statechange', checkState);
    checkState(); // Activation can finish between the initial check and listener registration.
  });
  const messaging = messagingSdk.getMessaging(app);
  const token = await messagingSdk.getToken(messaging, {vapidKey: config.vapid_public_key, serviceWorkerRegistration: registration});
  if (!token) throw new Error('The browser did not provide a push registration.');
  let installation = localStorage.getItem('anyaicam-push-installation');
  if (!installation) { installation = crypto.randomUUID(); localStorage.setItem('anyaicam-push-installation', installation); }
  const response = await fetch('/api/mobile/push/devices', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({installation_id: installation, platform: 'web', token})});
  if (!response.ok) throw new Error(response.status === 409 ? 'Disconnect this browser from its previous account before enabling it here.' : 'Could not save push enrollment.');
  status.textContent = 'Browser enrolled. Delivery still depends on server configuration and browser permissions.';
  await listDevices();
}
if (enable) {
  enable.addEventListener('click', async () => {
    enable.disabled = true;
    try {
      if (!('Notification' in window) || !('serviceWorker' in navigator)) throw new Error('This browser does not support push.');
      // Permission prompt stays within the customer's explicit click gesture.
      if (await Notification.requestPermission() !== 'granted') throw new Error('Notification permission was not granted.');
      const response = await fetch('/api/mobile/push/config');
      const config = await response.json();
      if (!config.available) throw new Error('Push has not been configured for this deployment yet.');
      await enroll(config);
    } catch (e) { status.textContent = e.message; }
    finally { enable.disabled = false; }
  });
  (async () => {
    try {
      const enrolled = await listDevices();
      const config = await (await fetch('/api/mobile/push/config')).json();
      const installation = localStorage.getItem('anyaicam-push-installation');
      // Refresh this browser's token whenever it is still enrolled, including after
      // FCM reported the old token unregistered (the server then pauses the device;
      // 2026-09-30: a phone stayed paused until Enable was tapped again). Disconnect
      // deletes the enrollment, so a disconnected browser is never re-enabled here.
      if (config.available && 'Notification' in window && Notification.permission === 'granted' && enrolled.some(d => d.installation_id === installation)) {
        await enroll(config);
        return;
      }
      enable.disabled = !config.available;
      status.textContent = config.available ? 'Ready to enroll this browser; delivery is not yet verified.' : 'Push has not been configured for this deployment yet.';
    } catch (e) { status.textContent = e.message; }
  })();
}
