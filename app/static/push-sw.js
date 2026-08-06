self.addEventListener('push', event => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (error) {
    data = { title: 'AnyAiCam Alert', body: event.data ? event.data.text() : 'New event' };
  }

  const title = data.title || 'AnyAiCam Alert';
  const options = {
    body: data.body || 'New camera event',
    icon: '/static/brand-icon.png',
    badge: '/static/brand-icon.png',
    data: { url: data.url || '/alerts' },
    tag: `${data.event_type || 'alert'}-${data.camera || 'system'}`,
    renotify: true,
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = event.notification.data?.url || '/alerts';
  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(windowClients => {
      for (const client of windowClients) {
        if ('focus' in client) {
          client.navigate(url);
          return client.focus();
        }
      }
      return clients.openWindow(url);
    })
  );
});
