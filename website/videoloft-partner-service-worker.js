self.addEventListener('install',event=>{self.skipWaiting();});
self.addEventListener('activate',event=>{event.waitUntil(self.clients.claim());});
// Intentionally no offline cache: always use the current server response so partner data and portal code do not become stale.
self.addEventListener('fetch',()=>{});
