/* Retail camera sources, setup and scene-context editing. */
(() => {
  'use strict';
  window.RocotoWorkspaceExtensions.register('retail', host => {
  let storageTimer = null, storageEpoch = 0, storagePending = null, storageReview = null;
  const storageWritable = item => host.customConnectorWritable(item);
  function storageCurrent(item, generation, detail) { return generation === host.sessionGeneration && detail === host.customDetailGeneration && host.currentView === 'marketplace' && host.marketplaceSelection?.id === item.id; }
  function resetStorageSetup() { ++storageEpoch; clearTimeout(storageTimer); storagePending = null; storageReview = null; }
  function storageError(error) { return ({sftp_host_key_mismatch:'Review the server key with your storage administrator.',sftp_authentication_failed:'Update the private login in the setup chat.',sftp_authority_changed:'Review the updated folder setup.',sftp_connection_failed:'Check the server address and access in the setup chat.',source_service_unavailable:'Your workspace owner can enable folder imports.',embedding_allowance_required:'Your workspace owner can add an import allowance.',embedding_allowance_reserved:'The current import allowance is reserved.',storage_runtime_unavailable:'Your workspace owner can enable folder imports.'})[typeof error === 'string' ? error : error?.code] || 'Review the folder setup in chat, then try again.'; }
  const storageMode = mode => mode === 'watch' ? 'Watch for new recordings' : 'Import once';
  function storageDate(value) { if (!value) return ''; const date = new Date(typeof value === 'number' ? value * 1000 : value); return Number.isFinite(date.getTime()) ? date.toLocaleString(undefined, {dateStyle:'medium',timeStyle:'short'}) : ''; }
  function storageStatus(setup) { return ({queued:'Waiting to start',running:'Importing recordings',waiting:'Watching folder',complete:'Folder checked',stopping:'Pausing imports…',needs_attention:'Needs attention',paused:'Paused'})[setup?.acquisition?.status] || ({configured:'Configured',paused:'Paused',review_required:'Review folder',setup:'Setup in progress'})[setup?.status] || 'Setup in progress'; }
  async function refreshStorageSetup(item) {
    clearTimeout(storageTimer); if (host.currentView !== 'marketplace' || host.marketplaceSelection?.id !== item.id || storagePending?.id === item.id) return;
    const generation = host.sessionGeneration, detail = host.customDetailGeneration, epoch = ++storageEpoch;
    try {
      const response = await fetch(`/api/retail/storage/${encodeURIComponent(item.id)}`, {signal:AbortSignal.timeout(10000)});
      if (!response.ok) throw new Error(); const setup = await response.json();
      let files = (item.storage_imports || []).filter(row => row.source_id === setup.acquisition?.source_id && row.collection === item.collection), filesReconnecting = false;
      if (setup.acquisition?.source_id) {
        const imports = await fetch(`/api/retail/library/imports?collection=${encodeURIComponent(item.collection)}`, {signal:AbortSignal.timeout(10000)});
        if (imports.ok) files = ((await imports.json()).imports || []).filter(row => row.source_id === setup.acquisition.source_id && row.collection === item.collection);
        else filesReconnecting = true;
      }
      if (!storageCurrent(item,generation,detail) || epoch !== storageEpoch) return;
      Object.assign(host.marketplaceSelection, {storage_setup:setup,storage_imports:files,storage_reconnecting:false,storage_files_reconnecting:filesReconnecting});
      if (storageReview?.id === item.id && (storageReview.proposal_id !== setup.proposal?.id || !setup.proposal?.current)) {
        const review = storageReview; storageReview = null;
        if (host.$('connection-setup-content').firstElementChild === review.content) host.$('connection-setup-dialog').close();
      }
      replaceStorageSetup(host.marketplaceSelection);
    } catch { if (storageCurrent(item,generation,detail) && epoch === storageEpoch) { host.marketplaceSelection.storage_reconnecting = true; replaceStorageSetup(host.marketplaceSelection); } }
    finally { if (storageCurrent(item,generation,detail) && epoch === storageEpoch) storageTimer = setTimeout(() => refreshStorageSetup(host.marketplaceSelection), 1500); }
  }
  function replaceStorageSetup(item) {
    const section = document.querySelector('.storage-setup'); if (!section) return;
    const focused = section.contains(document.activeElement) ? document.activeElement.dataset.storageAction : null, fragment = document.createDocumentFragment();
    renderStorageSetup(fragment,item); section.replaceWith(fragment);
    if (focused) document.querySelector(`.storage-setup [data-storage-action="${focused}"]`)?.focus({preventScroll:true});
  }
  async function storageAction(item, action, body) {
    if (!storageWritable(item) || storagePending) return;
    const generation = host.sessionGeneration, detail = host.customDetailGeneration, epoch = ++storageEpoch;
    const pending = {id:item.id,action,epoch}; storagePending = pending; item.storage_error = ''; replaceStorageSetup(item);
    const review = storageReview;
    if (action === 'approve' && review?.id === item.id && host.$('connection-setup-content').firstElementChild === review.content) host.$('connection-setup-content').querySelectorAll('button').forEach(button => { button.disabled = true; });
    try {
      const response = await fetch(`/api/retail/storage/${encodeURIComponent(item.id)}/${action}`, {method:'POST',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify(body),signal:AbortSignal.timeout(action === 'pause' ? 60000 : 30000)});
      const setup = await response.json();
      if (!storageCurrent(item,generation,detail) || epoch !== storageEpoch) return;
      if (!response.ok) throw new Error();
      item.storage_setup = setup; item.storage_reconnecting = false;
      if (action === 'approve' && review === storageReview && host.$('connection-setup-content').firstElementChild === review?.content) { storageReview = null; host.$('connection-setup-dialog').close(); }
    } catch { if (storageCurrent(item,generation,detail) && epoch === storageEpoch) { if (action === 'approve' && review === storageReview && host.$('connection-setup-content').firstElementChild === review?.content) { storageReview = null; host.$('connection-setup-dialog').close(); } item.storage_error = action === 'approve' ? 'Review the latest folder details and approve again.' : action === 'pause' ? 'Check the import status, then pause again if needed.' : 'Check the folder status before starting another import.'; } }
    finally {
      if (storagePending === pending) storagePending = null;
      if (storageCurrent(item,generation,detail) && epoch === storageEpoch) { replaceStorageSetup(item); refreshStorageSetup(item); }
      if (review === storageReview && review?.generation === host.sessionGeneration && host.$('connection-setup-content').firstElementChild === review.content) host.$('connection-setup-content').querySelectorAll('button').forEach(button => { button.disabled = !storageWritable(item); });
    }
  }
  function reviewStorageFolder(item, opener) {
    const setup = item.storage_setup, proposal = setup?.proposal;
    if (!storageWritable(item) || !proposal?.current || storagePending) return;
    const dialog = host.$('connection-setup-dialog'), content = host.$('connection-setup-content'), target = proposal.target;
    host.resetConnectionDialog(); content.replaceChildren(); dialog.setAttribute('aria-label','Review storage folder');
    const mark = host.node('span','custom-connector-mark'); mark.append(host.icon('folder')); content.append(mark,host.node('h2','','Review storage folder'),host.node('p','',proposal.reason));
    const facts = host.node('dl','connection-information storage-target-facts');
    for (const [label,value] of [['Server',target.host],['Port',String(target.port)],['Folder',target.root],['Import mode',storageMode(target.mode)]]) facts.append(host.node('dt','',label),host.node('dd','',value));
    const names = (item.secret_fields || []).filter(field => Object.values(target.authentication || {}).includes(field.name)).map(field => field.label || host.humanize(field.name));
    facts.append(host.node('dt','','Login'),host.node('dd','',names.join(', ') || 'Private login form')); content.append(facts);
    const key = host.node('section','storage-server-key'); key.append(host.node('h3','','Server identity'));
    if (proposal.host_key_fingerprint) key.append(host.node('code','storage-fingerprint',proposal.host_key_fingerprint));
    const details = host.node('details'), summary = host.node('summary','', 'Exact server key'); details.append(summary,host.node('code','storage-key-value',target.host_key)); key.append(details,host.node('p','workspace-help','Confirm this key with your storage administrator or the storage service’s published key.')); content.append(key);
    content.append(host.node('p','workspace-help','Approve this folder, then choose when to import its recordings.'));
    const actions = host.node('div','custom-access-actions'), cancel = host.agentButton('Cancel',() => dialog.close(),false,'close'), approve = host.agentButton('Approve folder',() => storageAction(item,'approve',{proposal_id:proposal.id}),true,'check'); actions.append(cancel,approve); content.append(actions);
    storageReview = {id:item.id,proposal_id:proposal.id,generation:host.sessionGeneration,content:mark};
    dialog.addEventListener('close',() => { if (storageReview?.content === mark) storageReview = null; if (opener?.isConnected) opener.focus(); },{once:true}); dialog.showModal(); approve.focus();
  }
  function renderStorageSetup(content,item) {
    if (item.kind !== 'sftp') return;
    const setup = item.storage_setup, section = host.node('section','connection-details-section storage-setup'); section.setAttribute('aria-label','Folder imports');
    section.append(host.node('h2','','Folder imports'));
    if (!setup) { section.append(host.node('p','workspace-help','Loading folder setup…')); content.append(section); clearTimeout(storageTimer); storageTimer = setTimeout(() => refreshStorageSetup(item),0); return; }
    const acquisition = setup.acquisition || {}, pending = storagePending?.id === item.id, target = setup.target;
    const status = host.node('p','storage-import-state',storageStatus(setup)); status.setAttribute('role','status'); status.prepend(host.icon(acquisition.status === 'running' ? 'refresh' : acquisition.status === 'waiting' ? 'clock' : 'folder')); section.append(status);
    if (target) { const facts = host.node('dl','connection-information storage-target-facts'); for (const [label,value] of [['Server',`${target.host}:${target.port}`],['Folder',target.root],['Import mode',storageMode(target.mode)]]) facts.append(host.node('dt','',label),host.node('dd','',value)); section.append(facts); }
    const actions = host.node('div','custom-access-actions');
    const add = (label, key, action, symbol, primary = false, enabled = true) => { const button = host.agentButton(label,action,primary,symbol); button.dataset.storageAction = key; button.disabled = !storageWritable(item) || pending || !enabled; actions.append(button); return button; };
    if (setup.proposal?.current) { const review = add('Review folder','review',() => reviewStorageFolder(item,review),'lock',true); }
    if (target && setup.status === 'configured' && !['queued','running','waiting','stopping','complete'].includes(acquisition.status)) add(target.mode === 'watch' ? 'Watch folder' : 'Import once','run',() => storageAction(item,'run',{expected_generation:setup.generation}),'play',true,setup.can_run === true);
    if (setup.can_pause || acquisition.status === 'stopping') add(acquisition.status === 'stopping' ? 'Retry pause' : 'Pause imports','pause',() => storageAction(item,'pause',{expected_generation:setup.generation}),acquisition.status === 'stopping' ? 'refresh' : 'pause',false,setup.can_pause === true);
    section.append(actions);
    if (acquisition.status === 'complete' && target?.mode === 'one_time') section.append(host.node('p','workspace-help','Continue setup to approve another import.'));
    if (target && setup.status === 'configured' && !setup.can_run && !['queued','running','waiting','stopping','complete'].includes(acquisition.status)) section.append(host.node('p','workspace-help',!setup.credentials_ready ? 'Complete the private login form in the setup chat.' : !setup.runtime_available ? 'Your workspace owner can enable folder imports.' : !setup.funded_ready ? 'Your workspace owner can add an import allowance.' : 'Refresh this folder’s setup to continue.'));
    if (setup.status === 'paused' || setup.status === 'review_required') section.append(host.node('p','workspace-help','Continue setup to review this folder and its import mode.'));
    if (acquisition.last_check) section.append(host.node('p','workspace-help',`Last checked ${storageDate(acquisition.last_check)}`));
    if (acquisition.status === 'waiting' && acquisition.next_check) section.append(host.node('p','workspace-help',`Next check ${storageDate(acquisition.next_check)}`));
    if (acquisition.error) section.append(host.node('p','storage-import-error',storageError(acquisition.error)));
    if (item.storage_error) section.append(host.node('p','storage-import-error',item.storage_error));
    if (item.storage_reconnecting) section.append(host.node('p','storage-import-error','Folder status is reconnecting. Your saved setup remains available.'));
    if (item.storage_files_reconnecting) section.append(host.node('p','storage-import-error','Recording progress is reconnecting. The last confirmed status stays visible.'));
    renderImportProgress(section,item,acquisition);
    content.append(section); clearTimeout(storageTimer); storageTimer = setTimeout(() => refreshStorageSetup(item),1500);
  }

  function renderImportProgress(section,item,acquisition){    const files = item.storage_imports || [];
    if (files.length) {
      const list = host.node('div','storage-import-files');
      for (const file of files.slice(0,10)) {
        const row = host.node('div','gallery-import'), copy = host.node('div','gallery-import-content'), heading = host.node('div','gallery-import-heading');
        const label = file.status === 'uploading' ? acquisition.status === 'running' ? 'Importing recording' : 'Waiting to continue' : uploadStatus(file);
        heading.append(host.node('strong','',file.filename),host.node('span','gallery-import-state',label)); copy.append(heading);
        if (file.expected_bytes > 0) { copy.append(host.node('p','gallery-import-bytes',`${host.fileSize(file.committed_bytes || 0)} of ${host.fileSize(file.expected_bytes)}`)); const progress = host.node('progress','gallery-import-progress'); progress.max = file.expected_bytes; progress.value = file.committed_bytes || 0; progress.setAttribute('aria-label',`${file.filename} transfer progress`); copy.append(progress); }
        row.append(host.icon('video'),copy); list.append(row);
      }
      section.append(list); const gallery = host.agentButton('View in Gallery',() => showLibrary(item.collection),false,'grid'); gallery.dataset.storageAction = 'gallery'; section.append(gallery);
    }
}
  const businessScopes = new Map(), chatLocationScopes = new Map();
  let chatLocationSignature = '';
  function resetBusinessScopes() { businessScopes.clear(); chatLocationScopes.clear(); chatLocationSignature = ''; host.$('chat-location-scope')?.replaceChildren(); }
  function businessScopeKey(collection) { return `${host.sessionGeneration}:${collection}`; }
  function businessScopeEntry(collection) { const key = businessScopeKey(collection); if (!businessScopes.has(key)) businessScopes.set(key, { data: null, error: '', loading: false, epoch: 0 }); return businessScopes.get(key); }
  async function scopeJSON(response) {
    if (!response.ok) { const error = new Error(response.status === 409 ? 'Locations changed. Reopen the editor to review the latest access.' : response.status === 403 ? 'Your current access allows viewing these locations.' : 'Locations are reconnecting. Try again.'); error.status = response.status; throw error; }
    return response.json();
  }
  async function readBusinessScope(collection, force = false) {
    const entry = businessScopeEntry(collection), generation = host.sessionGeneration;
    if (!force && entry.promise) return entry.promise;
    if (!force && entry.data) return entry.data;
    const epoch = ++entry.epoch; entry.loading = true; entry.error = '';
    const current = () => generation === host.sessionGeneration && businessScopes.get(businessScopeKey(collection)) === entry && epoch === entry.epoch;
    const promise = (async () => {
      try {
        const response = await fetch(`/api/retail/scope?collection=${encodeURIComponent(collection)}`), data = await scopeJSON(response);
        if (!current()) throw new Error('Open the current workspace to view its locations.');
        if (data.schema_version !== 1 || data.collection !== collection || !Array.isArray(data.locations)) throw new Error('Locations are reconnecting. Try again.');
        entry.data = data; return data;
      } catch (error) { if (current()) entry.error = error.message; throw error; }
      finally { if (current()) { entry.loading = false; entry.promise = null; } }
    })(); entry.promise = promise; return promise;
  }
  async function loadBusinessScope(collection = host.businessCollection) {
    const generation = host.sessionGeneration; try { await readBusinessScope(collection, true); } catch {} finally { if (generation === host.sessionGeneration && host.currentView === 'business' && collection === host.businessCollection) host.renderBusiness(); }
  }
  function businessScopeSurface(collection) {
    const entry = businessScopeEntry(collection), generation = host.sessionGeneration;
    return window.RocotoRetailScope.render({ data: entry.data, loading: entry.loading || !entry.data && !entry.error, error: entry.error, onRetry: () => loadBusinessScope(collection), onSave: async (kind, value, expected_revision) => {
      if (generation !== host.sessionGeneration || host.currentView !== 'business' || collection !== host.businessCollection || host.observing()) throw new Error('Open your workspace to manage its locations.');
      const response = await fetch(`/api/retail/scope/${encodeURIComponent(kind)}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Rocoto-Page': '1' }, body: JSON.stringify({ collection, expected_revision, value }) });
      let data;
      try { data = await scopeJSON(response); }
      catch (error) { if (error.status === 409 && generation === host.sessionGeneration) { try { error.scopeGraph = await readBusinessScope(collection, true); } catch {} } throw error; }
      if (generation !== host.sessionGeneration || host.currentView !== 'business' || collection !== host.businessCollection) throw new Error('Open the current business to review its locations.');
      entry.data = data; entry.error = ''; chatLocationScopes.clear(); chatLocationSignature = ''; return data;
    } }, { node:host.node });
  }
  function chatLocationKey(chat) { return `${host.sessionGeneration}:${chat.id}:${chat.dataset || 'company'}`; }
  async function readChatLocations(chat, force = false) {
    const key = chatLocationKey(chat), generation = host.sessionGeneration;
    let entry = chatLocationScopes.get(key);
    if (!entry) { entry = { data: null, graph: null, error: '', loading: false, epoch: 0 }; chatLocationScopes.set(key, entry); }
    if (!force && entry.promise) return entry.promise;
    if (!force && entry.data) return entry;
    const epoch = ++entry.epoch; entry.loading = true; entry.error = '';
    const current = () => generation === host.sessionGeneration && chatLocationScopes.get(key) === entry && epoch === entry.epoch;
    const promise = (async () => {
      try {
        const [graph, response] = await Promise.all([readBusinessScope(chat.dataset || 'company', force), fetch(`/api/retail/chats/${encodeURIComponent(chat.id)}/scope`)]), data = await scopeJSON(response);
        if (!current()) throw new Error('Open the current chat to choose its locations.');
        if (!Array.isArray(data.location_ids) || !Number.isInteger(data.selection_revision)) throw new Error('Chat locations are reconnecting. Try again.');
        entry.data = data; entry.graph = graph; return entry;
      } catch (error) { if (current()) entry.error = error.message; throw error; }
      finally { if (current()) { entry.loading = false; entry.promise = null; } }
    })(); entry.promise = promise; return promise;
  }
  function renderChatLocations() {
    const target = host.$('chat-location-scope'), chat = host.activeChat(); if (!target) return;
    if (host.currentView !== 'chat' || host.homeMode !== 'chat' || !chat?.server_saved || chat.parent_id || !host.session?.authenticated) { target.replaceChildren(); target.hidden = true; chatLocationSignature = ''; return; }
    const key = chatLocationKey(chat); let entry = chatLocationScopes.get(key);
    if (!entry) { readChatLocations(chat).catch(() => {}).finally(() => { if (key === chatLocationKey(chat) && host.activeId === chat.id && host.currentView === 'chat') renderChatLocations(); }); entry = chatLocationScopes.get(key); }
    if (!entry?.graph?.locations?.length || !entry.data) { target.replaceChildren(); target.hidden = true; return; }
    target.hidden = false;
    const signature = JSON.stringify([key, entry.data, entry.graph.revision, entry.error, host.isBusy(chat.id), host.observing(), chat.read_only]); if (signature === chatLocationSignature) return; chatLocationSignature = signature;
    const generation = host.sessionGeneration, current = () => generation === host.sessionGeneration && host.currentView === 'chat' && host.activeId === chat.id && chatLocationKey(host.activeChat()) === key;
    target.replaceChildren(window.RocotoRetailScope.renderSelector({ data: entry.data, locations: entry.graph.locations, busy: host.isBusy(chat.id), readOnly: host.observing() || chat.read_only === true, error: entry.error,
      onOpen: async () => { const fresh = await readChatLocations(chat, true); if (!current()) throw new Error('Open the current chat to choose its locations.'); return { data: fresh.data, locations: fresh.graph.locations }; },
      onRetry: async () => { try { await readChatLocations(chat, true); } catch {} if (current()) { chatLocationSignature = ''; renderChatLocations(); } },
      onSave: async (location_ids, expected_revision, expected_selection_revision) => {
        if (!current() || host.observing() || host.isBusy(chat.id)) throw new Error('Choose locations when this response finishes.');
        const response = await fetch(`/api/retail/chats/${encodeURIComponent(chat.id)}/scope`, { method: 'PUT', headers: { 'Content-Type': 'application/json', 'X-Rocoto-Page': '1' }, body: JSON.stringify({ location_ids, expected_revision, expected_selection_revision }) }), data = await scopeJSON(response);
        if (!current()) throw new Error('Open the current chat to review its locations.'); entry.data = data; entry.error = ''; return data;
      }
    }, { node:host.node }));
  }
    const scopeLabel = (value) => (host.demoEnvironment() && host.status?.demo_guidance?.[canonicalScope(value)]?.name) || ({ cafe: 'Café', warehouse: 'Warehouse', 'steel-process': 'Factory' }[value] || datasetLabel(value));
    const canonicalScope = (value) => value === 'cafe-preview' ? 'cafe' : value === 'warehouse-026' ? 'warehouse' : value;
    const datasetLabel = (value) => ({ 'smartspaces-retail': 'Synthetic retail', 'cafe-preview': 'Staged café previews', 'cafe': 'Staged cafés', 'steel-process': 'Steel rolling process', 'warehouse': 'Warehouse', 'warehouse-026': 'Warehouse' }[value] || host.humanize(value));
    function sourceCollections(status) { const corpus = status?.corpus || []; return [...new Set(corpus.map((video) => host.demoEnvironment() ? canonicalScope(video.dataset) : video.dataset).filter(Boolean))].map((id) => ({ id, label: host.demoEnvironment() ? scopeLabel(id) : datasetLabel(id), videos: corpus.filter((video) => (host.demoEnvironment() ? canonicalScope(video.dataset) : video.dataset) === id) })); }
    const homeSearches=new Map();
    let CLIP_DRAFTS_KEY='rocoto-video-clip-drafts-v1',clipDrafts=new Map();
    try{clipDrafts=new Map(JSON.parse(localStorage.getItem(CLIP_DRAFTS_KEY)||'[]').filter(([id,items])=>host.chats.some(chat=>chat.id===id)&&Array.isArray(items)));}catch{}
const videoCount = (videos) => `${videos.length} ${videos.length === 1 ? 'video' : 'videos'} · ${Math.round(videos.reduce((sum, video) => sum + (Number(video.duration) || 0), 0) / 60)} min`;
const normalizeClips = (clips) => (clips || []).map((clip) => typeof clip === 'string' ? { id: clip } : clip).filter((clip) => clip?.id);
function preciseTime(value) { const milliseconds = Math.max(0, Math.round((Number(value) || 0) * 1000)), whole = Math.floor(milliseconds / 1000), fraction = milliseconds % 1000; return `${host.time(whole)}${fraction ? `.${String(fraction).padStart(3, '0').replace(/0+$/, '')}` : ''}`; }
function toolFramePreviews(tool) { return /^(?:mcp__rocoto__)?inspect_clip$/.test(tool?.name) ? window.RocotoToolTrail.normalizeFramePreviews(tool.frame_previews) : []; }
function saveClipDrafts() { host.persistDraft(); try { localStorage.setItem(CLIP_DRAFTS_KEY, JSON.stringify([...clipDrafts])); } catch { host.notice('Selected clips remain available in this open page.'); } }

        const fragments = [
      ["chat-context", '<div id="chat-location-scope" hidden></div>'],
      ["source-summary", "<details class=\"corpus-details\" hidden><summary><svg class=\"icon\"><use href=\"#i-video\"/></svg><span id=\"corpus-summary\">Video collection</span></summary><div id=\"corpus-list\"></div></details>"],
      ["agent-input-options", "<label class=\"agent-field\" for=\"agent-fresh-inspections\"><span>Fresh Cosmos inspections per run</span><select id=\"agent-fresh-inspections\"><option value=\"1\">1 inspection</option><option value=\"0\">Existing observations</option></select></label>"],
      ["agent-resource-estimate", "<p id=\"agent-credit-estimate\" class=\"agent-credit-estimate\"></p>"],
      ["source-library", `<section id="library-view" class="library-view" aria-label="Gallery" hidden>

        <header class="library-heading gallery-heading"><div class="gallery-title-row"><h1>Gallery</h1><span id="gallery-company-picker"></span><p id="gallery-description">Original files and their indexed intervals.</p></div>        <nav class="gallery-tabs" aria-label="Gallery content"><button id="gallery-media-tab" type="button" aria-pressed="true"><svg class="icon"><use href="#i-images"/></svg>Media</button><button id="workspace-files-tab" type="button" aria-pressed="false"><svg class="icon"><use href="#i-document"/></svg>Files</button></nav><div class="dialog-header-actions"><label class="workspace-file-search" id="workspace-file-search-wrap" hidden><svg class="icon"><use href="#i-search"/></svg><input id="workspace-file-search" type="search" aria-label="Search files" placeholder="Search files" /></label><button id="gallery-connect-storage" class="agent-secondary" type="button" disabled><svg class="icon"><use href="#i-at"/></svg>Connect storage</button><button id="gallery-upload" class="agent-secondary" type="button" disabled><svg class="icon"><use href="#i-upload"/></svg>Upload files</button><input id="gallery-upload-input" type="file" accept="video/*,.mkv,.avi,.mov,.mp4,.webm,.mts,.m2ts" multiple hidden /><div id="library-collection-control" class="collection-control"></div>        <div class="gallery-view-toggle" role="group" aria-label="Gallery view"><button type="button" data-gallery-view="grid" aria-pressed="true" aria-label="Grid view"><svg class="icon"><use href="#i-grid"/></svg></button><button type="button" data-gallery-view="list" aria-pressed="false" aria-label="List view"><svg class="icon"><use href="#i-list"/></svg></button></div>
        <form id="library-search" class="library-search"><label class="sr-only" for="library-search-query">Search footage in this collection</label><input id="library-search-query" type="search" placeholder="Search this collection…" maxlength="4000" autocomplete="off" /><button id="library-search-submit" class="agent-primary" type="submit">Search</button></form>
</div></header>
        <div class="gallery-upload-notice"><p id="gallery-upload-status" role="status" hidden></p><button id="gallery-upload-retry" class="text-button" type="button" hidden><svg class="icon"><use href="#i-refresh"/></svg>Check again</button></div>
        <section id="gallery-imports" class="gallery-imports" aria-label="Uploads" hidden><header><h2>Uploads</h2><span id="gallery-import-count"></span></header><ul id="gallery-import-list"></ul></section>
        <nav id="library-breadcrumbs" class="library-breadcrumbs" aria-label="Gallery folders"></nav>
        <div id="workspace-message" class="workspace-message" role="status"></div>
        <section id="generated-files" class="generated-files" aria-label="Created files" hidden></section>
        <div id="library-layout" class="library-layout"><section id="library-files" class="library-files" aria-label="Source files"></section><aside id="library-asset" class="library-asset" aria-label="Source file details" hidden></aside></div>
      </section>`],
      ["source-context", `<aside id="gallery-context" class="context-nav" aria-label="Gallery collections" hidden><span>Gallery</span><div id="gallery-context-sources"></div></aside>`],
      ["source-shortcut", `<button id="library-shortcut" hidden disabled class="text-button library-shortcut" type="button"><svg class="icon"><use href="#i-images"/></svg>Gallery</button>`],
      ["source-attachment-dialog", `<dialog id="clip-picker" class="clip-picker" aria-labelledby="clip-picker-title">
      <header class="dialog-header"><div><h2 id="clip-picker-title">Video from Gallery</h2><p id="clip-picker-scope"></p></div><button id="close-clip-picker" class="icon-button" type="button" aria-label="Close video picker"><svg class="icon"><use href="#i-close"/></svg></button></header>
      <div class="gallery-view-toggle" role="group" aria-label="Gallery view"><button type="button" data-gallery-view="grid" aria-pressed="true" aria-label="Grid view"><svg class="icon"><use href="#i-grid"/></svg></button><button type="button" data-gallery-view="list" aria-pressed="false" aria-label="List view"><svg class="icon"><use href="#i-list"/></svg></button></div>
      <form id="clip-picker-search" hidden class="library-search"><label class="sr-only" for="clip-picker-query">Search clips</label><input id="clip-picker-query" type="search" placeholder="Search clips…" maxlength="4000" /><button id="clip-picker-search-submit" class="agent-secondary" type="submit">Search</button></form>
      <div id="clip-picker-message" class="workspace-help" role="status"></div><nav id="clip-picker-breadcrumbs" class="library-breadcrumbs" aria-label="Source folders"></nav><div id="clip-picker-content" class="clip-picker-content"></div>
      <footer class="clip-picker-footer"><span id="clip-picker-count">0 clips selected</span><button id="add-picker-clips" class="agent-primary" type="button">Add clips</button></footer>
    </dialog>`],
      ["source-attachment-action", `<button id="attach-video" type="button"><svg class="icon"><use href="#i-video"/></svg>Video from Gallery</button>`],
      ["source-attachments", `<section id="clip-attachment-previews" class="clip-attachment-previews" aria-label="Video attachments" hidden></section>`],
      ["source-upload-dialog", `<dialog id="source-upload-dialog" class="source-upload-dialog" aria-labelledby="source-upload-title"><header><h2 id="source-upload-title">Upload files</h2><button id="close-source-upload" class="icon-button" type="button" aria-label="Close upload"><svg class="icon"><use href="#i-close"/></svg></button></header><div id="upload-dropzone" class="upload-dropzone" aria-disabled="true" aria-describedby="source-upload-status"><svg class="icon"><use href="#i-upload"/></svg><strong id="upload-drop-label">Checking availability…</strong><p>Videos up to 256 MiB · 4 files at a time</p><button id="upload-choose" class="agent-secondary" type="button"><svg class="icon"><use href="#i-upload"/></svg>Choose files</button></div><form id="source-link-form"><label for="source-link-input">Or paste a link</label><div class="source-link-input-row"><input id="source-link-input" type="url" placeholder="Google Drive, Dropbox, WeTransfer or a file link" autocomplete="off" maxlength="4096" required /><button id="source-link-submit" class="agent-primary" type="submit"><svg class="icon"><use href="#i-link"/></svg>Add link</button></div></form><p id="source-upload-status" class="workspace-help" role="status"></p><div id="source-link-list" class="source-link-list"></div><div id="upload-progress"></div></dialog>`],
      ["source-results-dialog", `<dialog id="gallery-dialog" class="gallery-dialog" aria-labelledby="gallery-title"><header class="clip-dialog-header"><h2 id="gallery-title">Video evidence</h2><div class="gallery-view-toggle" role="group" aria-label="Gallery view"><button type="button" data-gallery-view="grid" aria-pressed="true" aria-label="Grid view"><svg class="icon"><use href="#i-grid"/></svg></button><button type="button" data-gallery-view="list" aria-pressed="false" aria-label="List view"><svg class="icon"><use href="#i-list"/></svg></button></div><button id="close-gallery" class="icon-button" aria-label="Close gallery"><svg class="icon"><use href="#i-close"/></svg></button></header><div id="gallery-grid" class="gallery-grid"></div><button id="gallery-more" class="gallery-more text-button" type="button" hidden>Load more clips</button></dialog>`],
      ["source-preview-dialog", `<dialog id="clip-dialog" class="clip-dialog" aria-labelledby="clip-dialog-title">
      <header class="clip-dialog-header"><div><h2 id="clip-dialog-title">Video evidence</h2><p id="clip-dialog-meta"></p></div><button id="close-clip" class="icon-button" aria-label="Close video"><svg class="icon"><use href="#i-close"/></svg></button></header>
      <div class="clip-dialog-body">
        <div class="clip-player-area">
          <div id="clip-stage" class="clip-stage"><video id="clip-player" controls controlslist="nofullscreen" playsinline preload="metadata"></video><canvas id="focus-overlay" aria-hidden="true" hidden></canvas></div>
          <div class="focus-controls"><div class="focus-toggle" role="group" aria-label="Video appearance"><button id="show-original" type="button" aria-pressed="true">Original</button><button id="show-focus" type="button" aria-pressed="false" disabled>Focus</button></div><span id="focus-status">Choose a subject in chat to track it.</span></div>
          <div class="clip-player-controls"><button id="previous-clip" class="text-button"><svg class="icon previous-icon"><use href="#i-chevron"/></svg>Previous</button><span id="clip-position"></span><button id="next-clip" class="text-button">Next<svg class="icon"><use href="#i-chevron"/></svg></button><button id="fullscreen-clip" class="icon-button" aria-label="Open video fullscreen"><svg class="icon"><use href="#i-fullscreen"/></svg></button></div>
          <p id="clip-interval-note" class="clip-interval-note"></p>
        </div>
        <details id="clip-metadata-drawer" class="clip-metadata-drawer"><summary><svg class="icon"><use href="#i-list"/></svg>Details<svg class="icon"><use href="#i-chevron"/></svg></summary><aside id="clip-details-sidebar" class="clip-observations" aria-label="Clip details and analysis">
          <section class="clip-source-section"><div class="clip-download-actions"><a id="download-clip" class="clip-download-primary" download><svg class="icon"><use href="#i-download"/></svg>Download clip</a><a id="download-manifest" class="clip-download-manifest" download><svg class="icon"><use href="#i-document"/></svg>Manifest</a></div><h3>Source</h3><dl id="clip-source-details" class="clip-source-details"></dl><a id="clip-show-library" class="source-reference" hidden>Show in gallery</a><a id="clip-source-reference" class="source-reference" target="_blank" rel="noopener noreferrer" hidden>Source reference</a><details class="clip-identifiers"><summary>Identifiers</summary><dl id="clip-identifiers" class="clip-source-details"></dl></details></section>
          <section class="clip-connections-section"><details id="clip-connections"><summary>Connections <span id="clip-connection-count"></span></summary><div id="clip-relation-list" class="clip-relation-list"></div></details></section>
          <section><h3>Model observations</h3><p class="observation-explanation">Scene context describes the setting. Visible events describe changes over time. Each observation answers the question shown.</p><div id="clip-model-description" class="observation-text"></div></section>
          <section><h3>Chat answer</h3><p class="observation-explanation">Codex’s interpretation in the conversation that selected this clip.</p><div id="clip-interpretation" class="observation-text"></div></section>
          <section class="clip-structure"><div class="structure-placeholder"><h3>Entities</h3><span id="clip-entities-status" class="development-status">In development</span><div id="clip-entities" class="structure-empty">Entity occurrences</div></div><div class="structure-placeholder"><h3>Room</h3><span id="clip-room-status" class="development-status">In development</span><div id="clip-room" class="structure-empty">Room assignment</div></div></section>
        </aside></details>
      </div>
    </dialog>`],
      ["source-view", `<section id="cameras-view" class="cameras-view" aria-label="Cameras" hidden>
        <div class="camera-toolbar"><label class="collection-search"><svg class="icon"><use href="#i-search"/></svg><input id="camera-search" type="search" placeholder="Search cameras" aria-label="Search cameras" /></label><button id="camera-upload" class="agent-secondary" type="button"><svg class="icon"><use href="#i-upload"/></svg>Upload files</button><button id="review-cameras" class="agent-secondary review-cameras" type="button"><svg class="icon"><use href="#i-video"/></svg>Review cameras</button><button id="add-cameras" class="agent-primary" type="button"><svg class="icon"><use href="#i-plus"/></svg>Add cameras</button></div>
        <header class="library-heading"><div><p>Source views, recordings and confirmed scene context.</p></div><div id="camera-collection-control" class="collection-control"></div></header>
        <nav id="camera-tabs" class="camera-tabs" aria-label="Camera source type"><button id="camera-streams-tab" type="button" aria-pressed="false">Streams</button><button id="camera-recordings-tab" type="button" aria-pressed="true">Recordings</button></nav><div id="camera-streams-content" hidden></div>
        <p id="camera-message" class="workspace-message" role="status"></p>
        <div id="camera-layout" class="camera-layout"><nav id="camera-list" class="camera-list" aria-label="Source views"></nav><article id="camera-detail" class="camera-detail" aria-label="Camera details"></article></div>
      </section>`],
      ["source-dialog", `<dialog id="camera-provider-dialog" class="camera-provider-dialog" aria-labelledby="camera-provider-title" aria-describedby="camera-provider-description"><header class="dialog-header"><div><h2 id="camera-provider-title">Connect cameras</h2><p id="camera-provider-description">Choose your camera system. We’ll walk through setup and connection checks in chat.</p></div><button id="close-camera-provider" class="icon-button" type="button" aria-label="Close camera providers"><svg class="icon"><use href="#i-close"/></svg></button></header><div id="camera-provider-options" class="camera-provider-options"></div><details id="camera-provider-more" class="camera-provider-more"><summary><svg class="icon"><use href="#i-video"/></svg>More camera systems<svg class="icon"><use href="#i-chevron"/></svg></summary><div id="camera-provider-more-options" class="camera-provider-options"></div></details><section class="camera-provider-method"><h3>Connection method</h3><div id="camera-provider-methods"></div></section><p id="camera-provider-error" class="agent-form-error" role="alert" hidden></p><footer><button id="camera-provider-other" class="agent-secondary" type="button"><svg class="icon"><use href="#i-chat"/></svg>None of these<svg class="icon"><use href="#i-chevron"/></svg></button><span>Identify your system together in chat.</span></footer></dialog>`],
      ["source-nav", `<button id="cameras-nav" class="agents-nav cameras-nav" type="button" hidden><svg class="icon"><use href="#i-video"/></svg><span>Cameras</span></button>`],
      ["source-settings", `<button id="settings-cameras" type="button" data-settings-group="Organization" data-settings-after="stats" data-settings-icon="video">Cameras</button>`]
    ];
    for(const [slot,markup] of fragments){
      const anchor=document.querySelector(`[data-workspace-slot="${slot}"]`);
      if(anchor){const template=document.createElement('template');template.innerHTML=markup;anchor.replaceWith(template.content);}
    }

let cameras = [], cameraDetail = null, cameraCollection = new URL(location.href).searchParams.get('collection') || '', cameraId = new URL(location.href).searchParams.get('camera') || '', cameraLoading = false, cameraError = '', cameraGeneration = 0, cameraEnvironment = null;

let cameraProviderOpener = null, cameraProviderCollection = '', cameraProviderCreating = false;

let cameraTab=new URL(location.href).searchParams.get('camera_tab')==='streams'?'streams':'recordings',cameraStreams=[],cameraStreamsLoading=false,cameraStreamsError='',cameraStreamsEpoch=0;

const cameraProviders = [
    { id: 'hikvision', name: 'Hikvision', summary: 'Cameras and NVRs', logo: 'hikvision.png', reference: 'https://supportusa.hikvision.com/support/solutions/articles/17000129064-how-do-i-get-my-rtsp-stream-', route: 'Identify the Hikvision camera or NVR and firmware, then check selected channels, ONVIF and RTSP through an authorized local bridge.' },
    { id: 'dahua', name: 'Dahua', summary: 'Cameras and NVRs', logo: 'dahua.png', reference: 'https://dahuawiki.com/Remote_Access/RTSP_via_VLC', route: 'Identify the Dahua camera or NVR, then check selected channels, RTSP and model-specific ONVIF or playback access through an authorized local bridge.' },
    { id: 'axis', name: 'Axis', summary: 'Cameras and recorders', logo: 'axis.png', reference: 'https://developer.axis.com/vapix/network-video/', route: 'Identify the Axis model and recorder, then check VAPIX, ONVIF and local RTSP capabilities through an authorized local bridge.' },
    { id: 'hanwha', name: 'Hanwha Vision', summary: 'Cameras and recorders', logo: 'hanwha.ico', reference: 'https://supportportal.hanwhavision.com/global/products/XNO-6085R-en', route: 'Identify the Hanwha camera or recorder and firmware. Check its documented SUNAPI, ONVIF profiles and RTSP access for the exact model through an authorized local bridge.' },
    { id: 'uniview', name: 'Uniview', summary: 'Cameras and NVRs', logo: 'uniview.svg', reference: 'https://www.uniview.com/Support/Download_Center/SDK/', route: 'Identify the Uniview camera or NVR and firmware. Use its official device SDK and model-specific RTSP documentation to check selected channels and recording retrieval through an authorized local bridge.' },
    { id: 'vigi', name: 'TP-Link VIGI', summary: 'Cameras and NVRs', logo: 'vigi.svg', reference: 'https://www.tp-link.com/us/support/faq/4201/', route: 'Identify the VIGI camera or NVR and firmware. Check documented ONVIF and RTSP capabilities for the exact model through an authorized local bridge; establish recording-history access separately.' },
    { id: 'unifi', group: 'more', name: 'UniFi Protect', summary: 'Protect consoles', logo: 'unifi.png', reference: 'https://developer.ui.com/protect', route: 'Identify the Protect console and supported Integration API. Verify authorization and stream reachability separately from recording export.' },
    { id: 'eagle-eye', group: 'more', name: 'Eagle Eye Networks', summary: 'Cloud video systems', logo: 'eagle-eye.ico', reference: 'https://developer.eagleeyenetworks.com/reference/using-the-api', route: 'Identify the authorized Eagle Eye account and its region, then explain the official delegated application or machine-to-machine route and available media access.' },
    { id: 'verkada', group: 'more', name: 'Verkada', summary: 'Cloud-managed cameras', logo: 'verkada.png', reference: 'https://apidocs.verkada.com/reference/quick-start-guide', route: 'Identify the Verkada organization and selected cameras, then check scoped API authorization and separate streaming permissions.' },
    { id: 'bosch', group: 'more', name: 'Bosch', summary: 'Cameras and video systems', logo: 'bosch.svg', reference: 'https://cdn.commerce.boschsecurity.com/public/documents/BVMS_11.1.1_Configuration_Manual_enUS_92198062091.pdf', route: 'Identify the Bosch camera, recorder or BVMS system and version. Check the supported local integration and selected streams using its model-specific documentation; verify retained recordings separately.' },
    { id: 'avigilon', group: 'more', name: 'Avigilon', summary: 'Unity and Alta systems', logo: 'avigilon.svg', reference: 'https://www.avigilon.com/fs/documents/avigilon-camera-web-interface-user-guide-en.pdf', route: 'Identify whether the installation uses Avigilon Unity or Alta and the exact camera model. Choose its documented local or cloud integration, with separate authorization and recording-access checks.' },
    { id: 'onvif', group: 'method', name: 'ONVIF / RTSP', summary: 'Compatible cameras and recorders', logo: 'onvif.png', reference: 'https://www.onvif.org/profiles/profile-g/', route: 'Identify the actual camera or recorder and supported ONVIF profiles or RTSP stream. Verify live access and retained recording access separately through an authorized local bridge.' },
  ];

function openCameraProviderPicker(opener, collection = '') {
    if (host.observing() || !host.session?.can_query) return;
    cameraProviderOpener = opener || document.activeElement; cameraProviderCollection = collection || (host.currentView === 'cameras' ? cameraCollection : host.currentView === 'business' ? host.businessCollection : host.activeChat()?.dataset) || 'company';
    const options = host.$('camera-provider-options'), more = host.$('camera-provider-more-options'), methods = host.$('camera-provider-methods'); for (const container of [options, more, methods]) container.replaceChildren(); host.$('camera-provider-more').open = false; host.$('camera-provider-dialog').scrollTop = 0; host.$('camera-provider-error').hidden = true;
    for (const provider of cameraProviders) { const button = host.node('button', 'camera-provider'), brand = host.node('span', 'camera-provider-brand'), image = host.node('img'), copy = host.node('span', 'camera-provider-copy'); button.type = 'button'; image.src = `/static/retail/assets/camera-providers/${provider.logo}`; image.alt = ''; image.decoding = 'async'; brand.append(image); copy.append(host.node('strong', '', provider.name), host.node('small', '', provider.summary)); button.append(brand, copy, host.icon('chevron')); button.addEventListener('click', () => startCameraSetup(provider)); (provider.group === 'more' ? more : provider.group === 'method' ? methods : options).append(button); }
    host.$('camera-provider-other').disabled = false; host.$('camera-provider-dialog').showModal(); options.querySelector('button')?.focus();
  }

function openConnectionEditor(event) { openCameraProviderPicker(event?.currentTarget || document.activeElement, host.businessCollection); }

async function startCameraSetup(provider = null) {
    if(cameraProviderCreating||host.observing()||!host.session?.can_query)return;cameraProviderCreating=true;
    try{host.$('camera-provider-dialog').close();const chat=await host.openOrganizationChat(host.fixedPurpose('cameras'),cameraProviderCollection||host.currentCollection());if(chat&&provider){host.elements.message.value=`Help me connect my ${provider.name} cameras. ${provider.route} Official reference: ${provider.reference}`;host.resizeComposer();host.persistDraft();host.elements.message.focus();}}
    finally{cameraProviderCreating=false;}
  }

function agentUsesCameraInput(agent) { return host.harness.surfaces.some(surface => surface.id === 'cameras') && ['camera_footage', 'tracker_events'].includes(agent?.input_kind || 'camera_footage'); }

function showCameras(collection = null, id = '') {
    host.persistDraft(); clearTimeout(host.agentsTimer); host.currentView = 'cameras'; const nextCollection = collection ?? (cameraCollection || host.currentCollection()); if (cameraCollection !== nextCollection) cameras = []; cameraCollection = nextCollection; cameraId = id; cameraDetail = null; cameraError = '';
    const url = new URL('/', location.href); url.searchParams.set('cameras', '1'); if (cameraCollection) url.searchParams.set('collection', cameraCollection); if (id) url.searchParams.set('camera', id);if(cameraTab==='streams'&&!id)url.searchParams.set('camera_tab','streams'); history.replaceState({}, '', url); host.render(); host.setSidebar(false); loadCameras();
  }

async function reviewCameras(ids) { if (host.observing()) return; if (!host.session?.can_query) { host.showSession('Enter your session code to review cameras.'); return; } try { const response = await host.request('/api/retail/cameras/review', { collection: cameraCollection, camera_ids: ids }), data = await response.json(); const chat = data.chat || data; if (chat.id) { host.mergeRemoteChat(chat); await host.openChatId(chat.id); } } catch (error) { host.notice(error.message); } }

function chooseCameraReview() { if (!cameras.length) return; const dialog = host.node('dialog', 'camera-review-dialog'), heading = host.node('h2', '', 'Review cameras'), close = host.agentButton('Close', () => dialog.close(), false, 'close'), list = host.node('div', 'camera-review-options'), selected = new Set(); const run = host.agentButton('Review selected cameras', async () => { dialog.close(); await reviewCameras([...selected]); }, true, 'video'); run.disabled = true; for (const camera of [...cameras].sort((a, b) => host.sourceNameOrder.compare(a.name || a.id, b.name || b.id))) { const label = host.node('label'), input = host.node('input'); input.type = 'checkbox'; input.value = camera.id; input.addEventListener('change', () => { input.checked ? selected.add(camera.id) : selected.delete(camera.id); run.disabled = !selected.size; list.querySelectorAll('input').forEach((item) => { item.disabled = selected.size >= 4 && !item.checked; }); }); label.append(input, host.node('span', '', camera.name || camera.id)); list.append(label); } dialog.append(close, heading, host.node('p', 'workspace-help', 'Select up to 4 source views for a scene-context review.'), list, run); document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove(), { once: true }); dialog.showModal(); }

function setCameraTab(value){cameraTab=value;cameraId='';cameraDetail=null;const url=new URL(location.href);url.searchParams.set('camera_tab',value);url.searchParams.delete('camera');history.replaceState({},'',url);renderCameras();if(value==='streams')loadCameraStreams();}

async function loadCameraStreams(){
    const epoch=++cameraStreamsEpoch,generation=host.sessionGeneration,collection=cameraCollection||'company';cameraStreamsLoading=true;cameraStreamsError='';renderCameras();
    try{const response=await fetch(`/api/business?collection=${encodeURIComponent(collection)}`);if(!response.ok)throw new Error('Camera connections are reconnecting. Try again.');const data=await response.json();if(epoch!==cameraStreamsEpoch||generation!==host.sessionGeneration||(cameraCollection||'company')!==collection)return;cameraStreams=Array.isArray(data.connections)?data.connections.filter(item=>item.type==='camera_stream'):[];}
    catch(error){if(epoch===cameraStreamsEpoch&&generation===host.sessionGeneration)cameraStreamsError=error.message;}
    finally{if(epoch===cameraStreamsEpoch&&generation===host.sessionGeneration){cameraStreamsLoading=false;if(host.currentView==='cameras')renderCameras();}}
  }

function cameraSetup(connection,opener){if(!connection){openCameraProviderPicker(opener,cameraCollection);return;}if(host.observing())return;const chat=host.newChat();chat.dataset=cameraCollection||'company';host.elements.message.value=`Help me finish setting up the camera connection “${connection.provider||'Camera'}” (connection ID ${connection.id}).`;host.resizeComposer();host.save();host.render();host.elements.message.focus();}

function recordingReferences(){return cameras.filter(camera=>camera.reference_clip?.id).map(camera=>({...camera.reference_clip,camera:camera.name,dataset:camera.collection||cameraCollection}));}

async function loadCameras() {
    const generation = ++cameraGeneration, environment = host.session?.environment?.id; if (!environment || host.currentView !== 'cameras') return;
    if (cameraEnvironment && cameraEnvironment !== environment) { cameraId = ''; cameras = []; cameraDetail = null; } cameraEnvironment = environment; cameraLoading = Boolean(cameraCollection); cameraError = ''; renderCameras();if(cameraTab==='streams')loadCameraStreams(); if (!cameraCollection || !host.scopes().some(item=>item.id===cameraCollection)) {cameras=[];cameraLoading=false;renderCameras();return;}
    try { const response = await fetch(`/api/retail/cameras?collection=${encodeURIComponent(cameraCollection)}`); if (!response.ok) throw new Error('Camera views are reconnecting. Retry to load the collection.'); const data = await response.json(); if (generation !== cameraGeneration || environment !== host.session?.environment?.id) return; cameras = data.cameras || [];
      if (cameraId) { const response = await fetch(`/api/retail/cameras/${encodeURIComponent(cameraId)}?collection=${encodeURIComponent(cameraCollection)}`); if (!response.ok) throw new Error('Choose a source view from this collection.'); const data = await response.json(); if (generation !== cameraGeneration || environment !== host.session?.environment?.id) return; cameraDetail = data.camera || data; }
    } catch (error) { if (generation === cameraGeneration) cameraError = error.message; }
    finally { if (generation === cameraGeneration) { cameraLoading = false; if (host.currentView === 'cameras') renderCameras(); } }
  }

function renderCameras() { host.collectionNavigation();const streams=cameraTab==='streams'&&!cameraId;host.$('camera-tabs').hidden=Boolean(cameraId);host.$('camera-streams-tab').setAttribute('aria-pressed',String(streams));host.$('camera-recordings-tab').setAttribute('aria-pressed',String(!streams));host.$('camera-streams-content').hidden=!streams;host.$('camera-layout').hidden=streams;host.$('review-cameras').hidden=streams;document.querySelector('.camera-toolbar').hidden=Boolean(cameraId);document.querySelector('#cameras-view > .library-heading').hidden=Boolean(cameraId);host.$('camera-upload').disabled=!host.pluginWritable();if(cameraDetail)host.detailBreadcrumb('Cameras',cameraDetail.name || cameraDetail.id,()=>showCameras(cameraCollection)); host.$('add-cameras').disabled = host.observing() || !host.session?.can_query;
    host.$('review-cameras').disabled = host.observing() || !cameras.length;
    if (cameraEnvironment && host.session?.environment?.id && cameraEnvironment !== host.session.environment.id) { cameraId = ''; cameras = []; cameraDetail = null; }
    host.renderCollectionControl('camera-collection-control',cameraCollection,host.scopes(),value=>showCameras(value));
    const message = host.$('camera-message'), list = host.$('camera-list'), detail = host.$('camera-detail'); message.replaceChildren(); message.hidden = false; list.replaceChildren(); detail.replaceChildren(); host.$('camera-layout').classList.toggle('has-selection', Boolean(cameraId));
    if(streams){message.hidden=true;host.$('camera-streams-content').replaceChildren(window.RocotoCameraStreams.render({connections:cameraStreams,query:host.$('camera-search').value,loading:cameraStreamsLoading,error:cameraStreamsError,canManage:host.pluginWritable(),onSetup:cameraSetup,onRetry:loadCameraStreams},{node:host.node,icon:host.icon}));return;}
    if (!cameraCollection) {message.append(host.workspaceEmpty('video','Camera recordings','Uploaded recordings appear here.',host.pluginWritable()?[host.agentButton('Upload files',event=>openUploadDialog(event.currentTarget,host.currentView==='cameras'?cameraCollection:libraryCollection),false,'upload')]:[]));return;}
    if (cameraError) message.append(host.node('span', '', cameraError), host.agentButton('Retry', loadCameras)); else if (cameraLoading) message.textContent = 'Loading camera views…'; else message.hidden = true;
    [...cameras].filter(camera=>`${camera.name || camera.id}`.toLowerCase().includes(host.$('camera-search').value.toLowerCase())).sort((a, b) => host.sourceNameOrder.compare(a.name || a.id, b.name || b.id)).forEach((camera) => { const button = host.node('button', 'camera-link'); button.type = 'button'; button.setAttribute('aria-current', String(camera.id === cameraId)); const reference = camera.reference_clip, thumbnail = host.safeUrl(reference?.thumbnail_url || reference?.thumbnail); if (thumbnail) { const image = host.node('img'); image.src = thumbnail; image.alt = ''; image.loading = 'lazy'; button.append(image); } const text = host.node('span'); text.append(host.node('strong', '', camera.name || camera.id), host.node('small', '', `${camera.asset_ids?.length || 0} recording${camera.asset_ids?.length === 1 ? '' : 's'} · ${camera.clip_count || 0} ${camera.clip_count === 1 ? 'interval' : 'intervals'}`)); button.append(text); button.addEventListener('click', () => showCameras(cameraCollection, camera.id));const row=host.node('div','camera-recording-row');row.append(button);if(reference?.id){const preview=host.agentButton('Preview',()=>{const results=recordingReferences();openClip(results,results.findIndex(item=>item.id===reference.id),null,preview);},false,'play');preview.setAttribute('aria-label',`Preview ${camera.name||camera.id}`);row.append(preview);}list.append(row); });
    if (!cameraDetail) { if (!cameraId && cameras.length) detail.append(host.node('p', 'workspace-help', 'Choose a source view to inspect its recordings and scene context.'));else if(!cameraLoading&&!cameraError){message.hidden=false;message.append(host.workspaceEmpty('video','Camera recordings','Uploaded recordings appear here.',host.pluginWritable()?[host.agentButton('Upload files',event=>openUploadDialog(event.currentTarget,host.currentView==='cameras'?cameraCollection:libraryCollection),false,'upload')]:[]));}return; }
    const camera = cameraDetail; detail.append(host.agentButton('Back to cameras', () => showCameras(cameraCollection))); detail.lastElementChild.classList.add('camera-back'); detail.append(host.node('h2', '', camera.name || camera.id), host.node('p', 'camera-description', 'Imported source view. Confirmed scene context applies to its selected recording and time range.'));
    const review = host.agentButton('Review camera', () => reviewCameras([camera.id]), false, 'video'); review.disabled = host.observing(); detail.append(review);
    const reference = camera.reference_clip; if (reference?.id) { const area = host.node('div', 'camera-reference'); const result = { ...reference, dataset: camera.collection || cameraCollection, camera: camera.name, thumbnail: reference.thumbnail_url || reference.thumbnail }; const references=recordingReferences(),index=references.findIndex(item=>item.id===result.id);area.append(evidenceCard(result,index>=0?references:[result],index>=0?index:0,null)); detail.append(area, host.node('p', 'workspace-help', 'Reference interval · Open the clip to review its frames and saved model observations.')); }
    detail.append(host.node('h3', '', 'Recordings'));
    for (const asset of camera.assets || []) { const link = host.node('a', 'camera-source-link', asset.filename || asset.asset_id); link.href = libraryUrl(cameraCollection, asset.folder || '', asset.asset_id); link.append(host.node('small', '', [preciseTime(asset.duration), asset.width && asset.height ? `${asset.width} × ${asset.height}` : '', asset.fps ? `${Number(asset.fps).toFixed(3).replace(/\.?0+$/, '')} fps` : '', host.fileSize(asset.bytes)].filter(Boolean).join(' · '))); link.addEventListener('click', (event) => { event.preventDefault(); showLibrary(cameraCollection, asset.folder || '', asset.asset_id); }); detail.append(link); }
    const identifiers = host.node('details', 'camera-history'); identifiers.append(host.node('summary', '', 'Source identity')); const metadata = host.node('dl', 'workspace-metadata'); for (const [label, value] of [['View ID', camera.id], ['Identity basis', host.humanize(camera.identity_basis)], ['Registry revision', camera.revision], ['Confirmed view revision', camera.view_revision]]) if (value !== undefined && value !== null) metadata.append(host.node('dt', '', label), host.node('dd', '', String(value))); identifiers.append(metadata); detail.append(identifiers);
    detail.append(host.node('h3', '', 'Scene context'));
    function contextCard(saved, draft = false) { const context = { ...saved, ...saved.provenance, ...(saved.context || {}) }; const card = host.node('section', 'camera-context'); card.append(host.node('h3', '', draft ? 'Draft scene context' : 'Confirmed scene context')); if (context.room) card.append(host.node('p', '', `Room: ${context.room}`)); if (context.layout_notes) card.append(host.node('p', '', context.layout_notes)); if (context.zones?.length) { const zones = host.node('ul'); context.zones.forEach((zone) => { const item = host.node('li', '', `${zone.name}: ${zone.description}`); if (Array.isArray(zone.bbox)) item.append(host.node('p', 'workspace-help', `Frame box [x, y, width, height]: ${zone.bbox.join(', ')} · normalized 0–1`)); zones.append(item); }); card.append(zones); } card.append(host.node('p', 'workspace-help', `Applies from ${preciseTime(context.valid_from_seconds || 0)} in the selected source recording${context.view_revision ? ` · View revision ${context.view_revision}` : ''}.`)); if (context.anchor_clip_id) { const link = host.node('a', 'source-reference', 'Open reference interval'); link.href = `/?clip=${encodeURIComponent(context.anchor_clip_id)}`; link.addEventListener('click', (event) => { event.preventDefault(); openEvidenceId(context.anchor_clip_id, link); }); card.append(link); }
      if (draft) { const confirm = host.agentButton('Confirm scene context', async () => { if (!host.session?.can_query) { host.showSession('Enter your access code to confirm scene context.'); return; } confirm.disabled = true; try { await host.request(`/api/retail/cameras/${encodeURIComponent(camera.id)}/confirm`, { expected_revision: camera.revision, draft_id: context.id || context.draft_id }); await loadCameras(); } catch (error) { cameraError = error.message; renderCameras(); } }, true); card.append(confirm); } return card; }
    if (camera.confirmed_context) detail.append(contextCard(camera.confirmed_context)); else detail.append(host.node('p', 'workspace-help', 'Add operator notes to identify the room, zones and layout. Review the draft against a reference interval, then confirm it.'));
    if (camera.draft) detail.append(contextCard(camera.draft, true));
    if (camera.descriptions?.length) { const observations = host.node('details', 'camera-history'); observations.append(host.node('summary', '', `${camera.descriptions.length} saved model observations`)); camera.descriptions.forEach((observation) => { const entry = host.node('details', 'camera-context'); entry.append(host.node('summary', '', `${(observation.model || 'Video model').replace(/^nvidia\//, '')} · ${observation.created_at || 'Saved observation'}`)); if (observation.question || observation.prompt) entry.append(host.node('p', 'workspace-help', observation.question || observation.prompt)); entry.append(host.node('span', 'inspection-output-label', 'Model observation'), host.node('p', '', observation.text || '')); observations.append(entry); }); detail.append(observations); }
    const edit = host.node('details', 'camera-context-form'); edit.append(host.node('summary', '', camera.confirmed_context || camera.draft ? 'Edit scene context' : 'Add scene context')); edit.firstElementChild.addEventListener('click', (event) => { if (!host.session?.can_query) { event.preventDefault(); host.showSession('Enter your access code to add scene context.'); } }); const form = host.node('form'), savedContext = camera.draft || camera.confirmed_context || {}, values = { ...savedContext, ...savedContext.provenance, ...(savedContext.context || {}) }, fields = {};
    function field(label, key, tag = 'input', value = '') { const wrapper = host.node('label', 'agent-field'), input = host.node(tag); input.name = key; input.value = value; if (tag === 'textarea') input.rows = key === 'zones' ? 3 : 4; wrapper.append(host.node('span', '', label), input); form.append(wrapper); fields[key] = input; return input; }
    field('Room', 'room', 'input', values.room || '').maxLength = 160; field('Layout and operator notes', 'layout_notes', 'textarea', values.layout_notes || '').maxLength = 6000; const zoneField = field('Zones', 'zones', 'textarea', (values.zones || []).map((zone) => `${zone.name}: ${zone.description}`).join('\n')); zoneField.placeholder = 'Counter: service area on the left\nTables: seating in the centre'; form.append(host.node('p', 'workspace-help', 'One zone per line: name followed by its description. Existing frame boxes follow unchanged, unique zone names.'));
    const change = field('Context change', 'change_type', 'select'); for (const [value, label] of [['context_update', 'Update scene context'], ['camera_moved', 'Camera moved']]) { const option = host.node('option', '', label); option.value = value; change.append(option); } change.value = values.change_type || 'context_update';
    const anchor = field('Reference interval', 'anchor_clip_id', 'select'); for (const clip of [reference, ...(camera.clips || []), ...(camera.assets || []).flatMap((asset) => asset.clips || [])].filter(Boolean).filter((clip, index, all) => all.findIndex((item) => item.id === clip.id) === index)) { const option = host.node('option', '', `${preciseTime(clip.start)}–${preciseTime(clip.end)} · ${clip.camera || camera.name}`); option.value = clip.id; anchor.append(option); } anchor.value = values.anchor_clip_id || reference?.id || '';
    const starts = field('Applies from source time (seconds)', 'valid_from_seconds', 'input', values.valid_from_seconds ?? reference?.start ?? 0); starts.type = 'number'; starts.min = '0'; starts.step = '0.001'; starts.required = true;
    const error = host.node('p', 'agent-form-error'); error.hidden = true; error.setAttribute('role', 'alert'); form.append(error); const save = host.node('button', 'agent-primary', 'Save draft'); save.type = 'submit'; form.append(save); form.addEventListener('submit', async (event) => { event.preventDefault(); if (!host.session?.can_query) { host.showSession('Enter your access code to save scene context.'); return; } const zones = zoneField.value.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => { const colon = line.indexOf(':'); return { name: colon < 0 ? line : line.slice(0, colon).trim(), description: colon < 0 ? '' : line.slice(colon + 1).trim() }; }); for (const zone of zones) { const previous = (values.zones || []).filter((item) => item.name === zone.name); if (previous.length === 1 && zones.filter((item) => item.name === zone.name).length === 1 && Array.isArray(previous[0].bbox)) zone.bbox = [...previous[0].bbox]; } save.disabled = true; error.hidden = true; try { await host.request(`/api/retail/cameras/${encodeURIComponent(camera.id)}/context`, { expected_revision: camera.revision, anchor_clip_id: anchor.value, valid_from_seconds: Number(starts.value), change_type: change.value, room: fields.room.value.trim(), layout_notes: fields.layout_notes.value.trim(), zones }); await loadCameras(); } catch (failure) { error.textContent = failure.message; error.hidden = false; save.disabled = false; } }); edit.append(form); detail.append(edit);
    if (camera.history?.length) { const history = host.node('details', 'camera-history'); history.append(host.node('summary', '', `${camera.history.length} confirmed context revision${camera.history.length === 1 ? '' : 's'}`)); camera.history.forEach((context) => history.append(contextCard(context))); detail.append(history); }
  }

host.$('review-cameras').addEventListener('click', chooseCameraReview);
host.$('settings-cameras').addEventListener('click', () => showCameras());
host.$('camera-streams-tab').addEventListener('click',()=>setCameraTab('streams'));
host.$('camera-recordings-tab').addEventListener('click',()=>setCameraTab('recordings'));
host.$('add-cameras').addEventListener('click', event => openCameraProviderPicker(event.currentTarget));
host.$('camera-provider-other').addEventListener('click', () => startCameraSetup());
host.$('close-camera-provider').addEventListener('click', () => host.$('camera-provider-dialog').close());
host.$('camera-provider-dialog').addEventListener('close', () => { if (cameraProviderOpener?.isConnected) cameraProviderOpener.focus(); });
host.$('camera-search').addEventListener('input',renderCameras);
host.$('cameras-nav').addEventListener('click', () => showCameras());
host.$('camera-upload').addEventListener('click', () => openUploadDialog(host.$('camera-upload'),cameraCollection));
let observationSignature = null;

let clipContext = null;

let descriptionTimer = null;

let clipGeneration = 0, evidenceOpenEpoch = 0, pendingEvidenceOpen = null;

let modalOpener = null;

let focusData = null;

let focusEnabled = false;

let focusAnimation = null;

let clipMetadataSignature = null;

const playbackUrl = (result) => `/clips/${encodeURIComponent(result.id)}.mp4`;

function clipDownloadUrl(result) { const provided = host.safeUrl(result.download_url); return provided && new URL(provided).origin === location.origin ? provided : `/downloads/${encodeURIComponent(result.id)}.mp4`; }

function clipManifestUrl(result) { const provided = host.safeUrl(result.manifest_url), url = new URL(provided && new URL(provided).origin === location.origin ? provided : `/api/retail/clip/${encodeURIComponent(result.id)}/manifest`, location.href); if (result.focus_id) url.searchParams.set('focus_id', result.focus_id); return url.href; }

function findEvidence(id) { const available = host.visibleChats(), ordered = [host.activeChat(), ...available.filter((chat) => chat.id !== host.activeId)].filter(Boolean); for (const chat of ordered) for (const message of [...chat.messages].reverse()) { const index = message.results?.findIndex((result) => result.id === id); if (index >= 0) return { message, index }; } return null; }

function evidenceOpenScope() { return JSON.stringify([host.sessionGeneration,host.session?.environment?.id,host.principalId(),host.activeId,host.currentView,host.homeMode,host.currentView==='library'?[libraryCollection,libraryFolder,libraryAssetId]:host.currentView==='cameras'?[cameraCollection,cameraId]:null]); }

function cancelEvidenceOpen() { ++evidenceOpenEpoch; pendingEvidenceOpen=null; }

async function openEvidenceId(id, opener, context = null) {
    const intent={epoch:++evidenceOpenEpoch,scope:evidenceOpenScope()};pendingEvidenceOpen=intent;
    const current=()=>pendingEvidenceOpen===intent && intent.epoch===evidenceOpenEpoch && intent.scope===evidenceOpenScope() && Boolean(host.session) && (!opener || opener.isConnected);
    const contextIndex = context?.results?.findIndex((result) => result.id === id), found = contextIndex >= 0 ? { message: context, index: contextIndex } : findEvidence(id); if (found) { openClip(found.message.results, found.index, found.message, opener); return; }
    try { const response = await fetch(`/api/retail/clip/${encodeURIComponent(id)}`); if (!response.ok) throw new Error(); const data = await response.json(); if(!current())return;const result = { ...(data.metadata || data), id }; openClip([result], 0, null, opener); }
    catch { if(current())host.notice('This clip is being prepared. Try opening it again.'); }
    finally { if(pendingEvidenceOpen===intent)pendingEvidenceOpen=null; }
  }

function mediaReferenceResult(url) {
    let videoId;try{videoId=decodeURIComponent(url.pathname.slice('/media/'.length));}catch{return null;}
    const start=Number(url.hash.match(/t=([\d.]+)/)?.[1]),candidates=host.visibleChats().flatMap(chat=>chat.messages.flatMap(message=>message.results||[])).filter(result=>result.video_id===videoId);
    return candidates.find(result=>Number(result.start)===start)||candidates[0]||null;
  }

function messageMediaDependencies(message) {
    const dependencies=[];
    for(const text of [message.text,...(message.progress||[]).map(entry=>entry.text)])for(const match of String(text||'').matchAll(/(?:https?:\/\/[^\s<>()[\]"']+)?\/media\/[^\s<>()[\]"']+/g)){
      const href=host.safeUrl(match[0]);if(!href)continue;const url=new URL(href);if(url.origin!==location.origin||!url.pathname.startsWith('/media/'))continue;
      dependencies.push([url.href,mediaReferenceResult(url)?.id||null]);
    }
    return dependencies;
  }

function evidencePresentation(value) {
    if(!value || !['players','gallery'].includes(value.mode) || !Array.isArray(value.items))return null;
    const seen=new Set(),items=value.items.slice(0,24).filter(item=>item && typeof item.id==='string' && item.id.length>0 && item.id.length<=200 && !seen.has(item.id) && seen.add(item.id)).map(item=>({...item,caption:typeof item.caption==='string'?item.caption.slice(0,240):''}));
    return items.length?{mode:value.mode==='players' && items.length<=3?'players':'gallery',items}:null;
  }

function inlineEvidence(result,results,index,message) {
    const card=host.node('article','evidence-card evidence-inline-player'),video=host.node('video');video.controls=true;video.playsInline=true;video.preload='none';video.src=playbackUrl(result);video.setAttribute('aria-label',result.caption || `${result.camera || 'Video evidence'} · ${host.time(result.start)}–${host.time(result.end)}`);
    const poster=host.safeUrl(result.thumbnail_url || result.thumbnail);if(poster && new URL(poster).origin===location.origin)video.poster=poster;
    video.addEventListener('play',()=>document.querySelectorAll('.evidence-inline-player video').forEach(other=>{if(other!==video)other.pause();}));card.append(video);
    const info=host.node('div','evidence-info');if(result.caption)info.append(host.node('p','evidence-caption',result.caption));
    const meta=host.node('div','evidence-player-meta'),open=host.agentButton(`${result.camera || 'Source'} · ${host.time(result.start)}–${host.time(result.end)}`,()=>openClip(results,index,message,open),false,'video');open.title='Open source details';
    const download=host.node('a','icon-button');download.href=clipDownloadUrl(result);download.download='';download.setAttribute('aria-label','Download source clip');download.title='Download source clip';download.append(host.icon('download'));meta.append(open,download);info.append(meta);card.append(info);return card;
  }

function evidenceCard(result, results, index, message) {
    const card = host.node('article', 'evidence-card');
    const media = host.node('div', 'evidence-media');
    const preview = host.node('button', 'evidence-preview');
    preview.type = 'button'; preview.setAttribute('aria-label', `Play ${result.camera || result.video_id}, ${host.time(result.start)} to ${host.time(result.end)}`);
    const thumbnail = result.thumbnail || result.thumbnail_url, thumbnailUrl = host.safeUrl(thumbnail || '');
    if (thumbnailUrl && thumbnail) { const img = host.node('img'); img.src = thumbnailUrl; img.alt = `Video evidence from ${result.camera || result.video_id}`; img.loading = 'lazy'; preview.append(img); }
    const play = host.node('span', 'play-icon'); play.append(host.icon('play')); preview.append(play);const timestamp=host.node('span','evidence-timestamp',`${host.time(result.start)}–${host.time(result.end)}`);timestamp.title='Time in the original recording';preview.append(timestamp);
    preview.setAttribute('aria-haspopup', 'dialog');
    let hoverVideo = null, hoverTimer = null;
    function stopPreview() { clearTimeout(hoverTimer); if (hoverVideo) { hoverVideo.pause(); hoverVideo.removeAttribute('src'); hoverVideo.load(); hoverVideo.remove(); hoverVideo = null; } }
    preview.addEventListener('pointerenter', (event) => { if (event.pointerType !== 'mouse') return; hoverTimer = setTimeout(() => { if (!preview.isConnected) return; hoverVideo = host.node('video'); hoverVideo.muted = true; hoverVideo.defaultMuted = true; hoverVideo.loop = true; hoverVideo.playsInline = true; hoverVideo.preload = 'metadata'; hoverVideo.src = playbackUrl(result); preview.append(hoverVideo); hoverVideo.play().catch(() => {}); }, 160); });
    preview.addEventListener('pointerleave', stopPreview);
    preview.addEventListener('click', () => { stopPreview(); openClip(results, index, message, preview); });
    const download = host.node('a', 'thumbnail-download'); download.href = clipDownloadUrl(result); download.download = ''; download.title = 'Download clip at source resolution'; download.setAttribute('aria-label', `Download clip from ${result.camera || result.video_id}, ${host.time(result.start)} to ${host.time(result.end)}`); download.append(host.icon('download')); download.addEventListener('click', (event) => { event.stopPropagation(); stopPreview(); }); media.append(preview, download);
    const info = host.node('div', 'evidence-info'); if(result.caption)info.append(host.node('p','evidence-caption',result.caption));
    const meta = host.node('div', 'evidence-meta'); meta.append(host.node('span', '', datasetLabel(result.dataset)));
    info.append(meta); card.append(media, info); return card;
  }

function renderClipMetadata(data, result) {
    const clip = { ...result, ...(data.metadata || {}), ...data }, relations = Array.isArray(clip.relations) ? clip.relations : [], signature = JSON.stringify([clip.id, clip.asset_id, clip.video_id, clip.source_filename, clip.source_reference, clip.library_folder, clip.collection, clip.camera, clip.start, clip.end, clip.download_url, clip.manifest_url, clip.focus_id, clip.entities, clip.room, relations]);
    if (signature === clipMetadataSignature) return; const firstDetails = clipMetadataSignature === null; clipMetadataSignature = signature;
    host.$('download-clip').href = clipDownloadUrl(clip); host.$('download-clip').title = 'Download clip at source resolution'; host.$('download-manifest').href = clipManifestUrl(clip); host.$('download-manifest').title = 'Download source and analysis metadata';
    const source = host.$('clip-source-details'), identifiers = host.$('clip-identifiers'); source.replaceChildren(); identifiers.replaceChildren();
    const addRow = (container, label, value) => { if (value === undefined || value === null || value === '') return; const detail = host.node('dd', '', String(value)); detail.title = String(value); container.append(host.node('dt', '', label), detail); };
    addRow(source, 'File', clip.source_filename || 'Loading source details…'); addRow(source, 'Camera', clip.camera); addRow(source, 'Interval', `${preciseTime(clip.start)}–${preciseTime(clip.end)}`);
    const assetId = clip.asset_id || clip.video_id, libraryLink = host.$('clip-show-library'); libraryLink.hidden = true; if (assetId) { const collection = clip.collection || canonicalScope(clip.dataset || ''); libraryLink.href = libraryUrl(collection, clip.library_folder || '', assetId); libraryLink.onclick = (event) => { event.preventDefault(); closeClip(); if (host.$('gallery-dialog').open) host.$('gallery-dialog').close(); showLibrary(collection, clip.library_folder || '', assetId); }; }
    addRow(identifiers, 'Asset ID', clip.asset_id || clip.video_id); addRow(identifiers, 'Clip ID', clip.id);
    const referenceValue = typeof clip.source_reference === 'string' ? clip.source_reference : clip.source_reference?.url, reference = typeof referenceValue === 'string' && /^https?:\/\//i.test(referenceValue) ? host.safeUrl(referenceValue) : null;
    host.$('clip-source-reference').hidden = !reference; if (reference) host.$('clip-source-reference').href = reference; else host.$('clip-source-reference').removeAttribute('href');
    const list = host.$('clip-relation-list'); list.replaceChildren(); let count = 0;
    for (const relation of relations) {
      const otherId = relation.from_clip_id === clip.id ? relation.to_clip_id : relation.to_clip_id === clip.id ? relation.from_clip_id : null; if (!otherId || otherId === clip.id) continue; count++;
      const item = host.node('div', 'clip-relation'), header = host.node('div', 'relation-header'), rejected = relation.review_status === 'rejected', label = ({ same_person_candidate: 'Person link', same_object_candidate: 'Object link', same_event_candidate: 'Event link' }[relation.relation] || host.humanize(relation.relation || 'Clip connection'));
      header.append(host.node('span', 'relation-label', label), host.node('span', `relation-status${rejected ? ' rejected' : ''}`, rejected ? 'Rejected' : 'Candidate'));
      const linkedSource = (relation.sources || []).find((source) => source.id === otherId), link = host.node('a', 'relation-link'); link.href = `/?clip=${encodeURIComponent(otherId)}`; link.append(host.icon('video'), host.node('span', '', linkedSource?.camera ? `${linkedSource.camera}${Number.isFinite(linkedSource.start) && Number.isFinite(linkedSource.end) ? ` · ${preciseTime(linkedSource.start)}–${preciseTime(linkedSource.end)}` : ''}` : 'Open related clip')); link.addEventListener('click', (event) => { event.preventDefault(); openEvidenceId(otherId, link); }); item.append(header, link);
      if (relation.evidence || relation.review_note || relation.uncertainty || relation.model) { const review = host.node('details', 'relation-review'); review.append(host.node('summary', '', 'Evidence and review')); for (const [label, value] of [['Review', relation.review_note], ['Model evidence', relation.evidence], ['Uncertainty', relation.uncertainty]]) if (typeof value === 'string' && value.trim()) { const paragraph = host.node('p'); paragraph.append(host.node('span', 'review-label', label), document.createTextNode(value)); review.append(paragraph); } if (relation.model) review.append(host.node('p', '', relation.model)); item.append(review); }
      list.append(item);
    }
    host.$('clip-connection-count').textContent = String(count); if (!count) list.append(host.node('p', 'observation-status', '0 recorded connections')); if (firstDetails || count && !host.$('clip-connections').dataset.loaded) host.$('clip-connections').open = count > 0; if (count) host.$('clip-connections').dataset.loaded = 'true';
    const entities = clip.entities || { status: 'in_development', occurrences: [] }, room = clip.room || { status: 'in_development', id: null };
    const statusLabel = (value) => !value || value === 'in_development' ? 'In development' : host.humanize(value);
    host.$('clip-entities-status').textContent = statusLabel(entities.status); host.$('clip-entities').textContent = entities.occurrences?.length ? `${entities.occurrences.length} recorded occurrences` : 'Entity occurrences';
    host.$('clip-room-status').textContent = statusLabel(room.status); host.$('clip-room').replaceChildren(document.createTextNode(room.name || room.id || 'Room assignment')); const cameraId = room.camera_id || clip.camera_context?.camera_id; if (cameraId) { const link = host.node('button', 'text-button'); link.type = 'button'; link.append(host.icon('video'), host.node('span', '', 'Review camera context')); link.addEventListener('click', () => { closeClip(); showCameras?.(clip.collection || canonicalScope(clip.dataset), cameraId); }); host.$('clip-room').append(link); }
  }

function prioritizedEvidence(message) { const presentation=evidencePresentation(message.evidence_presentation);if(presentation)return presentation.items;const results = message.results || [], ordered = [], seen = new Set(); for (const match of (message.text || '').matchAll(/(?:[?&]clip=|\/clips\/)([^\s)&#/]+?)(?:\.mp4|[)&#\s]|$)/g)) { let id; try { id = decodeURIComponent(match[1]); } catch { continue; } const result = results.find((item) => item.id === id); if (result && !seen.has(id)) { seen.add(id); ordered.push(result); } } return ordered.concat(results.filter((result) => !seen.has(result.id))); }

function openGallery(message, opener) { applyGalleryView(); const gallery = host.$('gallery-dialog'), grid = host.$('gallery-grid'), more = host.$('gallery-more'), results = prioritizedEvidence(message); let count = 0; host.$('gallery-title').textContent = `${results.length} clips`; grid.replaceChildren(); const load = () => { const end = Math.min(count + 36, results.length); for (; count < end; count++) grid.append(evidenceCard(results[count], results, count, message)); more.hidden = count === results.length; more.textContent = `Load more clips · ${count} of ${results.length}`; }; more.onclick = load; load(); gallery.dataset.openerMessage = message.id; gallery.showModal(); gallery.addEventListener('close', () => { gallery.querySelectorAll('video').forEach((video) => video.pause()); more.onclick = null; if (opener?.isConnected) opener.focus(); }, { once: true }); }

function openClip(results, index, message, opener) {
    cancelEvidenceOpen();
    document.body.classList.toggle('gallery-clip-viewer',host.currentView==='library');
    if (host.$('gallery-dialog').open) host.$('gallery-dialog').close(); host.$('clip-metadata-drawer').open=false;
    if (opener) modalOpener = opener;
    clipContext = { results, index, message }; const result = results[index], video = host.$('clip-player'); clipMetadataSignature = null; observationSignature = null; delete host.$('clip-connections').dataset.loaded; renderClipMetadata({}, result); host.$('clip-details-sidebar').scrollTop = 0;
    host.pauseMedia(document); clearTimeout(descriptionTimer); const generation = ++clipGeneration;
    host.$('clip-dialog-title').textContent = host.humanize(result.camera || result.video_id || 'Video evidence');
    host.$('clip-dialog-meta').textContent = `${datasetLabel(result.dataset)} · ${host.time(result.start)}–${host.time(result.end)} in source`;
    host.$('clip-interval-note').textContent = `Bounded source interval · ${preciseTime(result.start)}–${preciseTime(result.end)} in the original file. Playback starts at 0:00 for this clip.`;
    host.$('clip-position').textContent = `${index + 1} of ${results.length}`; host.$('previous-clip').disabled = index === 0; host.$('next-clip').disabled = index >= results.length - 1;
    focusData = null; setFocus(false); host.$('show-focus').disabled = true; host.$('focus-status').textContent = 'Choose a subject in chat to track it.'; video.src = playbackUrl(result); video.currentTime = 0;
    host.$('clip-model-description').replaceChildren(host.node('p', 'observation-status', 'Awaiting model analysis.'));
    renderInterpretation(message);
    if (host.filesPaneOpen) host.setFilesPane(false); document.body.classList.add('clip-viewer-open'); if (!host.$('clip-dialog').open) host.$('clip-dialog').show();
    video.play().catch(() => {});
    async function readDescriptions() {
      try {
        const response = await fetch(`/api/retail/clip/${encodeURIComponent(result.id)}${result.focus_id ? `?focus_id=${encodeURIComponent(result.focus_id)}` : ''}`); if (!response.ok) throw new Error(); const data = await response.json();
        if (generation !== clipGeneration || !host.$('clip-dialog').open) return;
        renderClipMetadata(data, result);
        const descriptions = (data.descriptions || []).filter((item) => typeof item.text === 'string' && item.text.trim());
        renderObservations(descriptions);
        let focus = data.focus_json || data.focus; if (typeof focus === 'string') { const url = host.safeUrl(focus); if (url && new URL(url).origin === location.origin) { const response = await fetch(url); if (response.ok) focus = await response.json(); } }
        if (generation !== clipGeneration || !host.$('clip-dialog').open) return;
        if (focus && Array.isArray(focus.samples) && (!focusData || focusData.created_at !== focus.created_at)) { focusData = focus; host.$('show-focus').disabled = false; setFocus(true); }
        descriptionTimer = setTimeout(readDescriptions, 5000);
      } catch { if (generation === clipGeneration) host.$('clip-model-description').replaceChildren(host.node('p', 'observation-status', 'Clip details are reconnecting.')); }
    }
    readDescriptions();
  }

function renderObservations(descriptions) {
    const signature = JSON.stringify(descriptions); if (signature === observationSignature) return; observationSignature = signature;
    const container = host.$('clip-model-description'); container.replaceChildren(); if (!descriptions.length) { container.append(host.node('p', 'observation-status', 'Model observations appear here after an inspection.')); return; }
    const timestamp = (item) => typeof item.created_at === 'number' ? item.created_at * (item.created_at < 1e12 ? 1000 : 1) : Date.parse(item.created_at || '') || 0;
    const isSuper = (item) => (item.model || '').split('/').at(-1) === 'Cosmos3-Super';
    const groups = new Map(); descriptions.forEach((item, index) => { const question = typeof item.question === 'string' ? item.question : typeof item.prompt === 'string' ? item.prompt : null, key = question === null ? `legacy-${index}` : `question-${question}`; if (!groups.has(key)) groups.set(key, { question, items: [] }); groups.get(key).items.push(item); });
    const ordered = [...groups.values()].map((group) => { group.items.sort((a, b) => timestamp(b) - timestamp(a)); group.primary = group.items.find(isSuper) || group.items[0]; return group; }).sort((a, b) => Number(isSuper(b.primary)) - Number(isSuper(a.primary)) || timestamp(b.primary) - timestamp(a.primary));
    function observation(item, primary = false) {
      const block = host.node('div', 'model-observation'), heading = host.node('div', 'inspection-model-heading'); heading.append(host.node('strong', '', (item.model || 'Video model').replace(/^nvidia\//, ''))); if (primary) heading.append(host.node('span', 'inspection-current', isSuper(item) ? 'Latest Super' : 'Latest observation')); block.append(heading);
      const input = item.input_settings || item.inspection_settings || {}, provenance = host.node('details', 'inspection-provenance'), summary = host.node('summary', '', 'Input and provenance'), details = host.node('dl');
      const add = (label, value) => { if (value === undefined || value === null || value === '') return; details.append(host.node('dt', '', label), host.node('dd', '', typeof value === 'object' ? JSON.stringify(value) : String(value))); };
      const at = timestamp(item); if (at) block.append(host.node('p', 'inspection-time', `${new Date(at).toISOString().replace('T', ' ').replace(/\.\d+Z$/, ' UTC')}`));
      const conciseInput = []; const width = input.width || input.input_width, height = input.height || input.input_height, fps = input.fps ?? input.sample_fps, frames = input.num_frames ?? input.frames ?? input.max_frames; if (width && height) conciseInput.push(`${width} × ${height}`); else if (input.target_height) conciseInput.push(`${input.target_height}p input`); if (fps !== undefined) conciseInput.push(`${fps} fps submitted`); if (Array.isArray(input.sampled_frame_indices) && Number.isInteger(frames) && frames > 0 && input.sampled_frame_indices.length === frames && input.sampled_frame_indices.every((frame, index) => frame === index)) conciseInput.push('All available frames'); else if (input.sampling_fps !== undefined && input.sampling_fps !== null) conciseInput.push(`${input.sampling_fps} fps analysis`); if (frames !== undefined) conciseInput.push(`${input.max_frames && !input.num_frames && !input.frames ? 'up to ' : ''}${frames} frames`); if (conciseInput.length) block.append(host.node('p', 'inspection-input-summary', conciseInput.join(' · ')));
      add('Model', item.model); add('Revision', item.model_revision); add('Recorded at', item.created_at); add('Run', item.run_id); for (const [key, value] of Object.entries(input)) add(({ fps: 'Submitted video fps', num_frames: 'Submitted frame count', sampling_fps: 'Analysis sampling fps', max_new_tokens: 'Token limit', reasoning_requested: 'Reasoning requested' })[key] || host.humanize(key), value); add('Input provenance', item.input_provenance || item.settings_provenance); add('Verification', item.verification); add('Applied instruction', item.applied_instruction); add('Applied question prompt', item.applied_question_prompt); add('Input tokens', item.input_tokens); add('Output tokens', item.output_tokens); add('Reasoning completed', item.reasoning_completed); add('Input SHA256', item.source?.sha256); if (item.source?.start !== undefined) add('Source interval', `${preciseTime(item.source.start)}–${preciseTime(item.source.end)}`); provenance.append(summary, details); block.append(provenance, host.node('span', 'inspection-output-label', 'Model observation'), host.node('div', 'inspection-raw-output', item.text)); return block;
    }
    function questionGroup(group) { const section = host.node('div', 'inspection-question-group'), question = host.node('details', 'inspection-question'); question.open = true; question.append(host.node('summary', '', group.question === null ? 'Legacy observation' : 'Question')); if (group.question !== null) question.append(host.node('p', '', group.question)); section.append(question, observation(group.primary, true)); const earlier = group.items.filter((item) => item !== group.primary); if (earlier.length) { const history = host.node('details', 'inspection-history'); history.append(host.node('summary', '', `${earlier.length} other ${earlier.length === 1 ? 'observation' : 'observations'} for this question`)); earlier.forEach((item) => history.append(observation(item))); section.append(history); } return section; }
    container.append(questionGroup(ordered[0])); if (ordered.length > 1) { const others = host.node('details', 'inspection-other-questions'); others.append(host.node('summary', '', `${ordered.length - 1} other ${ordered.length === 2 ? 'question' : 'questions'}`)); ordered.slice(1).forEach((group) => others.append(questionGroup(group))); container.append(others); }
  }

function focusBox(at) {
    const samples = focusData?.samples || []; if (!samples.length) return null;
    let previous = null, next = null; for (const sample of samples) { if (sample.time <= at) previous = sample; else { next = sample; break; } }
    if (!previous || !Array.isArray(previous.bbox) || at - previous.time > 1 / (focusData.sample_fps || 5) + .15) return null;
    let box = previous.bbox; if (next?.bbox && next.time > previous.time) { const fraction = Math.max(0, Math.min(1, (at - previous.time) / (next.time - previous.time))); box = box.map((value, index) => value + (next.bbox[index] - value) * fraction); }
    return box.length === 4 && box.every((value) => Number.isFinite(value) && value >= 0 && value <= 1) && box[2] > 0 && box[3] > 0 && box[0] + box[2] <= 1.001 && box[1] + box[3] <= 1.001 ? box : null;
  }

function drawFocus() {
    const video = host.$('clip-player'), stage = host.$('clip-stage'), canvas = host.$('focus-overlay'), box = focusEnabled && video.readyState >= 2 ? focusBox(video.currentTime) : null;
    stage.classList.toggle('focus-active', Boolean(box)); canvas.hidden = !box;
    if (!box) { if (focusEnabled) host.$('focus-status').textContent = 'Original view · tracking is outside this moment.'; return; }
    const rect = stage.getBoundingClientRect(), ratio = Math.min(devicePixelRatio || 1, 2), width = Math.round(rect.width * ratio), height = Math.round(rect.height * ratio);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
    const context = canvas.getContext('2d'); context.clearRect(0, 0, width, height); const scale = Math.min(width / video.videoWidth, height / video.videoHeight), offsetX = (width - video.videoWidth * scale) / 2, offsetY = (height - video.videoHeight * scale) / 2;
    const [x, y, w, h] = box, sourceX = x * video.videoWidth, sourceY = y * video.videoHeight, sourceW = w * video.videoWidth, sourceH = h * video.videoHeight;
    context.drawImage(video, sourceX, sourceY, sourceW, sourceH, offsetX + sourceX * scale, offsetY + sourceY * scale, sourceW * scale, sourceH * scale);
    context.strokeStyle = getComputedStyle(document.body).getPropertyValue('--accent').trim(); context.lineWidth = 2 * ratio; context.strokeRect(offsetX + sourceX * scale, offsetY + sourceY * scale, sourceW * scale, sourceH * scale);
    host.$('focus-status').textContent = 'Image tracking · approximate subject box';
  }

function animateFocus() { if (focusAnimation !== null) cancelAnimationFrame(focusAnimation); focusAnimation = null; drawFocus(); if (focusEnabled && !host.$('clip-player').paused && host.$('clip-dialog').open) focusAnimation = requestAnimationFrame(animateFocus); }

function setFocus(enabled) { focusEnabled = Boolean(enabled && focusData); host.$('show-original').setAttribute('aria-pressed', String(!focusEnabled)); host.$('show-focus').setAttribute('aria-pressed', String(focusEnabled)); if (!focusEnabled && focusData) host.$('focus-status').textContent = 'Original footage'; animateFocus(); }

function renderInterpretation(message) { const el = host.$('clip-interpretation'); if (message?.mode === 'chat' && message.text) { host.renderText(el, message.text, message); } else el.replaceChildren(host.node('p', 'observation-status', message?.pending ? 'Rocoto is reviewing the footage.' : 'Ask Rocoto about this clip to add an interpretation.')); }

function closeClip() { cancelEvidenceOpen();document.body.classList.remove('clip-viewer-open','gallery-clip-viewer'); host.$('clip-dialog').close(); }

function renderEvidence(article,message){
    const evidence=prioritizedEvidence(message),presentation=evidencePresentation(message.evidence_presentation);
    if (evidence.length) {
      article.append(host.node('div', 'evidence-heading', 'Video evidence'));
      if(presentation?.mode==='players'){const players=host.node('div','evidence-players');evidence.forEach((result,index)=>players.append(inlineEvidence(result,evidence,index,message)));article.append(players);}
      else {const grid=host.node('div','evidence-grid');evidence.slice(0,4).forEach((result,index)=>grid.append(evidenceCard(result,evidence,index,message)));article.append(grid);if(evidence.length>4){const gallery=host.node('button','gallery-button',`View all ${evidence.length} clips`);gallery.append(host.icon('chevron'));gallery.addEventListener('click',()=>openGallery(message,gallery));article.append(gallery);}}
    }
  }

function refreshEvidenceMessage(message){if(clipContext?.message?.id===message.id && host.$("clip-dialog").open)renderInterpretation(message); }

host.$('close-clip').addEventListener('click', closeClip);
host.$('close-gallery').addEventListener('click', () => host.$('gallery-dialog').close());
host.$('previous-clip').addEventListener('click', () => { if (clipContext && clipContext.index > 0) openClip(clipContext.results, clipContext.index - 1, clipContext.message); });
host.$('next-clip').addEventListener('click', () => { if (clipContext && clipContext.index < clipContext.results.length - 1) openClip(clipContext.results, clipContext.index + 1, clipContext.message); });
host.$('fullscreen-clip').addEventListener('click', () => { const player = host.$('clip-player'), stage = host.$('clip-stage'); if (stage.requestFullscreen) stage.requestFullscreen().catch(() => {}); else if (player.webkitEnterFullscreen) player.webkitEnterFullscreen(); });
host.$('show-original').addEventListener('click', () => setFocus(false));
host.$('show-focus').addEventListener('click', () => setFocus(true));
for (const event of ['play', 'pause', 'seeked', 'loadeddata']) host.$('clip-player').addEventListener(event, animateFocus);
document.addEventListener('fullscreenchange', drawFocus);
host.$('clip-dialog').addEventListener('close', () => { cancelEvidenceOpen();document.body.classList.remove('clip-viewer-open'); ++clipGeneration; clearTimeout(descriptionTimer); setFocus(false); focusData = null; const video = host.$('clip-player'); video.pause(); video.removeAttribute('src'); video.load(); clipContext = null; if (modalOpener?.isConnected) modalOpener.focus(); else host.elements.message.focus(); });
host.$('clip-dialog').addEventListener('keydown', (event) => { if (event.target === host.$('clip-player') || ['INPUT', 'TEXTAREA'].includes(event.target.tagName)) return; if (event.key === 'ArrowRight') { event.preventDefault(); host.$('next-clip').click(); } if (event.key === 'ArrowLeft') { event.preventDefault(); host.$('previous-clip').click(); } });
for (const dialog of [host.$('clip-dialog'), host.$('gallery-dialog')]) dialog.addEventListener('click', (event) => { if (event.target !== dialog) return; const box = dialog.getBoundingClientRect(); if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close(); });
const UPLOAD_CHUNK_BYTES = 2 * 1024 * 1024, UPLOAD_MAX_BYTES = 256 * 1024 * 1024;

const uploadWork = new Map();

let importScope = '', importRows = [], importPolicy = null, importError = '', uploadNotice = '', importLoading = false, importTimer = null, importEpoch = 0, uploadTarget = null, uploadPumping = false;

let uploadDialogState=null,sourceLinkRows=[],sourceLinkPolicy=null,sourceLinkError='',sourceLinkReadError='',sourceLinkTimer=null,sourceLinkBusy=false,sourceLinkEpoch=0;

function closeUploadDialog() { clearTimeout(sourceLinkTimer);++sourceLinkEpoch;sourceLinkBusy=false;sourceLinkError='';sourceLinkReadError='';sourceLinkPolicy=null;sourceLinkRows=[];host.$('source-link-list').replaceChildren();delete host.$('source-link-list').dataset.signature;host.$('source-link-input').value='';if(uploadDialogState){uploadDialogState.paused=true;const {anchor,opener}=uploadDialogState;if(opener?.isConnected)queueMicrotask(()=>opener.focus({preventScroll:true}));anchor.after(host.$('gallery-imports'));anchor.remove();uploadDialogState=null;}host.$('source-upload-dialog').close(); }

async function openUploadDialog(opener,collection) {
    if(!host.pluginWritable())return;closeUploadDialog();libraryCollection=collection || '';const anchor=document.createComment('upload-progress-home');host.$('gallery-imports').before(anchor);host.$('upload-progress').append(host.$('gallery-imports'));uploadDialogState={generation:host.sessionGeneration,collection:libraryCollection,opener,anchor,checkingUploads:true,dropFeedback:''};host.$('source-upload-dialog').showModal();host.$('source-link-submit').disabled=true;renderImports();host.$('source-link-input').focus();await Promise.allSettled([loadImports(),loadSourceLinks()]);
  }

async function loadSourceLinks() {
    clearTimeout(sourceLinkTimer);const state=uploadDialogState,epoch=++sourceLinkEpoch;if(!state||!host.$('source-upload-dialog').open)return;
    try{const response=await fetch(`/api/retail/source-links${libraryCollection ? '?collection=' + encodeURIComponent(libraryCollection) : ''}`,{signal:AbortSignal.timeout(10000)});if(!response.ok)throw new Error();const data=await response.json();if(uploadDialogState!==state||state.generation!==host.sessionGeneration||epoch!==sourceLinkEpoch)return;sourceLinkReadError='';sourceLinkRows=(data.links || []).filter(link=>link.collection===libraryCollection);for(const link of sourceLinkRows)if(link.import)applyImport(link.import);sourceLinkPolicy=data.intake || {};if(!libraryCollection&&sourceLinkPolicy.collections?.includes(sourceLinkPolicy.default_collection)){libraryCollection=sourceLinkPolicy.default_collection;state.collection=libraryCollection;loadImports();}renderSourceLinks();
    }catch{if(uploadDialogState===state&&epoch===sourceLinkEpoch){sourceLinkReadError='Link imports are reconnecting. Choose files to upload a recording.';renderSourceLinks();}}
    finally{if(uploadDialogState===state&&epoch===sourceLinkEpoch)sourceLinkTimer=setTimeout(loadSourceLinks,1500);}
  }

function sourceLinkLabel(link) { return link.import?uploadStatus(link.import):({queued:'Waiting to download',downloading:'Downloading',received:'Received',needs_action:'Action needed',interrupted:'Paused',cancelling:'Cancelling…',cancelled:'Cancelled'})[link.status] || 'Checking import'; }

function renderSourceLinks() {
    host.$('source-link-submit').disabled=!host.pluginWritable()||!sourceLinkPolicy?.can_create||sourceLinkBusy;const list=host.$('source-link-list'),signature=JSON.stringify([sourceLinkRows,sourceLinkPolicy?.can_manage,sourceLinkBusy,importWritable()]);if(list.dataset.signature===signature){renderImports();return;}list.dataset.signature=signature;const focus=list.contains(document.activeElement)?{id:document.activeElement.closest('[data-link-id]')?.dataset.linkId,label:document.activeElement.textContent}:null;list.replaceChildren();
    for(const link of sourceLinkRows.filter(link=>!link.import)){const row=host.node('article','gallery-import'),content=host.node('div','gallery-import-content'),heading=host.node('div','gallery-import-heading');heading.append(host.node('strong','',link.filename || ({google_drive:'Google Drive',dropbox:'Dropbox',wetransfer:'WeTransfer',https:'File link'})[link.provider] || 'Shared file'),host.node('span','gallery-import-state',sourceLinkLabel(link)));content.append(heading);if(link.size_bytes){const progress=host.node('progress','gallery-import-progress');progress.max=link.size_bytes;progress.value=link.received_bytes || 0;content.append(host.node('p','gallery-import-bytes',`${host.fileSize(link.received_bytes || 0)} of ${host.fileSize(link.size_bytes)}`),progress);}if(link.action?.message)content.append(host.node('p','workspace-help',link.action.message));row.dataset.linkId=link.id;row.append(host.icon('link'),content);const actions=host.node('div','gallery-import-actions');if(link.status==='needs_action'){const choose=host.agentButton('Choose downloaded file',()=>chooseUpload(),false,'upload');choose.disabled=!importWritable();actions.append(choose);}if(sourceLinkPolicy?.can_manage&&link.status==='interrupted'){const retry=host.agentButton('Retry',()=>sourceLinkAction(link,'retry'),false,'refresh');retry.disabled=sourceLinkBusy;actions.append(retry);}if(sourceLinkPolicy?.can_manage&&!['cancelled','received'].includes(link.status)){const cancel=host.agentButton('Cancel',()=>sourceLinkAction(link,'cancel'),false,'close');cancel.disabled=sourceLinkBusy;actions.append(cancel);}row.append(actions);list.append(row);}if(focus){const row=[...list.children].find(row=>row.dataset.linkId===focus.id);[...(row?.querySelectorAll('button') || [])].find(button=>button.textContent===focus.label)?.focus({preventScroll:true});}renderImports();
  }

async function addSourceLink(event) {
    event.preventDefault();const state=uploadDialogState;if(!state||!host.pluginWritable()||!sourceLinkPolicy?.can_create||sourceLinkBusy)return;let url=host.$('source-link-input').value.trim();try{const parsed=new URL(url);if(parsed.protocol!=='https:'||parsed.username||parsed.password)throw new Error();}catch{sourceLinkError='Paste an HTTPS sharing link or choose files to upload.';renderSourceLinks();return;}
    const body=JSON.stringify({collection:libraryCollection,url,idempotency_key:crypto.randomUUID()});url='';host.$('source-link-input').value='';sourceLinkBusy=true;sourceLinkError='';++sourceLinkEpoch;clearTimeout(sourceLinkTimer);renderSourceLinks();
    try{await importRequest('/api/retail/source-links',{method:'POST',headers:{'Content-Type':'application/json'},body,retry:true,work:state,interruptible:false});}catch{if(uploadDialogState===state)sourceLinkError='Check the saved import status before adding the link again.';}finally{if(uploadDialogState===state&&state.generation===host.sessionGeneration){sourceLinkBusy=false;await loadSourceLinks();}}
  }

async function sourceLinkAction(link,action) { const state=uploadDialogState;if(!state||!host.pluginWritable()||!sourceLinkPolicy?.can_manage||sourceLinkBusy)return;sourceLinkBusy=true;++sourceLinkEpoch;clearTimeout(sourceLinkTimer);renderSourceLinks();try{await importRequest(`/api/retail/source-links/${encodeURIComponent(link.id)}${action==='retry'?'/retry':''}`,{method:action==='retry'?'POST':'DELETE',headers:{'Content-Type':'application/json'},body:action==='retry'?'{}':undefined,retry:true,work:state,interruptible:false});}catch{if(uploadDialogState===state)sourceLinkError='Check the saved import status and try again.';}finally{if(uploadDialogState===state){sourceLinkBusy=false;await loadSourceLinks();}} }

function uploadStorageKey() { return `rocoto-upload-transfers-v1:${encodeURIComponent(host.storageIdentity || '')}`; }

function uploadTickets() { try { const saved = JSON.parse(localStorage.getItem(uploadStorageKey()) || '[]'); return Array.isArray(saved) ? saved.filter(t => t && /^[a-f0-9]{64}$/.test(t.fingerprint) && typeof t.key === 'string' && t.key.length < 100 && typeof t.collection === 'string' && Number.isInteger(t.chunkBytes) && t.chunkBytes > 0 && t.chunkBytes <= UPLOAD_CHUNK_BYTES && (!t.id || /^upload-[a-f0-9]{32}$/.test(t.id))).slice(-100) : []; } catch { return []; } }

function saveUploadTicket(ticket) { try { localStorage.setItem(uploadStorageKey(), JSON.stringify([...uploadTickets().filter(t => t.key !== ticket.key), ticket].slice(-100))); } catch { /* The live transfer remains usable when browser storage is full. */ } }

function removeUploadTicket(id) { try { localStorage.setItem(uploadStorageKey(), JSON.stringify(uploadTickets().filter(t => t.id !== id))); } catch {} }

function resetImports() { closeUploadDialog(); ++importEpoch; clearTimeout(importTimer); for (const work of uploadWork.values()) { work.paused = true; work.controller?.abort(); work.file = null; } uploadWork.clear(); importScope = ''; importRows = []; importPolicy = null; importError = ''; uploadNotice = ''; importLoading = false; uploadTarget = null; host.$('gallery-upload-input').value = ''; host.$('gallery-upload').disabled = true; }

function importWritable() { return Boolean(host.session?.authenticated && host.session.can_query && !host.session.read_only && !host.observing() && importPolicy?.available && importPolicy?.can_upload && libraryCollection && !uploadDialogState?.checkingUploads); }

function importManageable() { return Boolean(host.session?.authenticated && host.session.can_query && !host.session.read_only && !host.observing() && libraryCollection && (importPolicy?.can_manage_imports ?? (importPolicy?.available && importPolicy?.can_upload))); }

function importCurrentScope() { return `${host.storageIdentity || ''}:${libraryCollection}`; }

function importFailure(error) { return ({source_service_unavailable:'Video uploads are paused for this workspace.',embedding_allowance_required:'An upload allowance is needed for this workspace.',embedding_allowance_reserved:'The current upload allowance is reserved.',workspace_access_required:'Open the workspace that owns this upload.',embedding_authorized_scope_exceeded:'Choose a shorter recording for the current upload allowance.',file_size_limit:'Choose a video up to 256 MiB.',storage_quota:'Workspace storage is full. Ask your workspace owner for more space.',upload_capacity:'Four transfers are already open. Finish or cancel one to continue.',upload_receive_capacity:'The upload service is busy. Retry to continue.',upload_not_open:'This transfer has ended. Choose Upload files to start again.',chunk_conflict:'Choose the original file to resume this transfer.',idempotency_conflict:'Choose the original file to resume this transfer.',read_only_workspace:'Open your own workspace to upload files.',intake_unavailable:'Video uploads are paused for this workspace.',checksum_mismatch:'Choose the original file to resume this transfer.'})[error?.code] || (error?.code ? 'Review this upload with your workspace owner.' : error?.message) || 'Connection interrupted. Retry to continue from the saved progress.'; }

async function importRequest(path, {method = 'GET', body, headers = {}, work, retry = false, interruptible = true} = {}) {
    for (let attempt = 0; ; attempt++) {
      const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 30000);
      if (work && interruptible) work.controller = controller;
      try {
        const response = await fetch(path, {method, credentials:'same-origin', cache:'no-store', headers:{...(method === 'GET' ? {} : {'X-Rocoto-Page':'1'}), ...headers}, body, signal:controller.signal});
        const data = await response.json();
        if (!response.ok) { const error = new Error('The upload needs attention. Retry to check its saved progress.'); error.code = typeof data.detail === 'string' ? data.detail : ''; error.status = response.status; throw error; }
        return data;
      } catch (error) {
        if (retry && attempt === 0 && !error.status && (!work || !work.paused && work.generation === host.sessionGeneration)) continue;
        if (!error.status && error.name === 'AbortError') error.message = 'Connection interrupted. Retry to continue from the saved progress.';
        throw error;
      } finally { clearTimeout(timeout); if (work?.controller === controller) work.controller = null; }
    }
  }

function importPath(id) { return `/api/retail/library/imports/${encodeURIComponent(id)}`; }

function applyImport(row) {
    if (!row || !/^upload-[a-f0-9]{32}$/.test(row.id)) return;
    const previous = importRows.find(item => item.id === row.id), work = [...uploadWork.values()].find(item => item.info?.id === row.id);
    // Delayed poll/chunk replies cannot undo confirmed bytes, sealing or Stop.
    const phase = item => ({prepare:1,embed:2,publish:3}[item.stage] || 0);
    if (previous && (previous.committed_bytes > row.committed_bytes || ['ready','cancelled','expired'].includes(previous.status) && previous.status !== row.status || previous.transfer_status === 'sealed' && row.transfer_status === 'uploading' || ['received','preparing','indexing'].includes(previous.status) && ['received','preparing','indexing'].includes(row.status) && phase(previous) > phase(row))) { if (work) work.info = {...previous}; return; }
    if (previous) Object.assign(previous, row); else importRows.unshift({...row});
    if (work) work.info = {...row};
    if (['ready','cancelled','failed','expired'].includes(row.status)) removeUploadTicket(row.id);
    if (row.status === 'ready' && previous && previous._lastStatus && previous._lastStatus !== 'ready') { if (host.currentView === 'library' && galleryTab === 'media' && libraryCollection === row.collection) loadLibrary(); else if (host.currentView === 'cameras' && cameraCollection === row.collection) loadCameras?.(); }
    const saved = importRows.find(item => item.id === row.id); saved._lastStatus = row.status;
  }

async function loadImports() {
    if (!host.session?.authenticated || !(host.currentView === 'library' && galleryTab === 'media' || host.$('source-upload-dialog').open)) { renderImports(); return; }
    const scope = importCurrentScope();
    if (importScope !== scope) { ++importEpoch; importScope = scope; importRows = []; importPolicy = null; importError = ''; uploadNotice = ''; importLoading = false; }
    if (importLoading) return;
    const epoch = importEpoch, generation = host.sessionGeneration; importLoading = true; renderImports();
    try {
      const data = await importRequest(`/api/retail/library/imports${libraryCollection ? '?collection=' + encodeURIComponent(libraryCollection) : ''}`);
      if (epoch !== importEpoch || generation !== host.sessionGeneration || scope !== importCurrentScope()) return;
      importPolicy = data.intake || null; importError = ''; if (uploadDialogState) uploadDialogState.checkingUploads = false;
      if (!libraryCollection && importPolicy?.default_collection && importPolicy.collections?.includes(importPolicy.default_collection)) { libraryCollection = importPolicy.default_collection; importScope = importCurrentScope(); if (uploadDialogState) uploadDialogState.collection=libraryCollection; if(host.currentView==='library'){history.replaceState({}, '', libraryUrl(libraryCollection));loadLibrary();}else if(host.currentView==='cameras'&&!cameraCollection){cameraCollection=libraryCollection;loadCameras?.();} }
      for (const row of data.imports || []) if (row.collection === libraryCollection) applyImport(row);
      // Recover a retained transfer even when newer uploads fill the first page.
      for (const ticket of uploadTickets().filter(t => t.id && t.collection === libraryCollection && !importRows.some(row => row.id === t.id)).slice(-4)) {
        const detail = await importRequest(importPath(ticket.id));
        if (epoch !== importEpoch || generation !== host.sessionGeneration || scope !== importCurrentScope()) return;
        applyImport(detail.import);
      }
    } catch (error) { if (epoch === importEpoch && generation === host.sessionGeneration) { importError = 'Upload status is reconnecting. Check again to view saved progress.'; importPolicy = null; if (uploadDialogState) uploadDialogState.checkingUploads = false; } }
    finally { if (epoch === importEpoch && generation === host.sessionGeneration) { importLoading = false; renderImports(); clearTimeout(importTimer); importTimer = setTimeout(loadImports, 1500); } }
  }

function uploadStatus(row, work) {
    if (work?.cancelRequested && !['cancelled','ready'].includes(row.status)) return 'Cancelling…';
    if (row.status === 'cancelling') return 'Cancelling…';
    if (work?.error) return 'Upload paused';
    if (row.status === 'uploading' || !row.id) return work?.paused ? 'Paused' : work?.state === 'checking' ? 'Checking file…' : work?.state === 'starting' && work.busy ? 'Starting upload…' : work?.busy ? 'Uploading' : work?.file ? 'Queued' : 'Paused';
    if (row.status === 'ready') return 'Ready';
    if (row.status === 'cancelled') return 'Cancelled';
    if (['failed','needs_attention','needs_reconciliation','expired'].includes(row.status)) return row.status === 'expired' ? 'Transfer expired' : 'Needs attention';
    if (row.stage_status === 'queued') return {prepare:'Waiting for preparation',embed:'Waiting for indexing',publish:'Waiting to add to Gallery'}[row.stage] || 'Waiting for indexing';
    return {prepare:'Preparing video',embed:'Indexing video',publish:'Adding to Gallery'}[row.stage] || {received:'Waiting for preparation',preparing:'Preparing video',indexing:'Indexing video'}[row.status] || 'Checking upload status…';
  }

function renderImports() {
    if (host.$('source-upload-dialog').open) {
      const allowed = importWritable(), checking = Boolean(uploadDialogState?.checkingUploads), zone = host.$('upload-dropzone');
      host.$('upload-choose').disabled = !allowed; zone.setAttribute('aria-disabled', String(!allowed)); zone.setAttribute('aria-busy', String(checking));
      if (!allowed) zone.classList.remove('is-dragging');
      host.$('upload-drop-label').textContent = allowed ? 'Drop files here' : checking ? 'Checking availability…' : 'Video uploads paused';
      if (allowed && uploadDialogState) uploadDialogState.dropFeedback = '';
      const readiness = checking ? 'Checking upload availability…' : uploadNotice || importError || sourceLinkError || sourceLinkReadError || importPolicy?.notice || (!allowed ? 'Video uploads are paused for this workspace.' : '');
      const message = [readiness, uploadDialogState?.dropFeedback].filter(Boolean).join(' ');
      if (host.$('source-upload-status').textContent !== message) host.$('source-upload-status').textContent = message;
    }
    const button = host.$('gallery-upload'), section = host.$('gallery-imports'), list = host.$('gallery-import-list'), visible = host.currentView === 'library' && galleryTab === 'media' || host.$('source-upload-dialog').open;
    button.hidden = !visible; button.disabled = !host.pluginWritable(); button.title = host.observing() || host.session?.read_only ? 'Open your own workspace to upload files.' : !libraryCollection ? 'Choose a collection to upload files.' : importWritable() ? 'Upload videos up to 256 MiB' : (importPolicy?.notice || 'Video uploads are paused for this workspace.'); delete button.dataset.autoTitle;
    const storage=host.$('gallery-connect-storage'); storage.hidden=!visible; storage.disabled=!host.session?.can_query || host.session?.read_only || host.observing() || !libraryCollection;
    const state = host.$('gallery-upload-status'); state.textContent = uploadNotice || importError || (visible && importPolicy?.notice ? importPolicy.notice : '') || (visible && libraryCollection && importPolicy && !importPolicy.available ? 'Video uploads are paused for this workspace.' : ''); state.hidden = !state.textContent || !visible || host.$('source-upload-dialog').open;
    const records = importRows.filter(row => row.collection === libraryCollection).map(row => ({row,work:[...uploadWork.values()].find(work => work.info?.id === row.id)}));
    for (const work of uploadWork.values()) if (work.collection === libraryCollection && !work.info?.id) records.unshift({row:{filename:work.name,expected_bytes:work.size,committed_bytes:0,status:'uploading'},work});
    section.hidden = !visible || !records.length;
    host.$('gallery-upload-retry').hidden = !visible || !importError;
    host.$('gallery-import-count').textContent = String(records.length);
    const signature = JSON.stringify([importWritable(),importManageable(),records.map(({row,work}) => [row.id, row.filename,row.status,row.stage,row.stage_status,row.committed_bytes,row.expected_bytes,row.error,work?.key,work?.state,work?.paused,work?.busy,work?.error,work?.cancelRequested])]);
    if (list.dataset.signature === signature) return; list.dataset.signature = signature;
    const focus = list.contains(document.activeElement) ? {label:document.activeElement?.getAttribute('aria-label'),key:document.activeElement.closest('[data-import-key]')?.dataset.importKey} : null; list.replaceChildren();
    for (const {row,work} of records.slice(0,100)) {
      const item = host.node('li','gallery-import'), content = host.node('div','gallery-import-content'), heading = host.node('div','gallery-import-heading'), statusText = uploadStatus(row,work), active = !['ready','cancelled','failed','needs_attention','needs_reconciliation','expired'].includes(row.status);
      item.dataset.importId = row.id || work.key; item.dataset.importKey = work?.key || row.id; item.append(host.icon(row.status === 'ready' ? 'check' : 'video')); heading.append(host.node('strong','',row.filename),host.node('span','gallery-import-state',statusText)); content.append(heading);
      const bytes = host.node('p','gallery-import-bytes',`${host.fileSize(row.committed_bytes)} of ${host.fileSize(row.expected_bytes)} uploaded`); content.append(bytes);
      if (row.status === 'uploading' || row.status === 'ready') { const progress = host.node('progress','gallery-import-progress'); progress.max = Math.max(1,row.expected_bytes); progress.value = row.committed_bytes; progress.setAttribute('aria-label',`Uploaded bytes for ${row.filename}`); content.append(progress); }
      else if (active) { const line = host.node('div','gallery-import-stage'); line.setAttribute('aria-hidden','true'); content.append(line); }
      const problem = work?.error || (row.error && ['failed','needs_attention','needs_reconciliation'].includes(row.status) ? (row.transfer_status === 'sealed' ? 'Your file is saved. The indexing service needs attention before it can continue.' : 'Choose Upload files to start this transfer again.') : '');
      if (problem) content.append(host.node('p','gallery-import-error',problem));
      else if (row.status === 'uploading' && !work?.file && !uploadTickets().some(ticket => ticket.id === row.id)) content.append(host.node('p','gallery-import-bytes','Continue this transfer in the browser where it started.'));
      item.append(content); const actions = host.node('div','gallery-import-actions');
      const add = (label, action, symbol) => { const control = host.agentButton(label,action,false,symbol); control.setAttribute('aria-label',`${label} ${row.filename}`); control.disabled = !importManageable(); actions.append(control); };
      if (row.status === 'uploading' && !work?.cancelRequested) {
        if (work?.file && !work.paused) add('Pause',() => { work.paused = true; work.controller?.abort(); renderImports(); },'pause');
        else if (work?.file) add(work.error ? 'Retry' : 'Resume',() => { work.paused = false; work.error = ''; pumpUploads(); renderImports(); },work.error ? 'refresh' : 'play');
        else if (uploadTickets().some(ticket => ticket.id === row.id || !ticket.id && ticket.collection === row.collection)) add('Choose file to resume',() => chooseUpload(row.id),'upload');
      }
      if (active && !work?.cancelRequested && row.status !== 'cancelling') add('Cancel',() => cancelImport(row,work),'close');
      if (work?.cancelRequested && work.error) add('Retry cancel',() => cancelImport(row,work),'refresh');
      item.append(actions); list.append(item);
    }
    if (focus) ([...list.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === focus.label) || [...list.children].find(row => row.dataset.importKey === focus.key)?.querySelector('button'))?.focus({preventScroll:true});
  }

function chooseUpload(id = null) { if (!(id ? importManageable() : importWritable())) return; uploadTarget = id; const input = host.$('gallery-upload-input'); input.multiple = !id; input.value = ''; input.click(); }

const uploadHex = bytes => [...new Uint8Array(bytes)].map(value => value.toString(16).padStart(2,'0')).join('');

async function fileFingerprint(file,chunkBytes,work) {
    const count = Math.ceil(file.size/chunkBytes);
    if (count > 65536) throw new Error('Choose Upload files to start this transfer again.');
    const hashes = new Uint8Array(count * 32);
    for (let offset = 0,index = 0; offset < file.size; offset += chunkBytes,index++) {
      if (work.paused || work.cancelRequested || work.generation !== host.sessionGeneration) throw new DOMException('Paused','AbortError');
      hashes.set(new Uint8Array(await crypto.subtle.digest('SHA-256',await file.slice(offset,offset+chunkBytes).arrayBuffer())),index*32);
    }
    return uploadHex(await crypto.subtle.digest('SHA-256',await new Blob([String(file.size),':',hashes]).arrayBuffer()));
  }

async function acceptUploadFiles(dropped = null) {
    const input = host.$('gallery-upload-input'), selected = Array.isArray(dropped) ? dropped : Array.from(input.files), count = selected.length, files = selected.slice(0,4), target = uploadTarget; input.value = ''; uploadTarget = null;
    if (!(target ? importManageable() : importWritable())) return;
    uploadNotice = '';
    if (count > 4) { uploadNotice = 'Choose up to 4 files at a time.'; renderImports(); return; }
    const pending = [...uploadWork.values()].filter(work => work.file && !work.cancelRequested && (!work.info || work.info.transfer_status === 'uploading'));
    if (!target && pending.length + files.length > 4) { uploadNotice = 'Finish or cancel a transfer to add more files.'; renderImports(); return; }
    for (const file of files) {
      if (!file.size || file.size > UPLOAD_MAX_BYTES) { uploadNotice = 'Choose a video between 1 byte and 256 MiB.'; renderImports(); continue; }
      const row = target && importRows.find(row => row.id === target), ticket = row && uploadTickets().find(ticket => ticket.id === row.id);
      if (target && (!row || !ticket && !uploadTickets().some(item => !item.id && item.collection === row.collection))) continue;
      const key = ticket?.key || crypto.randomUUID(), work = {key, ticket, expectedImportId:target, info:row && ticket && {...row}, name:file.name,size:file.size,file,collection:libraryCollection,generation:host.sessionGeneration,chunkBytes:ticket?.chunkBytes || Math.min(UPLOAD_CHUNK_BYTES,Number(importPolicy.chunk_bytes_max) || UPLOAD_CHUNK_BYTES),paused:false,busy:false,error:'',state:'queued'};
      if (row && (file.name !== row.filename || file.size !== row.expected_bytes)) { work.file = null; work.error = 'Choose the original file to resume this transfer.'; work.paused = true; }
      uploadWork.set(key,work); if (target) break;
    }
    renderImports(); pumpUploads();
  }

async function pumpUploads() {
    if (uploadPumping) return; uploadPumping = true;
    try { for (;;) { const work = [...uploadWork.values()].find(item => item.generation === host.sessionGeneration && item.file && !item.paused && !item.cancelRequested && !item.busy && (!item.info || item.info.transfer_status === 'uploading')); if (!work) break; await sendUpload(work); } }
    finally { uploadPumping = false; }
  }

async function sendUpload(work) {
    const valid = () => work.generation === host.sessionGeneration && !work.paused && !work.cancelRequested;
    work.busy = true; work.error = ''; work.state = 'checking'; renderImports();
    try {
      const fingerprint = await fileFingerprint(work.file,work.chunkBytes,work); if (!valid()) return;
      if (work.ticket && work.ticket.fingerprint !== fingerprint) { work.file = null; throw Object.assign(new Error('Choose the original file to resume this transfer.'),{code:'chunk_conflict'}); }
      if (!work.ticket) {
        const existing = uploadTickets().find(ticket => ticket.collection === work.collection && ticket.fingerprint === fingerprint && ticket.chunkBytes === work.chunkBytes);
        if (work.expectedImportId && !existing) { work.file = null; throw Object.assign(new Error('Choose the original file to resume this transfer.'),{code:'chunk_conflict'}); }
        work.ticket = existing || {key:work.key,fingerprint,chunkBytes:work.chunkBytes,collection:work.collection,id:null};
        if (existing && existing.key !== work.key) { const prior = uploadWork.get(existing.key); if (prior) { prior.paused = true; prior.file = null; prior.controller?.abort(); } uploadWork.delete(work.key); work.key = existing.key; uploadWork.set(work.key,work); }
        if (existing?.id) work.info = (await importRequest(importPath(existing.id),{work})).import;
        if (work.generation !== host.sessionGeneration) return;
        saveUploadTicket(work.ticket);
      }
      if (!valid()) return;
      if (!work.info?.id) {
        work.admissionStarted = true; work.state = 'starting'; renderImports();
        const data = await importRequest('/api/retail/library/imports',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':work.ticket.key},body:JSON.stringify({collection:work.collection,filename:work.name,size:work.size}),work,retry:true,interruptible:false});
        if (work.generation !== host.sessionGeneration) return;
        work.info = data.import;
        if (work.expectedImportId && work.info.id !== work.expectedImportId) { work.file = null; throw Object.assign(new Error('Choose the original file to resume this transfer.'),{code:'chunk_conflict'}); }
        work.ticket.id = work.info.id; saveUploadTicket(work.ticket);
      } else work.info = (await importRequest(importPath(work.info.id),{work})).import;
      if (work.generation !== host.sessionGeneration) return;
      applyImport(work.info); if (!valid()) return;
      work.state = 'sending'; renderImports();
      while (work.info.transfer_status === 'uploading' && work.info.committed_bytes < work.size) {
        if (!valid()) return;
        const offset = work.info.committed_bytes, bytes = await work.file.slice(offset,offset + work.chunkBytes).arrayBuffer();
        const sha256 = uploadHex(await crypto.subtle.digest('SHA-256',bytes)); if (!valid()) return;
        const data = await importRequest(`${importPath(work.info.id)}/content`,{method:'PATCH',headers:{'Content-Type':'application/octet-stream','Upload-Offset':String(offset),'X-Chunk-SHA256':sha256},body:bytes,work,retry:true});
        if (work.generation !== host.sessionGeneration) return;
        applyImport(data.import); renderImports();
      }
      if (!valid()) return;
      if (work.info.transfer_status === 'uploading') { const data = await importRequest(`${importPath(work.info.id)}/complete`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}',work,retry:true}); if (work.generation !== host.sessionGeneration) return; applyImport(data.import); }
      work.file = null; work.state = 'complete'; removeUploadTicket(work.info.id);
    } catch (error) { if (work.generation === host.sessionGeneration && !work.paused && !work.cancelRequested) { work.error = importFailure(error); work.paused = true; } }
    finally { work.busy = false; if (work.generation === host.sessionGeneration) { if (work.cancelRequested) await cancelImport(work.info || {},work); renderImports(); } }
  }

async function cancelImport(row,work) {
    if (!importManageable()) return;
    const generation = host.sessionGeneration;
    if (work) { work.cancelRequested = true; work.paused = true; work.error = ''; work.controller?.abort(); if (work.busy) { renderImports(); return; } }
    try {
      // Recover the same admission when its response was lost before cancelling.
      // This can create only an empty transfer; it cannot allocate indexing work.
      if (!row.id && !work?.info?.id && work?.admissionStarted) {
        const recovered = await importRequest('/api/retail/library/imports',{method:'POST',headers:{'Content-Type':'application/json','Idempotency-Key':work.ticket.key},body:JSON.stringify({collection:work.collection,filename:work.name,size:work.size}),retry:true});
        if (generation !== host.sessionGeneration) return;
        work.info = recovered.import; work.ticket.id = work.info.id; saveUploadTicket(work.ticket); applyImport(work.info);
      }
      if (!row.id && !work?.info?.id) { if (work) uploadWork.delete(work.key); renderImports(); return; }
      const data = await importRequest(importPath(row.id || work.info.id),{method:'DELETE',retry:true}); if (generation !== host.sessionGeneration) return; applyImport(data.import); if (work) { work.file = null; work.cancelRequested = data.import.status === 'cancelling'; } }
    catch (error) { if (generation === host.sessionGeneration) { if (work) work.error = 'Reconnect to confirm cancellation.'; else importError = 'Reconnect to confirm cancellation.'; } }
    renderImports();
  }

host.$('close-source-upload').addEventListener('click',closeUploadDialog);
host.$('source-upload-dialog').addEventListener('cancel',event=>{event.preventDefault();closeUploadDialog();});
host.$('upload-choose').addEventListener('click',()=>chooseUpload());
host.$('source-link-form').addEventListener('submit',addSourceLink);
for(const type of ['dragenter','dragover'])host.$('upload-dropzone').addEventListener(type,event=>{event.preventDefault();const allowed=importWritable();host.$('upload-dropzone').classList.toggle('is-dragging',allowed);if(event.dataTransfer)event.dataTransfer.dropEffect=allowed?'copy':'none';});
host.$('upload-dropzone').addEventListener('dragleave',()=>host.$('upload-dropzone').classList.remove('is-dragging'));
host.$('upload-dropzone').addEventListener('drop',event=>{event.preventDefault();host.$('upload-dropzone').classList.remove('is-dragging');if(importWritable())acceptUploadFiles(Array.from(event.dataTransfer.files));else if(uploadDialogState){uploadDialogState.dropFeedback='Try this file again when uploads are ready.';renderImports();}});
host.$('gallery-upload').addEventListener('click', () => openUploadDialog(host.$('gallery-upload'),libraryCollection));
host.$('gallery-connect-storage').addEventListener('click', () => host.openCustomConnectorDialog(host.$('gallery-connect-storage'), {purpose:'source_import',collection:libraryCollection}));
host.$('gallery-upload-input').addEventListener('change', () => acceptUploadFiles());
host.$('gallery-upload-retry').addEventListener('click', loadImports);
let clipPicker = null, clipPickerGeneration = 0, clipPickerOpener = null;

const initialLibraryParams = new URL(location.href).searchParams;

let libraryCollection = initialLibraryParams.get('collection') || '', libraryFolder = initialLibraryParams.get('folder') || '', libraryAssetId = initialLibraryParams.get('asset') || '', libraryData = null, libraryAsset = null, libraryLoading = false, libraryError = '', libraryGeneration = 0, libraryEnvironment = null;

let librarySearchQuery = '', librarySearchedQuery = '', librarySearchResults = null, librarySearchElapsed = null, librarySearching = false;

let galleryTab = initialLibraryParams.get('gallery_tab') === 'files' ? 'files' : 'media';

let galleryView = 'grid';

function renderHomeSearch() {
    const container = host.$('home-search-results'), search = homeSearches.get(host.activeId); container.hidden = host.homeMode !== 'search' || !search; container.replaceChildren(); if (container.hidden) return;
    container.append(host.node('h1', '', 'Quick Search'), host.node('p', 'library-search-caption', `“${search.query}” · ${scopeLabel(search.dataset)}`));
    if (search.pending) { container.append(host.node('p', 'search-status', 'Searching footage…')); return; }
    if (search.error) { container.append(host.node('p', 'search-status', search.error)); return; }
    const results = search.results || []; container.append(host.node('p', 'library-search-caption', `${results.length} results${search.elapsed === null ? '' : ` · ${(search.elapsed / 1000).toFixed(2)} seconds`}`));
    search.selected ||= new Map(); const actions = host.node('div', 'search-investigate-bar'), count = host.node('span', 'workspace-help'), investigate = host.agentButton('Investigate in chat', () => { const chat = host.newChat(); chat.dataset = search.dataset; clipDrafts.set(chat.id, [...search.selected.values()]); host.elements.message.value = search.query; host.textDrafts.set(chat.id, search.query); host.save(); saveClipDrafts(); host.resizeComposer(); host.render(); host.elements.message.focus(); }, true); const update = () => { count.textContent = `${search.selected.size} clips selected`; investigate.disabled = !search.selected.size; }; actions.append(count, investigate); update(); container.append(actions);
    const grid = host.node('div', 'evidence-grid home-search-grid'); results.forEach((result, index) => { const card = evidenceCard?.(result, results, index, null); addClipSelection(card, result, search.selected, update); grid.append(card); }); container.append(grid);
  }

function addClipSelection(card, result, selected, changed) { const label = host.node('label', 'clip-selection'), checkbox = host.node('input'); checkbox.type = 'checkbox'; checkbox.checked = selected.has(result.id); checkbox.setAttribute('aria-label', `Select clip from ${result.camera || result.video_id || 'footage'}, ${host.time(result.start)} to ${host.time(result.end)}`); label.append(checkbox, host.node('span', 'sr-only', 'Select clip')); label.addEventListener('click', (event) => event.stopPropagation()); checkbox.addEventListener('change', () => { if (checkbox.checked && selected.size >= 12) { checkbox.checked = false; host.notice('Choose up to 12 clips for one message.'); return; } if (checkbox.checked) selected.set(result.id, result); else selected.delete(result.id); card.classList.toggle('clip-selected', checkbox.checked); changed(); }); card.classList.toggle('clip-selected', checkbox.checked); card.querySelector('.evidence-media').append(label); }

async function searchHome() { host.showHome(); }

async function legacySearchHome() {
    const query = host.elements.message.value.trim(); if (!query || homeSearches.get(host.activeId)?.pending) return;
    if (host.demoEnvironment() && !host.activeChat()?.dataset) { if(host.founderAllowed())host.openFounder();return; }
    if (!host.session?.can_query) { host.showSession('Enter your access code to search this footage.'); return; }
    const chat = host.activeChat() || host.newChat(), dataset = chat.dataset || '', environment = host.session.environment.id, search = { query, dataset, pending: true, results: [], elapsed: null }; host.homeMode = 'search'; homeSearches.set(chat.id, search); host.render();
    try { const response = await host.request('/api/retail/search', { query, limit: 24, dataset }), data = await response.json(); if (homeSearches.get(chat.id) !== search || host.session?.environment?.id !== environment) return; search.results = Array.isArray(data.results) ? data.results : []; search.elapsed = Number.isFinite(data.elapsed_ms) ? data.elapsed_ms : null; }
    catch (error) { search.error = error.message; }
    finally { search.pending = false; if (chat.id === host.activeId && host.currentView === 'chat') host.render(); }
  }

function libraryUrl(collection, folder = '', asset = '') { const url = new URL('/', location.href); url.searchParams.set('library', '1'); if (collection) url.searchParams.set('collection', collection); if (folder) url.searchParams.set('folder', folder); if (asset) url.searchParams.set('asset', asset); return url.pathname + url.search; }

function galleryScopes() { const result = host.scopes(); for (const id of importPolicy?.collections || []) if (!result.some(scope => scope.id === id)) result.push({id,label:id === importPolicy.default_collection ? host.session?.environment?.name || 'Company' : datasetLabel(id),videos:[]}); return result; }

function renderGalleryContext() { const list = host.$('gallery-context-sources'); list.replaceChildren(); for (const scope of galleryScopes()) { const button = host.node('button', 'context-source'); button.type = 'button'; button.setAttribute('aria-current', scope.id === libraryCollection ? 'page' : 'false'); button.append(host.icon('video'), host.node('span', '', scope.label)); button.addEventListener('click', () => showLibrary(scope.id, '', '', galleryTab)); list.append(button); if (galleryTab === 'media' && scope.id === libraryCollection) for (const folder of libraryData?.folders || []) { const nested = host.node('button', 'context-source nested-source'); nested.type = 'button'; nested.append(host.icon('folder'), host.node('span', '', folder.name)); nested.addEventListener('click', () => showLibrary(scope.id, folder.path)); list.append(nested); } } }

function showLibrary(collection = null, folder = '', asset = '', tab = 'media') { host.showHome(); }

function legacyLibraryRecordView(collection = null, folder = '', asset = '', tab = 'media') {
    galleryTab = tab;
    document.body.classList.remove('context-nav-open');
    host.persistDraft(); clearTimeout(host.agentsTimer); host.currentView = 'library'; libraryCollection = collection ?? libraryCollection ?? ''; if (!libraryCollection) libraryCollection = host.demoEnvironment()?host.currentCollection():canonicalScope(host.activeChat()?.dataset || ''); libraryFolder = folder; libraryAssetId = asset; libraryData = null; libraryAsset = null; libraryError = ''; librarySearchResults = null; librarySearchElapsed = null; librarySearching = false; const galleryUrl = new URL(libraryUrl(libraryCollection, folder, asset), location.href); if (galleryTab === 'files') galleryUrl.searchParams.set('gallery_tab', 'files'); history.replaceState({}, '', galleryUrl); host.render(); host.$('library-view').scrollTop = 0; host.setSidebar(false); if (galleryTab === 'media') loadLibrary(); else host.loadFiles();
  }

async function loadLibrary() {
    loadImports?.();
    const generation = ++libraryGeneration, environment = host.session?.environment?.id; if (!environment || host.currentView !== 'library') return;
    if (libraryEnvironment && libraryEnvironment !== environment) { libraryCollection = ''; libraryFolder = ''; libraryAssetId = ''; libraryData = null; libraryAsset = null; }
    libraryEnvironment = environment; libraryLoading = Boolean(libraryCollection); libraryError = ''; renderLibrary(); if (!libraryCollection) return;
    try {
      const params = new URLSearchParams({ collection: libraryCollection, folder: libraryFolder }); const response = await fetch(`/api/retail/library?${params}`); if (!response.ok) throw new Error('The source gallery is reconnecting. Choose the collection again to retry.'); const data = await response.json(); if (generation !== libraryGeneration || environment !== host.session?.environment?.id) return;
      libraryData = data; libraryFolder = data.folder ?? libraryFolder;
      if (libraryAssetId) { const assetResponse = await fetch(`/api/retail/assets/${encodeURIComponent(libraryAssetId)}?collection=${encodeURIComponent(libraryCollection)}`); if (!assetResponse.ok) throw new Error('Choose a source file from this collection to view its details.'); const asset = await assetResponse.json(); if (generation !== libraryGeneration || environment !== host.session?.environment?.id) return; libraryAsset = asset.asset || asset; }
    } catch (error) { if (generation === libraryGeneration) libraryError = error.message; }
    finally { if (generation === libraryGeneration) { libraryLoading = false; if (host.currentView === 'library') renderLibrary(); } }
  }

async function searchLibrary(event) { event.preventDefault(); host.showHome(); }

async function legacySearchLibrary(event) {
    event.preventDefault(); const query = host.$('library-search-query').value.trim(); librarySearchQuery = query; if (!query || !libraryCollection || librarySearching) return;
    if (!host.session?.can_query) { host.showSession('Enter your access code to search this footage.'); return; }
    const collection = libraryCollection, environment = host.session.environment.id, generation = ++libraryGeneration; librarySearching = true; libraryError = ''; host.$('library-search-submit').disabled = true; host.$('library-search-submit').textContent = 'Searching…';
    try { const response = await host.request('/api/retail/search', { query, limit: 24, dataset: collection }), data = await response.json(); if (generation !== libraryGeneration || libraryCollection !== collection || host.session?.environment?.id !== environment) return; librarySearchResults = Array.isArray(data.results) ? data.results : []; librarySearchedQuery = query; librarySearchElapsed = Number.isFinite(data.elapsed_ms) ? data.elapsed_ms : null; libraryAsset = null; libraryAssetId = ''; history.replaceState({}, '', libraryUrl(collection, libraryFolder)); }
    catch (error) { if (generation === libraryGeneration) libraryError = error.message; }
    finally { if (generation === libraryGeneration) { librarySearching = false; libraryLoading = false; if (host.currentView === 'library') renderLibrary(); } }
  }

function applyGalleryView() { document.body.classList.toggle('gallery-list-view', galleryView === 'list'); document.body.classList.toggle('gallery-grid-view', galleryView === 'grid'); document.querySelectorAll('[data-gallery-view]').forEach((button) => button.setAttribute('aria-pressed', String(button.dataset.galleryView === galleryView))); }

function setGalleryView(value) { galleryView = value === 'list' ? 'list' : 'grid'; try { localStorage.setItem('rocoto-gallery-view', galleryView); } catch {} applyGalleryView(); }

function renderLibrary() {
    host.placeCompanyPicker(); host.collectionNavigation();
    if (host.currentView === 'library') host.elements['chat-title'].textContent = galleryTab === 'files' ? 'Files' : scopeLabel(libraryCollection);
    applyGalleryView(); renderGalleryContext(); renderImports?.();
    document.querySelector('#library-view > .library-heading').hidden=Boolean(libraryAssetId); const generated = galleryTab === 'files'; host.$('gallery-media-tab').setAttribute('aria-pressed', String(!generated)); host.$('workspace-files-tab').setAttribute('aria-pressed', String(generated)); host.$('generated-files').hidden = !generated; host.$('library-layout').hidden = generated; host.$('library-breadcrumbs').hidden = generated; host.$('library-search').hidden = generated; host.$('workspace-file-search-wrap').hidden = !generated; host.$('gallery-description').textContent = generated ? 'Reports, documents and editing packages from your chats.' : 'Original files and their indexed intervals.';
    if (generated) { host.renderCollectionControl('library-collection-control',libraryCollection,[{id:'',label:'All collections'},...host.scopes()],value=>showLibrary(value,'','',galleryTab)); host.$('workspace-message').textContent = host.filesError || (host.filesLoading ? 'Loading files…' : ''); host.updateFilesSurface(); host.renderGeneratedFiles(); return; }
    if (libraryEnvironment && host.session?.environment?.id && libraryEnvironment !== host.session.environment.id) { libraryCollection = ''; libraryFolder = ''; libraryAssetId = ''; libraryData = null; libraryAsset = null; librarySearchResults = null; librarySearchQuery = ''; }
    const choices = galleryScopes(), message = host.$('workspace-message'), files = host.$('library-files'), detail = host.$('library-asset'), layout = host.$('library-layout'), crumbs = host.$('library-breadcrumbs');
    host.renderCollectionControl('library-collection-control',libraryCollection,choices,value=>showLibrary(value,'','',galleryTab)); host.$('library-search').hidden = !libraryCollection; if (host.$('library-search-query').value !== librarySearchQuery) host.$('library-search-query').value = librarySearchQuery; host.$('library-search-query').placeholder = `Search ${scopeLabel(libraryCollection)} footage…`; host.$('library-search-submit').disabled = librarySearching || host.observing(); host.$('library-search-query').readOnly = host.observing(); host.$('library-search-submit').textContent = librarySearching ? 'Searching…' : 'Search';
    message.replaceChildren(); message.hidden = false; files.replaceChildren(); detail.replaceChildren(); detail.hidden = !libraryAsset; layout.classList.toggle('has-asset', Boolean(libraryAsset)); crumbs.replaceChildren();
    if (!libraryCollection) { message.append(host.node('p', '', 'Choose a collection to browse its original files.')); const chooser = host.node('div', 'source-choices'); for (const scope of choices) { const button = host.node('button', 'source-choice'); button.append(host.icon('folder'), host.node('strong', '', scope.label), host.node('small', '', videoCount(scope.videos))); button.addEventListener('click', () => showLibrary(scope.id)); chooser.append(button); } message.append(chooser); return; }
    const crumb = (label, path) => { const link = host.node('button', 'text-button', label); link.type = 'button'; link.addEventListener('click', () => showLibrary(libraryCollection, path)); crumbs.append(link); };
    crumb(scopeLabel(libraryCollection), ''); let path = ''; for (const part of libraryFolder.split('/').filter(Boolean)) { crumbs.append(host.icon('chevron')); path = path ? `${path}/${part}` : part; crumb(part, path); }
    if (libraryError) message.append(host.node('p', '', libraryError), host.agentButton('Retry', loadLibrary)); else if (libraryLoading) message.textContent = 'Loading source files…'; else message.hidden = true;
    if (librarySearchResults !== null) { const results = librarySearchResults, heading = host.node('div', 'library-results-heading'); heading.append(host.node('h2', '', `${results.length} search ${results.length === 1 ? 'result' : 'results'}`), host.agentButton('Back to files', () => { librarySearchResults = null; renderLibrary(); })); files.append(heading, host.node('p', 'library-search-caption', `“${librarySearchedQuery}” · ${scopeLabel(libraryCollection)}${librarySearchElapsed === null ? '' : ` · ${(librarySearchElapsed / 1000).toFixed(2)} seconds`}`)); const grid = host.node('div', 'evidence-grid library-search-results'); results.forEach((result, index) => grid.append(evidenceCard?.(result, results, index, null))); files.append(grid); return; }
    if (!libraryData) return;
    const entries = [...(libraryData.folders || []).map((folder) => ({ folder })), ...(libraryData.assets || []).map((asset) => ({ asset }))], heading = host.node('div', 'library-file-heading'); heading.append(host.node('span', '', 'Name'), host.node('span', '', 'Duration'), host.node('span', '', 'Size')); files.append(heading);
    if (!entries.length && !libraryError && !libraryLoading) files.append(host.workspaceEmpty('folder','Source files','Uploaded recordings appear here.',host.pluginWritable()?[host.agentButton('Upload files',event=>openUploadDialog?.(event.currentTarget,host.currentView==='cameras'?cameraCollection:libraryCollection),false,'upload')]:[]));
    let shown = 0; const more = host.node('button', 'text-button library-more', 'Load more files'); more.type = 'button';
    const appendFiles = () => { for (const end = Math.min(shown + 100, entries.length); shown < end; shown++) { const { folder, asset } = entries[shown], row = host.node('button', 'library-file'); row.type = 'button'; if (asset) row.setAttribute('aria-current', asset.asset_id === libraryAssetId ? 'true' : 'false'); const name = host.node('span', 'library-file-name'); const thumbnail = asset && host.safeUrl(asset.thumbnail_url || asset.thumbnail || ''); if (thumbnail) { const image = host.node('img', 'library-file-thumbnail'); image.src = thumbnail; image.alt = ''; image.loading = 'lazy'; name.append(image); } else name.append(host.icon(folder ? 'folder' : 'video'));  const text = host.node('span'); text.append(host.node('strong', '', folder?.name || asset.filename)); if (folder) { if (Number.isFinite(folder.asset_count)) text.append(host.node('small', '', `${folder.asset_count} source files`)); } else text.append(host.node('small', '', asset.camera || asset.relative_path || 'Original video')); name.append(text); row.append(name, host.node('span', 'library-file-duration', asset ? host.time(asset.duration) : ''), host.node('span', 'library-file-size', asset ? host.fileSize(asset.bytes) : '')); row.addEventListener('click', () => showLibrary(libraryCollection, folder ? folder.path : libraryFolder, asset?.asset_id || '')); files.insertBefore(row, more.isConnected ? more : null); } more.hidden = shown >= entries.length; };
    more.addEventListener('click', appendFiles); appendFiles(); files.append(more);
    if (!libraryAsset) return;
    const asset = libraryAsset, back = host.agentButton('Gallery', () => showLibrary(libraryCollection, libraryFolder), false, 'chevron'); back.classList.add('library-back'); detail.append(back, host.node('h2', '', asset.filename), host.node('p', 'library-asset-label', 'Imported source file'));
    const thumbnail = host.safeUrl(asset.thumbnail_url || asset.thumbnail || ''); if (thumbnail) { const image = host.node('img', 'library-asset-thumbnail'); image.src = thumbnail; image.alt = `Source thumbnail for ${asset.filename}`; detail.append(image); }
    const downloadUrl = host.safeUrl(asset.original_download_url || `/originals/${encodeURIComponent(asset.asset_id)}?collection=${encodeURIComponent(libraryCollection)}`); if (downloadUrl && new URL(downloadUrl).origin === location.origin) { const download = host.node('a', 'agent-primary library-download', 'Download original'); download.prepend(host.icon('download')); download.href = downloadUrl; download.download = asset.filename; detail.append(download); }
    const metadata = host.node('dl', 'workspace-metadata'); for (const [label, value] of [['Camera', asset.camera], ['Duration', preciseTime(asset.duration)], ['Resolution', asset.width && asset.height ? `${asset.width} × ${asset.height}` : ''], ['Frame rate', asset.fps ? `${Number(asset.fps).toFixed(3).replace(/\.?0+$/, '')} fps` : ''], ['File size', host.fileSize(asset.bytes)], ['Path', asset.relative_path || asset.folder], ['Asset ID', asset.asset_id]]) { if (value === undefined || value === null || value === '') continue; metadata.append(host.node('dt', '', label), host.node('dd', '', String(value))); } detail.append(metadata);
    const sourceReference = typeof asset.source_reference === 'string' ? host.safeUrl(asset.source_reference) : host.safeUrl(asset.source_reference?.url); if (sourceReference) { const reference = host.node('a', 'source-reference', 'Source reference'); reference.href = sourceReference; reference.target = '_blank'; reference.rel = 'noopener noreferrer'; detail.append(reference); }
    if (asset.camera_semantics) detail.append(host.node('p', 'library-interval-note', asset.camera_semantics)); if (asset.provenance) { const provenance = host.node('details', 'library-source-provenance'); provenance.append(host.node('summary', '', 'Source provenance')); if (typeof asset.provenance === 'string') provenance.append(host.node('p', '', asset.provenance)); else { const values = host.node('dl', 'workspace-metadata'); for (const [key, value] of Object.entries(asset.provenance)) { if (value === null || value === undefined) continue; values.append(host.node('dt', '', host.humanize(key)), host.node('dd', '', typeof value === 'object' ? JSON.stringify(value) : String(value))); } provenance.append(values); } detail.append(provenance); }
    const intervals = (asset.clips || []).map((clip) => ({ ...clip, video_id: asset.asset_id, dataset: clip.dataset || asset.collection || libraryCollection, camera: clip.camera || asset.camera })), intervalsHeading = host.node('div', 'library-interval-heading'); intervalsHeading.append(host.node('h3', '', 'Indexed intervals'), host.node('span', '', String(intervals.length))); detail.append(intervalsHeading, host.node('p', 'library-interval-note', 'Each interval opens its bounded evidence clip.'));
    const grid = host.node('div', 'evidence-grid library-intervals'); let count = 0; const load = host.node('button', 'text-button library-more', 'Load more intervals'); load.type = 'button'; const appendIntervals = () => { for (const end = Math.min(count + 12, intervals.length); count < end; count++) grid.append(evidenceCard?.(intervals[count], intervals, count, null)); load.hidden = count >= intervals.length; }; load.addEventListener('click', appendIntervals); appendIntervals(); detail.append(grid, load);
  }

function clipChip(clip, remove = null) { const item = host.node('div', 'clip-attachment-chip'), open = host.node('button'); open.type = 'button'; open.append(host.icon('video'), host.node('span', '', `${clip.camera || clip.video_id || 'Video clip'}${Number.isFinite(clip.start) ? ` · ${preciseTime(clip.start)}–${preciseTime(clip.end)}` : ''}`)); open.addEventListener('click', () => openEvidenceId?.(clip.id, open)); item.append(open); if (remove) { const button = host.node('button', 'icon-button'); button.type = 'button'; button.setAttribute('aria-label', `Remove clip ${clip.camera || clip.id}`); button.append(host.icon('close')); button.disabled=host.draftAdmitting();button.addEventListener('click', remove); item.append(button); } return item; }

function renderClipAttachments() { const container = host.$('clip-attachment-previews'), clips = clipDrafts.get(host.activeId) || []; container.replaceChildren(); container.hidden = !clips.length || host.homeMode === 'search'; clips.forEach((clip) => container.append(clipChip(clip, () => { if(host.draftAdmitting())return;clipDrafts.set(host.activeId, (clipDrafts.get(host.activeId) || []).filter((item) => item.id !== clip.id)); saveClipDrafts(); renderClipAttachments(); host.updateMode(); }))); }

function openClipPicker() { if(host.draftAdmitting())return; host.toggleAttachmentMenu(false); if (host.demoEnvironment() && !host.activeChat()?.dataset) { if(host.founderAllowed())host.openFounder();return; } const chat = host.activeChat() || host.newChat(); clipPickerOpener = document.activeElement; clipPicker = { chatId: chat.id, dataset: chat.dataset, environment: host.session?.environment?.id, selected: new Map((clipDrafts.get(chat.id) || []).map((clip) => [clip.id, clip])), folder: '', asset: null, data: null, results: null, loading: false, error: '' }; host.$('clip-picker-query').value = ''; host.$('clip-picker').showModal(); loadClipPicker(); }

async function loadClipPicker(folder = '', assetId = '') { const picker = clipPicker, generation = ++clipPickerGeneration; if (!picker) return; picker.folder = folder; picker.asset = null; picker.results = null; picker.loading = true; picker.error = ''; renderClipPicker(); try { const path = assetId ? `/api/retail/assets/${encodeURIComponent(assetId)}?collection=${encodeURIComponent(picker.dataset)}` : `/api/retail/library?${new URLSearchParams({ collection: picker.dataset, folder })}`, response = await fetch(path); if (!response.ok) throw new Error('Gallery files are reconnecting. Open the folder again to retry.'); const data = await response.json(); if (generation !== clipPickerGeneration || picker !== clipPicker) return; if (assetId) { picker.asset = data.asset || data; picker.results = picker.asset.clips || []; } else picker.data = data; } catch (error) { picker.error = error.message; } finally { if (generation === clipPickerGeneration && picker === clipPicker) { picker.loading = false; renderClipPicker(); } } }

function renderClipPicker() { applyGalleryView(); const picker = clipPicker; if (!picker) return; host.$('clip-picker-scope').textContent = `${scopeLabel(picker.dataset)} · Select source intervals`; host.$('clip-picker-count').textContent = `${picker.selected.size} of 12 clips selected`; host.$('add-picker-clips').disabled = !picker.selected.size; host.$('clip-picker-message').textContent = picker.error || (picker.loading ? 'Loading clips…' : ''); host.$('clip-picker-search-submit').disabled = picker.loading; const crumbs = host.$('clip-picker-breadcrumbs'), content = host.$('clip-picker-content'); crumbs.replaceChildren(); content.replaceChildren(); const root = host.node('button', 'text-button', scopeLabel(picker.dataset)); root.type = 'button'; root.addEventListener('click', () => loadClipPicker()); crumbs.append(root); let path = ''; for (const part of picker.folder.split('/').filter(Boolean)) { path = path ? `${path}/${part}` : part; const folder = path, button = host.node('button', 'text-button', part); button.type = 'button'; button.addEventListener('click', () => loadClipPicker(folder)); crumbs.append(host.icon('chevron'), button); } if (picker.asset) crumbs.append(host.icon('chevron'), host.node('span', '', picker.asset.filename)); if (picker.loading) return;
    if (picker.results) { const results = picker.results, grid = host.node('div', 'evidence-grid picker-grid'); results.forEach((result, index) => { const normalized = { ...result, thumbnail: result.thumbnail || result.thumbnail_url }, card = evidenceCard?.(normalized, results, index, null); addClipSelection(card, normalized, picker.selected, () => { host.$('clip-picker-count').textContent = `${picker.selected.size} of 12 clips selected`; host.$('add-picker-clips').disabled = !picker.selected.size; }); grid.append(card); }); content.append(grid); return; }
    for (const folder of picker.data?.folders || []) { const row = host.agentButton(folder.name, () => loadClipPicker(folder.path), false, 'folder'); row.classList.add('picker-file'); content.append(row); } for (const asset of picker.data?.assets || []) { const row = host.agentButton(asset.filename, () => loadClipPicker(picker.folder, asset.asset_id), false, 'video'); row.append(host.node('small', '', preciseTime(asset.duration))); row.classList.add('picker-file'); content.append(row); }
  }

async function searchClipPicker(event) { event.preventDefault(); }

async function legacySearchClipPicker(event) { event.preventDefault(); const picker = clipPicker, query = host.$('clip-picker-query').value.trim(); if (!picker || !query || picker.loading) return; if (!host.session?.can_query) { host.$('clip-picker').close(); host.showSession('Enter your access code to search clips.'); return; } const generation = ++clipPickerGeneration; picker.loading = true; picker.error = ''; renderClipPicker(); try { const response = await host.request('/api/retail/search', { query, dataset: picker.dataset, limit: 24 }), data = await response.json(); if (generation !== clipPickerGeneration || picker !== clipPicker) return; picker.results = data.results || []; picker.asset = null; } catch (error) { picker.error = error.message; } finally { if (generation === clipPickerGeneration && picker === clipPicker) { picker.loading = false; renderClipPicker(); } } }

try { galleryView = localStorage.getItem('rocoto-gallery-view') === 'list' ? 'list' : 'grid'; } catch {}
document.querySelectorAll('[data-gallery-view]').forEach((button) => { if (!button.querySelector('svg')) button.prepend(host.icon(button.dataset.galleryView === 'list' ? 'list' : 'grid')); });
document.querySelectorAll('[data-gallery-view]').forEach((button) => button.addEventListener('click', () => setGalleryView(button.dataset.galleryView)));
applyGalleryView();
host.$('workspace-file-search').addEventListener('input',event=>{host.fileSearch=event.target.value.toLowerCase();host.renderGeneratedFiles();});
host.$('library-nav')?.addEventListener('click', () => { showLibrary(); });
host.$('library-shortcut').addEventListener('click', () => showLibrary());
host.$('gallery-media-tab').addEventListener('click', () => showLibrary(libraryCollection));
host.$('workspace-files-tab').addEventListener('click', () => showLibrary(libraryCollection, '', '', 'files'));
host.$('library-search').addEventListener('submit', searchLibrary);
host.$('library-search-query').addEventListener('input', (event) => { librarySearchQuery = event.target.value; });
host.$('attach-video').addEventListener('click', openClipPicker);
host.$('close-clip-picker').addEventListener('click', () => host.$('clip-picker').close());
host.$('clip-picker-search').addEventListener('submit', searchClipPicker);
host.$('add-picker-clips').addEventListener('click', () => { if (!clipPicker || host.draftAdmitting(clipPicker.chatId) || clipPicker.environment !== host.session?.environment?.id) return; clipDrafts.set(clipPicker.chatId, [...clipPicker.selected.values()]); saveClipDrafts(); renderClipAttachments(); host.updateMode(); host.$('clip-picker').close(); });
host.$('clip-picker').addEventListener('close', () => { ++clipPickerGeneration; if (clipPickerOpener?.isConnected) clipPickerOpener.focus(); });
  function updateAgentEstimate() {
    const fresh = Number(host.$('agent-fresh-inspections').value), count = Math.max(1, Math.min(20, Number(host.$('agent-max-runs').value) || 1)), ceiling = Number(host.agentCapabilities.fresh_inspection_ceiling_usd), estimate = host.$('agent-credit-estimate');
    if (!fresh) estimate.replaceChildren(document.createTextNode('Uses existing model observations. Fresh GPU credit: '), host.node('span', 'agent-price', '$0.00 per run'), document.createTextNode('.'));
    else if (Number.isFinite(ceiling) && ceiling > 0) { estimate.replaceChildren(document.createTextNode('GPU credit ceiling: '), host.node('span', 'agent-price', `${host.money(ceiling)} per run`), document.createTextNode(` · ${host.money(ceiling * count)} total for ${count} ${count === 1 ? 'run' : 'runs'}.`)); }
    else estimate.textContent = 'Fresh-inspection credit estimates are loading.';
    if (host.agentCapabilities.available_credit_usd !== undefined && Number.isFinite(Number(host.agentCapabilities.available_credit_usd))) estimate.append(document.createTextNode(` Available credit: ${host.money(Number(host.agentCapabilities.available_credit_usd))}.`));
  }

    host.$('agent-fresh-inspections').addEventListener('input',updateAgentEstimate);
    host.$('agent-collection-label').textContent='Footage collection';
    host.$('workspace-popover').setAttribute('aria-label','Filter footage');
    host.$('workspace-search').placeholder='Search footage…';
    document.querySelector('label[for="workspace-search"]').textContent='Search footage collections';
    // The shared shell sees lifecycle and content hooks; retail state stays here.
    return {
      resetScope:resetBusinessScopes,renderChatContext:renderChatLocations,loadBusinessContext:loadBusinessScope,
      renderBusinessSections(content,collection){content.append(businessScopeSurface(collection));},
      renderConnectorDetails:renderStorageSetup,resetConnectorDetails:resetStorageSetup,
      connectorStatus(item){return item.kind==='sftp'&&item.storage_setup?storageStatus(item.storage_setup):null;},
      connectorDialog(options){return options.purpose==='source_import'?{title:'Connect storage',introduction:'Connect a recording archive, shared folder or storage service. Rocoto will help you set it up in a chat, including new files as they arrive.',kind:'sftp',namePlaceholder:'Recording archive',descriptionPlaceholder:'Connect our SFTP, FTP, object store or NAS and import new recordings.'}:null;},
      renderBusinessContext(facts,data){if(data.footage_relationship?.basis)facts.append(host.node('p','workspace-help',data.footage_relationship.basis));},
      renderFilePreview(container,file){if(file.kind!=='media_export')return false;container.append(host.node('p','workspace-help',file.status==='ready'?'Editing package with source clips, timing and an edit plan.':`${host.humanize(file.status)} editing package.`));return true;},
      buttonIcon(label){return {'Connect us to your camera':'video','Confirm scene context':'check'}[label];},
      renderWelcomeExamples(chat,{needsScope,quick}){    host.elements.welcome.querySelector('.suggestions').hidden=!host.demoEnvironment()||needsScope||quick||Boolean(chat?.agent_setup);
    const guidance = host.demoEnvironment() ? host.status?.demo_guidance?.[canonicalScope(chat?.dataset || '')] : null, suggestions = host.elements.welcome.querySelector('.suggestions');
    host.elements.welcome.querySelector('.demo-company-context')?.remove();
    const companyUrl = host.safeUrl(guidance?.company_url);
    if (!host.elements.welcome.hidden && !needsScope && !quick && !chat?.agent_setup && /^\/assets\/demo-companies\/[a-z0-9-]+\.(png|svg|webp)$/.test(guidance?.brand_image || '') && companyUrl?.startsWith('https://')) {
      const context = host.node('div','demo-company-context'), link = host.node('a'), logo = host.node('img'); link.href = companyUrl; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.setAttribute('aria-label',`${guidance.name} website`); logo.src = guidance.brand_image; logo.alt = guidance.name; link.append(logo); context.append(link); if (guidance.description) context.append(host.node('p','',guidance.description)); suggestions.before(context);
    }
    suggestions.replaceChildren();
    const questions = Array.isArray(guidance?.questions) ? guidance.questions.filter(question => typeof question?.label === 'string' && typeof question?.prompt === 'string').slice(0, 4) : [];
    suggestions.hidden ||= !questions.length;
    if (!suggestions.hidden) for (const question of questions) { const button = host.node('button'); button.type = 'button'; button.dataset.prompt = question.prompt; button.append(document.createTextNode(question.label), host.icon('chevron')); button.addEventListener('click', () => { host.elements.message.value = question.prompt; host.resizeComposer(); host.updateMode(); host.elements.message.focus(); }); suggestions.append(button); }
},
      updateStatus(status){const corpus=Array.isArray(status.corpus)?status.corpus:[];
      const seconds = corpus.reduce((sum, video) => sum + (Number(video.duration) || 0), 0);
      host.$('corpus-summary').textContent = `${corpus.length} videos · ${Math.round(seconds / 60)} min`;
      host.elements.dataset.replaceChildren(host.node('option', '', 'All footage')); const datasets = [...new Set(corpus.map((video) => video.dataset).filter(Boolean))];
      datasets.forEach((dataset) => { const option = host.node('option', '', datasetLabel(dataset)); option.value = dataset; host.elements.dataset.append(option); });
      host.$('corpus-list').replaceChildren();
      corpus.forEach((video) => { const row = host.node('div', 'corpus-row'); row.append(host.node('strong', '', host.humanize(video.camera || video.video_id)), host.node('span', '', `${datasetLabel(video.dataset)} · ${host.time(video.duration)}`)); host.$('corpus-list').append(row); });
},
      renderAgentDetails(selected,detail,technical){    if (selected.watch_plan?.families?.length) { const criteria = host.node('section', 'agent-detail-section'); criteria.append(host.node('h3', '', 'What it looks for')); const queries = host.node('section', 'watch-query-details'); queries.append(host.node('h3', '', 'Candidate retrieval')); for (const family of selected.watch_plan.families) { const item = host.node('div', 'watch-criterion'); item.append(host.node('strong', '', family.observable), host.node('p', 'workspace-help', family.verification_question)); criteria.append(item); const query = host.node('div', 'watch-criterion'); query.append(host.node('strong', '', family.observable), host.node('p', 'workspace-help', (family.queries || []).join(' · ')), host.node('p', 'workspace-help', `Candidate score threshold: ${family.score_threshold}. ${family.threshold_basis}`)); queries.append(query); } detail.insertBefore(criteria, technical); technical.append(queries); }
    if (selected.mode==='ongoing'&&selected.screening_status==='plan_review_required') { const review = host.node('div', 'agent-plan-review'); review.append(host.node('p', '', 'Review the watch criteria in chat before activating.')); detail.insertBefore(review, technical); }
    if (selected.last_screening) { const screening = host.node('section', 'agent-detail-section'); screening.append(host.node('h3', '', 'Latest footage check'), host.node('p', 'workspace-help', `${selected.last_screening.screened_window_count} windows checked · ${selected.last_screening.candidate_window_count} candidate windows · ${host.agentDate(selected.last_screening.created_at)}`)); if (selected.last_screening.meaning) screening.append(host.node('p', 'workspace-help', selected.last_screening.meaning)); detail.append(screening); }
},
      renderAgentSettings(selected,setting){    setting('Fresh inspections', Number(selected.fresh_inspections_per_run) ? '1 Cosmos inspection per run' : 'Existing observations');
    const ceiling = selected.credit_ceiling_usd; if (ceiling !== undefined && ceiling !== null && Number.isFinite(Number(ceiling))) setting('GPU credit ceiling', `${host.money(Number(ceiling))} total`);
},
      normalizeCollection:canonicalScope,collectionLabel:scopeLabel,datasetLabel,collections:sourceCollections,
      collectionOptions(status){const rows=sourceCollections(status);return host.demoEnvironment()?rows:[{id:'',label:'All footage',videos:status?.corpus||[]},...rows];},
      companyPickerTarget(){return host.currentView==='library'?host.$('gallery-company-picker'):null;},
      alternateState(id){return homeSearches.get(id);},clearAlternate(id){homeSearches.delete(id);},
      openAgentEditor(agent){host.$('agent-fresh-inspections').value=String(agent?.fresh_inspections_per_run??1);},agentEditorData(){return {fresh_inspections_per_run:Number(host.$('agent-fresh-inspections').value)};},updateAgentEditor:updateAgentEstimate,
      agentDefinitionData(agent){return Object.fromEntries(['watch_for','fresh_inspections_per_run','evidence_window','lookback_seconds','watch_plan','profile_ids','event_types'].filter(key=>agent[key]!==undefined).map(key=>[key,agent[key]]));},
      agentReviewRequired(agent){return agent.mode==='ongoing'&&agent.screening_status==='plan_review_required';},
      renderAgentSummary(detail,agent){if(agent.watch_for?.length)detail.append(host.agentItems('Watch for',agent.watch_for));},
      renderAgentInputNotice(detail,agent,ready){if(agent.mode==='ongoing'&&ready)detail.append(host.node('p','agent-context-note','Checks newly indexed footage. After a completed run, it waits for new footage in this collection.'));},
      renderAgentCommunicationNotice(detail,agent){if(agent.agent_kind!=='tracker')detail.append(host.node('p','agent-field-help agent-phone-rule','Alerts on your phone come from Tracking Agents. An Action Agent messages other people under its own grants.'));},
      collectionFieldLabel:'Footage',agentOutputLabel:'In-app report and evidence clips',templateOutputLabel:'Evidence report',runHistoryHint:'Each run appears here with its report, evidence and status.',
      runSummary(run){return Number.isFinite(run.source_count)?`${run.source_count} source windows`:'';},agentFrequency(agent){return agent.mode==='ongoing'?'When new footage is indexed':null;},
      agentScheduleVerb(agent){return agent.mode==='ongoing'?'Check new footage':'Run';},agentNextRunLabel(agent){return agent.mode==='ongoing'?'Next footage check':'Next run';},
      agentModeDescription(mode){return mode==='ongoing'?'Checks newly indexed footage. Completed runs advance its place in the collection; static footage waits for new additions.':mode==='scheduled'?'Repeats the investigation across the selected collection on this schedule.':null;},
      collectionPrompt(){return host.demoEnvironment()?'Choose footage':'All footage';},composerPlaceholder(mode){return mode==='search'?'Search footage…':null;},composerNote:'Answers link to the footage used.',alternateWelcome:'Search the footage and open matching clips.',
      sessionIntroduction:'Explore the demo footage, or open your company’s workspace.',mcpIntroduction:'Connect an MCP-compatible app to search your footage and view source previews. Each access key reads one collection in your workspace.',mcpEmptyHint:'Create an access key to use your footage in another app.',connectorPlaceholder:'Read deliveries from our ERP and find the matching footage.',
      updateAlternateComposer(){if(host.homeMode!=='search')return false;host.$('composer-note').textContent='Results open source clips.';host.elements.send.setAttribute('aria-label','Search footage');host.elements.send.replaceChildren(host.icon('arrow'));host.elements.send.disabled=Boolean(homeSearches.get(host.activeId)?.pending);return true;},
      setComposerReadOnly(value){host.$('clip-attachment-previews').querySelectorAll('button').forEach(button=>{button.disabled=value;});},
      templateSummary(template){return template.watch_for?.[0];},renderTemplateInputs(content,template){if(template.watch_for?.length)content.append(host.agentItems('What it looks for',template.watch_for));},
      renderUsageRoutes(routes,data){if(data.local_search?.additional_provider_cost_usd!==undefined)routes.append(host.node('dt','','Quick Search'),host.node('dd','',`${host.money(Number(data.local_search.additional_provider_cost_usd))} additional provider cost`));},
      renderUsageStorage(content,data){if(data.storage)content.append(host.node('p','workspace-help',`Source files ${host.fileSize(data.storage.source_bytes)} · Index ${host.fileSize(data.storage.index_bytes)}`));},
      applyHarness(shell){const available=Boolean(shell.standIn)||shell.surfaces.some(surface=>surface.id==='cameras');const option=host.$('settings-cameras');option.dataset.settingsAvailable=String(available);option.hidden=!available;if(!available)host.$('cameras-nav').hidden=true;if(!available&&host.currentView==='cameras')host.showHome();},
      initializeRoute(params){if(params.has('camera')||params.has('cameras'))host.currentView='cameras';if(params.has('library')||params.has('asset')||params.has('legacy')){const url=new URL(location.href);for(const key of ['library','asset','folder','gallery_tab','legacy'])url.searchParams.delete(key);history.replaceState({},'',url);}},
      openInitialRoute(params){const id=params.get('clip');if(id)openEvidenceId(id);},
      selectCollection(collection){cameraCollection=libraryCollection=collection;cameraDetail=libraryAsset=null;libraryAssetId=cameraId='';},
      openCollection(collection){if(host.currentView==='cameras'){showCameras(collection);return true;}if(host.currentView==='library'){showLibrary(collection,'','',galleryTab);return true;}return false;},
      selectScope(dataset){if(host.currentView!=='library')return false;host.setFilesPane(false);showLibrary(dataset,'','',galleryTab);return true;},
      resetEnvironment(){libraryCollection=libraryFolder=libraryAssetId=cameraCollection=cameraId='';},
      resetWorkspace(){homeSearches.clear();resetImports();cameraStreams=[];cameraStreamsError='';cameraStreamsLoading=false;++cameraStreamsEpoch;libraryData=libraryAsset=cameraDetail=null;cameras=[];cancelEvidenceOpen();},
      beforeRender(){if(pendingEvidenceOpen&&pendingEvidenceOpen.scope!==evidenceOpenScope())cancelEvidenceOpen();},
      renderNavigation(){host.$('cameras-nav').setAttribute('aria-current',host.currentView==='cameras'?'page':'false');},
      renderView(){document.body.classList.remove('gallery-context-active');host.$('gallery-context').hidden=true;host.$('library-shortcut').hidden=true;host.$('library-view').hidden=host.currentView!=='library';host.$('cameras-view').hidden=host.currentView!=='cameras';host.$('settings-cameras').setAttribute('aria-current',host.currentView==='cameras'?'page':'false');},
      renderActiveSurface(){if(host.currentView==='library'){renderLibrary();return true;}if(host.currentView==='cameras'){host.elements['chat-title'].textContent='Cameras';renderCameras();return true;}return false;},
      surfaceLayout(){return {hideTopbar:host.currentView==='library',settings:host.currentView==='cameras'};},
      navigationState(){return host.currentView==='cameras'?{detail:Boolean(cameraId)}:host.currentView==='library'?{detail:Boolean(libraryAssetId||host.filesPaneOpen),key:galleryTab,inputId:galleryTab==='files'?'workspace-file-search':'library-search-query'}:{};},
      async resumeWorkspace(){if(host.currentView==='library'){if(host.demoEnvironment())libraryCollection ||= host.currentCollection();if(galleryTab==='files')await host.loadFiles();else await loadLibrary();}if(host.currentView==='cameras'){cameraCollection ||= host.currentCollection();await loadCameras();}},
      filesSurface(){return {collection:libraryCollection,active:host.currentView==='library'&&galleryTab==='files'};},
      renderFilesStatus(message){if(host.currentView==='library'&&galleryTab==='files')host.$('workspace-message').textContent=message;},
      focusFiles(){host.$('workspace-files-tab').focus();},
      renderComposerContent:renderHomeSearch,renderComposerAttachments:renderClipAttachments,submitAlternate:searchHome,
      cancelPending:cancelEvidenceOpen,resize:drawFocus,
      closeOverlay(){if(host.$('clip-dialog').open)closeClip();},
      appendSetupActions(container,agent,dataset){if(!agentUsesCameraInput(agent))return;const button=host.agentButton('Connect cameras',event=>openCameraProviderPicker(event.currentTarget,dataset),false,'video');button.disabled=host.observing();container.append(button);},
      toolIcon(name){return /clip|inspect|cosmos|camera/.test(name)?'video':null;},
      toolLabel(name,working){return {cosmos_inspect:['Inspecting footage','Inspected footage'],cosmos_compare:['Comparing footage','Compared footage'],sample_gallery:['Sampling source frames','Sampled source frames'],search_video:['Searching footage','Searched footage'],inspect_clip:['Viewing a clip','Viewed a clip'],}[name]?.[working?0:1];},
      clearRoute(url){for(const key of ['clip','library','folder','asset','cameras','camera','gallery_tab','camera_tab'])url.searchParams.delete(key);},
      collectionIcon:'video',collectionSummary({videos=[]}){return videoCount(videos);},
      draftData(id){return {clips:normalizeClips(clipDrafts.get(id)).slice(0,12)};},
      restoreDraft(id,value){if(Array.isArray(value.clips))clipDrafts.set(id,normalizeClips(value.clips).slice(0,12));},
      loadDrafts(suffix,read){CLIP_DRAFTS_KEY=`rocoto-video-clip-drafts-v1${suffix}`;clipDrafts=new Map(read(CLIP_DRAFTS_KEY));},
      hasDraft(id){return Boolean(clipDrafts.get(id)?.length);},clearDraft(id){clipDrafts.delete(id);},saveDrafts:saveClipDrafts,
      moveDraft(from,to){const value=clipDrafts.get(from);if(value){clipDrafts.set(to,value);clipDrafts.delete(from);saveClipDrafts();}},
      editQueued(id,item){clipDrafts.set(id,normalizeClips(item.clip_attachments).map(clip=>({...clip})));},
      composerData(id){return {clip_attachments:clipDrafts.get(id)||[]};},
      messageData(message){return {clip_attachments:normalizeClips(message.clip_attachments),evidence_presentation:evidencePresentation(message.evidence_presentation)};},
      requestData(message){return {clip_attachments:normalizeClips(message.clip_attachments).map(clip=>clip.id)};},
      hasMessageContent(message){return Boolean(message.clip_attachments?.length);},
      defaultMessage(message){return message.clip_attachments?.length?'Review the attached clips.':null;},
      toolData(tool){return {frame_previews:toolFramePreviews(tool)};},
      messageDependencies:messageMediaDependencies,renderMessageContent:renderEvidence,refreshMessage:refreshEvidenceMessage,
      receiveEvent(event,answer){if(event.type==='evidence_presentation')answer.evidence_presentation=evidencePresentation(event.presentation);},
      renderUserAttachments(bubble,message){if(!message.clip_attachments?.length)return;const clips=host.node('div','user-clip-attachments');normalizeClips(message.clip_attachments).forEach(clip=>clips.append(clipChip(clip)));bubble.append(clips);},
      renderChatSources(sources,chat){const clips=[...new Map(chat.messages.flatMap(message=>normalizeClips(message.clip_attachments)).map(clip=>[clip.id,clip])).values()];for(const clip of clips){const button=host.node('button','chat-context-item');button.type='button';button.append(host.icon('video'),host.node('span','',clip.title||clip.name||'Video clip'));button.addEventListener('click',()=>{host.setChatContext(false);openEvidenceId(clip.id,button);});sources.append(button);} return clips.length;},
      decorateLink(a,url,message){const internal=url.origin===location.origin,href=url.href;       let clipId = internal ? (url.searchParams.get('clip') || url.pathname.match(/^\/clips\/([^/]+)\.mp4$/)?.[1]) : null;
      if (internal && url.pathname.startsWith('/media/')) {
        const matching = mediaReferenceResult(url);
        if (!matching) { a.replaceWith(document.createTextNode(a.textContent)); return true; } clipId = matching.id;
      }
      if (clipId) { let id; try { id = decodeURIComponent(clipId); } catch { a.replaceWith(document.createTextNode(a.textContent)); return true; } a.href = `/?clip=${encodeURIComponent(id)}`; a.addEventListener('click', (event) => { event.preventDefault(); openEvidenceId(id, a, message); }); }
      else if (internal && (url.searchParams.has('library') || url.searchParams.has('asset'))) { a.href = href; a.addEventListener('click', (event) => { event.preventDefault(); showLibrary(url.searchParams.get('collection') || '', url.searchParams.get('folder') || '', url.searchParams.get('asset') || ''); }); }
      else if (internal && (url.searchParams.has('cameras') || url.searchParams.has('camera'))) { a.href = href; a.addEventListener('click', (event) => { event.preventDefault(); showCameras(url.searchParams.get('collection') || '', url.searchParams.get('camera') || ''); }); }
 else return false;return true;},
      resolveReference({id},message,current){const clips=[...(message?.results||[]),...(evidencePresentation(message?.evidence_presentation)?.items||[])],clip=clips.find(item=>item.id===id);return clip?{label:clip.source_filename||clip.camera||'Source interval',open:opener=>{if(current())openClip(clips,clips.indexOf(clip),message,opener);}}:null;},
      resolveMedia(tool,message,current){const frames=toolFramePreviews(tool);if(frames.length)return frames.map(frame=>({label:`Snapshot · ${preciseTime(frame.time)} in source`,caption:`${preciseTime(frame.time)} in source`,thumbnailUrl:frame.url,open:opener=>{if(current())openEvidenceId(frame.clip_id,opener,message);}}));let input=tool.arguments||{};if(typeof input==='string'){try{input=JSON.parse(input);}catch{input={};}}const all=[...(message?.results||[]),...(evidencePresentation(message?.evidence_presentation)?.items||[])],clips=/^(?:mcp__rocoto__)?(inspect_clip|cosmos_inspect|focus_clip)$/.test(tool.name)?[input.clip_id,...(Array.isArray(input.clip_ids)?input.clip_ids:[])].map(id=>all.find(clip=>clip.id===id)).filter(Boolean):[];return clips.map(clip=>({label:`${clip.camera||'Source interval'} · ${host.time(clip.start)}–${host.time(clip.end)}`,thumbnailUrl:clip.thumbnail_url||clip.thumbnail,open:opener=>{if(current())openClip(all,all.indexOf(clip),message,opener);}}));},
      queuedThumbnail(item){const clip=normalizeClips(item.clip_attachments).find(value=>value.thumbnail||value.thumbnail_url),value=clip?.thumbnail||clip?.thumbnail_url;if(!value)return null;try{const url=new URL(value,location.href);if(url.origin!==location.origin||url.username||url.password||!/^\/thumbnails\//.test(url.pathname))return null;}catch{return null;}const image=host.node('img','queued-thumbnail');image.src=value;image.alt=clip.camera||'Attached clip';return image;},

    };
  });
})();
