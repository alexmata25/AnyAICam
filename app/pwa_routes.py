
from typing import Callable

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, Response


def register_pwa_routes(app, *, page_shell: Callable, current_user: Callable):
    @app.get("/manifest.webmanifest")
    def manifest():
        return JSONResponse(
            {
                "id": "/customer-portal",
                "name": "ANY AI CAM",
                "short_name": "ANY AI CAM",
                "description": "Secure customer cameras, events, playback, analytics, and alerts.",
                "start_url": "/customer-portal",
                "scope": "/",
                "display": "standalone",
                "background_color": "#071032",
                "theme_color": "#071032",
                "orientation": "any",
                "icons": [
                    {"src": "/static/brand-icon.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                    {"src": "/static/brand-icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                ],
                "shortcuts": [
                    {"name": "Live cameras", "url": "/", "icons": [{"src": "/static/brand-icon.png", "sizes": "192x192"}]},
                    {"name": "Alerts", "url": "/alerts", "icons": [{"src": "/static/brand-icon.png", "sizes": "192x192"}]},
                    {"name": "Playback", "url": "/playback", "icons": [{"src": "/static/brand-icon.png", "sizes": "192x192"}]},
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/service-worker.js")
    def service_worker():
        source = r'''
const CACHE='anyaicam-mobile-v1';
const CORE=['/offline','/static/brand-icon.png','/manifest.webmanifest'];
self.addEventListener('install',event=>{event.waitUntil(caches.open(CACHE).then(cache=>cache.addAll(CORE)));self.skipWaiting();});
self.addEventListener('activate',event=>{event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',event=>{
  if(event.request.method!=='GET')return;
  const url=new URL(event.request.url);
  if(url.origin!==location.origin)return;
  if(event.request.mode==='navigate'){event.respondWith(fetch(event.request).catch(()=>caches.match('/offline')));return;}
  event.respondWith(caches.match(event.request).then(hit=>hit||fetch(event.request).then(response=>{
    if(response.ok&&url.pathname.startsWith('/static/')){const copy=response.clone();caches.open(CACHE).then(cache=>cache.put(event.request,copy));}
    return response;
  })));
});
self.addEventListener('push',event=>{
  let data={title:'ANY AI CAM Alert',body:'A camera event needs your attention.',url:'/alerts'};
  try{data={...data,...event.data.json()}}catch{}
  event.waitUntil(self.registration.showNotification(data.title,{body:data.body,icon:'/static/brand-icon.png',badge:'/static/brand-icon.png',data:{url:data.url||'/alerts'}}));
});
self.addEventListener('notificationclick',event=>{
  event.notification.close();
  event.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{
    const target=event.notification.data?.url||'/alerts';
    for(const client of list){if('focus' in client){client.navigate(target);return client.focus();}}
    if(clients.openWindow)return clients.openWindow(target);
  }));
});
'''
        return Response(source, media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

    @app.get("/offline", response_class=HTMLResponse)
    def offline_page():
        return HTMLResponse(
            '''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#071032"><title>Offline · ANY AI CAM</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#071032;color:white;font-family:Arial;padding:24px}.card{max-width:520px;text-align:center;padding:32px;border-radius:20px;background:#111a3b}.card img{width:100px}.card p{color:#d8e2ff;line-height:1.6}.card button{padding:12px 18px;border:0;border-radius:999px;font-weight:800}</style></head><body><div class="card"><img src="/static/brand-icon.png"><h1>Internet connection unavailable</h1><p>The local VMS can continue operating, but public camera access, cloud uploads, and push delivery require the Samsung hub to reconnect to the internet.</p><button onclick="location.reload()">Try again</button></div></body></html>'''
        )

    @app.get("/mobile-app", response_class=HTMLResponse)
    def mobile_app_page(request: Request):
        current_user(request)
        content = '''
        <header class="topbar">
          <div><p class="eyebrow">Phase 6D</p><h1>Install ANY AI CAM on your phone</h1></div>
          <span class="pill">Android + iPhone</span>
        </header>
        <section class="panel">
          <div class="panel-head"><div><h2>Installable mobile web app</h2><div class="health-detail">The app opens directly to your secure portal and uses the same approved account.</div></div></div>
          <div class="feature-grid">
            <article class="feature-card"><div class="feature-icon">A</div><h2>Android</h2><p>Open app.anyaicam.com in Chrome, then choose Install app or Add to Home screen.</p><button class="action-button" id="install-app" type="button">Install on this device</button></article>
            <article class="feature-card"><div class="feature-icon"></div><h2>iPhone and iPad</h2><p>Open in Safari, tap Share, choose Add to Home Screen, and enable Open as Web App.</p><button class="compact-button" id="ios-help" type="button">Show iPhone instructions</button></article>
            <article class="feature-card"><div class="feature-icon">✦</div><h2>Push alerts</h2><p>After installation, open Camera apps & alerts and allow notifications.</p><a class="download" href="/customer-app-settings">Program alerts</a></article>
          </div>
          <div id="install-message" class="launch-note" style="margin-top:16px">Your browser will show the install option when the public HTTPS portal and PWA requirements are ready.</div>
        </section>
        '''
        scripts = '''
        <script>
        let installPrompt=null;
        const button=document.getElementById('install-app');
        const message=document.getElementById('install-message');
        window.addEventListener('beforeinstallprompt',event=>{event.preventDefault();installPrompt=event;message.textContent='This device is ready to install ANY AI CAM.';});
        button.onclick=async()=>{
          if(installPrompt){
            installPrompt.prompt();
            const result=await installPrompt.userChoice;
            message.textContent=result.outcome==='accepted'?'ANY AI CAM installation started.':'Installation was dismissed.';
            installPrompt=null;
          }else{
            message.textContent='On Android, use the browser menu and choose Install app. On iPhone, use Safari Share → Add to Home Screen.';
          }
        };
        document.getElementById('ios-help').onclick=()=>showToast('Safari: Share button → Add to Home Screen → Add');
        window.addEventListener('appinstalled',()=>{message.textContent='ANY AI CAM is installed on this device.'});
        </script>
        '''
        return page_shell("Install mobile app", "mobile-app", content, scripts)
