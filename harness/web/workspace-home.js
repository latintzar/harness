/* The default Home belongs to the harness; a vertical may supply its own tab. */
(() => {
  'use strict';
  function mount(root,context) {
    const page=document.createElement('section');page.className='workspace-home';
    const heading=document.createElement('h1');heading.textContent='Your workspace';
    const description=document.createElement('p');description.textContent='Work with Organization to set up your workspace, connect your tools and create agents.';
    const button=document.createElement('button');button.type='button';button.className='agent-primary';button.textContent='Open Organization';
    button.addEventListener('click',()=>context.openOrganization('organization'),{signal:context.signal});
    page.append(heading,description,button);root.replaceChildren(page);
    return ()=>page.remove();
  }
  window.RocotoWorkspaceHome=Object.freeze({mount});
})();
