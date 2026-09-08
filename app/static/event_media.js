/* Shared event readiness and cancellable playback. No media bytes are proxied. */
(function(root){
  const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function state(hasClip,timestamp,now=Date.now()){
    if(hasClip)return 'ready';
    const text=String(timestamp||'');
    const age=now-new Date(/Z$|[+-]\d\d:?\d\d$/.test(text)?text:text+'Z').getTime();
    return age>=0&&age<120000?'processing':'unavailable';
  }
  function player({video,status,isCurrent=()=>true,onReady=()=>{},fetcher=fetch,now=()=>Date.now(),later=setTimeout,clear=clearTimeout}){
    let generation=0,controller=null,timer=null,listeners=[],rejectWait=null;
    function cleanup(){if(timer!==null)clear(timer);timer=null;listeners.forEach(([event,fn])=>video.removeEventListener(event,fn));listeners=[];}
    function cancel(){generation++;if(controller)controller.abort();controller=null;cleanup();if(rejectWait)rejectWait(new Error('cancelled'));rejectWait=null;}
    function listen(event,fn){video.addEventListener(event,fn);listeners.push([event,fn]);}
    async function start(cameraId,eventId,autoplay=true){
      cancel();const mine=generation,deadline=now()+120000;
      const current=()=>mine===generation&&isCurrent(cameraId,eventId);
      const message=text=>{if(current())status.textContent=text;};
      while(current()&&now()<deadline){
        controller=new AbortController();const requestController=controller;
        let response,payload,loaded=false;
        try{
          message('Processing event media…');
          const timeout=new Promise((resolve,reject)=>{rejectWait=reject;timer=later(()=>{requestController.abort();reject(new Error('timeout'));},Math.min(8000,deadline-now()));});
          const request=(async()=>{const result=await fetcher(`/api/customer/events/${encodeURIComponent(cameraId)}/${encodeURIComponent(eventId)}/media/url`,{credentials:'same-origin',cache:'no-store',signal:requestController.signal});return [result,result.ok?await result.json():null];})();
          [response,payload]=await Promise.race([request,timeout]);
          clear(timer);timer=null;rejectWait=null;
          if(!current())return;
          if(response.ok&&typeof payload?.url==='string'&&/^(https?:\/\/|\/(?!\/))/.test(payload.url)){
            video.pause();video.src=payload.url;message('Loading event clip…');
            const ready=()=>{if(!current())return;if(timer!==null)clear(timer);timer=null;message(video.paused?'Event clip ready — press Play to start.':'Playing event clip');};
            listen('canplay',ready);listen('playing',()=>message('Playing event clip'));
            listen('error',()=>{if(current()){cleanup();message('Event clip is unavailable.');}});
            timer=later(()=>{if(current()){cleanup();message('Event clip is unavailable.');}},15000);
            loaded=true;video.load();onReady();
            if(autoplay)Promise.resolve(video.play()).catch(()=>message('Event clip ready — press Play to start.'));
            return;
          }
          if(response.status!==404&&response.status<500){message('Event clip is unavailable.');return;}
        }catch(error){if(!current())return;}
        finally{if(mine===generation){if(!loaded){if(timer!==null)clear(timer);timer=null;}rejectWait=null;}}
        if(!current())return;
        const remaining=deadline-now();if(remaining<=0)break;
        message('Checking event media…');
        try{await new Promise((resolve,reject)=>{rejectWait=reject;timer=later(resolve,Math.min(4000,remaining));});}
        catch(error){return;}
        timer=null;rejectWait=null;
      }
      message('Event media is not ready yet. Try again.');
    }
    return {start,cancel};
  }
  root.AnyAiCamEventMedia={escape,state,player};
})(globalThis);
