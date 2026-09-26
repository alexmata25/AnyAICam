/* AnyAiCam inline media player (2026-09-25).
 * One shared "select a result -> it expands and plays right there" player,
 * used by Playback's recording lists and every Analytics workspace. It
 * inserts a Live-style card (video + integrated controls strip) right after
 * the selected row/card, one at a time; lists that re-render call
 * reattach(list) so a playing clip is never interrupted; it never scrolls
 * the page to the top. Only controls that work are shown; no talk-down on
 * recorded media. Requires /static/event_media.js for event clips. */
(function(root){
function create(opts){
  opts=opts||{};
  let current=null;
  function shareUrl(cameraId,item){
    if(item.shareUrl)return item.shareUrl;
    const cam=encodeURIComponent(cameraId);
    if(item.kind==='event')return `${location.origin}/playback?camera=${cam}&event=${encodeURIComponent(item.id)}&autoplay=event`;
    const ms=(opts.dateOf?opts.dateOf(item.start):new Date(item.start)).getTime();
    return `${location.origin}/playback?camera=${cam}${Number.isFinite(ms)?`&t=${ms}`:''}&autoplay=event`;
  }
  function close(){
    if(!current)return;
    const closing=current;current=null;
    try{if(closing.player)closing.player.cancel();}catch(error){}
    closing.video.pause();closing.video.removeAttribute('src');closing.video.load();
    closing.card.remove();
    if(closing.row)closing.row.setAttribute('aria-expanded','false');
    if(opts.onClose)opts.onClose(closing.row);
  }
  function open(row,cameraId,item){
    const key=`${item.kind}:${item.id}`;
    if(current&&current.key===key){close();return;}
    close();
    if(opts.topVideo&&!opts.topVideo.paused)opts.topVideo.pause();
    const card=document.createElement('div');
    card.className='inline-media-card';
    card.dataset.inlineFor=key;
    card.setAttribute('role','region');
    card.setAttribute('aria-label',`Playing ${item.title||'recording'}`);
    const snapshot=item.kind==='snapshot';
    const esc=value=>String(value==null?'':value).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch]);
    card.innerHTML=`<div class="camera-view inline-media-view">${snapshot?`<img alt="" src="${esc(item.thumbnail||'')}">`:''}<video playsinline preload="auto"${snapshot?' hidden':''}></video></div>
      <p class="inline-media-status health-detail" role="status" aria-live="polite">Loading…</p>
      <div class="camera-tools inline-media-tools" role="toolbar" aria-label="Recording controls">
        <button class="camera-tool" type="button" data-act="play" title="Pause" aria-label="Pause">⏸</button>
        <button class="camera-tool" type="button" data-act="mute" title="Mute" aria-label="Mute">♪</button>
        <button class="camera-tool" type="button" data-act="fullscreen" title="Fullscreen" aria-label="Fullscreen">⛶</button>
        <a class="camera-tool" data-act="download" title="Download" aria-label="Download" target="_blank" rel="noopener" download hidden>⬇</a>
        <button class="camera-tool" type="button" data-act="share" title="Share link" aria-label="Share link">↗</button>
        ${item.playbackHref?`<a class="camera-tool" data-act="playback" href="${esc(item.playbackHref)}" title="Open in Playback" aria-label="Open in Playback">◴</a>`:''}
        <button class="camera-tool" type="button" data-act="close" title="Close player" aria-label="Close player">✕</button>
      </div>`;
    row.after(card);
    row.setAttribute('aria-expanded','true');
    const clipVideo=card.querySelector('video');
    const statusEl=card.querySelector('.inline-media-status');
    const download=card.querySelector('[data-act="download"]');
    const playButton=card.querySelector('[data-act="play"]');
    const muteButton=card.querySelector('[data-act="mute"]');
    const view=card.querySelector('.inline-media-view');
    current={key,card,video:clipVideo,row,list:row.parentElement,player:null};
    const syncPlay=()=>{const label=clipVideo.paused?'Play':'Pause';playButton.textContent=clipVideo.paused?'▶':'⏸';playButton.title=label;playButton.setAttribute('aria-label',label)};
    const syncMute=()=>{const label=clipVideo.muted?'Unmute':'Mute';muteButton.textContent=clipVideo.muted?'🔇':'♪';muteButton.title=label;muteButton.setAttribute('aria-label',label)};
    clipVideo.addEventListener('play',()=>{syncPlay();statusEl.textContent=''});
    clipVideo.addEventListener('pause',syncPlay);
    clipVideo.addEventListener('volumechange',syncMute);
    clipVideo.addEventListener('error',()=>{if(clipVideo.getAttribute('src'))statusEl.textContent='This recording could not be played.'});
    const start=()=>Promise.resolve(clipVideo.play()).catch(()=>{clipVideo.muted=true;return clipVideo.play()}).catch(()=>{statusEl.textContent='Press play to start.'});
    if(snapshot){
      ['play','mute','download'].forEach(act=>{card.querySelector(`[data-act="${act}"]`).hidden=true});
      statusEl.textContent=item.note||'No video clip was recorded for this detection.';
    }else if(item.kind==='event'){
      const mine=current;
      current.player=AnyAiCamEventMedia.player({video:clipVideo,status:statusEl,isCurrent:()=>current===mine,onReady:()=>{
        const src=clipVideo.getAttribute('src')||clipVideo.currentSrc;
        if(src){download.href=src;download.hidden=false;}
      }});
      current.player.start(cameraId,item.id,true);
    }else{
      const url=opts.mediaUrl(cameraId,item.id);
      clipVideo.src=url;download.href=url;download.hidden=false;
      start();
    }
    if(!view.requestFullscreen&&!(clipVideo.webkitEnterFullscreen&&!snapshot))card.querySelector('[data-act="fullscreen"]').hidden=true;
    card.addEventListener('click',event=>{
      const control=event.target.closest('[data-act]');
      if(!control)return;
      const act=control.dataset.act;
      if(act==='play'){if(clipVideo.paused)start();else clipVideo.pause();}
      else if(act==='mute'){clipVideo.muted=!clipVideo.muted;}
      else if(act==='fullscreen'){
        if(document.fullscreenElement)document.exitFullscreen().catch(()=>{});
        else if(view.requestFullscreen)view.requestFullscreen().catch(()=>{});
        else if(clipVideo.webkitEnterFullscreen&&!snapshot)clipVideo.webkitEnterFullscreen();
      }
      else if(act==='share'){
        const url=shareUrl(cameraId,item);
        if(navigator.share){navigator.share({title:item.title||'AnyAiCam recording',url}).catch(()=>{});}
        else if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(url).then(()=>{statusEl.textContent='Link copied -- it opens this recording for people on your account.'}).catch(()=>{statusEl.textContent=url});}
        else{statusEl.textContent=url;}
      }
      else if(act==='close'){const openedFrom=current&&current.row;close();if(openedFrom&&openedFrom.focus)openedFrom.focus({preventScroll:true});}
    });
    if(!snapshot){syncPlay();syncMute();}
    if(opts.onOpen)opts.onOpen(row,item);
    card.scrollIntoView({block:'nearest',behavior:'smooth'});
  }
  function reattach(container){
    // Only the list it was opened from (the desktop and mobile lists
    // share keys, and the hidden one re-renders too).
    if(!current||container!==current.list)return;
    const row=container.querySelector(`[data-inline-key="${CSS.escape(current.key)}"]`);
    if(!row){if(!current.card.isConnected)close();return;}
    current.row=row;
    row.setAttribute('aria-expanded','true');
    if(row.nextElementSibling!==current.card)row.after(current.card);
  }
  // The top (timeline) player taking over pauses an inline recording.
  if(opts.topVideo)opts.topVideo.addEventListener('play',()=>{if(current&&!current.video.paused)current.video.pause()});
  return {open,close,reattach,isOpen:()=>Boolean(current)};
}

  function openFromKeyboard(event,action){if(event.key==='Enter'||event.key===' '){event.preventDefault();action()}}
  root.AnyAiCamInlineMedia={create,openFromKeyboard};
})(typeof window!=='undefined'?window:globalThis);
