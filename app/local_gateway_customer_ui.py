"""Customer subscription panel, embedded in the existing My subscription page."""


def panel_html() -> str:
    return '''<section class="panel" style="margin-top:14px" id="local-gateway-remote">
    <h2>Local Gateway remote access</h2>
    <p>$9.99/month per appliance. This is separate from your perpetual local VMS software license.</p>
    <p>Cancellation or expiration disables subscription-provided remote access only. Local viewing, recording,
    playback and licensed camera capacity continue with your valid local license.</p>
    <div id="local-gateway-appliances" aria-live="polite">Loading appliance status…</div>
    <p class="health-detail">Status is the last confirmed billing state; changes can take time to appear.</p>
    <p id="local-gateway-message" role="status" aria-live="polite"></p></section>
    <section class="panel" style="margin-top:14px" id="cloud-gateway-subscription" hidden>
    <h2>Cloud Gateway</h2><p>$9.99/month per camera. Local recording, basic motion detection and notifications use your licensed system and permissions.</p>
    <p>Cloud event services are optional. Availability requires an eligible plan, camera permission and opt-in. Advanced analytics require separate authorization.</p>
    <div id="cloud-gateway-appliances" aria-live="polite"></div></section>'''


def panel_script() -> str:
    return '''<script>
    (async function(){
      const target=document.getElementById('local-gateway-appliances'), msg=document.getElementById('local-gateway-message');
      const labels={not_subscribed:'Not subscribed',pending:'Payment or activation pending',active:'Active',canceling:'Cancellation scheduled',canceled:'Canceled',expired:'Expired',payment_failed:'Payment failed',suspended:'Suspended',unknown:'Status unavailable'};
      async function post(url, body, button){
        button.disabled=true;msg.textContent='';
        try {const options={method:'POST'};if(body){options.headers={'Content-Type':'application/json'};options.body=JSON.stringify(body);}
          const response=await fetch(url,options), data=await response.json().catch(()=>({}));
          const destination=body?data.checkout_url:data.url;
          if(!response.ok||!destination){msg.textContent=typeof data.detail==='string'?data.detail:'Billing is unavailable. Please try again.';button.disabled=false;return;}
          location.href=destination;
        } catch(e){msg.textContent='Could not reach AnyAiCam. Please try again.';button.disabled=false;}
      }
      try {const response=await fetch('/api/v2/customer/local-gateway/appliances');
        if(!response.ok)throw new Error('status');const data=await response.json();target.textContent='';
        const local=Array.isArray(data.appliances)?data.appliances:[], pending=Array.isArray(data.pending_appliances)?data.pending_appliances:[], cloud=Array.isArray(data.cloud_appliances)?data.cloud_appliances:[];
        if(!local.length&&!pending.length){target.textContent='No confirmed Local Gateway appliances on this account. If you just linked a computer, contact your administrator to verify its edition.';}
        local.forEach(item=>{const row=document.createElement('div');row.className='health-row';
          const detail=document.createElement('span');detail.textContent=(item.name||'Appliance')+' — Billing: '+(labels[item.status]||labels.unknown)+' · Local VMS license: '+(item.local_license_active?'active':'not active');row.appendChild(detail);
          if(!item.edition_verified){const note=document.createElement('span');note.textContent='Edition pending verification; remote access is unavailable.';row.appendChild(note);}
          if(!item.appliance_ready){const note=document.createElement('span');note.textContent='Device is not ready for remote access.';row.appendChild(note);}
          if(item.can_subscribe){const button=document.createElement('button');button.type='button';button.className='action-button';button.textContent='Subscribe';button.onclick=()=>post('/api/v2/customer/local-gateway/checkout',{appliance_id:item.appliance_id},button);row.appendChild(button);}
          else if(item.can_manage){const button=document.createElement('button');button.type='button';button.className='ghost-button';button.textContent='Manage subscription';button.onclick=()=>post('/api/customer/billing-portal',null,button);row.appendChild(button);}
          else if(!item.local_license_active){const note=document.createElement('span');note.textContent='An active local VMS license is required to subscribe.';row.appendChild(note);}
          else if(item.is_owner&&!item.checkout_enabled&&item.status==='not_subscribed'){const note=document.createElement('span');note.textContent='Subscription checkout is not available yet.';row.appendChild(note);}
          target.appendChild(row);});
        pending.forEach(item=>{const row=document.createElement('div');row.className='health-row';row.textContent=(item.name||'Appliance')+' — Edition pending verification. Contact your administrator.';target.appendChild(row);});
        if(cloud.length){const panel=document.getElementById('cloud-gateway-subscription'), list=document.getElementById('cloud-gateway-appliances');panel.hidden=false;cloud.forEach(item=>{const row=document.createElement('div');row.className='health-row';const heading=document.createElement('strong');heading.textContent=item.name||'Appliance';row.appendChild(heading);const cameras=Array.isArray(item.cameras)?item.cameras:[];if(!cameras.length){const note=document.createElement('div');note.textContent='No verified camera status available.';row.appendChild(note);}cameras.forEach(camera=>{const detail=document.createElement('div');detail.textContent=(camera.name||'Camera')+' — Camera mapping: '+(camera.mapping_status||'unavailable')+'; Cloud Gateway entitlement: unavailable; event uploads: on hold.';row.appendChild(detail);});list.appendChild(row);});}
      } catch(e){target.textContent='Appliance status is unavailable. Please try again.';}
    })();
    </script>'''
