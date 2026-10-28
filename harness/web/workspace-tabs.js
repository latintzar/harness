/* Source-owned vertical tabs run in the shared workspace shell. */
(() => {
  'use strict';
  const verticals=new Map();
  const valid=value=>typeof value==='string'&&/^[a-z][a-z0-9_-]{0,79}$/.test(value);
  function register(id,tabs) {
    if(!valid(id)||!tabs||typeof tabs!=='object')throw new Error('Register one tab set for the installed vertical.');
    const entries=Object.entries(tabs);
    if(entries.some(([key,tab])=>!valid(key)||typeof tab?.mount!=='function'))throw new Error('Give every tab its mount function.');
    const prior=verticals.get(id)||{};
    if(entries.some(([key])=>Object.hasOwn(prior,key)))throw new Error('Register each vertical tab once.');
    verticals.set(id,Object.freeze({...prior,...Object.fromEntries(entries.map(([key,tab])=>[key,Object.freeze({...tab})]))}));
  }
  function create({root,context,defaultHome,onError=()=>{}}) {
    let selected=null,identity='',epoch=0,active=null,visible=false;
    const dispose=()=>{++epoch;const old=active;active=null;if(old){old.abort.abort();old.cleanup?.();}root.replaceChildren();};
    function configure(shell,workspaceIdentity) {
      const next=JSON.stringify([shell.vertical?.id||'bare',shell.vertical?.revision||'',workspaceIdentity]);
      selected=shell;
      if(next!==identity){dispose();identity=next;}
    }
    async function open(id='home') {
      if(!selected)return;
      if(id!=='home'&&!selected.surfaces.some(surface=>surface.id===id))throw new Error('Open a tab declared by this vertical.');
      if(active?.id===id)return;
      dispose();
      const mine=epoch,abort=new AbortController(),slot={id,abort,cleanup:null};active=slot;
      const definition=verticals.get(selected.vertical?.id)?.[id]||(id==='home'?defaultHome:null);
      if(!definition){root.textContent=selected.surfaces.find(surface=>surface.id===id)?.title||'Workspace';return;}
      try {
        const mounted=await definition.mount(root,Object.freeze({...context,signal:abort.signal,
          shell:selected,isCurrent:()=>active===slot&&!abort.signal.aborted}));
        const cleanup=typeof mounted==='function'?mounted:typeof mounted?.unmount==='function'?()=>mounted.unmount():null;
        if(mine!==epoch||active!==slot){cleanup?.();return;}
        slot.cleanup=cleanup;slot.controller=mounted;
        if(visible)mounted?.show?.();else mounted?.hide?.();
      } catch(error) {
        if(mine===epoch&&active===slot&&!abort.signal.aborted)onError(error);
      }
    }
    function setVisible(value){visible=Boolean(value);root.hidden=!visible;if(visible)active?.controller?.show?.();else active?.controller?.hide?.();}
    return Object.freeze({configure,open,setVisible,refresh:()=>active?.controller?.refresh?.(),destroy:dispose});
  }
  window.RocotoWorkspaceTabs=Object.freeze({register,create,has:(vertical,id)=>Boolean(verticals.get(vertical)?.[id])});
})();
