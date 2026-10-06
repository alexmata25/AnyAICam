/* AnyAiCam Analytics workspace (2026-09-25): one dedicated page per
 * analytic (config from window.__AW). Results are visual cards; selecting
 * one expands it in place and plays its clip right there (inline_media.js),
 * with "Open in Playback" as an optional extra. Data:
 * /api/customer/analytics/<key>/events (tenant-scoped, entitlement-aware). */
(function(){
  const {config,cameras}=window.__AW;
  const key=config.key;
  const $=id=>document.getElementById(id);
  const results=$('aw-results'),stats=$('aw-stats'),extra=$('aw-extra'),more=$('aw-more');
  const cameraName=Object.fromEntries(cameras.map(c=>[c.id,c.name]));
  const state={camera:config.selected||'',site:'',range:'7',from:'',to:'',result:'',q:'',before:null,loading:false,items:new Map()};
  const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'})[ch]);
  const LABELS={motion:'Motion',smart_motion:'Motion',person:'Person',vehicle:'Vehicle',car:'Car',truck:'Truck',bus:'Bus',motorcycle:'Motorcycle',bicycle:'Bicycle'};
  const inline=AnyAiCamInlineMedia.create({dateOf:v=>new Date(v)});

  function when(ms){
    if(typeof ms!=='number')return '';
    const d=new Date(ms),now=new Date(),y=new Date(now.getFullYear(),now.getMonth(),now.getDate()-1);
    const t=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit'});
    if(d.toDateString()===now.toDateString())return `Today, ${t}`;
    if(d.toDateString()===y.toDateString())return `Yesterday, ${t}`;
    return d.toLocaleDateString([],{weekday:'short',month:'short',day:'numeric'})+`, ${t}`;
  }
  const localDay=o=>{const n=new Date();return new Date(n.getFullYear(),n.getMonth(),n.getDate()+o)};
  function range(){
    if(state.range==='today')return [localDay(0).getTime(),localDay(1).getTime()];
    if(state.range==='custom'&&state.from&&state.to){
      const [fy,fm,fd]=state.from.split('-').map(Number),[ty,tm,td]=state.to.split('-').map(Number);
      return [new Date(fy,fm-1,fd).getTime(),new Date(ty,tm-1,td+1).getTime()];
    }
    const days=Number(state.range)||7;return [localDay(1-days).getTime(),localDay(1).getTime()];
  }
  function scope(){
    if(state.camera)return [state.camera];
    return state.site?cameras.filter(c=>c.site_id===state.site).map(c=>c.id):null;
  }
  const pct=v=>v==null?null:Math.round(Number(v)*100);
  function playbackHref(e){
    const cam=encodeURIComponent(e.camera_id);
    return e.has_clip?`/playback?camera=${cam}&event=${encodeURIComponent(e.event_id)}&autoplay=event`
      :(typeof e.timestamp_ms==='number'?`/playback?camera=${cam}&t=${e.timestamp_ms}&autoplay=event`:'/playback');
  }
  const pretty=v=>String(v||'').replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());

  // What each analytic shows: [badge, badgeClass, title, extra lines]
  function describe(e){
    const d=e.details||{};
    if(key==='smart_motion'){
      const t=String(e.event_type||'').toLowerCase(),name=LABELS[t]||pretty(t);
      return [name,'',`${name} detected`,[d.object_count>1?`${d.object_count} objects`:null,e.confidence!=null?`${pct(e.confidence)}% confidence`:null]];
    }
    if(key==='people_counting'){
      return [d.direction==='in'?'Entry':d.direction==='out'?'Exit':'Count',d.direction==='in'?'good':'',d.direction==='in'?'Person entered':d.direction==='out'?'Person left':'People count',[]];
    }
    if(key==='lpr')return ['LPR','',d.plate?`Plate ${d.plate}`:'License plate read',[d.plate?null:'Plate not recorded',e.confidence!=null?`${pct(e.confidence)}% confidence`:null]];
    if(key==='ppe'){
      const part=(n,v)=>v==null?null:`${n} ${v?'✓':'✗ missing'}`;
      return [d.status==='violation'?'Violation':d.status==='compliant'?'Compliant':'PPE',d.status==='violation'?'bad':d.status==='compliant'?'good':'',
        d.status==='violation'?'PPE violation':d.status==='compliant'?'PPE compliant':'PPE check',[part('Hard hat',d.hard_hat),part('Vest',d.vest)]];
    }
    if(key==='facial_recognition'){
      const known=d.state==='known';
      return [known?'Recognized':'Unknown',known?'good':'',d.person||(known?'Recognized person':'Unknown face'),
        [d.watchlist?`Watchlist: ${d.watchlist}`:null,known&&e.confidence!=null?`${pct(e.confidence)}% match`:null]];
    }
    if(key==='loitering'){
      const stayed=d.dwell_seconds>=60?`${Math.floor(d.dwell_seconds/60)} min ${d.dwell_seconds%60?d.dwell_seconds%60+' s':''}`.trim():d.dwell_seconds!=null?`${d.dwell_seconds} s`:null;
      return ['Loitering','',d.rule?`Loitering: ${d.rule}`:'Person loitering',[stayed?`Stayed ${stayed}`:null,e.confidence!=null?`${pct(e.confidence)}% confidence`:null]];
    }
    const kind=key==='line_crossing'?'Line crossed':'Zone intrusion';
    return [key==='line_crossing'?'Line':'Zone','',d.rule?`${kind}: ${d.rule}`:kind,[d.direction?`Direction: ${pretty(d.direction)}`:null,e.confidence!=null?`${pct(e.confidence)}% confidence`:null]];
  }
  const thumbUrl=(e,size)=>`/api/customer/events/${encodeURIComponent(e.camera_id)}/${encodeURIComponent(e.event_id)}/thumbnail${size?`?size=${size}`:''}`;
  const EAGER_CARDS=8;  // the first screenful loads at once; the rest as they scroll into view
  function card(e,index){
    const [badge,badgeClass,title,lines]=describe(e);
    const img=e.has_thumbnail
      ?`<span class="aw-thumb-note">Loading preview…</span><img src="${thumbUrl(e,'card')}" alt="" loading="${index<EAGER_CARDS?'eager':'lazy'}" decoding="async"${index<EAGER_CARDS?' fetchpriority="high"':''}>`
      :'<span class="aw-thumb-note">No preview available</span>';
    const plate=key==='lpr'&&e.details&&e.details.plate?`<span class="aw-plate">${esc(e.details.plate)}</span>`:'';
    const meta=[cameraName[e.camera_id]||'Camera',when(e.timestamp_ms),...lines].filter(Boolean).map(esc).join(' · ');
    return `<button type="button" class="aw-card" data-inline-key="${e.has_clip?'event':'snapshot'}:${esc(e.event_id)}" data-event="${esc(e.event_id)}" aria-expanded="false" aria-label="${esc(title)}, ${esc(when(e.timestamp_ms))}${e.has_clip?', play clip':''}">
      <span class="aw-thumb" data-preview="${e.has_thumbnail?'loading':'none'}">${img}<span class="aw-badge ${badgeClass}">${esc(badge)}</span>${plate}${e.has_clip?'<span class="aw-play" aria-hidden="true">▶</span>':''}</span>
      <span class="aw-body"><strong>${esc(title)}</strong><span class="aw-meta">${meta}</span></span></button>`;
  }
  // License Plates (2026-09-30): a table, one row per plate read -- time,
  // camera, plate, plate image, make, model, colour/type and its clip.
  // Anything not known reliably reads "Unknown" (never guessed); reads
  // synced before these fields existed show what they have.
  const LPR=key==='lpr';
  const LPR_COLUMNS=['Time','Camera','Plate','Plate image','Make','Model','Color / type','Clip'];
  const LPR_HEAD=`<div class="aw-lpr-head" role="row">${LPR_COLUMNS.map(c=>`<span role="columnheader">${esc(c)}</span>`).join('')}</div>`;
  if(LPR){results.className='aw-lpr';results.setAttribute('role','table')}
  function whenPrecise(ms){
    if(typeof ms!=='number')return '';
    const d=new Date(ms),now=new Date(),y=new Date(now.getFullYear(),now.getMonth(),now.getDate()-1);
    const t=d.toLocaleTimeString([],{hour:'numeric',minute:'2-digit',second:'2-digit'});
    if(d.toDateString()===now.toDateString())return `Today, ${t}`;
    if(d.toDateString()===y.toDateString())return `Yesterday, ${t}`;
    return d.toLocaleDateString([],{month:'short',day:'numeric',year:d.getFullYear()===now.getFullYear()?undefined:'numeric'})+`, ${t}`;
  }
  const unknown='<span class="aw-muted">Unknown</span>';
  function colorType(d){
    if(d.vehicle_color&&d.vehicle_type)return esc(`${d.vehicle_color} ${d.vehicle_type.toLowerCase()}`);
    if(d.vehicle_color)return esc(d.vehicle_color);
    if(d.vehicle_type)return `${esc(d.vehicle_type)} <span class="aw-muted">· color unknown</span>`;
    return unknown;
  }
  function lprRow(e){
    const d=e.details||{};
    const plate=d.plate?`<span class="aw-plate-text">${esc(d.plate)}</span>`:'<span class="aw-muted">Not recorded</span>';
    const image=d.has_plate_image?`<img class="aw-plate-img" src="/api/customer/analytics/lpr/${encodeURIComponent(e.event_id)}/plate-image" alt="Plate ${esc(d.plate||'')}" loading="lazy" decoding="async">`:'<span class="aw-muted">—</span>';
    const clip=e.has_clip?`<button type="button" class="ghost-button aw-lpr-open" aria-label="Play clip of ${esc(d.plate||'this plate read')}">▶ Play clip</button>`
      :e.has_thumbnail?'<button type="button" class="ghost-button aw-lpr-open">View snapshot</button>':'<span class="aw-muted">No clip</span>';
    const cells=[esc(whenPrecise(e.timestamp_ms)),esc(cameraName[e.camera_id]||'Camera'),plate,image,d.vehicle_make?esc(d.vehicle_make):unknown,d.vehicle_model?esc(d.vehicle_model):unknown,colorType(d),clip];
    return `<div class="aw-lpr-row" role="row" data-inline-key="${e.has_clip?'event':'snapshot'}:${esc(e.event_id)}" data-event="${esc(e.event_id)}" aria-expanded="false">${cells.map((c,i)=>`<span role="cell" data-label="${esc(LPR_COLUMNS[i])}">${c}</span>`).join('')}</div>`;
  }
  function openItem(button){
    const e=state.items.get(button.dataset.event);if(!e)return;
    const [, , title]=describe(e);
    const href=playbackHref(e);
    inline.open(button,e.camera_id,{
      kind:e.has_clip?'event':'snapshot',id:e.event_id,start:e.timestamp_ms,title:`${title} — ${cameraName[e.camera_id]||''}`,
      thumbnail:e.has_thumbnail?thumbUrl(e):'',poster:e.has_thumbnail?thumbUrl(e,'card'):'',
      note:e.has_thumbnail?'No video clip was recorded for this detection.':'No snapshot or clip was recorded for this detection.',
      playbackHref:href,shareUrl:location.origin+href,
    });
  }
  results.addEventListener('click',ev=>{
    if(LPR){const b=ev.target.closest('.aw-lpr-open');if(b)openItem(b.closest('.aw-lpr-row'));return}
    const b=ev.target.closest('.aw-card');if(b)openItem(b);
  });
  // A plate image that cannot load reads "—", like a read without one.
  results.addEventListener('error',ev=>{const t=ev.target;if(t.tagName==='IMG'&&t.classList.contains('aw-plate-img'))t.outerHTML='<span class="aw-muted">—</span>'},true);
  // Preview images: load/error don't bubble, so listen in the capture phase.
  results.addEventListener('load',ev=>{const t=ev.target;if(t.tagName==='IMG'&&t.parentElement.classList.contains('aw-thumb'))t.parentElement.dataset.preview='ready'},true);
  results.addEventListener('error',ev=>{
    const t=ev.target;if(t.tagName!=='IMG'||!t.parentElement.classList.contains('aw-thumb'))return;
    const thumb=t.parentElement;thumb.dataset.preview='none';t.remove();
    const note=thumb.querySelector('.aw-thumb-note');if(note)note.textContent='No preview available';
  },true);

  function stat(name,value){return `<div class="aw-stat"><span>${esc(name)}</span><strong>${esc(typeof value==='number'?value.toLocaleString():value)}</strong></div>`}
  function renderSummary(s){
    const t=s.total||0,by=s.by_type||{},res=s.by_result||{};
    const sum=keys=>keys.reduce((n,k)=>n+(by[k]||0),0);
    let html='';extra.innerHTML='';
    if(key==='smart_motion')html=stat('Detections',t)+stat('People',sum(['person']))+stat('Vehicles',sum(['vehicle','car','truck','bus','motorcycle','bicycle']))+stat('Motion',sum(['motion','smart_motion']));
    else if(key==='people_counting'){
      const i=by.people_counting_in||0,o=by.people_counting_out||0;
      html=stat('Entries',i)+stat('Exits',o)+stat('Net change',(i-o>0?'+':'')+(i-o));
      const days={};
      (s.hourly||[]).forEach(h=>{const k=new Date(h.hour_ms).toLocaleDateString([],{month:'short',day:'numeric'});const e=days[k]||(days[k]={in:0,out:0});e.in+=h.in;e.out+=h.out});
      const max=Math.max(1,...Object.values(days).flatMap(d=>[d.in,d.out]));
      const trend=Object.keys(days).length?`<h2 class="aw-section">Per day</h2><div class="aw-trend" role="img" aria-label="Entries and exits per day">${Object.entries(days).map(([k,d])=>`<div class="aw-bar" title="${esc(k)}: ${d.in} in, ${d.out} out"><div><i style="height:${Math.round(d.in/max*90)}px"></i><i class="out" style="height:${Math.round(d.out/max*90)}px"></i></div>${esc(k)}</div>`).join('')}</div>`:'';
      const rows=Object.entries(s.per_camera||{}).map(([id,v])=>`<tr><td>${esc(cameraName[id]||'Camera')}</td><td>${v.in}</td><td>${v.out}</td><td>${(v.in-v.out>0?'+':'')+(v.in-v.out)}</td></tr>`).join('');
      extra.innerHTML=trend+(rows?`<h2 class="aw-section">By camera</h2><table class="aw-table"><thead><tr><th>Camera</th><th>Entries</th><th>Exits</th><th>Net</th></tr></thead><tbody>${rows}</tbody></table>`:'')+'<h2 class="aw-section">Crossings</h2>';
    }
    else if(key==='ppe')html=stat('Checks',t)+stat('Violations',res.violation||0)+stat('Missing hard hat',res.missing_hard_hat||0)+stat('Missing vest',res.missing_vest||0);
    else if(key==='facial_recognition')html=stat('Faces',t)+stat('Recognized',res.known||0)+stat('Unknown',res.unknown||0);
    else if(key==='lpr')html=stat('Plates read',t);
    else{html=stat(key==='line_crossing'?'Crossings':key==='loitering'?'Loitering':'Intrusions',t)+Object.entries(res).slice(0,3).map(([rule,n])=>stat(rule,n)).join('')}
    stats.innerHTML=html;
  }
  const EMPTY={smart_motion:'No people, vehicle or motion detections',people_counting:'No entries or exits',lpr:'No license plate reads',ppe:'No PPE checks',facial_recognition:'No faces',line_crossing:'No line crossings',intrusion:'No zone intrusions',loitering:'No loitering'};
  const emptyText=()=>`${EMPTY[key]||'Nothing'} in this period.`;
  // The current filters as an events query; null when the chosen site has no cameras.
  function eventsQuery(){
    const [start,end]=range(),ids=scope();
    if(ids!==null&&!ids.length)return null;
    const q=new URLSearchParams({start_ms:start,end_ms:end,result:state.result,q:state.q});
    if(ids)q.set('camera_id',ids.join(','));
    return q;
  }
  let generation=0,shownSummary='';  // generation: bumped by every fresh load, so a refresh begun under older filters is dropped
  async function load(append){
    if(state.loading)return;state.loading=true;
    if(!append){generation++;state.before=null;state.items.clear();inline.close();results.innerHTML=(LPR?LPR_HEAD:'')+'<div class="aw-empty">Loading…</div>'}
    const q=eventsQuery();
    try{
      if(q===null){results.innerHTML=(LPR?LPR_HEAD:'')+'<div class="aw-empty">No cameras at this site.</div>';stats.innerHTML='';shownSummary='';more.hidden=true;return}
      if(state.before)q.set('before',state.before);
      const r=await fetch(`/api/customer/analytics/${key}/events?${q}`,{credentials:'same-origin'});
      const body=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:'Analytics could not be loaded.');
      if(!append){shownSummary=JSON.stringify(body.summary||{});renderSummary(body.summary||{})}
      body.events.forEach(e=>state.items.set(e.event_id,e));
      const offset=append?results.querySelectorAll('.aw-card').length:0;
      const html=body.events.map((e,i)=>LPR?lprRow(e):card(e,offset+i)).join('');
      if(append){results.insertAdjacentHTML('beforeend',html);inline.reattach(results)}
      else results.innerHTML=(LPR?LPR_HEAD:'')+(html||`<div class="aw-empty">${esc(emptyText())}</div>`);
      state.before=body.next_before;more.hidden=!body.next_before;
    }catch(error){if(!append)results.innerHTML=(LPR?LPR_HEAD:'')+`<div class="aw-empty">${esc(error.message)}</div>`}
    finally{state.loading=false}
  }
  // License Plates keep themselves current (2026-10-06): while the page is
  // visible, plate reads that arrive -- and clips attached to a read after
  // it -- appear every LPR_REFRESH_MS, using the filters as they are now and
  // leaving the rows already shown, "Load more" and an open clip alone. One
  // timer at a time; paused while the tab is hidden, refreshed at once when
  // it is shown again. A failed refresh keeps the table and the next tick
  // tries again. Other analytics pages do not poll.
  const LPR_REFRESH_MS=5000;
  let refreshTimer=null,refreshing=false;
  const rowFor=id=>[...results.querySelectorAll('.aw-lpr-row')].find(r=>r.dataset.event===id);
  function placeRow(e){
    // Newest first: before the first row that is older. A read older than
    // everything loaded belongs on a later page while one exists.
    const rows=[...results.querySelectorAll('.aw-lpr-row')];
    const newer=rows.find(r=>{const o=state.items.get(r.dataset.event);return o&&o.timestamp_ms<e.timestamp_ms});
    if(newer){newer.insertAdjacentHTML('beforebegin',lprRow(e));return true}
    if(state.before)return false;
    const last=rows[rows.length-1],card=last.nextElementSibling;
    (card&&card.classList.contains('inline-media-card')?card:last).insertAdjacentHTML('afterend',lprRow(e));
    return true;
  }
  function mergeLatest(body){
    const summary=JSON.stringify(body.summary||{});
    if(summary!==shownSummary){shownSummary=summary;renderSummary(body.summary||{})}
    const shown=results.querySelectorAll('.aw-lpr-row').length;
    const fresh=body.events.filter(e=>!state.items.has(e.event_id));
    if(!shown||(fresh.length&&fresh.length===body.events.length&&body.next_before)){
      // Nothing listed yet (no reads, or an earlier error), or a whole page
      // of new reads: show this page as a fresh load would.
      if(!shown&&!body.events.length){
        const note=results.querySelector('.aw-empty');
        if(!note||note.textContent!==emptyText())results.innerHTML=LPR_HEAD+`<div class="aw-empty">${esc(emptyText())}</div>`;
        return;
      }
      state.items.clear();body.events.forEach(e=>state.items.set(e.event_id,e));
      results.innerHTML=LPR_HEAD+body.events.map(lprRow).join('');
      state.before=body.next_before;more.hidden=!body.next_before;
      inline.reattach(results);
      return;
    }
    body.events.forEach(e=>{
      const known=state.items.get(e.event_id);
      if(!known||JSON.stringify(known)===JSON.stringify(e))return;
      const row=rowFor(e.event_id);
      if(row&&row.getAttribute('aria-expanded')==='true')return;  // never disturb an open clip
      state.items.set(e.event_id,e);
      if(row)row.outerHTML=lprRow(e);
    });
    fresh.forEach(e=>{if(placeRow(e))state.items.set(e.event_id,e)});
    inline.reattach(results);
  }
  async function refresh(){
    if(!LPR||document.hidden||state.loading||refreshing)return;
    if(state.range==='custom'&&!(state.from&&state.to&&state.from<=state.to))return;
    const q=eventsQuery();if(q===null)return;
    const startedUnder=generation;refreshing=true;
    try{
      const r=await fetch(`/api/customer/analytics/${key}/events?${q}`,{credentials:'same-origin'});
      if(!r.ok)return;
      const body=await r.json();
      if(startedUnder!==generation||state.loading||!body||!Array.isArray(body.events))return;
      mergeLatest(body);
    }catch(error){/* offline or busy: keep what is shown */}
    finally{refreshing=false}
  }
  function scheduleRefresh(){
    clearTimeout(refreshTimer);refreshTimer=null;
    if(!LPR||document.hidden)return;
    refreshTimer=setTimeout(async()=>{refreshTimer=null;await refresh();if(!refreshTimer)scheduleRefresh()},LPR_REFRESH_MS);
  }
  if(LPR){
    document.addEventListener('visibilitychange',()=>{
      if(document.hidden){clearTimeout(refreshTimer);refreshTimer=null;return}
      scheduleRefresh();refresh();
    });
    window.addEventListener('pagehide',()=>{clearTimeout(refreshTimer);refreshTimer=null});
    // Back/forward cache: a restored page resumes, and catches up at once.
    window.addEventListener('pageshow',ev=>{if(ev.persisted&&!document.hidden){scheduleRefresh();refresh()}});
  }
  function syncUrl(){const q=new URLSearchParams();if(state.camera)q.set('camera',state.camera);history.replaceState(null,'',`/analytics/${config.slug}${q.toString()?'?'+q:''}`)}
  $('aw-camera').addEventListener('change',e=>{state.camera=e.target.value;syncUrl();load(false)});
  if($('aw-site'))$('aw-site').addEventListener('change',e=>{
    state.site=e.target.value;state.camera='';
    const sel=$('aw-camera');[...sel.options].forEach(o=>{if(!o.value)return;const c=cameras.find(x=>x.id===o.value);o.hidden=!!state.site&&c.site_id!==state.site});sel.value='';
    syncUrl();load(false);
  });
  $('aw-range').addEventListener('change',e=>{
    state.range=e.target.value;const custom=state.range==='custom';
    $('aw-from-wrap').hidden=!custom;$('aw-to-wrap').hidden=!custom;
    if(custom){const f=d=>`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
      if(!state.from){state.from=f(localDay(-6));$('aw-from').value=state.from}if(!state.to){state.to=f(localDay(0));$('aw-to').value=state.to}}
    load(false);
  });
  ['aw-from','aw-to'].forEach(id=>$(id).addEventListener('change',e=>{state[id==='aw-from'?'from':'to']=e.target.value;if(state.from&&state.to&&state.from<=state.to)load(false)}));
  if($('aw-result'))$('aw-result').addEventListener('change',e=>{state.result=e.target.value;load(false)});
  if($('aw-search')){let timer=null;$('aw-search').addEventListener('input',e=>{clearTimeout(timer);timer=setTimeout(()=>{state.q=e.target.value.trim();load(false)},300)})}
  more.addEventListener('click',()=>load(true));
  load(false);
  scheduleRefresh();
})();
