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
    if(key==='lpr')return ['LPR','',d.plate?`Plate ${d.plate}`:'License plate read',[d.plate?null:'Plate text is kept on the appliance',e.confidence!=null?`${pct(e.confidence)}% confidence`:null]];
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
    const kind=key==='line_crossing'?'Line crossed':'Zone intrusion';
    return [key==='line_crossing'?'Line':'Zone','',d.rule?`${kind}: ${d.rule}`:kind,[d.direction?`Direction: ${pretty(d.direction)}`:null,e.confidence!=null?`${pct(e.confidence)}% confidence`:null]];
  }
  function card(e){
    const [badge,badgeClass,title,lines]=describe(e);
    const img=e.has_thumbnail?`<img src="/api/customer/events/${encodeURIComponent(e.camera_id)}/${encodeURIComponent(e.event_id)}/thumbnail" alt="" loading="lazy" decoding="async" onerror="this.remove()">`:'<span>No snapshot</span>';
    const plate=key==='lpr'&&e.details&&e.details.plate?`<span class="aw-plate">${esc(e.details.plate)}</span>`:'';
    const meta=[cameraName[e.camera_id]||'Camera',when(e.timestamp_ms),...lines].filter(Boolean).map(esc).join(' · ');
    return `<button type="button" class="aw-card" data-inline-key="${e.has_clip?'event':'snapshot'}:${esc(e.event_id)}" data-event="${esc(e.event_id)}" aria-expanded="false" aria-label="${esc(title)}, ${esc(when(e.timestamp_ms))}${e.has_clip?', play clip':''}">
      <span class="aw-thumb">${img}<span class="aw-badge ${badgeClass}">${esc(badge)}</span>${plate}${e.has_clip?'<span class="aw-play" aria-hidden="true">▶</span>':''}</span>
      <span class="aw-body"><strong>${esc(title)}</strong><span class="aw-meta">${meta}</span></span></button>`;
  }
  function openItem(button){
    const e=state.items.get(button.dataset.event);if(!e)return;
    const [, , title]=describe(e);
    const href=playbackHref(e);
    inline.open(button,e.camera_id,{
      kind:e.has_clip?'event':'snapshot',id:e.event_id,start:e.timestamp_ms,title:`${title} — ${cameraName[e.camera_id]||''}`,
      thumbnail:e.has_thumbnail?`/api/customer/events/${encodeURIComponent(e.camera_id)}/${encodeURIComponent(e.event_id)}/thumbnail`:'',
      note:e.has_thumbnail?'No video clip was recorded for this detection.':'No snapshot or clip was recorded for this detection.',
      playbackHref:href,shareUrl:location.origin+href,
    });
  }
  results.addEventListener('click',ev=>{const b=ev.target.closest('.aw-card');if(b)openItem(b)});

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
    else{html=stat(key==='line_crossing'?'Crossings':'Intrusions',t)+Object.entries(res).slice(0,3).map(([rule,n])=>stat(rule,n)).join('')}
    stats.innerHTML=html;
  }
  const EMPTY={smart_motion:'No people, vehicle or motion detections',people_counting:'No entries or exits',lpr:'No license plate reads',ppe:'No PPE checks',facial_recognition:'No faces',line_crossing:'No line crossings',intrusion:'No zone intrusions'};
  async function load(append){
    if(state.loading)return;state.loading=true;
    if(!append){state.before=null;state.items.clear();inline.close();results.innerHTML='<div class="aw-empty">Loading…</div>'}
    const [start,end]=range(),ids=scope();
    const q=new URLSearchParams({start_ms:start,end_ms:end,result:state.result,q:state.q});
    if(state.before)q.set('before',state.before);
    try{
      if(ids!==null&&!ids.length){results.innerHTML='<div class="aw-empty">No cameras at this site.</div>';stats.innerHTML='';more.hidden=true;return}
      if(ids)q.set('camera_id',ids.join(','));
      const r=await fetch(`/api/customer/analytics/${key}/events?${q}`,{credentials:'same-origin'});
      const body=await r.json().catch(()=>({}));
      if(!r.ok)throw new Error(typeof body.detail==='string'?body.detail:'Analytics could not be loaded.');
      if(!append)renderSummary(body.summary||{});
      body.events.forEach(e=>state.items.set(e.event_id,e));
      const html=body.events.map(card).join('');
      if(append){results.insertAdjacentHTML('beforeend',html);inline.reattach(results)}
      else results.innerHTML=html||`<div class="aw-empty">${esc(EMPTY[key]||'Nothing')} in this period.</div>`;
      state.before=body.next_before;more.hidden=!body.next_before;
    }catch(error){if(!append)results.innerHTML=`<div class="aw-empty">${esc(error.message)}</div>`}
    finally{state.loading=false}
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
})();
