/* The selected vertical installs a fresh UI component for each workspace host. */
(() => {
  'use strict';
  let selected=null;
  function register(id,factory) {
    if(typeof id!=='string'||!/^[a-z][a-z0-9_-]{0,79}$/.test(id)||typeof factory!=='function')throw new Error('Register a vertical with its UI factory.');
    if(selected)throw new Error('A page serves one installed vertical.');
    selected={id,factory};
  }
  function install(context) {
    if(!context||typeof context!=='object')throw new Error('Supply the workspace host context.');
    if(!selected)return {};
    const instance=selected.factory(Object.freeze(context));
    if(!instance||typeof instance!=='object')throw new Error('Return the installed workspace component.');
    return instance;
  }
  window.RocotoWorkspaceExtensions=Object.freeze({register,install});
})();
