const CACHE='anyaicam-shell-v1';
const SHELL=['/manifest.webmanifest','/pwa-offline','/customer-login.html','/static/brand-icon.png'];
self.addEventListener('install',event=>event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(SHELL)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(key=>key!==CACHE).map(key=>caches.delete(key)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
  const request=event.request,url=new URL(request.url);
  if(request.method!=='GET'||url.origin!==location.origin||url.pathname.startsWith('/api/'))return;
  if(request.mode==='navigate'){
    event.respondWith(fetch(request).catch(()=>caches.match('/pwa-offline'))); return;
  }
  event.respondWith(caches.match(request).then(cached=>cached||fetch(request).then(response=>{if(response.ok&&['style','script','image','manifest'].includes(request.destination)){const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(request,copy))}return response})));
});
self.addEventListener('push',event=>{const data=event.data?event.data.json():{title:'AnyAiCam',body:'New notification'};event.waitUntil(self.registration.showNotification(data.title||'AnyAiCam',{body:data.body||'',icon:'/static/brand-icon.png',data:{url:data.url||'/customer-portal?page=alerts'}}))});
self.addEventListener('notificationclick',event=>{event.notification.close();event.waitUntil(clients.openWindow(event.notification.data.url||'/customer-portal?page=alerts'))});
