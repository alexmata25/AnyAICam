// Own notification display in both foreground and background. Firebase's default
// foreground forwarding assumes every visible portal tab has an onMessage handler;
// Live/Playback tabs do not. Handle the Web Push event before the SDK to avoid
// both silent foreground alerts and duplicate background notifications.
self.addEventListener('push', event => {
  event.stopImmediatePropagation();
  let payload;
  try { payload = event.data.json(); } catch (_) { return; }
  const id = payload.data?.notification_id;
  if (!id) return;
  const type = payload.data?.event_type;
  const title = type === 'intrusion_alarm' ? 'INTRUSION ALARM' : type === 'aac_voice_call' ? 'Visitor Call' : 'AnyAiCam activity';
  event.waitUntil(self.registration.showNotification(title, {
    body: 'Open AnyAiCam to view this alert.', tag: id, data: {notification_id: id},
    icon: '/static/brand-icon.png'
  }));
});
// Register click handling before Firebase, so only our authorized destination is used.
self.addEventListener('notificationclick', event => {
  event.stopImmediatePropagation();
  event.notification.close();
  const data = event.notification.data || {};
  const id = data.notification_id || data.FCM_MSG?.data?.notification_id;
  event.waitUntil((async () => {
    let path = '/dashboard';
    if (id) {
      try {
        const response = await fetch(`/api/mobile/push/notifications/${encodeURIComponent(id)}`, {credentials: 'same-origin'});
        if (response.ok) path = (await response.json()).path;
        else if (response.status === 401 || response.status === 403) path = '/customer-login.html';
      } catch (_) { path = '/dashboard'; }
    }
    const destination = new URL(path, self.location.origin);
    if (destination.origin !== self.location.origin) return;
    const windows = await clients.matchAll({type: 'window', includeUncontrolled: true});
    const existing = windows.find(w => new URL(w.url).origin === self.location.origin);
    // Portal tabs are not controlled by this /mobile-push/-scoped worker, and
    // browsers reject navigate() on an uncontrolled client -- which used to
    // make the tap do nothing whenever AnyAiCam was already open. Try it, and
    // open the destination in a window when it is refused.
    if (existing) {
      try {
        const navigated = await existing.navigate(destination.href);
        if (navigated) return navigated.focus();
      } catch (_) {}
    }
    return clients.openWindow(destination.href);
  })());
});
importScripts('https://www.gstatic.com/firebasejs/12.0.0/firebase-app-compat.js');
importScripts('https://www.gstatic.com/firebasejs/12.0.0/firebase-messaging-compat.js');
importScripts('/api/mobile/push/firebase-config.js');
firebase.initializeApp(self.ANYAICAM_FIREBASE_CONFIG);
firebase.messaging();
// Firebase still owns subscription-change handling. Display/click are handled above.
