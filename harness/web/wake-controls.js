/* Private controls for the shared signal sources of one saved conversation. */
(() => {
  'use strict';
  function create({node,icon,scope}) {
    let active=null;
    function close(restore=true) {
      const previous=active;active=null;if(!previous)return;
      previous.abort.abort();for(const input of previous.dialog.querySelectorAll('input'))input.value='';
      previous.dialog.close();previous.dialog.remove();
      if(restore&&previous.opener?.isConnected)previous.opener.focus();
    }
    function current(state) {
      const now=scope();return active===state&&now.writable&&now.key===state.key&&now.chatId===state.chatId;
    }
    function sync(){if(active&&!current(active))close(false);}
    async function open(opener) {
      close(false);const selected=scope();if(!selected.writable||!selected.chatId)return;
      const dialog=node('dialog','wake-controls'),header=node('header'),heading=node('h2','','Wakes'),dismiss=node('button','icon-button'),body=node('div','wake-controls-body'),status=node('p','wake-controls-status');
      dialog.setAttribute('aria-label','Wakes');status.setAttribute('role','status');dismiss.type='button';dismiss.setAttribute('aria-label','Close wakes');dismiss.append(icon('close'));dismiss.addEventListener('click',()=>close());
      header.append(heading,dismiss);dialog.append(header,body,status);document.body.append(dialog);
      const state={...selected,dialog,opener,abort:new AbortController()};active=state;
      dialog.addEventListener('cancel',event=>{event.preventDefault();close();});dialog.addEventListener('close',()=>{if(active===state)close();});
      dialog.showModal();dismiss.focus();status.textContent='Loading wakes…';
      try {
        const response=await fetch(`/api/wakes?chat_id=${encodeURIComponent(state.chatId)}`,{cache:'no-store',signal:state.abort.signal});
        if(!current(state))return;if(!response.ok)throw Error();const data=await response.json();if(!current(state))return;
        status.textContent='';const sources=[...(Array.isArray(data.sources)?data.sources:[]),...(Array.isArray(data.endpoints)?data.endpoints:[]).map(row=>({...row,kind:'endpoint'}))];
        if(!sources.length)body.append(node('p','','Ask this agent to set up a webhook or a scheduled wake.'));
        for(const source of sources) {
          const row=node('section','wake-source');row.append(node('h3','',source.label||'Wake'),node('p','wake-source-meta',`${source.kind==='hook'?'Incoming webhook':source.kind==='endpoint'?'Outgoing webhook':'Timer'} · ${source.status||'Saved'}`));
          if(['hook','endpoint'].includes(source.kind)&&typeof source.url==='string')row.append(node('code','wake-source-address',source.url));
          if(source.kind==='timer'&&source.next_at)row.append(node('p','wake-source-meta',`Next wake ${new Date(source.next_at*1000).toLocaleString()}`));
          if(['hook','endpoint'].includes(source.kind)&&source.secret_available===true) {
            const reveal=node('button','agent-secondary','Show signing secret');reveal.type='button';row.append(reveal);
            reveal.addEventListener('click',async()=>{
              if(!current(state)||reveal.disabled)return;reveal.disabled=true;status.textContent='Opening signing secret…';
              try {
                const response=await fetch(`/api/wakes/${source.kind==='endpoint'?'endpoints':'hooks'}/${encodeURIComponent(source.id)}/secret`,{method:'POST',headers:{'X-Rocoto-Page':'1'},cache:'no-store',signal:state.abort.signal});
                if(!current(state))return;if(!response.ok)throw Error();const result=await response.json();if(!current(state))return;
                if(typeof result.secret!=='string'||!result.secret||result.secret.length>4096)throw Error();
                reveal.remove();const field=node('input','wake-secret'),label=node('label','wake-secret-label','Signing secret'),copy=node('button','agent-secondary','Copy signing secret');
                field.type='text';field.readOnly=true;field.autocomplete='off';field.spellcheck=false;field.setAttribute('aria-label','Signing secret');field.value=result.secret;result.secret='';
                label.append(field);copy.type='button';copy.addEventListener('click',async()=>{
                  if(!current(state))return;
                  try{await navigator.clipboard.writeText(field.value);if(current(state))status.textContent='Signing secret copied.';}
                  catch{if(current(state)){field.focus();field.select();status.textContent='Copy the selected signing secret.';}}
                });row.append(label,copy);status.textContent=`Save this signing secret in your webhook ${source.kind==='endpoint'?'receiver':'sender'}. It is available in this dialog until you close it.`;
              }catch{if(current(state))status.textContent='Reopen Wakes to check this webhook’s signing-secret availability.';}
            });
          }
          body.append(row);
        }
        const limits=Object.entries(data.daily_limits||{}).filter(([,value])=>Number.isFinite(value?.used)&&Number.isFinite(value?.limit));
        if(limits.length)body.append(node('p','wake-source-meta',limits.map(([kind,value])=>`${kind}: ${value.used} of ${value.limit} / day`).join(' · ')));
      }catch{if(current(state))status.textContent='Open Wakes again to load this conversation’s sources.';}
    }
    return Object.freeze({open,sync,close});
  }
  window.RocotoWakeControls=Object.freeze({create});
})();
