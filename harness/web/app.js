(() => {
  'use strict';
  let initialAccessCode = new URLSearchParams(location.hash.slice(1)).get('access');
  if (initialAccessCode !== null) history.replaceState({}, '', location.pathname + location.search);
  const $ = (id) => document.getElementById(id);
  let STORAGE_KEY = 'rocoto-chats-v1';
  let QUEUE_KEY = 'rocoto-queue-v1';
  let DRAFT_KEY = null, usageCheckedAt = 0, usageEpoch = 0;
  let storageIdentity = null, sessionGeneration = 0, observers = [], chatListLoading = false;
  const elements = Object.fromEntries(['chat-list', 'chat-title', 'messages', 'welcome', 'conversation', 'message', 'mode', 'dataset', 'send', 'notice'].map((id) => [id, $(id)]));
  let chats = [];
  try { const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '[]'); if (Array.isArray(saved)) chats = saved.filter((chat) => chat && typeof chat.id === 'string' && Array.isArray(chat.messages)); } catch { /* Start a fresh local conversation when stored data is malformed. */ }
  function preferredChatId(records) { const requested = new URL(location.href).searchParams.get('chat'); return (requested ? records.find(chat => chat.id === requested) : records.find(chat => !chat.archived))?.id || null; }
  let activeId = preferredChatId(chats);
  let renderedChatId = null;
  const running = new Map();
  const seenNativeQuestions = new Set(), sendingNativeQuestions = new Set();
  let questionModal = null, questionSyncQueued = false, questionRestoreFocus = true;
  const seenCredentialRequests = new Set();
  let credentialModal = null, credentialSyncQueued = false, credentialRestoreFocus = true;
  const attachmentDrafts = new Map();

  const textDrafts = new Map();
  const childPolls = new Map();
  const verifiedRunning = new Set();
  const isBusy = (chatId = activeId) => running.has(chatId) || verifiedRunning.has(chatId) && Boolean(chats.find((chat) => chat.id === chatId)?.remote_running);
  let status = null;
  let session = null;
  let statusTimer = null;
  let statusReceivedAt = 0, connectionFailed = false;
  let homeMode = 'chat';
  let collapsedChatSections = {}; try { collapsedChatSections = JSON.parse(localStorage.getItem('rocoto-chat-sections') || '{}'); } catch {}
  const HARNESS_STAND_IN = window.RocotoHarnessShell.fallback;
  const HARNESS_KEPT = 'rocoto-harness-shells', HARNESS_WAIT = 8000, HARNESS_RETRY = [2000, 5000, 15000];
  let harness = HARNESS_STAND_IN, harnessTimer = null;
  const harnessShell = window.RocotoHarnessShell.parse;
  const fixedPurposes = () => harness.fixed_chats.map((chat) => chat.purpose);
  /* Whether a company chat of this purpose belongs in the list: a purpose of the shell, or any purpose while the shell is on its way. */
  const fixedPurposeKnown = (purpose) => typeof purpose === 'string' && purpose.length > 0 && (Boolean(harness.standIn) || fixedPurposes().includes(purpose));
  /* The fixed chat a surface asks for: that purpose where the shell has it or is on its way, else the Organization chat every template has. */
  const fixedPurpose = (purpose) => fixedPurposeKnown(purpose) ? purpose : 'organization';
  /* The fixed chat that opens a member's own setup conversation, where the template has one. */
  const memberSetupPurpose = () => harness.fixed_chats.find((chat) => chat.opens === 'member_setup')?.purpose || null;
  const harnessIcon = (name, standIn) => typeof name === 'string' && document.getElementById(`i-${name}`) ? name : standIn;
  /* The last shell this browser drew for each company, so a reload and a slow answer start from the company's own rows. */
  function keptHarness() { try { return harnessShell(JSON.parse(localStorage.getItem(HARNESS_KEPT) || '{}')[session?.environment?.id]); } catch { return null; } }
  function keepHarness(shell) { try { const kept = JSON.parse(localStorage.getItem(HARNESS_KEPT) || '{}'), company = session?.environment?.id; if (!company) return; delete kept[company]; kept[company] = shell; for (const old of Object.keys(kept).slice(0, -8)) delete kept[old]; localStorage.setItem(HARNESS_KEPT, JSON.stringify(kept)); } catch {} }
  function applyHarness() { workspaceExtension.applyHarness?.(harness); renderVerticalNavigation(); }
  function renderVerticalNavigation() {
    const nav=$('vertical-navigation');if(!nav)return;nav.replaceChildren();
    for(const surface of harness.surfaces.filter(row=>row.nav==='primary'&&row.id!=='home')) {
      if(!window.RocotoWorkspaceTabs.has(harness.vertical?.id,surface.id))continue;
      const button=node('button','agents-nav');button.type='button';button.dataset.verticalTab=surface.id;
      button.append(icon('grid'),node('span','',surface.title));button.addEventListener('click',()=>showVerticalTab(surface.id));nav.append(button);
    }
  }
  function showVerticalTab(id) {
    const surface=harness.surfaces.find(row=>row.id===id);if(!surface||!window.RocotoWorkspaceTabs.has(harness.vertical?.id,id))return;
    persistDraft();currentView=`vertical:${id}`;const url=new URL(location.href);
    for(const key of ['chat','agent','agents','settings','customize'])url.searchParams.delete(key);
    url.searchParams.set('view',id);history.replaceState({},'',url);render();setSidebar(false);
  }
  /* Starts the read and returns at once: chats load on the rows the page holds, and the list is drawn again when the shell lands.
     One read waits eight seconds; a read that ends without a shell is tried again after 2, 5 and 15 seconds. */
  function loadHarness(attempt = 0) {
    const generation = sessionGeneration, controller = new AbortController(), limit = setTimeout(() => controller.abort(), HARNESS_WAIT);
    clearTimeout(harnessTimer); if (!attempt) { harness = keptHarness() || HARNESS_STAND_IN; applyHarness(); }
    fetch('/api/harness', { signal: controller.signal }).then(async (response) => response.ok ? harnessShell(await response.json()) : null).catch(() => null).then((shell) => {
      clearTimeout(limit); if (generation !== sessionGeneration) return;
      if (shell) { harness = shell; keepHarness(shell); applyHarness(); const tab=new URL(location.href).searchParams.get('view'); if(tab&&harness.surfaces.some(row=>row.id===tab)&&window.RocotoWorkspaceTabs.has(harness.vertical?.id,tab))currentView=`vertical:${tab}`; render(); return; }
      if (attempt < HARNESS_RETRY.length) harnessTimer = setTimeout(() => { if (generation === sessionGeneration) loadHarness(attempt + 1); }, HARNESS_RETRY[attempt]);
    });
  }
  const initialRoute = new URL(location.href).searchParams, initialSetting = initialRoute.get('settings'), legacyQuickSearch = false;
  let currentView = initialRoute.has('business') || initialSetting==='business' ? 'business'
    : initialRoute.has('stats') || initialSetting==='organisation-profile' ? 'stats'
    : initialRoute.has('customize') ? 'marketplace'
    : initialRoute.has('settings') ? (['profile','plugins','appearance','notifications','team','messaging','billing','mcp','archives','usage','stats'].includes(initialSetting) ? initialSetting : 'appearance')
    : initialRoute.has('agents') || initialRoute.has('agent') ? 'agents'
    : initialRoute.has('chat') ? 'chat' : 'home';
  if (['business','stats'].includes(currentView)) { const url=new URL(location.href);url.searchParams.delete('business');url.searchParams.delete('stats');url.searchParams.set('settings',currentView);if(currentView==='stats')url.searchParams.delete('collection');history.replaceState({},'',url); }
  let founderCollection = '', founderSwitching = false, collectionPicker = null;
  let statsActivity = null, statsActivityLoading = false, statsActivityError = '', statsActivityEpoch = 0;

  let workspaceTabs = null;
  let agents = [], agentCapabilities = {}, selectedAgentId = new URL(location.href).searchParams.get('agent'), agentsTimer = null, agentsLoading = false, agentsEnvironment = null, agentsError = '', agentDetailSignature = null, agentEditingId = null, agentEditingRevision = null, agentEditorOpener = null;
  let agentMutation = false, agentNavigationEpoch = 0, agentInfo = null;
  const agentPhoneBlocks = new Map(); let agentPhoneActive = null, agentPhoneLoading = null, phoneContactCard = null, linkedAlert = new URL(location.href).searchParams.get('alert'), linkedAlertShown = false;
  for (const input of ['pointerdown', 'keydown', 'wheel', 'touchstart']) window.addEventListener(input, () => releaseLinkedAlert(), { capture: true, passive: true });
  const workspaceCapabilities = new Map(), workspaceCapabilityRequests = new Map();

  let businessCollection = new URL(location.href).searchParams.get('collection') || '', businessData = null, businessLoading = false, businessError = '', businessGeneration = 0, businessEnvironment = null;
  const businessMaps=new Map();let mapDialogOpener=null;
  let messagingData=null,messagingLoading=false,messagingError='',messagingEpoch=0,memberDialog=null;
  let browserPreferences=window.RocotoWorkspaceSettings.defaults,teamLoading=false,teamError='';
  let profileData=null,profileLoading=false,profileError='',profileEpoch=0,profileMode='daily',organisationAllowed=null;

  let usageData = null, usageLoading = false, usageError = '', composerCommand = null, commandIndex = 0;
  let skillCatalogue = null, installedSkills = [], skillsError = '', skillsLoadEpoch = 0, attachmentMention = null, menuIndex = -1;
  let onboardingChatId = null, onboardingEpoch = 0, businessEditor=null;
  let companyConnections = [];
  let marketplaceDetail = null;
  let marketplaceTab = new URL(location.href).searchParams.get('tab') || 'connections', agentTemplates = null, skillFilters = { industry: '', use_case: '', mode: '', cadence: '' }, marketplaceError = '', marketplaceSelection = null, contextHoverTimer = null;
  let customConnections = [], customConnectorError = '', customConnectorLoading = false, customConnectorOpener = null, customConnectorCreating = false, customDetailGeneration = 0, customConnectorScope = null;
  let customExecutionTimer = null;
  const customExecutionPending = new Map();
  let nativePlugins = [], nativePluginsAvailable = false, nativeInstallable = new Set(), nativePluginError = '', nativePluginEpoch = 0, nativePluginDialog = null;
  const nativePluginOperations = new Map(), nativePluginCleanup = new Set();
  let connectionCatalogue = [], catalogueLoading = false, catalogueError = '', brokerConnections = [], connectionFlow = null, connectionFlowTimer = null, connectionSearch = '';

  let queued = [];
  try { const saved = JSON.parse(localStorage.getItem(QUEUE_KEY) || '[]'); if (Array.isArray(saved)) queued = saved.filter((item) => item && typeof item.query === 'string' && chats.some((chat) => chat.id === item.chat_id)); } catch { /* Preserve the current session if browser storage is unavailable. */ }
  let filesPaneOpen = false, filesScope = '', filesData = [], filesLoading = false, filesError = '', filesGeneration = 0, filePreviewGeneration = 0, selectedFileId = null, filePreview = null, filePreviewLoading = false, filePreviewError = '', filesOpener = null;
  let filePreviewController = null;

  const messageRenderSignatures = new WeakMap();
  let renderedMessagesScope = null;

  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const node = (tag, className, text) => { const el = document.createElement(tag); if (className) el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const icon = (name, className = '') => { const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('class', `icon ${className}`); svg.setAttribute('aria-hidden', 'true'); const use = document.createElementNS(svg.namespaceURI, 'use'); use.setAttribute('href', `#i-${name}`); svg.append(use); return svg; };
    const workspaceExtension=window.RocotoWorkspaceExtensions.install({
    get chats(){return chats;},
    get setChatContext(){return setChatContext;},
    get renderSidebar(){return renderSidebar;},
    get status(){return status;},
    get agentCapabilities(){return agentCapabilities;},
    get money(){return money;},
    get agentDate(){return agentDate;},
    get agentItems(){return agentItems;},
    get cadenceLabel(){return cadenceLabel;},
    get demoEnvironment(){return demoEnvironment;},
    get draftAdmitting(){return draftAdmitting;},
    get fileSearch(){return fileSearch;},
    set fileSearch(value){fileSearch=value;},
    get filesError(){return filesError;},
    set filesError(value){filesError=value;},
    get filesLoading(){return filesLoading;},
    set filesLoading(value){filesLoading=value;},
    get founderAllowed(){return founderAllowed;},
    get loadFiles(){return loadFiles;},
    get openFounder(){return openFounder;},
    get placeCompanyPicker(){return placeCompanyPicker;},
    get renderGeneratedFiles(){return renderGeneratedFiles;},
    get scopeLabel(){return scopeLabel;},
    get textDrafts(){return textDrafts;},
    get toggleAttachmentMenu(){return toggleAttachmentMenu;},
    get updateFilesSurface(){return updateFilesSurface;},
    get updateMode(){return updateMode;},
    get openCustomConnectorDialog(){return openCustomConnectorDialog;},
    get customConnectorWritable(){return customConnectorWritable;},
    get customDetailGeneration(){return customDetailGeneration;},
    get marketplaceSelection(){return marketplaceSelection;},
    resetConnectionDialog(){nativePluginDialog=null;connectionFlow=null;clearTimeout(connectionFlowTimer);},
    get storageIdentity(){return storageIdentity;},
    set storageIdentity(value){storageIdentity=value;},
    get activeId(){return activeId;},
    set activeId(value){activeId=value;},
    get canonicalScope(){return canonicalScope;},
    get datasetLabel(){return datasetLabel;},
    get filesPaneOpen(){return filesPaneOpen;},
    set filesPaneOpen(value){filesPaneOpen=value;},
    get homeMode(){return homeMode;},
    set homeMode(value){homeMode=value;},
    get pauseMedia(){return pauseMedia;},
    get principalId(){return principalId;},
    get renderText(){return renderText;},
    get setFilesPane(){return setFilesPane;},
    get time(){return time;},
    get visibleChats(){return visibleChats;},
    get $(){return $;},
    get showHome(){return showHome;},
    get activeChat(){return activeChat;},
    get agentButton(){return agentButton;},
    get agentsTimer(){return agentsTimer;},
    set agentsTimer(value){agentsTimer=value;},
    get renderBusiness(){return renderBusiness;},
    get isBusy(){return isBusy;},
    get businessCollection(){return businessCollection;},
    set businessCollection(value){businessCollection=value;},
    get collectionNavigation(){return collectionNavigation;},
    get currentCollection(){return currentCollection;},
    get currentView(){return currentView;},
    set currentView(value){currentView=value;},
    get detailBreadcrumb(){return detailBreadcrumb;},
    get elements(){return elements;},
    get fileSize(){return fileSize;},
    get fixedPurpose(){return fixedPurpose;},
    get harness(){return harness;},
    set harness(value){harness=value;},
    get humanize(){return humanize;},
    get icon(){return icon;},
    get mergeRemoteChat(){return mergeRemoteChat;},
    get newChat(){return newChat;},
    get node(){return node;},
    get notice(){return notice;},
    get observing(){return observing;},
    get openChatId(){return openChatId;},
    get openOrganizationChat(){return openOrganizationChat;},
    get persistDraft(){return persistDraft;},
    get pluginWritable(){return pluginWritable;},
    get render(){return render;},
    get renderCollectionControl(){return renderCollectionControl;},
    get request(){return request;},
    get resizeComposer(){return resizeComposer;},
    get safeUrl(){return safeUrl;},
    get save(){return save;},
    get scopes(){return scopes;},
    get session(){return session;},
    set session(value){session=value;},
    get sessionGeneration(){return sessionGeneration;},
    set sessionGeneration(value){sessionGeneration=value;},
    get setSidebar(){return setSidebar;},
    get showSession(){return showSession;},
    get sourceNameOrder(){return sourceNameOrder;},
    get workspaceEmpty(){return workspaceEmpty;}
  });
  const settingsShell = window.RocotoSettingsShell.mount({node:$('settings-nav'),icon});
  const wakeControls=window.RocotoWakeControls.create({node,icon,scope:()=>({key:`${sessionGeneration}:${principalId()}:${activeId}:${currentView}`,chatId:activeId,writable:currentView==='chat'&&queueWritable()})});
  workspaceExtension.initializeRoute?.(initialRoute);
  function delegateIcon(id, active = false) {
    const hash = [...String(id)].reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0), variant = hash % 10;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('viewBox', '0 0 32 32'); svg.setAttribute('class', `delegate-icon${active ? ' activity-shimmer' : ''}`); svg.setAttribute('aria-hidden', 'true'); svg.style.setProperty('--delegate-hue', `${variant * 31}deg`);
    const petals = 3 + variant % 6; for (let index = 0; index < petals; index++) { const petal = document.createElementNS(svg.namespaceURI, 'ellipse'); petal.setAttribute('cx', '16'); petal.setAttribute('cy', String(9 + variant % 3)); petal.setAttribute('rx', String(4 + variant % 3)); petal.setAttribute('ry', String(7 - variant % 3)); petal.setAttribute('transform', `rotate(${index * 360 / petals + variant * 7} 16 16)`); petal.setAttribute('opacity', String(.4 + index % 3 * .2)); svg.append(petal); } const center = document.createElementNS(svg.namespaceURI, 'circle'); center.setAttribute('cx', '16'); center.setAttribute('cy', '16'); center.setAttribute('r', String(2 + variant % 3)); svg.append(center); return svg;
  }
  function toolIcon(name) { return workspaceExtension.toolIcon?.(name) || /run_command|exec_command|shell/.test(name) ? 'terminal' : /edit_files/.test(name) ? 'edit' : /read_files/.test(name) ? 'document' : /compact/.test(name) ? 'document' : /connection|web/.test(name) ? 'globe' : /create_file|read_file|list_files/.test(name) ? 'document' : /search|screen/.test(name) ? 'search' : /delegate|agent/.test(name) ? 'agent' : /export|download/.test(name) ? 'download' : /business|web|source/.test(name) ? 'document' : /skill/.test(name) ? 'skill' : 'check'; }
  function childActivity(chat) { const message = [...(chat?.messages || [])].reverse().find((item) => item.role === 'assistant'); const tool = [...(message?.tools || [])].reverse().find((item) => item.status === 'running'); return isBusy(chat?.id) ? tool?.name ? toolLabel(tool,true) : message?.progress?.at(-1)?.text || 'Thinking' : 'View investigation'; }
  const eventWithin = (event, selector) => event.composedPath().some((element) => element.matches?.(selector));
  const activeChat = () => chats.find((chat) => chat.id === activeId);
  const principalId = () => session?.effective_principal_id || session?.principal?.id || '';
  const observing = () => Boolean(session?.viewing_as);
  const visibleChats = () => chats.filter((chat) => (!session?.environment?.id || chat.environment_id === session.environment.id) && (!principalId() || (chat.owner_principal_id === principalId() || chat.environment_id===session?.environment?.id && chat.server_saved && (chat.kind==='organization_chat' || chat.participant===true && chat.kind==='agent_conversation'))));
  const demoEnvironment = () => session?.environment?.id === 'demo';
  const sourceNameOrder = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });
  function scopes() {
    const selected=workspaceExtension.collections?.(status);if(selected)return selected;
    const declared=status?.collections || session?.environment?.collections || session?.environments?.find(item=>item.id===session?.environment?.id)?.collections;
    const rows=Array.isArray(declared)?declared:[{id:activeChat()?.dataset || 'company',name:session?.environment?.name || 'Workspace'}];
    return rows.map(item=>typeof item==='string'?{id:item,label:datasetLabel(item)}:{id:item.id,label:item.label||item.name||datasetLabel(item.id)}).filter(item=>typeof item.id==='string'&&item.id);
  }
  const scopeLabel=value=>workspaceExtension.collectionLabel?.(value) || datasetLabel(value);
  const canonicalScope=value=>workspaceExtension.normalizeCollection?.(value) ?? value;
  const datasetLabel=value=>workspaceExtension.datasetLabel?.(value) || humanize(value);
  const founderAllowed = () => session?.role === 'owner' && !observing() && session.environments?.some(item => item.id === 'demo');
  const founderChoices = () => session?.environments?.find(item=>item.id==='demo')?.collections || (demoEnvironment()?scopes().map(item=>({id:item.id,name:item.label})):[]);
  const founderStorageKey = () => session?.principal?.id ? `rocoto-founder-collection:${encodeURIComponent(session.principal.id)}` : '';
  function currentCollection() {
    if (!demoEnvironment()) return canonicalScope(activeChat()?.dataset || session?.environment?.id || scopes()[0]?.id || 'company');
    const choices=founderChoices();if(!founderCollection){try{founderCollection=localStorage.getItem(founderStorageKey())||'';}catch{}}
    return choices.find(item=>item.id===founderCollection)?.id || choices.find(item=>item.id===canonicalScope(activeChat()?.dataset))?.id || choices[0]?.id || scopes()[0]?.id || '';
  }
  function renderFounder() { const allowed=founderAllowed();$('founder-nav').hidden=!allowed;if(!allowed&&$('founder-demo-popover').open)$('founder-demo-popover').close(); }
  function openFounder() { if(!founderAllowed())return;setProfileMenu(false);$('founder-demo-search').value='';$('founder-demo-message').hidden=true;renderFounderOptions();$('founder-demo-popover').showModal();$('founder-demo-search').focus(); }
  function renderFounderOptions() {
    const list=$('founder-demo-options');list.replaceChildren();if(!founderAllowed())return;const query=$('founder-demo-search').value.toLocaleLowerCase();
    for(const choice of founderChoices()){const label=choice.name||scopeLabel(choice.id);if(!label.toLocaleLowerCase().includes(query))continue;const button=node('button','workspace-option');button.type='button';button.disabled=founderSwitching;button.setAttribute('aria-pressed',String(demoEnvironment()&&currentCollection()===choice.id));button.append(icon('building'),node('span','',label));button.addEventListener('click',()=>selectFounderCollection(choice.id));list.append(button);}if(!list.childElementCount)list.append(node('p','workspace-empty','Try another demo company.'));
  }
  async function selectFounderCollection(collection) {
    if(!founderAllowed()||founderSwitching||!founderChoices().some(item=>item.id===collection))return;const actor=session.principal.id,view=currentView,generation=sessionGeneration;
    founderSwitching=true;renderFounderOptions();try{
      if(!demoEnvironment()){await request('/api/session/company',{environment_id:'demo'});if(generation!==sessionGeneration||session?.principal?.id!==actor)return;await loadSession();}
      if(!founderAllowed()||session?.principal?.id!==actor||!demoEnvironment())return;
      founderCollection=collection;try{localStorage.setItem(founderStorageKey(),collection);}catch{notice('The demo selection stays available in this tab.');}
      businessCollection=collection;businessData=null;workspaceExtension.selectCollection?.(collection);$('founder-demo-popover').close();
      if(view==='business')showBusiness(collection);else if(workspaceExtension.openCollection?.(collection)){}else if(view==='chat')selectScope(collection);else render();
    }catch(error){if(session?.principal?.id===actor){$('founder-demo-message').textContent=error.message;$('founder-demo-message').hidden=false;}}
    finally{founderSwitching=false;renderFounderOptions();}
  }
  function renderCollectionControl(id,value,choices,onSelect) {
    const container=$(id);container.replaceChildren();const label=choices.find(item=>item.id===value)?.label || (value?scopeLabel(value):'Footage collection');
    if(demoEnvironment()||choices.length<2){container.append(node('span','collection-label',label));return;}
    const button=agentButton(label,event=>{collectionPicker={choices,onSelect,generation:sessionGeneration,opener:event.currentTarget};$('collection-picker-search').value='';renderCollectionOptions();$('collection-picker-dialog').showModal();$('collection-picker-search').focus();},false,'folder');container.append(button);
  }
  function renderCollectionOptions(){const list=$('collection-picker-options');list.replaceChildren();if(!collectionPicker||collectionPicker.generation!==sessionGeneration)return;const query=$('collection-picker-search').value.toLocaleLowerCase();for(const choice of collectionPicker.choices){if(!choice.label.toLocaleLowerCase().includes(query))continue;const button=node('button','workspace-option');button.type='button';button.append(icon('folder'),node('span','',choice.label));button.addEventListener('click',()=>{const selected=collectionPicker;if(!selected||selected.generation!==sessionGeneration)return;$('collection-picker-dialog').close();selected.onSelect(choice.id);});list.append(button);}if(!list.childElementCount)list.append(node('p','workspace-empty','Try another collection.'));}
  function workspaceEmpty(symbol,title,copy,actions=[]){const root=node('div','workspace-empty-state');root.append(icon(symbol),node('h2','',title),node('p','',copy));if(actions.length){const row=node('div','workspace-empty-actions');row.append(...actions);root.append(row);}return root;}
  function selectScope(dataset) { if (observing()) { notice('Open Home to explore this workspace.'); return; } if(workspaceExtension.selectScope?.(dataset)){setPicker('environment',false);return;} const mode = homeMode; let chat = activeChat(); if (!chat || (chat.dataset !== dataset && (chat.dataset || chat.server_saved || chat.messages.length || chat.agent_setup?.agent_id))) {const setup=chat&&!chat.server_saved&&!chat.messages.length&&!chat.agent_setup?.agent_id?chat.agent_setup:null;chat = newChat();if(setup)chat.agent_setup={mode:setup.mode,...(setup.template_id?{template_id:setup.template_id}:{})};} if (chat.dataset !== dataset) workspaceExtension.clearAlternate?.(chat.id); chat.dataset = dataset; loadWorkspaceCapabilities(dataset || 'company'); homeMode = mode; save(); render(); setPicker('workspace', false); elements.message.focus(); }
  const time = (value) => { const seconds = Math.max(0, Math.floor(Number(value) || 0)); return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`; };
  const humanize = (value) => String(value || '').replace(/[_-]+/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
  function notice(text) { elements.notice.textContent = text; elements.notice.hidden = !text || currentView === 'home'; }
  function save() { persistDraft(); try { localStorage.setItem(STORAGE_KEY, JSON.stringify(chats)); } catch { notice('This browser’s chat storage is full. Your current conversation remains open.'); } }
  function saveQueue() { try { localStorage.setItem(QUEUE_KEY, JSON.stringify(queued)); } catch { notice('Queued messages remain available in this open page.'); } }
  function draftAttachments(items) {
    return (items || []).filter(item => item && !item.uploading && /^[a-f0-9]{32}$/.test(item.id || '') && item.url === `/attachments/${item.id}.png`).slice(0,8).map(({id,url,name,mime})=>({id,url,name,mime}));
  }
  function persistDraft(chatId=activeId, capture=true) {
    const chat=chats.find(item=>item.id===chatId);if(!DRAFT_KEY || !session || observing() || !chat || chat.parent_id || chat.owner_principal_id!==principalId() || chat.environment_id!==session.environment.id)return;
    if(capture && chatId===activeId)textDrafts.set(chatId,elements.message.value);
    const value={text:textDrafts.get(chatId)||'',attachments:draftAttachments(attachmentDrafts.get(chatId)),...workspaceExtension.draftData?.(chatId),skills:(chat.skill_drafts||[]).slice(0,8)};
    try{const key=`${DRAFT_KEY}:${encodeURIComponent(chatId)}`;if(value.text || value.attachments.length || workspaceExtension.hasDraft?.(chatId) || value.skills.length)localStorage.setItem(key,JSON.stringify(value));else localStorage.removeItem(key);}catch{notice('Keep this page open to retain your draft. Browser storage is full.');}
  }
  function restoreDrafts(preserveLive=false) {
    if(!DRAFT_KEY)return;for(const chat of chats){if(chat.parent_id||preserveLive&&textDrafts.has(chat.id))continue;try{const value=JSON.parse(localStorage.getItem(`${DRAFT_KEY}:${encodeURIComponent(chat.id)}`)||'null');if(!value || typeof value.text!=='string')continue;textDrafts.set(chat.id,value.text);attachmentDrafts.set(chat.id,draftAttachments(value.attachments).map(item=>({...item,localId:uuid(),uploading:false})));workspaceExtension.restoreDraft?.(chat.id,value);if(Array.isArray(value.skills))chat.skill_drafts=value.skills.filter(id=>typeof id==='string').slice(0,8);}catch{/* Keep other drafts available if one saved record is malformed. */}}
  }
  function clearSentDraft(chatId) {
    textDrafts.delete(chatId);attachmentDrafts.delete(chatId);workspaceExtension.clearDraft?.(chatId);const chat=chats.find(item=>item.id===chatId);if(chat)chat.skill_drafts=[];
    if(activeId===chatId){elements.message.value='';resizeComposer();renderAttachments();}persistDraft(chatId,false);workspaceExtension.saveDrafts?.();save();
  }
  function setSidebar(open) { if (!open) setRailChats(false); document.body.classList.toggle('sidebar-open', open); $('sidebar-scrim').hidden = !open; $('sidebar-toggle').setAttribute('aria-expanded', String(open)); }
  function chooseChat(chat) { chat.unread_completion = false; chat.unread_question = false; loadWorkspaceCapabilities(chat.dataset || 'company'); if (activeId !== chat.id) { closeCredentialRequest(false); closeNativeQuestion(false); } setChatContext(false); currentView = 'chat'; document.body.classList.remove('agents-list-open'); homeMode = 'chat'; clearTimeout(agentsTimer); persistDraft(); activeId = chat.id; elements.message.value = textDrafts.get(chat.id) || ''; resizeComposer(); const url = new URL(location.href); url.searchParams.set('chat', chat.id); for(const key of ['agents','agent','collection','business','stats','settings','customize','tab','view'])url.searchParams.delete(key);workspaceExtension.clearRoute?.(url); if(conversationAgentId(chat)){url.searchParams.delete('chat');url.searchParams.set('agents','1');url.searchParams.set('agent',conversationAgentId(chat));selectedAgentId=conversationAgentId(chat);} history.replaceState({}, '', url); render(); setSidebar(false); if(conversationAgentId(chat))loadAgents(); if (chat.parent_id || chat.agent_id || chat.connector_id || chat.remote_running && !running.has(chat.id)) pollChild(chat.id); }
  function showHome() { persistDraft(); currentView = 'home'; homeMode = 'chat'; selectedAgentId = null; const url = new URL(location.href); for(const key of ['chat','agents','agent','collection','business','stats','settings','customize','tab','view'])url.searchParams.delete(key);workspaceExtension.clearRoute?.(url); history.replaceState({}, '', url); render(); setSidebar(false); workspaceTabs?.refresh(); }
  function startAgentConversation(kind = null, agent = null) { const prompts = Object.fromEntries(harness.sections.filter((part) => part.type === 'agent_class').map((part) => [part.kind, part.starter_prompt])); const chat = newChat(); if (!chat) return; const starter = prompts[kind] || window.RocotoHarnessShell.defaultClass(harness)?.starter_prompt || 'Help me define an agent for my workspace.', prompt = agent?.title ? `${starter} Continue from the agent “${agent.title}”.`.trim() : starter; elements.message.value = prompt; textDrafts.set(chat.id, prompt); resizeComposer(); render(); elements.message.focus(); }
  function openWorkspaceChat(chatId) { if (chatId) openChatId(chatId); else showHome(); }
  function openWorkspaceChatList() { setSidebar(true); }
  function openWorkspaceAgent(agent) { if (agent?.chat_id) { openChatId(agent.chat_id); return; } if (agent?.id) { startAgentConversation(agent.agent_kind || window.RocotoHarnessShell.defaultClass(harness)?.kind, agent); return; } startAgentConversation(); }
  function ensureWorkspaceTabs() {
    if (!workspaceTabs) workspaceTabs=window.RocotoWorkspaceTabs.create({root:$('workspace-tab-view'),
      defaultHome:window.RocotoWorkspaceHome,context:{node,createAgent:startAgentConversation,
        openChat:openWorkspaceChat,openChatList:openWorkspaceChatList,openAgent:openWorkspaceAgent,
        openOrganization:purpose=>openOrganizationChat(fixedPurpose(purpose||'organization'))},
      onError:()=>notice('Refresh the workspace to load this tab.')});
    workspaceTabs.configure(harness,`${sessionGeneration}:${session?.environment?.id||''}:${principalId()}`);
    workspaceTabs.setVisible(currentView==='home'||currentView.startsWith('vertical:'));
    workspaceTabs.open(currentView.startsWith('vertical:')?currentView.slice(9):'home');
  }
  function newChat() { if (observing()) { activeId = null; currentView = 'chat'; render(); return null; } const chat = { id: uuid(), title: 'New chat', messages: [], thread_id: null, dataset: demoEnvironment() ? currentCollection() : (session?.environment?.id || activeChat()?.dataset || currentCollection()), environment_id: session?.environment?.id, owner_principal_id: principalId(), updated: Date.now() }; chats.unshift(chat); chooseChat(chat); save(); elements.message.focus(); return chat; }
  /* The conversation list follows the harness shell: the fixed chats, one section for each agent class, Recent. One row grammar: icon, title, state. */
  function chatAgentKind(chat) { const id = conversationAgentId(chat); if (!id) return null; const kind = agents.find((agent) => agent.id === id)?.agent_kind || chat.agent_setup?.agent_kind || chat.agent_kind; return window.RocotoHarnessShell.classFor(harness, kind)?.kind || window.RocotoHarnessShell.defaultClass(harness)?.kind || null; }
  function renderSidebar() {
    $('add-chat').disabled = observing();
    $('new-chat').setAttribute('aria-current', currentView === 'home' ? 'page' : 'false');
    $('agents-nav').setAttribute('aria-current', currentView === 'agents' || currentView === 'chat' ? 'page' : 'false');
    $('marketplace-nav').setAttribute('aria-current', currentView === 'marketplace' ? 'page' : 'false');
    workspaceExtension.renderNavigation?.();
    $('library-shortcut')?.setAttribute('aria-pressed', String(currentView === 'library'));
    const savedAgents = agentsEnvironment === session?.environment?.id ? agents : [], activeAgents = savedAgents.filter((agent) => agent.status === 'active').length; $('agents-active-count').hidden = !activeAgents; $('agents-active-count').textContent = String(activeAgents);
    const list = elements['chat-list'], focused = list.contains(document.activeElement) ? document.activeElement.dataset.sidebarKey : null, shown = visibleChats(); list.replaceChildren();
    function section(key) {
      const box = node('section', 'chat-list-section'), heading = node('h2'), toggle = node('button', 'chat-section-toggle'), contents = node('div', 'chat-section-contents');
      const slug = key.toLowerCase().replace(/[^a-z0-9]+/g, '-'); box.dataset.chatSection = slug; contents.id = `chat-section-${slug}`; toggle.type = 'button'; toggle.dataset.sidebarKey = `section:${key}`; toggle.append(node('span', '', key), icon('chevron'));
      toggle.setAttribute('aria-controls', contents.id); toggle.setAttribute('aria-expanded', String(!collapsedChatSections[key])); contents.hidden = Boolean(collapsedChatSections[key]);
      toggle.addEventListener('click', () => { collapsedChatSections[key] = !collapsedChatSections[key]; try { localStorage.setItem('rocoto-chat-sections', JSON.stringify(collapsedChatSections)); } catch {} toggle.setAttribute('aria-expanded', String(!collapsedChatSections[key])); contents.hidden = Boolean(collapsedChatSections[key]); });
      heading.append(toggle); box.append(heading, contents); list.append(box); return contents;
    }
    function row(container, { key, symbol, title, state = '', chat = null, current = false, archive = false, open }) {
      const item = node('div', 'chat-row'), button = node('button', 'chat-link'); button.type = 'button'; button.dataset.sidebarKey = key; button.append(icon(symbol), node('span', 'chat-row-title', title));
      if (chat && isBusy(chat.id)) { const spinner = node('span', 'chat-active-spinner'); spinner.setAttribute('aria-label', 'Working'); button.append(spinner); }
      const opened = Boolean(chat) && chat.id === activeId && currentView === 'chat';
      if (chat?.unread_question && !opened) { const dot = node('span', 'chat-question-dot'); dot.setAttribute('aria-label', 'Question received'); button.append(dot); } else if (chat && !opened && !isBusy(chat.id) && chat.unread_completion) { const dot = node('span', 'chat-completion-dot'); dot.setAttribute('aria-label', 'Response complete'); button.append(dot); }
      if (state) button.append(node('small', 'chat-row-state', state));
      if (current || opened) button.setAttribute('aria-current', 'page');
      button.addEventListener('click', open); item.append(button);
      if (archive && chat) { const actions = node('div', 'chat-row-actions'), control = node('button', 'icon-button'); control.type = 'button'; control.dataset.sidebarKey = `${key}:archive`; control.setAttribute('aria-label', `Archive ${title}`); control.append(icon('archive')); control.disabled = observing() || Boolean(chat.read_only); control.addEventListener('click', () => updateChatPreferences(chat, { archived: true })); actions.append(control); item.append(actions); }
      container.append(item); return item;
    }
    const kinds = Object.fromEntries(harness.sections.filter(part=>part.type==='agent_class').map(part=>[part.kind,new Map()]));
    for (const agent of savedAgents) if (agent?.id && agent.chat_id) {
      const kind=window.RocotoHarnessShell.classFor(harness,agent.agent_kind)?.kind||window.RocotoHarnessShell.defaultClass(harness)?.kind;
      kinds[kind]?.set(agent.id,{agent,chat:shown.find(chat=>chat.id===agent.chat_id)||null});
    }
    for (const chat of shown) { const id=conversationAgentId(chat), kind=chatAgentKind(chat); if (!id||chat.archived||chat.parent_id||Object.values(kinds).some(rows=>rows.has(id))) continue; kinds[kind]?.set(id,{agent:null,chat}); }
    const spoken = (message) => message.role === 'user' && (String(message.text || message.content || '').trim() || message.attachments?.length || workspaceExtension.hasMessageContent?.(message));
    for (const part of harness.sections) {
      if (part.type === 'fixed') {
        if (!session?.can_query && !observing()) continue;
        const contents = section(part.key);
        for (const fixed of harness.fixed_chats) {
          const chat = shown.find((item) => item.kind === 'organization_chat' && item.organization_purpose === fixed.purpose && item.dataset === currentCollection()) || null;
          if (observing() && !chat) continue;
          row(contents, { key: `organization:${fixed.purpose}`, symbol: harnessIcon(fixed.icon, 'chat'), title: fixed.title, chat, open: () => chat ? chooseChat(chat) : openOrganizationChat(fixed.purpose) }).dataset.fixed = 'true';
        }
      } else if (part.type === 'agent_class') {
        const kind = part.kind, symbol = harnessIcon(part.icon, 'agent'), contents = section(part.key);
        for (const [id, { agent, chat }] of kinds[kind]) row(contents, { key: `agent:${id}`, symbol, title: agent?.title || chat?.title || 'Agent', state: agent ? agentStateLabel(agent.status) : '', chat, current: currentView === 'agents' && selectedAgentId === id, open: () => chat ? chooseChat(chat) : showAgents(id) }).dataset.agentKind = kind;
        if (kinds[kind].size) continue;
        if (session?.can_query && !observing()) row(contents, { key: `new:${kind}`, symbol: 'plus', title: part.create_label, open: () => startAgentConversation(kind) }).classList.add('chat-row-new'); else contents.append(node('p', 'chat-section-empty', part.empty_label));
      } else {
        const recent = section(part.key);
        for (const chat of shown.filter((chat) => !chat.archived && !chat.parent_id && chat.kind !== 'organization_chat' && !conversationAgentId(chat) && chat.messages.some(spoken)).sort((a, b) => (b.updated || 0) - (a.updated || 0))) row(recent, { key: `chat:${chat.id}`, symbol: 'chat', title: chat.title, chat, archive: true, open: () => chooseChat(chat) });
        if (!recent.childElementCount) recent.append(node('p', 'chat-section-empty', 'Your recent chats appear here.'));
      }
    }
    if (focused) list.querySelector(`[data-sidebar-key="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  }
  function setPicker(name, open) { $(`${name}-popover`).hidden = !open; $(`${name}-toggle`).setAttribute('aria-expanded', String(open)); if (open) { const other = name === 'workspace' ? 'environment' : 'workspace'; setPicker(other, false); $(`${name}-search`).value = ''; name === 'workspace' ? renderScopeOptions() : renderEnvironments(); $(`${name}-search`).focus(); } }
  function renderScopeOptions() {
    const filter = $('workspace-search').value.toLowerCase(), selected = activeChat()?.dataset || '', container = $('workspace-options'); container.replaceChildren();
    const options = workspaceExtension.collectionOptions?.(status) || scopes();
    for (const scope of options) { const {id:dataset,label}=scope; if (!label.toLowerCase().includes(filter)) continue; const option = node('button', 'workspace-option'); option.type = 'button'; option.setAttribute('aria-pressed', String(dataset === selected)); const text = node('span'); text.append(node('strong', '', label), node('small', '', workspaceExtension.collectionSummary?.(scope) || '')); option.append(icon(workspaceExtension.collectionIcon || 'folder'), text); option.addEventListener('click', () => selectScope(dataset)); container.append(option); }
    if (!container.childElementCount) container.append(node('div', 'workspace-empty', 'Try another collection name.'));
  }
  function renderEnvironments() { const container=$('environment-options'),filter=$('environment-search').value.toLowerCase();container.replaceChildren();for(const environment of session?.environments||[]){if(environment.id==='demo'||!environment.name.toLowerCase().includes(filter))continue;const option=node('button','workspace-option');option.type='button';option.append(icon('building'),node('span','',environment.name));option.addEventListener('click',async()=>{try{await request('/api/session/company',{environment_id:environment.id});await loadSession();setPicker('environment',false);}catch(error){notice(error.message);}});container.append(option);}const login=node('button','company-code-action');login.type='button';login.append(icon('plus'),node('span','','Log in with session code'));login.addEventListener('click',()=>{setPicker('environment',false);showSession('Enter a session code to add access.');});container.append(login); }
  function normalizeRemoteMessage(message, index, chatId) { return { ...message, id: message.id || `${chatId}-${index}`, text: typeof message.content === 'string' ? message.content : message.text || '', mode: 'chat', tools: (Array.isArray(message.tools)?message.tools:[]).filter(tool=>tool&&typeof tool==='object'&&!Array.isArray(tool)).map(tool=>({...tool,...workspaceExtension.toolData?.(tool)})), results: message.results || [],  business_map:businessMapMetadata(message.business_map), agents:normalizeAgentCards(message.agents), ...workspaceExtension.messageData?.(message), native_questions: (message.native_questions || []).map(nativeQuestionMetadata).filter(Boolean), native_approvals: (message.native_approvals || []).map(nativeApprovalMetadata).filter(Boolean), credential_requests: (message.credential_requests || []).map(credentialMetadata).filter(Boolean) }; }
  function registerChild(event, answer, parent) { let child = chats.find((chat) => chat.id === event.chat_id); const active = !event.status || event.status === 'running'; if (!child) { child = { id: event.chat_id, title: event.title || 'Investigation', parent_id: event.parent_id || parent.id, dataset: event.dataset || parent.dataset || '', environment_id: parent.environment_id, owner_principal_id: parent.owner_principal_id || principalId(), messages: [], updated: Date.now() }; chats.push(child); } child.remote_running = active; child.subagent_status = event.status || 'running'; if (event.title) child.title = event.title; verifiedRunning.add(child.id); answer.subchats ||= []; const saved = answer.subchats.find((item) => item.chat_id === child.id), item = { chat_id: child.id, title: child.title, status: child.subagent_status }; if (saved) Object.assign(saved, item); else answer.subchats.push(item); save(); renderSidebar(); pollChild(child.id); }
  async function pollChild(chatId, navigate = false) {
    if (childPolls.has(chatId)) { if (navigate) { const chat = chats.find((item) => item.id === chatId); if (chat) chooseChat(chat); } return; }
    childPolls.set(chatId, true); const generation = sessionGeneration; let shouldContinue = false;
    try {
      const response = await fetch(`/api/chats/${encodeURIComponent(chatId)}`); if (!response.ok) throw new Error(); const data = await response.json(); if (generation !== sessionGeneration) return; let chat = chats.find((item) => item.id === chatId);
      if (!chat) { chat = { id: chatId, messages: [], environment_id: session?.environment?.id, owner_principal_id: principalId() }; chats.push(chat); }
      const wasRunning = chat.remote_running; if (!running.has(chatId)) { Object.assign(chat, { owner_principal_id: data.owner_principal_id || principalId(), server_saved: true, pinned: Boolean(data.pinned), archived: Boolean(data.archived), metadata_revision: data.metadata_revision || 0, read_only: Boolean(data.read_only), title: data.title || chat.title || 'Investigation', parent_id: data.parent_id || chat.parent_id, kind: data.kind || chat.kind, organization_purpose:data.organization_purpose||chat.organization_purpose, waiting_for_reply:Boolean(data.waiting_for_reply), run_id: data.run_id || chat.run_id, agent_id: data.agent_id || chat.agent_id, agent_setup: data.agent_setup || chat.agent_setup, continuation_of: data.followup_run_chat_id || data.continuation_of || chat.continuation_of, dataset: data.dataset ?? chat.dataset ?? '', thread_id: data.thread_id || chat.thread_id, messages: (data.messages || []).map((message, index) => normalizeRemoteMessage(message, index, chatId)), remote_running: Boolean(data.running) }); if (chat.messages.at(-1)?.role === 'assistant') chat.messages.at(-1).pending = Boolean(data.running); }
      if (data.workspace_capabilities) workspaceCapabilities.set(chat.dataset || 'company', { data: data.workspace_capabilities, loaded: Date.now() });
      verifiedRunning.add(chatId); shouldContinue = Boolean(data.running || data.waiting_for_reply && chatId===activeId); save(); renderSidebar(); if (navigate) chooseChat(chat); else if (activeId === chatId && !running.has(chatId)) render();
      for (const message of activeChat()?.messages || []) if (message.subchats?.some((child) => child.chat_id === chatId)) updateMessage(activeChat(), message);
      if ((wasRunning || chat.kind === 'agent_run') && !chat.remote_running) drainQueue(null, chatId);
    } catch { if(generation!==sessionGeneration)return;shouldContinue = Boolean(chats.find((chat) => chat.id === chatId)?.remote_running); if (navigate) notice('This investigation is reconnecting. Open it again in a moment.'); }
    finally { if(generation===sessionGeneration)childPolls.delete(chatId); if (shouldContinue && generation === sessionGeneration) { const timer = setTimeout(() => { childPolls.delete(chatId); pollChild(chatId); }, 2500); childPolls.set(chatId, timer); } }
  }
  async function openChatId(chatId) { const chat = visibleChats().find((item) => item.id === chatId); if (chat) chooseChat(chat); else await pollChild(chatId, true); }
  function safeUrl(value) { if (typeof value !== 'string' || !value.trim()) return null; try { const url = new URL(value, location.href); return ['http:', 'https:'].includes(url.protocol) ? url.href : null; } catch { return null; } }


  function sourceIcon(value) {
    let url;try{url=new URL(value,location.href);}catch{return null;}
    if(url.username || url.password || !/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$/.test(url.hostname))return;
    const mark=node('span','source-link-icon'),fallback=icon('globe'),image=node('img','source-favicon');mark.setAttribute('aria-hidden','true');image.alt='';image.width=16;image.height=16;image.loading='lazy';image.referrerPolicy='no-referrer';image.src=`/api/source-icon?host=${encodeURIComponent(url.hostname)}`;image.addEventListener('load',()=>{image.classList.add('loaded');fallback.remove();});image.addEventListener('error',()=>image.remove());mark.append(fallback,image);return mark;
  }
  function addSourceIcon(anchor,url) { const mark=sourceIcon(url);if(mark)anchor.prepend(mark); }

  let markdownReady = false;
  function renderText(el, text, message = null) {
    el.classList.add('markdown-content');
    if (!window.marked || !window.DOMPurify) { el.textContent = text; return; }
    if (!markdownReady) { window.RocotoRichContent.configure(window.marked); window.marked.use({ gfm: true, renderer: { html() { return ''; }, image() { return ''; } } }); markdownReady = true; }
    const fragment = window.DOMPurify.sanitize(window.marked.parse(String(text || ''), { async: false }), { RETURN_DOM_FRAGMENT: true, ALLOWED_TAGS: ['p', 'br', 'strong', 'em', 'del', 'a', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'thead', 'tbody', 'tr', 'th', 'td', 'hr'], ALLOWED_ATTR: ['href', 'title', 'start', 'class'] });
    for(const item of fragment.querySelectorAll('[class]'))if(item.tagName!=='CODE' || !/^language-[a-z0-9_+-]{1,32}$/i.test(item.className))item.removeAttribute('class');
    el.replaceChildren(fragment);
    for (const a of el.querySelectorAll('a')) {
      const href = safeUrl(a.getAttribute('href')); if (!href) { a.replaceWith(document.createTextNode(a.textContent)); continue; }
      const url = new URL(href), internal = url.origin === location.origin;
      if(workspaceExtension.decorateLink?.(a,url,message))continue;
      if (internal && url.searchParams.has('agent')) { const agentId = url.searchParams.get('agent'); a.href = `/?agent=${encodeURIComponent(agentId)}`; a.addEventListener('click', (event) => { event.preventDefault(); showAgents(agentId); }); }
      else if (internal && url.searchParams.has('chat')) { const chatId = url.searchParams.get('chat'); a.href = `/?chat=${encodeURIComponent(chatId)}`; a.addEventListener('click', (event) => { event.preventDefault(); openChatId(chatId); }); }
      else { a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; if(!internal)addSourceIcon(a,url); }
    }
    for (const table of el.querySelectorAll('table')) { const wrapper = node('div', 'markdown-table'); table.replaceWith(wrapper); wrapper.append(table); }
    window.RocotoRichContent.enrich(el,{node,icon,pending:Boolean(message?.pending)});
  }

  function renderSubagentGroup(children) { const group = node('details', 'subagent-group'), summary = node('summary'), stack = node('span', 'delegate-stack'), list = node('div', 'subchat-list'), count = children.filter((child) => isBusy(child.chat_id)).length; children.slice(0, 3).forEach((child) => stack.append(delegateIcon(child.chat_id, isBusy(child.chat_id)))); summary.append(stack, node('span', '', `Subagents · ${children.length}${count ? ` · ${count} active` : ''}`), icon('chevron')); group.dataset.detailKey='subagents';group.append(summary); for (const child of children) { const chat = chats.find((item) => item.id === child.chat_id), card = node('a', 'subchat-card'); card.href = `/?chat=${encodeURIComponent(child.chat_id)}`; const info = node('span'); info.append(node('strong', '', chat?.title || child.title || 'Investigation'), node('small', isBusy(child.chat_id) ? 'activity-shimmer' : '', childActivity(chat))); card.append(delegateIcon(child.chat_id, isBusy(child.chat_id)), info, icon('chevron')); card.addEventListener('click', (event) => { event.preventDefault(); openChatId(child.chat_id); }); list.append(card); } group.append(list); return group; }
  function toolLabel(tool, working) { const name=String(tool.name || '').replace(/^mcp__[^_]+(?:_[^_]+)*__/,''),labels={run_command:['Running command','Ran command'],read_files:['Reading files','Read files'],search_files:['Searching files','Searched files'],edit_files:['Editing files','Edited files'],spawn_agent:['Starting a subagent','Started a subagent'],wait_agent:['Waiting for subagents','Subagents finished'],plan_investigation:['Planning the investigation','Planned the investigation'],create_file:['Creating a file','Created a file'],publish_file:['Publishing a file','Published a file'],read_file:['Reading a file','Read a file'],list_files:['Finding files','Found files'],prepare_export:['Preparing an editing package','Prepared an editing package'],list_connections:['Finding connections','Found connections'],list_connection_tools:['Loading tools','Loaded tools'],read_connection:['Reading a connected source','Read a connected source'],web_search:['Searching the web','Searched the web'],open_web_page:['Browsing the web','Browsed the web'],find_on_page:['Searching a web page','Searched a web page'],compact_context:['Compacting context','Context automatically compacted']};return workspaceExtension.toolLabel?.(name,working) || labels[name]?.[working?0:1] || (working?`Running ${humanize(name)}`:humanize(name)); }
  function workDuration(message) { const elapsed=Number.isFinite(message.elapsed_ms)?message.elapsed_ms:message.pending&&message.started_at?Date.now()-Date.parse(message.started_at):null;const seconds=Math.max(0,Math.floor((elapsed || 0)/1000));const duration=seconds>=60?`${Math.floor(seconds/60)}m ${seconds%60}s`:`${seconds}s`;return `${message.pending?'Working':'Worked'}${elapsed===null?'':` for ${duration}`}`; }
  function activityShimmer(element) { element.classList.add('activity-shimmer'); element.style.setProperty('--activity-delay',`${-(performance.now()%2400)}ms`); return element; }
  function toolCall(tool, {working=false, label=toolLabel(tool,working), index=0}={}) {
    const detail=node('details',`tool-call${working?' current-tool':''}`),heading=node('summary','activity-row'),text=node('span',working?'current-activity':'',label);
    detail.dataset.detailKey=`tool:${tool.id || `${index}:${tool.name}`}`;detail.dataset.toolId=tool.id || '';heading.setAttribute('aria-label',`${label} · Details`);
    heading.append(icon(toolIcon(tool.name)),working?activityShimmer(text):text,icon('chevron'));detail.append(heading);
    detail.append(node('pre','tool-arguments',typeof tool.arguments==='string'?tool.arguments:JSON.stringify(tool.arguments||{},null,2)));
    if(tool.output)detail.append(node('pre','tool-output',tool.output));return detail;
  }
  function toolReferences(message) {
    const generation=sessionGeneration, chatId=activeId;
    const current=()=>generation===sessionGeneration && chatId===activeId && currentView==='chat';
    const fileById=id=>(message?.artifacts || []).find(file=>file.id===id) || filesData.find(file=>file.id===id);
    async function openFile(file,opener) {
      if(!current())return;
      try { const response=await fetch(`/api/files/${encodeURIComponent(file.id)}`);if(!response.ok)throw Error();const fresh=await response.json();if(current() && fresh.id===file.id)openGeneratedFile(fresh,opener); }
      catch { if(current())notice('Open Files to review the saved file.'); }
    }
    function args(tool) { if(typeof tool.arguments==='object' && tool.arguments)return tool.arguments;try{return JSON.parse(tool.arguments || '{}');}catch{return {};} }
    return {
      resolveReference({kind,id}) { if(!id)return null;if(kind==='file'){const file=fileById(id);return file?{label:file.name || file.title,open:opener=>openFile(file,opener)}:null;}return workspaceExtension.resolveReference?.({kind,id},message,current) || null; },
      resolveMedia(tool) {
        const input=args(tool),rows=workspaceExtension.resolveMedia?.(tool,message,current) || [];
        const file=tool.name==='read_file' && fileById(input.file_id);if(file?.inline_preview_type==='image' && file.inline_preview_url)rows.push({label:file.name || file.title,thumbnailUrl:file.inline_preview_url,open:opener=>openFile(file,opener)});
        return rows;
      }
    };
  }
  function renderToolTrail(tools, pending=false, message=null) { return window.RocotoToolTrail.render(tools,{pending,renderTool:toolCall,icon,...toolReferences(message)}); }
  function disclosureState(article) { return new Map([...article.querySelectorAll('details')].map((detail,index)=>[detail.dataset.detailKey || `position:${index}`,detail.open])); }
  function restoreDisclosures(article,saved) { if(saved)[...article.querySelectorAll('details')].forEach((detail,index)=>{const key=detail.dataset.detailKey || `position:${index}`;if(saved.has(key))detail.open=saved.get(key);}); }

  function providerResourceUrl(file) { if (file.kind !== 'provider_resource' || typeof file.source_url !== 'string' || /[\\\s\x00-\x1f\x7f]/.test(file.source_url)) return null; try { const url=new URL(file.source_url),host=url.hostname.replace(/\.$/,'').toLowerCase(); return url.protocol==='https:'&&!url.username&&!url.password&&(!url.port||url.port==='443')&&host.includes('.')&&!/^[\d.]+$/.test(host)&&/^[a-z0-9.-]+$/.test(host)&&!/(?:\.localhost|\.local|\.internal|\.test|\.invalid)$/.test(host) ? url.href : null; } catch { return null; } }
  function providerResourceActions(file, container) { const url=providerResourceUrl(file); if(!url)return; const open=node('a','agent-secondary provider-resource-action','Open provider');open.href=url;open.target='_blank';open.rel='noopener noreferrer';open.referrerPolicy='no-referrer';open.prepend(icon('globe'));container.append(open);const copy=agentButton('Copy link',async()=>{try{await navigator.clipboard.writeText(url);copy.replaceChildren(icon('check'),document.createTextNode('Copied'));}catch{copy.replaceChildren(icon('copy'),document.createTextNode('Copy link again'));}},false,'copy');container.append(copy); }
  function providerResourceCard(file) { const card=node('div','artifact-card provider-resource-card'),main=node('button','artifact-card-main');main.type='button';const copy=node('span');copy.append(node('strong','',file.title||file.name),node('small','artifact-type',file.provider_name||'Provider resource'),node('small','artifact-open-preview',file.preview_format?'Open preview':'View resource'));main.append(icon('document'),copy);main.addEventListener('click',()=>openGeneratedFile(file,main));card.append(main);const menu=node('details','artifact-open-menu'),summary=node('summary');summary.append(document.createTextNode('Open in'),icon('chevron'));const actions=node('div','artifact-open-actions');if(file.preview_format){const preview=agentButton('Preview',()=>{menu.open=false;openGeneratedFile(file,main);},false,'document');actions.append(preview);}providerResourceActions(file,actions);if(actions.childElementCount){menu.append(summary,actions);card.append(menu);}return card; }
  function pauseMedia(root) { root.querySelectorAll('audio,video').forEach(media=>media.pause()); }
  function nativeMediaType(file) {
    const types={audio:['audio/wav','audio/mpeg','audio/ogg','audio/flac','audio/mp4'],video:['video/mp4','video/webm']},kind=file.inline_preview_type;
    return file.kind==='document' && file.representation==='native_original' && file.status==='ready' && types[kind]?.includes(file.media_type) && file.inline_preview_url===`/api/files/${encodeURIComponent(file.id)}/preview`?kind:null;
  }
  function nativeMediaPlayer(file) {
    const kind=nativeMediaType(file);if(!kind)return null;const player=node(kind,'file-media-player');player.controls=true;player.preload='none';player.src=`/api/files/${encodeURIComponent(file.id)}/preview`;player.setAttribute('aria-label',file.title || file.name);if(kind==='video')player.playsInline=true;
    player.addEventListener('play',()=>document.querySelectorAll('audio,video').forEach(other=>{if(other!==player)other.pause();}));return player;
  }
  function fileType(file) { if(file.kind==='provider_resource')return file.provider_name||'Provider resource'; return ({'application/vnd.openxmlformats-officedocument.wordprocessingml.document':'Word','application/pdf':'PDF','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet':'Excel','text/tab-separated-values':'Spreadsheet','text/markdown':'Markdown','text/csv':'Spreadsheet','application/json':'JSON','text/plain':'Text','application/zip':'Editing package'}[file.media_type] || ({document:'Word',spreadsheet:'Excel',image:'Image',audio:'Audio',video:'Video',table:'Spreadsheet',json:'JSON',text:'Text'}[file.display_type]) || (file.kind==='media_export'?'Editing package':'File')); }
  function artifactCard(file) { if(file.kind==='provider_resource')return providerResourceCard(file); const card=node('div','artifact-card'),main=node('button','artifact-card-main');main.type='button';const copy=node('span');copy.append(node('strong','',file.title||file.name),node('small','artifact-type',fileType(file)),node('small','artifact-open-preview',file.representation==='native_original'&&!file.preview_format&&!file.inline_preview_type?'Open file':'Open preview'));main.append(icon(nativeMediaType(file) || (file.inline_preview_type==='image'?'images':'document')),copy);main.addEventListener('click',()=>openGeneratedFile(file,main));card.append(main);const menu=node('details','artifact-open-menu'),summary=node('summary');summary.append(document.createTextNode('Open in'),icon('chevron'));const actions=node('div','artifact-open-actions'),preview=node('button','', file.representation==='native_original'&&!file.preview_format&&!file.inline_preview_type?'Open file':'Preview');preview.type='button';preview.prepend(icon('document'));preview.addEventListener('click',()=>{menu.open=false;openGeneratedFile(file,main);});actions.append(preview);const url=safeUrl(file.download_url);if(url&&new URL(url).origin===location.origin){const download=node('a','','Download');download.href=url;download.download=file.name;download.prepend(icon('download'));actions.append(download);}menu.append(summary,actions);card.append(menu);if(nativeMediaType(file)==='audio'){card.classList.add('artifact-audio');card.append(nativeMediaPlayer(file));}return card; }
  function renderChatContext() { const popup=$('chat-context-popup');popup.replaceChildren();const chat=activeChat();if(!chat)return;const outputs=node('section'),heading=node('div','chat-context-heading');heading.append(node('span','','Outputs'));outputs.append(heading);const artifacts=[...new Map(chat.messages.flatMap(message=>message.artifacts||[]).map(file=>[file.id,file])).values()];for(const file of artifacts){const button=node('button','chat-context-item');button.type='button';button.append(icon('document'),node('span','',file.title||file.name));button.addEventListener('click',()=>{setChatContext(false);openGeneratedFile(file,$('chat-context-toggle'));});outputs.append(button);}const files=node('button','chat-context-item');files.type='button';files.append(icon('folder'),node('span','','View all files'));files.addEventListener('click',()=>{setChatContext(false);setFilesPane(true,$('chat-context-toggle'));});outputs.append(files);if(queueWritable()){const wakes=node('button','chat-context-item');wakes.type='button';wakes.append(icon('clock'),node('span','','Wakes'));wakes.addEventListener('click',()=>{setChatContext(false);wakeControls.open($('chat-context-toggle'));});outputs.append(wakes);}popup.append(outputs);const children=visibleChats().filter(item=>item.parent_id===chat.id);if(children.length){const section=node('section');section.append(node('div','chat-context-heading','Subagents'));const button=node('button','chat-context-item');button.type='button';children.slice(0,3).forEach(child=>button.append(delegateIcon(child.id,isBusy(child.id))));const working=children.filter(child=>isBusy(child.id)).length;button.append(node('span','',working?`${working} working`:`${children.length} done`));button.addEventListener('click',()=>{section.querySelector('.subagent-group')?.remove();const group=renderSubagentGroup(children.map(child=>({chat_id:child.id,title:child.title})));group.open=true;section.append(group);});section.append(button);popup.append(section);}const sources=node('section'),sourceHeading=node('div','chat-context-heading');sourceHeading.append(node('span','','Sources'));const add=node('button','icon-button');add.type='button';add.setAttribute('aria-label','Add source');add.append(icon('plus'));add.disabled=Boolean(chat.parent_id)||observing();add.addEventListener('click',()=>{setChatContext(false);toggleAttachmentMenu(true);});sourceHeading.append(add);sources.append(sourceHeading);const extensionSources=workspaceExtension.renderChatSources?.(sources,chat) || 0;const attachments=[...new Map(chat.messages.flatMap(message=>message.attachments||[]).map(item=>[item.id,item])).values()];for(const item of attachments){const source=node('a','chat-context-item',item.name||'Image');const url=safeUrl(item.url);if(url&&new URL(url).origin===location.origin){source.href=url;source.target='_blank';source.rel='noopener noreferrer';}source.prepend(icon('images'));sources.append(source);}if(!extensionSources&&!attachments.length)sources.append(node('p','workspace-help','Add sources to this chat.'));popup.append(sources); }
  function setChatContext(open) { $('chat-context-popup').hidden=!open;$('chat-context-toggle').setAttribute('aria-expanded',String(open));if(open){renderChatContext();$('chat-context-popup').querySelector('button')?.focus();} }
  function credentialMetadata(request) {
    if (!request || typeof request.id !== 'string') return null;
    const result = {};
    for (const key of ['id', 'connector_id', 'title', 'reason', 'status', 'skip_reason', 'resolution_reason', 'expires_at']) if (typeof request[key] === 'string') result[key] = request[key];
    if (Number.isFinite(request.connector_revision)) result.connector_revision = request.connector_revision;
    result.fields = (request.fields || []).filter(field => /^[A-Z][A-Z0-9_]{0,63}$/.test(field.name)).map(field => ({ name: field.name, label: typeof field.label === 'string' ? field.label : humanize(field.name), description: typeof field.description === 'string' ? field.description : '', configured: field.configured === true }));
    return result;
  }
  function credentialRequestFor(chatId, requestId) { return chats.find(chat => chat.id === chatId)?.messages.flatMap(message => message.credential_requests || []).find(request => request.id === requestId); }
  function credentialOwnerChat(chat = activeChat()) {
    const organization = chat?.server_saved === true && chat.kind === 'organization_chat' && fixedPurposeKnown(chat.organization_purpose) && chat.environment_id === session?.environment?.id && chat.read_only === false;
    return Boolean(session?.can_query && !observing() && chat && !chat.parent_id && !chat.read_only && (chat.owner_principal_id === principalId() || organization));
  }
  function credentialKey(chatId, requestId) { return `${storageIdentity}:${chatId}:${requestId}`; }
  function credentialResolution(request) {
    if (request.status === 'submitted') return 'Credentials provided';
    if (request.status === 'skipped') return 'Credential setup skipped';
    if (request.status === 'timed_out') return 'Credential request timed out';
    if (request.status === 'cancelled') return ({ stopped: 'Request stopped', runtime_restarted: 'Request closed after reconnecting', connector_changed: 'Connector setup changed', turn_ended: 'Request closed', expired: 'Credential request expired' })[request.resolution_reason] || 'Request closed';
    return 'Credentials requested';
  }
  function credentialRequestCard(request) {
    const card = node('section', 'credential-request-card question-trail'); card.dataset.credentialRequestId = request.id;
    card.title = request.reason || 'Connection credentials';
    const mark = node('span', 'credential-card-mark'); mark.append(icon(request.status === 'submitted' ? 'check' : 'lock'));
    const content = node('div', 'credential-card-copy'); content.append(node('span', 'question-trail-source', request.title || 'Connection credentials'), node('small', '', credentialResolution(request)));
    if (request.skip_reason && request.skip_reason !== 'Skipped by user.') content.append(node('span', 'credential-skip-reason', request.skip_reason)); card.append(mark, content);
    if (request.status === 'pending') { const button = node('button', 'question-trail-open', 'Open'); button.type='button'; button.setAttribute('aria-label','Add credentials'); button.addEventListener('click',()=>openCredentialRequest(activeId,request.id,button)); button.disabled = !credentialOwnerChat(); card.append(button); }
    return card;
  }
  function scheduleCredentialSync() { if (credentialSyncQueued) return; credentialSyncQueued = true; queueMicrotask(() => { credentialSyncQueued = false; syncCredentialRequests(); }); }
  function syncCredentialRequests() {
    if (credentialModal) { const request = credentialRequestFor(credentialModal.chatId, credentialModal.requestId); if (!request || request.status !== 'pending' || request.connector_revision !== credentialModal.revision || credentialModal.generation !== sessionGeneration || credentialModal.chatId !== activeId || currentView !== 'chat' || !credentialOwnerChat()) closeCredentialRequest(false); else return; }
    if (questionModal || currentView !== 'chat' || document.visibilityState !== 'visible' || !credentialOwnerChat() || document.querySelector('dialog[open]')) return;
    const request = activeChat().messages.flatMap(message => message.credential_requests || []).find(request => request.status === 'pending' && !seenCredentialRequests.has(credentialKey(activeId, request.id)));
    if (request) openCredentialRequest(activeId, request.id);
  }
  function openCredentialRequest(chatId, requestId, opener = null) {
    const request = credentialRequestFor(chatId, requestId); if (chatId !== activeId || currentView !== 'chat' || !credentialOwnerChat() || request?.status !== 'pending') return;
    closeNativeQuestion(false); closeCredentialRequest(false); seenCredentialRequests.add(credentialKey(chatId, requestId)); credentialRestoreFocus = true;
    credentialModal = { chatId, requestId, revision: request.connector_revision, generation: sessionGeneration, opener: opener || document.activeElement, busy: false };
    renderCredentialModal(); const dialog=$('credential-request-dialog'); dialog.classList.add('question-box-host'); document.querySelector('.composer-area').prepend(dialog); dialog.show(); credentialModal.panel.focus();
  }
  function closeCredentialRequest(restoreFocus = true) {
    credentialRestoreFocus = restoreFocus; finishCredentialClose(); if ($('credential-request-dialog').open) $('credential-request-dialog').close();
  }
  function finishCredentialClose() {
    const previous = credentialModal; credentialModal = null; previous?.panel?.destroy(); $('credential-request-content').replaceChildren();
    if (credentialRestoreFocus && previous?.chatId === activeId) { const cardButton = document.querySelector(`[data-credential-request-id="${CSS.escape(previous.requestId)}"] button`); if (cardButton) cardButton.focus(); else if (previous.opener?.isConnected) previous.opener.focus(); }
  }
  function renderCredentialModal() {
    const state = credentialModal, request = state && credentialRequestFor(state.chatId, state.requestId); if (!request) return;
    state.panel=window.RocotoQuestionBox.mount($('credential-request-content'),{...request,secure:true},{icon,
      canAnswer:()=>credentialModal===state && state.generation===sessionGeneration && state.chatId===activeId && currentView==='chat' && credentialOwnerChat() && credentialRequestFor(state.chatId,state.requestId)?.status==='pending',
      onSecureSubmit:values=>submitCredentialRequest('submit',values), onSkip:()=>submitCredentialRequest('skip'),
      onTimeout:()=>{ if(credentialModal===state) { closeCredentialRequest(false); pollChild(state.chatId); } }
    });
  }
  function mergeCredentialReply(chatId, raw) {
    const request = credentialMetadata(raw), chat = chats.find(chat => chat.id === chatId); if (!request || !chat) return;
    const message = chat.messages.find(message => message.credential_requests?.some(item => item.id === request.id)); if (!message) return; message.credential_requests = message.credential_requests.map(item => item.id === request.id ? request : item); save(); updateMessage(chat, message);
  }
  async function submitCredentialRequest(action,values={}) {
    const state = credentialModal; if (!state || state.busy || state.generation!==sessionGeneration || state.chatId!==activeId || currentView!=='chat' || !credentialOwnerChat()) return;
    state.busy = true;
    try {
      const response = await fetch(`/api/chats/${encodeURIComponent(state.chatId)}/credential-requests/${encodeURIComponent(state.requestId)}`, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Rocoto-Page': '1' }, body: JSON.stringify(action==='skip' ? { action:'skip', reason:'Skipped by user.' } : { action:'submit', values }) });
      const data = await response.json(); if (state.generation !== sessionGeneration) return;
      if (response.ok || data.request) mergeCredentialReply(state.chatId, data.request || data);
      if (!response.ok) throw new Error(); if (credentialModal === state) closeCredentialRequest();
    } finally { for (const key of Object.keys(values)) delete values[key]; state.busy = false; if(state.generation===sessionGeneration) { scheduleCredentialSync(); scheduleQuestionSync(); } }
  }
  const sendingNativeApprovals = new Set(), nativeApprovalErrors = new Map();
  function nativeApprovalMetadata(raw) {
    if (!raw || typeof raw.id !== 'string' || raw.kind !== 'provider_write' || !raw.arguments_preview || typeof raw.arguments_preview !== 'object' || Array.isArray(raw.arguments_preview)) return null;
    let preview; try { const text = JSON.stringify(raw.arguments_preview); if (new TextEncoder().encode(text).length > 32768) return null; preview = JSON.parse(text); } catch { return null; }
    const request = { kind:'provider_write', arguments_preview:preview };
    for (const key of ['id','root_chat_id','actor_chat_id','provider','server_name','tool','status','created_at','expires_at','resolved_at','resolution_reason','arguments_sha256']) if (typeof raw[key] === 'string') request[key] = raw[key];
    return request;
  }
  function nativeApprovalFor(chatId, requestId) { return chats.find(chat => chat.id === chatId)?.messages.flatMap(message => message.native_approvals || []).find(request => request.id === requestId); }
  function canDecideApproval(request) { return credentialOwnerChat() && request?.root_chat_id === activeId && request.actor_chat_id === activeId && request.status === 'pending' && Date.parse(request.expires_at) > Date.now(); }
  function approvalResolution(request) { return ({pending:'Waiting for your approval',delivering:'Sending your decision',approved:'Allowed once',declined:'Declined',dispatching:'Applying your change',succeeded:'Completed',rejected:'Action declined by provider',uncertain:'Check the provider to confirm this action',delivery_uncertain:'Decision delivery needs review',expired:'Approval expired',cancelled:'Action closed',invalidated:'Connection changed'})[request.status] || 'Action closed'; }
  function nativeApprovalCard(request) {
    const card = node('section','native-approval-card'); card.dataset.nativeApprovalId = request.id;
    const heading = node('header','native-approval-heading'), mark = node('span','native-question-mark'); mark.append(icon(request.status === 'succeeded' ? 'check' : 'lock'));
    const copy = node('div','native-approval-copy'), provider = connectionCatalogue.find(item => item.id === request.provider);
    copy.append(node('strong','',humanize(request.tool)),node('p','',provider?.name || humanize(request.provider))); heading.append(mark,copy); card.append(heading);
    const details = node('details','native-approval-details'), summary = node('summary'); summary.append(icon('document'),node('span','','Action details'),icon('chevron')); details.open = request.status === 'pending'; details.append(summary);
    const args = node('dl','native-approval-arguments');
    for (const [key,value] of Object.entries(request.arguments_preview)) { const label=node('dt','',humanize(key)), content=node('dd'); content.append(node('pre','',typeof value === 'string' ? value : JSON.stringify(value,null,2))); args.append(label,content); }
    details.append(args); card.append(details);
    const footer = node('footer','native-approval-footer'), statusLabel=node('span','native-approval-status',approvalResolution(request)); statusLabel.setAttribute('role','status'); footer.append(statusLabel);
    if (request.status === 'pending') {
      const controls=node('div','native-approval-actions'), key=credentialKey(activeId,request.id);
      for (const [label,action,primary,glyph] of [['Decline','decline',false,'close'],['Allow once','allow_once',true,'check']]) { const button=agentButton(label,()=>submitNativeApproval(request.id,action),primary,glyph); button.disabled=!canDecideApproval(request) || sendingNativeApprovals.has(key); controls.append(button); }
      footer.append(controls);
    }
    card.append(footer);
    const error = nativeApprovalErrors.get(credentialKey(activeId,request.id)); if (error && request.status === 'pending') { const message=node('p','native-question-error',error);message.setAttribute('role','status');card.append(message); }
    return card;
  }
  async function submitNativeApproval(requestId,action) {
    const chatId=activeId, generation=sessionGeneration, key=credentialKey(chatId,requestId), request=nativeApprovalFor(chatId,requestId);
    if (!canDecideApproval(request) || sendingNativeApprovals.has(key) || !['allow_once','decline'].includes(action)) return;
    const chat=activeChat(), message=chat.messages.find(message=>message.native_approvals?.some(item=>item.id===requestId));
    if (!message) return;
    sendingNativeApprovals.add(key);nativeApprovalErrors.delete(key);updateMessage(chat,message);
    try {
      const response=await fetch(`/api/chats/${encodeURIComponent(chatId)}/approvals/${encodeURIComponent(requestId)}`,{method:'POST',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify({action})});
      const data=await response.json(); if(generation!==sessionGeneration)return;
      const current=nativeApprovalMetadata(data.request);
      if(current?.id===requestId && current.root_chat_id===chatId){message.native_approvals=message.native_approvals.map(item=>item.id===requestId?current:item);save();updateMessage(chat,message);}
      if(!response.ok)throw Error();
    } catch { if(generation===sessionGeneration)nativeApprovalErrors.set(key,'Your decision is ready to send. Please try again.'); }
    finally { sendingNativeApprovals.delete(key);if(generation===sessionGeneration)updateMessage(chat,message); }
  }
  function nativeQuestionMetadata(raw) {
    if (!raw || typeof raw.id !== 'string' || raw.kind !== 'question') return null;
    const request = { kind: 'question', blocking: raw.blocking === true };
    for (const key of ['id','root_chat_id','actor_chat_id','actor_title','status','created_at','expires_at','resolved_at','resolution_reason']) if (typeof raw[key] === 'string') request[key] = raw[key];
    request.questions = (Array.isArray(raw.questions) ? raw.questions : []).slice(0,3).filter(question => typeof question?.id === 'string' && typeof question?.question === 'string').map(question => ({ id: question.id, header: typeof question.header === 'string' ? question.header : '', question: question.question, is_other: question.is_other === true, multiple: question.multiple === true, options: (Array.isArray(question.options) ? question.options : []).filter(option => typeof option?.label === 'string').map(option => ({ label: option.label, description: typeof option.description === 'string' ? option.description : '' })) }));
    return request;
  }
  function nativeQuestionFor(chatId, requestId) { return chats.find(chat => chat.id === chatId)?.messages.flatMap(message => message.native_questions || []).find(request => request.id === requestId); }
  function canAnswerQuestion(request) { return credentialOwnerChat() && request?.root_chat_id === activeId && request.status === 'pending' && request.questions?.length > 0; }
  function questionResolution(request) { return ({pending:'Waiting for your answer',delivering:'Sending answers',submitted:'Answers sent',skipped:'Question skipped',timed_out:'Question timed out',resolved:'Question resolved',interrupted:'Question closed',delivery_uncertain:'Delivery needs review',unsupported:'Continue in chat'})[request.status] || 'Question closed'; }
  function nativeQuestionCard(request) {
    const card = node('section','native-question-card question-trail'); card.dataset.nativeQuestionId = request.id;
    card.title = (request.questions || []).map(question=>question.question).join(' ');
    const mark = node('span','native-question-mark'); mark.append(icon(request.status === 'submitted' ? 'check' : 'chat'));
    const source=request.actor_chat_id !== request.root_chat_id && request.actor_title ? request.actor_title : (request.questions || []).map(question=>question.header).filter(Boolean).join(' · ') || 'Question';
    const copy = node('div','native-question-copy'); copy.append(node('span','question-trail-source',source),node('small','',questionResolution(request))); card.append(mark,copy);
    if (request.status === 'pending' && request.questions?.length) { const button=node('button','question-trail-open','Open');button.type='button';button.setAttribute('aria-label',request.questions.length === 1 ? 'Answer question' : 'Answer questions');button.addEventListener('click',()=>openNativeQuestion(activeId,request.id,button));button.disabled = !canAnswerQuestion(request) || sendingNativeQuestions.has(credentialKey(activeId,request.id));card.append(button); }
    return card;
  }
  function scheduleQuestionSync() { if (questionSyncQueued) return; questionSyncQueued = true; queueMicrotask(() => { questionSyncQueued = false; syncNativeQuestions(); }); }
  function syncNativeQuestions() {
    if (questionModal) { const request = nativeQuestionFor(questionModal.chatId,questionModal.requestId); if (questionModal.generation !== sessionGeneration || currentView !== 'chat' || questionModal.chatId !== activeId || !canAnswerQuestion(request)) closeNativeQuestion(false); else return; }
    if (credentialModal || currentView !== 'chat' || document.visibilityState !== 'visible' || !credentialOwnerChat() || document.querySelector('dialog[open]')) return;
    const request = activeChat().messages.flatMap(message => message.native_questions || []).find(request => canAnswerQuestion(request) && !seenNativeQuestions.has(credentialKey(activeId,request.id)));
    if (request) openNativeQuestion(activeId,request.id);
  }
  function openNativeQuestion(chatId,requestId,opener = null) {
    const request = nativeQuestionFor(chatId,requestId); if (chatId !== activeId || currentView !== 'chat' || !canAnswerQuestion(request) || sendingNativeQuestions.has(credentialKey(chatId,requestId))) return;
    closeCredentialRequest(false); closeNativeQuestion(false); seenNativeQuestions.add(credentialKey(chatId,requestId)); questionRestoreFocus = true;
    const state=questionModal = {chatId,requestId,key:credentialKey(chatId,requestId),generation:sessionGeneration,opener:opener || document.activeElement,busy:false};
    state.panel=window.RocotoQuestionBox.mount($('native-question-content'),request,{icon,
      canAnswer:()=>questionModal===state && state.generation===sessionGeneration && state.chatId===activeId && currentView==='chat' && canAnswerQuestion(nativeQuestionFor(state.chatId,state.requestId)),
      onSubmit:answers=>submitNativeQuestion('submit',answers),onSkip:()=>submitNativeQuestion('skip'),onTimeout:()=>submitNativeQuestion('timeout')
    });
    const dialog=$('native-question-dialog');dialog.classList.add('question-box-host');document.querySelector('.composer-area').prepend(dialog);dialog.show();state.panel.focus();
  }
  function closeNativeQuestion(restoreFocus = true) { questionRestoreFocus = restoreFocus; finishQuestionClose(); if ($('native-question-dialog').open) $('native-question-dialog').close(); }
  function finishQuestionClose() {
    const previous = questionModal; questionModal = null; previous?.panel?.destroy(); $('native-question-content').replaceChildren();
    if (questionRestoreFocus && previous?.chatId === activeId) { const button = document.querySelector(`[data-native-question-id="${CSS.escape(previous.requestId)}"] button`); if (button) button.focus(); else if (previous.opener?.isConnected) previous.opener.focus(); }
  }
  function mergeQuestionReply(chatId,raw) {
    const request = nativeQuestionMetadata(raw), chat = chats.find(chat => chat.id === chatId); if (!request || !chat) return;
    const message = chat.messages.find(message => message.native_questions?.some(item => item.id === request.id)); if (!message) return;
    message.native_questions = message.native_questions.map(item => item.id === request.id ? request : item); save(); updateMessage(chat,message);
  }
  async function submitNativeQuestion(action,answers=null) {
    const state=questionModal,request=state&&nativeQuestionFor(state.chatId,state.requestId);
    if(!state || state.busy || state.generation!==sessionGeneration || state.chatId!==activeId || currentView!=='chat' || sendingNativeQuestions.has(state.key) || !canAnswerQuestion(request))return;
    state.busy=true;sendingNativeQuestions.add(state.key);const button=document.querySelector(`[data-native-question-id="${CSS.escape(state.requestId)}"] button`);if(button)button.disabled=true;
    try { const response=await fetch(`/api/chats/${encodeURIComponent(request.root_chat_id)}/questions/${encodeURIComponent(request.id)}`,{method:'POST',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify(action==='submit'?{action,answers}:{action})});const data=await response.json();if(state.generation!==sessionGeneration)return;if(data.request)mergeQuestionReply(state.chatId,data.request);if(!response.ok)throw Error();if(questionModal===state)closeNativeQuestion(); }
    finally {state.busy=false;sendingNativeQuestions.delete(state.key);if(state.generation===sessionGeneration){const button=document.querySelector(`[data-native-question-id="${CSS.escape(state.requestId)}"] button`);if(button)button.disabled=!canAnswerQuestion(nativeQuestionFor(state.chatId,state.requestId));scheduleQuestionSync();scheduleCredentialSync();}}
  }
  function messageActions(message, article) {
    if (!message.text || message.pending) return;
    article.tabIndex = 0;
    const actions = node('div','message-actions'); actions.setAttribute('role','group'); actions.setAttribute('aria-label','Message actions');
    const copy = node('button','icon-button message-copy'); copy.type = 'button'; copy.setAttribute('aria-label','Copy message'); copy.title = 'Copy message'; copy.append(icon('copy'));
    copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(message.text);if(copy.isConnected){copy.replaceChildren(icon('check'));copy.title='Copied';}}catch{if(copy.isConnected){copy.title='Copy again';notice('Select the message text to copy it.');}}});actions.append(copy);
    const recorded = message.role === 'assistant' ? message.completed_at || message.created_at || message.started_at : message.created_at;
    if (recorded) { const date = new Date(typeof recorded === 'number' ? recorded * (recorded < 1e12 ? 1000 : 1) : recorded); if (Number.isFinite(date.getTime())) { const time = node('time','message-timestamp',date.toLocaleTimeString(undefined,{hour:'2-digit',minute:'2-digit',hour12:false})); time.dateTime = date.toISOString();time.title=date.toLocaleString();actions.append(time); } }
    article.append(actions);
  }
  function messageChildren(message) { return message.subchats?.length ? message.subchats : message === [...(activeChat()?.messages || [])].reverse().find(item=>item.role==='assistant') ? visibleChats().filter(chat=>chat.parent_id===activeId).map(chat=>({chat_id:chat.id,title:chat.title})) : []; }
  function messageRenderSignature(message) {
    const chat=activeChat(),children=messageChildren(message).map(child=>{const saved=chats.find(item=>item.id===child.chat_id);return [child,saved?.title,isBusy(child.chat_id),childActivity(saved)];});
    return JSON.stringify([message,sessionGeneration,activeId,session?.can_query,observing(),chat?.read_only,chat?.parent_id,children,(message.skill_ids||[]).map(id=>[id,installedSkills.find(skill=>skill.id===id)?.name]),message.tools?.length?filesData:null,workspaceExtension.messageDependencies?.(message),message.channel_deliveries?.length?message.channel_deliveries.map(delivery=>phoneParticipantName(delivery.member_id)):null]);
  }
  function reconcileMessages(messages,scope) {
    const reusable=renderedMessagesScope===scope,existing=new Map([...elements.messages.children].map(article=>[article.dataset.messageId,article]));
    const wanted=new Set(messages.map(message=>String(message.id)));
    for(const article of existing.values())if(!reusable || !wanted.has(article.dataset.messageId)){pauseMedia(article);article.remove();}
    for(const [index,message] of messages.entries()){
      const prior=existing.get(String(message.id)),old=reusable?prior:null,signature=messageRenderSignature(message),saved=prior&&messageRenderSignatures.get(prior);
      let article=old;
      if(!old || saved?.signature!==signature){article=renderMessage(message);if(prior && saved?.scope===JSON.stringify([sessionGeneration,activeId]))restoreDisclosures(article,disclosureState(prior));if(old){pauseMedia(old);old.replaceWith(article);}}
      const position=elements.messages.children[index];if(position!==article)elements.messages.insertBefore(article,position||null);
    }
    renderedMessagesScope=scope;
  }
  function renderMessage(message) {
    const article = node('article', `message message-${message.role}`); article.dataset.messageId = message.id;messageRenderSignatures.set(article,{signature:messageRenderSignature(message),scope:JSON.stringify([sessionGeneration,activeId])});
    if (message.role === 'user') { const bubble = node('div', 'user-bubble'); if (message.steer) bubble.append(node('div', 'steer-label', 'Sent as guidance')); const phoneSender = window.RocotoMessaging?.senderLabel(message.channel, session?.principal?.id); if (phoneSender) bubble.append(node('div', 'message-channel-sender', phoneSender)); if (message.attachments?.length) { const attachments = node('div', 'user-attachments'); for (const item of message.attachments) { const url = safeUrl(item.url); if (url) { const image = node('img'); image.src = url; image.alt = item.name || 'Attached image'; attachments.append(image); } } bubble.append(attachments); } workspaceExtension.renderUserAttachments?.(bubble,message); if (message.skill_ids?.length) { const skills = node('div', 'message-skills'); for (const id of message.skill_ids) skills.append(node('span', '', installedSkills.find((skill) => skill.id === id)?.name || id)); bubble.append(skills); } bubble.append(node('div', 'user-text', message.text)); article.append(bubble); messageActions(message,article); return article; }
    const awaitingApproval = (message.native_approvals || []).some(request => request.status === 'pending'), awaitingCredentials = (message.credential_requests || []).some(request => request.status === 'pending'), awaitingQuestion = (message.native_questions || []).some(request => request.status === 'pending' && request.blocking);
    const completed = (message.tools || []).filter((tool) => tool.status !== 'running' || !message.pending);
    const recordedProgress = (message.progress || []).filter(entry => !['thinking', 'tool_progress', 'waiting_for_children'].includes(entry.activity));
    const children = messageChildren(message);
    if (message.pending) {
      const timer = node('div', 'work-timing', workDuration(message)); if (message.started_at) timer.dataset.startedAt = message.started_at; article.append(timer);
      const commentary = (message.progress || []).filter(entry => ['intermediate_answer', 'commentary'].includes(entry.activity)); for (const entry of commentary) { const text = node('div','assistant-commentary'); renderText(text,entry.text);article.append(text); }
      if (completed.length) article.append(renderToolTrail(completed, true, message));
      const live = node('div', 'live-activity'); live.setAttribute('role', 'status'); const activeTools = (message.tools || []).filter(tool => tool.status === 'running'); const activeTool = activeTools.at(-1);
      for (const entry of (message.progress || []).filter(item => ['child_completed','plan'].includes(item.activity))) { const milestone=node('div','activity-row activity-milestone');milestone.append(icon(entry.activity==='child_completed'?'check':'document'),node('span','',entry.text));live.append(milestone); }
      const latestEntry = (message.progress || []).filter(item => !['child_completed','plan','intermediate_answer','commentary'].includes(item.activity) && (item.activity !== 'tool_progress' || item.id && activeTools.some(tool => tool.id === item.id))).at(-1); const latest=latestEntry?.activity==='thinking'?'Thinking':latestEntry?.text;
      const waiting=awaitingCredentials || awaitingQuestion || awaitingApproval,label=awaitingApproval ? 'Waiting for your approval' : awaitingCredentials ? 'Waiting for credentials' : awaitingQuestion ? 'Waiting for your answer' : activeTool ? latestEntry?.activity==='tool_progress' && /^[A-Za-z]+ing(?:\s|[.…]|$)/.test(latest||'') ? latest : toolLabel(activeTool,true) : latest || 'Thinking';
      if(activeTool&&!waiting)live.append(toolCall(activeTool,{working:true,label,index:(message.tools||[]).indexOf(activeTool)}));else{const row=node('div',waiting?'activity-row credential-waiting':'activity-row'),text=node('span','current-activity',label);if(awaitingCredentials||awaitingApproval)row.append(icon('lock'));else if(awaitingQuestion)row.append(icon('chat'));row.append(waiting?text:activityShimmer(text));live.append(row);}article.append(live);
      if(children.length)article.append(renderSubagentGroup(children));
    } else if (completed.length || recordedProgress.length || children.length || Number.isFinite(message.elapsed_ms)) {
      const trail=node('details','completed-turn'),summary=node('summary');trail.dataset.detailKey='completed-turn';summary.append(node('span','',workDuration(message)),icon('chevron'));trail.append(summary);
      for(const entry of recordedProgress){const text=node('div','progress-text');renderText(text,entry.text);trail.append(text);}if(completed.length)trail.append(renderToolTrail(completed, false, message));if(children.length)trail.append(renderSubagentGroup(children));article.append(trail);
    }
    const text = node('div', 'assistant-text'); renderText(text, message.text || '', message); article.append(text);
    for (const request of message.credential_requests || []) article.append(credentialRequestCard(request));
    for (const request of message.native_questions || []) article.append(nativeQuestionCard(request));
    for (const request of message.native_approvals || []) article.append(nativeApprovalCard(request));
    for(const agent of normalizeAgentCards(message.agents))article.append(agentConversationCard(agent));
    if(message.business_map)article.append(businessMapSurface(message.business_map.collection,message.business_map));
    if (message.artifacts?.length) { const artifacts=node('div','message-artifacts');message.artifacts.forEach(file=>artifacts.append(artifactCard(file)));article.append(artifacts); }
    if (message.stopped) article.append(node('div', 'observation-status', 'Stopped'));
    workspaceExtension.renderMessageContent?.(article,message);
    if (message.error && !activeChat()?.parent_id) { const retry = node('button', 'retry-button', 'Try again'); retry.addEventListener('click', () => { elements.message.value = message.query || ''; resizeComposer(); elements.message.focus(); }); article.append(retry); }
    if(message.channel_delivery){const label=window.RocotoMessaging?.chipLabel(message.channel_delivery);if(label){const status=node('div',`message-channel-status${message.channel_delivery.status==='failed'?' phone-attention':''}`);status.append(icon('chat'),node('span','',label));article.append(status);}}
    if(Array.isArray(message.channel_deliveries)&&message.channel_deliveries.length){const list=node('ul','message-channel-deliveries');list.setAttribute('aria-label','Phone delivery');for(const delivery of message.channel_deliveries){const state=window.RocotoMessaging?.deliveryText(delivery);if(!state)continue;const item=node('li',delivery.status==='failed'?'phone-attention':'');item.append(icon('chat'),node('span','',`${phoneParticipantName(delivery.member_id)} · ${state}`));list.append(item);}if(list.childElementCount)article.append(list);}
    messageActions(message,article); return article;
  }

  function updateMessage(chat, message) {
    workspaceExtension.refreshMessage?.(message);
    if (chat.id !== activeId) return;
    const previousScroll=elements.conversation.scrollTop;const distance = elements.conversation.scrollHeight - previousScroll - elements.conversation.clientHeight;
    const old = elements.messages.querySelector(`[data-message-id="${CSS.escape(message.id)}"]`);
    if (old) { const expanded=disclosureState(old),updated=renderMessage(message);restoreDisclosures(updated,expanded);updated.style.minHeight=old.style.minHeight; pauseMedia(old); const oldGroup = old.querySelector('.subagent-group'), newGroup = updated.querySelector('.subagent-group'); if (oldGroup && newGroup) newGroup.open = oldGroup.open; old.replaceWith(updated); } else elements.messages.append(renderMessage(message));
    if (chat.turn_anchor) {reserveTurnSpace(chat);elements.conversation.scrollTop=previousScroll;} else if (distance < 180) elements.conversation.scrollTop = elements.conversation.scrollHeight;
    scheduleCredentialSync(); scheduleQuestionSync();
  }
  function reserveTurnSpace(chat) {
    const anchor=chat?.turn_anchor;if(!anchor||chat.id!==activeId)return;
    const user=elements.messages.querySelector(`[data-message-id="${CSS.escape(anchor.user)}"]`),answer=elements.messages.querySelector(`[data-message-id="${CSS.escape(anchor.answer)}"]`);
    if(user&&answer)answer.style.minHeight=`${Math.max(0,elements.conversation.clientHeight-72-user.getBoundingClientRect().height-32)}px`;
  }
  function anchorSubmittedTurn(chat) {
    reserveTurnSpace(chat);const user=elements.messages.querySelector(`[data-message-id="${CSS.escape(chat.turn_anchor.user)}"]`);
    if(user)elements.conversation.scrollTop+=user.getBoundingClientRect().top-elements.conversation.getBoundingClientRect().top-72;
  }
  let navigationFocusKey = '';
  const companyPicker=$('environment-toggle').closest('.workspace-picker'),companyPickerHome=document.createComment('company-picker-home');companyPicker.before(companyPickerHome);
  function placeCompanyPicker(){const target=workspaceExtension.companyPickerTarget?.(),parent=target || companyPickerHome.parentElement;if(companyPicker.parentElement!==parent){setPicker('environment',false);if(target)parent.append(companyPicker);else companyPickerHome.after(companyPicker);}const real=(session?.environments||[]).filter(item=>item.id!=='demo');companyPicker.hidden=demoEnvironment()?real.length===0:real.length<2;if(companyPicker.hidden)setPicker('environment',false);if(target)$('environment-label').textContent=session?.environment?.name||'Company';}

  function focusCollection(key,inputId) { if(navigationFocusKey===key)return;navigationFocusKey=key;queueMicrotask(()=>{const input=$(inputId);if(navigationFocusKey===key&&input?.isConnected&&input.getClientRects().length&&!document.querySelector('dialog[open]'))input.focus({preventScroll:true});}); }
  function detailBreadcrumb(parent,title,back) { const row=node('div','detail-breadcrumb'),button=agentButton(parent,back,false);row.append(button,icon('chevron'),node('span','',title));elements['chat-title'].replaceChildren(row); }
  function collectionNavigation() {
    const extensionNavigation=workspaceExtension.navigationState?.() || {};
    const detail=currentView==='marketplace'?Boolean(marketplaceSelection?.name||marketplaceDetail||new URL(location.href).searchParams.get('item')):currentView==='agents'?Boolean(selectedAgentId):Boolean(extensionNavigation.detail);
    const key=`${sessionGeneration}:${currentView}:${currentView==='marketplace'?marketplaceTab:extensionNavigation.key || ''}:${detail?'detail':'list'}`;
    const input=currentView==='marketplace'?'marketplace-search':currentView==='agents'?'agents-search':extensionNavigation.inputId || null;
    if(!detail&&input)focusCollection(key,input);else navigationFocusKey=key;
  }
  const catalogueMatches = item => `${item.name || item.title || ''} ${item.description || ''}`.toLowerCase().includes(connectionSearch.toLowerCase());

  function render() {
    wakeControls.sync();
    workspaceTabs?.setVisible(currentView === 'home' || currentView.startsWith('vertical:'));
    if (currentView !== 'chat') workspaceExtension.renderChatContext?.();
    workspaceExtension.beforeRender?.();
    if(currentView!=='chat'){pauseMedia(elements.messages);renderedMessagesScope=null;}
    if(businessEditor && !businessEditor.current())closeBusinessEditor();
    if(currentView!=='business' && !$('business-chat-drawer').hidden)closeBusinessChat();
    if(agentInfo && !agentInfo.current())closeAgentInfo();
    renderAgentHeader();placeCompanyPicker(); collectionNavigation();renderFounder();
    const emptyChat=currentView==='chat'&&!activeChat()?.messages.length&&!activeChat()?.parent_id&&!activeChat()?.agent_setup&&activeChat()?.kind!=='organization_chat'&&!workspaceExtension.alternateState?.(activeId);document.body.classList.toggle('empty-chat',emptyChat&&homeMode==='chat');document.body.classList.toggle('empty-search',emptyChat&&homeMode==='search');$('workspace-backdrop').hidden=!emptyChat;
    if (mcpModal && (currentView !== 'mcp' || !mcpWritable() || mcpModal.identity !== mcpIdentity())) closeMcpDialog();
    if (credentialModal && (currentView !== 'chat' || activeId !== credentialModal.chatId || observing() || activeChat()?.parent_id)) closeCredentialRequest(false);
    scheduleCredentialSync(); scheduleQuestionSync(); renderAgentSetupNotice();
    $('customize-context').hidden = currentView !== 'marketplace';  document.body.classList.toggle('customize-context-active', currentView === 'marketplace'); document.querySelector('.topbar').hidden = Boolean(workspaceExtension.surfaceLayout?.()?.hideTopbar) || emptyChat; $('agent-context').hidden = true; document.body.classList.remove('agents-context-active');
    for(const [view,id] of [['profile','profile-view'],['plugins','plugin-settings-view'],...['appearance','notifications','team','messaging','billing'].map(view=>[view,view+'-view'])]){$(id).hidden=currentView!==view;$('settings-'+view).setAttribute('aria-current',currentView===view?'page':'false');}
    $('mcp-view').hidden = currentView !== 'mcp'; $('settings-mcp').setAttribute('aria-current', currentView === 'mcp' ? 'page' : 'false');
    const statsOption=$('settings-stats');statsOption.dataset.settingsAvailable=String(statsAllowed());statsOption.hidden=!statsAllowed();$('settings-business').setAttribute('aria-current',currentView==='business'?'page':'false');statsOption.setAttribute('aria-current',currentView==='stats'?'page':'false');
    const settings = ['profile','plugins','appearance','notifications','team','messaging','billing','business','stats'].includes(currentView) || currentView === 'mcp' || Boolean(workspaceExtension.surfaceLayout?.()?.settings) || currentView === 'archives' || currentView === 'usage'; $('usage-view').hidden = currentView !== 'usage'; $('settings-usage').setAttribute('aria-current', currentView === 'usage' ? 'page' : 'false'); $('archives-view').hidden = currentView !== 'archives'; $('settings-archives').setAttribute('aria-current', currentView === 'archives' ? 'page' : 'false'); document.body.classList.toggle('settings-context-active', settings); document.body.classList.toggle('chat-context-active', currentView === 'chat' || currentView === 'agents');document.body.classList.toggle('agent-conversation-active',currentView==='chat'&&Boolean(conversationAgentId(activeChat()))); settingsShell.sync(settings);
     $('chat-context-toggle').hidden = currentView !== 'chat' || emptyChat; if (currentView !== 'chat') setChatContext(false); updateFilesSurface(); $('marketplace-view').hidden = currentView !== 'marketplace';
    $('stats-view').hidden = currentView !== 'stats';$('business-view').hidden=currentView!=='business';
    workspaceExtension.renderView?.();
    $('workspace-tab-view').hidden = currentView !== 'home'&&!currentView.startsWith('vertical:');

    $('home-mode-switch').hidden = currentView !== 'chat' || !legacyQuickSearch;
    $('agents-view').hidden = currentView !== 'agents';  elements.conversation.hidden = currentView !== 'chat'; document.querySelector('.composer-area').hidden = currentView !== 'chat' || Boolean(activeChat()?.parent_id);
    if (currentView.startsWith('vertical:')) { renderSidebar(); elements['chat-title'].textContent=harness.surfaces.find(row=>row.id===currentView.slice(9))?.title||'Workspace'; ensureWorkspaceTabs(); return; }
    if (currentView === 'home') { $('agent-setup-notice').hidden = true; renderSidebar(); elements['chat-title'].textContent = 'Home'; if (session) ensureWorkspaceTabs(); return; }
    if(workspaceExtension.renderActiveSurface?.()){renderSidebar();return;}
    if (currentView === 'mcp') { renderSidebar(); elements['chat-title'].textContent = 'Rocoto MCP'; renderMcp(); return; }
    if (currentView==='profile'){renderSidebar();elements['chat-title'].textContent='Profile';renderProfileSettings();return;}
    if(currentView==='plugins'){renderSidebar();elements['chat-title'].textContent='Plugins';renderPluginSettings();return;}
    if (['appearance','notifications','team','messaging','billing'].includes(currentView)){renderSidebar();elements['chat-title'].textContent={appearance:'Appearance',notifications:'Notifications',team:'Team',messaging:'Messaging',billing:'Billing'}[currentView];renderWorkspaceSettings();return;}
    if (currentView === 'usage') { renderSidebar(); elements['chat-title'].textContent = 'Usage'; renderUsage(); return; }
    if (currentView === 'business') { renderSidebar(); elements['chat-title'].textContent = 'Your Business'; renderBusiness(); return; }
    if (currentView === 'stats') { renderSidebar(); elements['chat-title'].textContent = 'Stats'; renderStatsActivity(); return; }
    if (currentView === 'archives') { renderSidebar(); elements['chat-title'].textContent = 'Archived chats'; renderArchives(); return; }
    if (currentView === 'marketplace') { renderSidebar(); elements['chat-title'].textContent = marketplaceTab === 'agents' ? 'Agents marketplace' : marketplaceTab === 'skills' ? 'Skills' : marketplaceTab === 'yours' ? 'Your connections' : 'Connections'; renderMarketplace(); return; }
    if (currentView === 'agents') { renderSidebar(); elements['chat-title'].textContent = 'Agents'; renderAgents(); return; }
    const previousTop = elements.conversation.scrollTop, changedChat = renderedChatId !== activeId; renderedChatId = activeId;
    renderSidebar(); const chat = activeChat(); elements['chat-title'].textContent = chat?.title || 'New chat';
    if (chat?.parent_id) { const title = elements['chat-title'], back = node('button', 'icon-button return-parent'); back.type = 'button'; back.title = 'Return to parent'; back.setAttribute('aria-label', 'Return to parent'); back.append(icon('chevron')); back.addEventListener('click', () => openChatId(chat.parent_id)); const name = node('span', '', chat.title); name.title = chat.title; title.replaceChildren(back, delegateIcon(chat.id, isBusy(chat.id)), name); title.classList.add('child-chat-title'); } else elements['chat-title'].classList.remove('child-chat-title');if(conversationAgentId(chat))detailBreadcrumb('Agents',chat.title,()=>showAgents());
    const continuation = $('chat-continuation'); continuation.replaceChildren(); continuation.hidden = !chat?.continuation_of; if (chat?.continuation_of) { const link = node('a', '', `Continued from ${chat.continuation_title || chats.find((item) => item.id === chat.continuation_of)?.title || 'saved report'}`); link.href = `/?chat=${encodeURIComponent(chat.continuation_of)}`; link.append(icon('chevron')); link.addEventListener('click', (event) => { event.preventDefault(); openChatId(chat.continuation_of); }); continuation.append(link); }
    const messages = chat?.messages || [], search = workspaceExtension.alternateState?.(chat?.id), quick = homeMode === 'search'; elements.welcome.hidden = quick ? Boolean(search) : messages.length > 0; elements.messages.hidden = quick; if (quick) continuation.hidden = true; reconcileMessages(messages,JSON.stringify([sessionGeneration,activeId,homeMode]));
    const orphanGroup = $('chat-subagents'), orphanChildren = visibleChats().filter((item) => activeId && item.parent_id === activeId).map((item) => ({ chat_id: item.id, title: item.title })), orphanOpen = orphanGroup.querySelector('details')?.open; orphanGroup.replaceChildren(); orphanGroup.hidden = quick || messages.some((item) => item.role === 'assistant') || !orphanChildren.length; if (!orphanGroup.hidden) { const group = renderSubagentGroup(orphanChildren); group.open = Boolean(orphanOpen); orphanGroup.append(group); }
    $('home-quick-search').setAttribute('aria-pressed', String(quick)); $('home-chat').setAttribute('aria-pressed', String(!quick)); workspaceExtension.renderComposerContent?.();
    const needsScope = false, choices = $('source-choices'); choices.hidden = !needsScope; choices.replaceChildren();
    elements.welcome.querySelector('h1').textContent = chat?.kind==='organization_chat' ? chat.title : chat?.agent_setup ? 'Configure your agent' : quick ? 'Quick Search' : 'New chat'; elements.welcome.querySelector('.welcome-icon use').setAttribute('href', chat?.agent_setup ? '#i-clock' : quick ? '#i-search' : '#i-chat'); elements.welcome.querySelector('p').textContent = chat?.kind==='organization_chat' ? (harness.fixed_chats.find(item => item.purpose === chat.organization_purpose)?.job || 'Your workspace and ongoing work.') : needsScope ? 'Choose a collection to begin.' : chat?.agent_setup ? 'Describe what it should look for and what you want it to deliver.' : quick ? workspaceExtension.alternateWelcome || 'Describe what you want to find.' : 'Describe the work you want to do.'; elements.welcome.querySelector('.suggestions').hidden = !demoEnvironment() || needsScope || quick || Boolean(chat?.agent_setup);
    if (needsScope) scopes().forEach(scope => { const {id,label}=scope; const choice = node('button', 'source-choice'); choice.type = 'button'; choice.append(icon(workspaceExtension.collectionIcon || 'folder'), node('strong', '', label), node('small', '', workspaceExtension.collectionSummary?.(scope) || '')); choice.addEventListener('click', () => selectScope(id)); choices.append(choice); });
    const suggestions=elements.welcome.querySelector('.suggestions');suggestions.replaceChildren();suggestions.hidden=true;workspaceExtension.renderWelcomeExamples?.(chat,{needsScope,quick});

    reserveTurnSpace(chat);
    if (changedChat) { const latest = Array.from(elements.messages.querySelectorAll('.message-assistant')).at(-1) || elements.messages.lastElementChild; elements.conversation.scrollTop = latest ? elements.conversation.scrollTop + latest.getBoundingClientRect().top - elements.conversation.getBoundingClientRect().top - 12 : 0; } else elements.conversation.scrollTop = previousTop;
    showLinkedAlert();
    renderQueue();
    renderAttachments(); updateMode();
  }

  // Only opaque transfer receipts and content fingerprints survive a reload. File
  // bytes stay in browser File handles; each hash/request buffer is bounded.

  function setRailChats(open) { document.body.classList.toggle('chats-open', open); $('rail-chats').setAttribute('aria-expanded', String(open)); }
  const mobileChatNavigation = matchMedia('(max-width: 760px)');
  function syncMobileChatNavigation() { $('rail-chats').hidden = !mobileChatNavigation.matches; if (!mobileChatNavigation.matches) setRailChats(false); }
  mobileChatNavigation.addEventListener('change', syncMobileChatNavigation);
  syncMobileChatNavigation();
  function selectSessionStorage(data) {
    const identity = `${data.environment.id}:${data.effective_principal_id || data.principal?.id || 'legacy'}`;
    if (session?.environment?.id && session.environment.id !== data.environment.id) { businessCollection = ''; workspaceExtension.resetEnvironment?.(); }
    if (identity === storageIdentity) return;
    const firstIdentity=storageIdentity===null;
    if (storageIdentity !== null) { persistDraft(); save(); saveQueue(); workspaceExtension.saveDrafts?.(); }
    closeCredentialRequest(false); closeNativeQuestion(false); clearCustomCredentialInputs(); clearConnectionCredential(); if (marketplaceSelection?.custom) { marketplaceSelection = null; $('marketplace-content').replaceChildren(); }
    resetBusinessMaps();workspaceExtension.resetScope?.();resetMcp(); workspaceExtension.resetWorkspace?.(); workspaceExtension.resetConnectorDetails?.(); storageIdentity = identity; ++sessionGeneration; resetFiles(); nativePlugins = []; nativePluginsAvailable = false; nativeInstallable.clear(); nativePluginOperations.clear(); nativePluginCleanup.clear(); nativePluginError = ''; nativePluginDialog = null; ++nativePluginEpoch; catalogueLoading = false; brokerConnections = []; customConnections = []; customConnectorError = ''; customConnectorLoading = false; ++customDetailGeneration; $('custom-connector-dialog').close(); $('custom-connector-form').reset(); connectionFlow = null; clearTimeout(connectionFlowTimer); $('connection-setup-dialog').close(); chatListLoading = false;
    for (const timer of childPolls.values()) if (timer !== true) clearTimeout(timer);
    childPolls.clear(); running.clear(); verifiedRunning.clear();  textDrafts.clear(); attachmentDrafts.clear();
    const suffix = data.effective_principal_id || data.principal?.id ? `:${encodeURIComponent(identity)}` : '';
    STORAGE_KEY = `rocoto-chats-v1${suffix}`; QUEUE_KEY = `rocoto-queue-v1${suffix}`; DRAFT_KEY = data.viewing_as ? null : `rocoto-draft-v1${suffix}`;
    // Move only this authenticated scope; other companies' saved drafts keep their own keys.
    try {
      for (const name of ['chats', 'queue', 'draft']) {
        const previous = `rocoto-video-${name}-v1${suffix}`, current = `rocoto-${name}-v1${suffix}`;
        for (const key of Object.keys(localStorage).filter(key => key === previous || name === 'draft' && key.startsWith(previous + ':'))) {
          const next = current + key.slice(previous.length);
          if (localStorage.getItem(next) === null) localStorage.setItem(next, localStorage.getItem(key));
          localStorage.removeItem(key);
        }
      }
    } catch { /* Keep saved state usable when storage is temporarily unavailable. */ }
    const read = (key) => { try { const items = JSON.parse(localStorage.getItem(key) || '[]'); return Array.isArray(items) ? items : []; } catch { return []; } };
    chats = read(STORAGE_KEY).filter((chat) => chat?.id && Array.isArray(chat.messages)); queued = read(QUEUE_KEY).filter((item) => chats.some((chat) => chat.id === item.chat_id)); workspaceExtension.loadDrafts?.(suffix,read);
    restoreDrafts();activeId = preferredChatId(chats); elements.message.value = textDrafts.get(activeId)||''; renderedChatId = null;
    messagingData=null;messagingLoading=false;messagingError='';++messagingEpoch;memberDialog?.close();memberDialog=null;agentPhoneBlocks.clear();agentPhoneActive=agentPhoneLoading=null;phoneContactCard?.close();phoneContactCard=null;statsActivity=null;statsActivityLoading=false;statsActivityError='';++statsActivityEpoch;founderCollection='';collectionPicker=null;teamLoading=false;teamError='';profileData=null;profileError='';profileLoading=false;++profileEpoch;organisationAllowed=null;
    workspaceCapabilities.clear(); workspaceCapabilityRequests.clear();
    closeAgentInfo();++agentNavigationEpoch;agentsLoading=false;clearTimeout(agentsTimer);
    agents = []; agentCapabilities = {}; agentsEnvironment = null; agentsError = ''; agentDetailSignature = null; selectedAgentId = firstIdentity&&currentView==='agents'?new URL(location.href).searchParams.get('agent'):null; businessData = null; usageData = null; usageCheckedAt=0;++usageEpoch; observers = []; installedSkills = []; skillCatalogue = null;
    $('business-chat-drawer').hidden = true; $('business-chat-frame').removeAttribute('src'); document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close());
  }
  function mergeRemoteChat(data) {
    const participantChat = data.participant===true && data.kind==='agent_conversation' && !data.parent_id && Boolean(data.agent_id);
    if (principalId() && data.owner_principal_id !== principalId() && !participantChat && !(data.kind==='organization_chat' && data.environment_id===session?.environment?.id && fixedPurposeKnown(data.organization_purpose))) return null;
    let chat = chats.find((item) => item.id === data.id);
    if (!chat) { chat = { id: data.id, messages: [] }; chats.push(chat); }
    if(chat.remote_running && !data.running && (data.messages || []).at(-1)?.role==='assistant')chat.unread_completion=true;
    const oldQuestions=new Set((chat.messages||[]).flatMap(m=>(m.native_questions||[]).map(q=>q.id)));if(chat.server_saved&&(data.messages||[]).some(m=>(m.native_questions||[]).some(q=>!oldQuestions.has(q.id))))chat.unread_question=true;
    const metadata = { owner_principal_id: data.owner_principal_id || principalId(), environment_id: session.environment.id, server_saved: true, pinned: Boolean(data.pinned), archived: Boolean(data.archived), metadata_revision: data.metadata_revision || 0, read_only: Boolean(data.read_only), participant: participantChat };
    if (running.has(chat.id)) Object.assign(chat, metadata);
    else Object.assign(chat, data, metadata, { messages: (data.messages || []).map((message, index) => normalizeRemoteMessage(message, index, chat.id)), remote_running: Boolean(data.running), updated: data.updated_at ? Number(data.updated_at) * 1000 : data.created_at ? Number(data.created_at) * 1000 : chat.updated || Date.now() });
    if (!running.has(chat.id) && chat.messages.at(-1)?.role === 'assistant') chat.messages.at(-1).pending = Boolean(data.running); verifiedRunning.add(chat.id); return chat;
  }
  async function loadServerChats() {
    if (!session || chatListLoading) return; chatListLoading = true; const generation = sessionGeneration;
    try { const response = await fetch('/api/chats?include_archived=true'); if (!response.ok) return; const data = await response.json(); if (generation !== sessionGeneration) return; const records = data.chats || [], ids = new Set(records.map((chat) => chat.id)); chats = chats.filter((chat) => !chat.server_saved || ids.has(chat.id) || running.has(chat.id)); records.forEach(mergeRemoteChat);restoreDrafts(true); if (activeId && !chats.some((chat) => chat.id === activeId)) activeId = null; if(!activeId&&currentView==='chat'){const requested=new URL(location.href).searchParams.get('chat'),loaded=visibleChats().find(chat=>chat.id===requested);if(loaded){activeId=loaded.id;elements.message.value=textDrafts.get(loaded.id)||'';}} save(); renderSidebar(); if (currentView !== 'agents') loadAgents(true); if (currentView === 'archives') renderArchives(); if (activeId && !running.has(activeId) && currentView === 'chat') render(); }
    catch { /* Retain this person's cached chats while the server reconnects. */ }
    finally { if (generation === sessionGeneration) chatListLoading = false; }
  }
  async function updateChatPreferences(chat, fields) {
    if (observing() || chat.read_only) return;
    try { if (chat.server_saved && session?.can_query) { const response = await request(`/api/chats/${encodeURIComponent(chat.id)}/metadata`, { expected_revision: chat.metadata_revision || 0, ...fields }); mergeRemoteChat(await response.json()); } else Object.assign(chat, fields); chat.updated = Date.now(); save(); if (fields.archived && activeId === chat.id) newChat(); else { renderSidebar(); if (currentView === 'archives') renderArchives(); } }
    catch (error) { notice(error.message); await loadServerChats(); }
  }
  async function loadObservers() { const generation=sessionGeneration;teamLoading=true;teamError='';if(['team','messaging'].includes(currentView))renderWorkspaceSettings();try{const response=await fetch('/api/observers');if(!response.ok)throw new Error();const data=await response.json();if(generation!==sessionGeneration)return;observers=Array.isArray(data.principals)?data.principals:[];renderObserver();}catch{if(generation===sessionGeneration)teamError='The team directory is reconnecting. Try again.';}finally{if(generation===sessionGeneration){teamLoading=false;if(['team','messaging'].includes(currentView))renderWorkspaceSettings();}} }
  function renderObserver() {
    renderFounder();
    const select = $('observer-select'); $('observer-control').hidden = session?.role !== 'owner' || session?.host_read_only === true; $('end-observation').hidden = session?.host_read_only === true; select.replaceChildren(); const self = node('option', '', 'Your workspace'); self.value = ''; select.append(self);
    for (const person of observers) for (const environment of person.environments || []) { if (person.id === session?.principal?.id) continue; const option = node('option', '', `${person.display_name} · ${environment.name}`); option.value = JSON.stringify([environment.id, person.id]); select.append(option); }
    select.value = observing() ? JSON.stringify([session.viewing_as.environment.id, session.viewing_as.principal.id]) : ''; $('viewing-banner').hidden = !observing(); $('viewing-label').textContent = observing() ? `Viewing ${session.viewing_as.principal.display_name} · ${session.viewing_as.environment.name} · Read only` : '';
  }
  async function changeObserver() { const value = $('observer-select').value; if (!value) { await endObservation(); return; } const [environment_id, principal_id] = JSON.parse(value); try { await request('/api/observer', { environment_id, principal_id }); currentView = 'chat'; history.replaceState({}, '', '/'); await loadSession(); render(); } catch (error) { notice(error.message); renderObserver(); } }
  async function endObservation() { if(session?.host_read_only)return;try { const response = await fetch('/api/observer', { method: 'DELETE', headers: { 'X-Rocoto-Page': '1' } }); if (!response.ok) throw new Error('Return to your workspace to continue.'); currentView = 'chat'; history.replaceState({}, '', '/'); await loadSession(); render(); } catch (error) { notice(error.message); } }
  function setProfileMenu(open) { $('profile-menu').hidden = !open; $('account-button').setAttribute('aria-expanded', String(open)); if (open) { setRailChats(false); $('profile-menu-name').textContent = session?.principal?.display_name || session?.operator?.name || session?.environment?.name || 'Account'; } }
  function showArchives() { currentView = 'archives'; setProfileMenu(false); setSidebar(false); history.replaceState({}, '', '/?settings=archives'); render(); }
  function renderArchives() { const list = $('archives-list'); list.replaceChildren(); const archived = visibleChats().filter((chat) => chat.archived); if (!archived.length) list.append(workspaceEmpty('archive','Archived chats','Archived conversations appear here.')); archived.forEach((chat) => { const item = node('article'); item.append(node('span', '', chat.title), agentButton('Restore', () => updateChatPreferences(chat, { archived: false }))); list.append(item); }); }
  function closeBusinessChat() { ++onboardingEpoch; $('business-chat-drawer').hidden = true; document.body.classList.remove('onboarding-open'); if (currentView === 'business') loadBusiness(); }
  function showMarketplace(tab = 'connections') { document.body.classList.remove('context-nav-open'); persistDraft(); clearTimeout(agentsTimer); currentView = 'marketplace'; marketplaceTab = tab; connectionSearch=''; $('marketplace-search').value=''; marketplaceDetail = null; marketplaceSelection = null; marketplaceError = ''; history.replaceState({}, '', `/?customize=1&tab=${encodeURIComponent(tab)}`); render(); setSidebar(false); if (tab === 'agents') loadAgentTemplates(); if (tab === 'skills') loadSkills(); loadConnectionCatalogue(); }
  async function loadAgentTemplates() { try { const response = await fetch('/api/ready-agents'); if (!response.ok) throw new Error('The agent catalogue is reconnecting.'); const data = await response.json(); agentTemplates = data.ready_agents || data.templates || []; marketplaceError = ''; } catch (error) { marketplaceError = error.message; } if (currentView === 'marketplace') renderMarketplace(); }
  async function loadConnectionDrafts() { const collection = businessCollection || canonicalScope(activeChat()?.dataset || '') || scopes()[0]?.id; if (!collection) return; try { const response = await fetch(`/api/business?collection=${encodeURIComponent(collection)}`); if (!response.ok) throw new Error('Source configurations are reconnecting.'); const data = await response.json(); if (currentView !== 'marketplace') return; companyConnections = data.connections || []; if (marketplaceTab === 'yours') marketplaceSelection = { collection, connections: companyConnections }; renderMarketplace(); } catch (error) { marketplaceError = error.message; renderMarketplace(); } }
  function startAgentChat(agent = null, template = null) { if (agent?.id) { closeAgentInfo(); showAgents(agent.id); return; } if (!session?.can_query) { showSession('Enter your access code to create or edit an agent.'); return; } const chat = newChat(); if (agent?.dataset) chat.dataset = agent.dataset; chat.agent_setup = { mode: agent?.mode || template?.mode || 'scheduled', ...(agent?.id ? { agent_id: agent.id } : {}), ...(template?.id ? { template_id: template.id } : {}) }; chat.title = agent ? `${agent.title} setup` : template ? `${template.name} setup` : 'New agent'; loadWorkspaceCapabilities(chat.dataset || 'company'); const text = agent ? `Help me update “${agent.title}”.` : template ? `Help me create an agent from “${template.name}”.` : 'Help me create an agent. Let’s work out what it should look for, when it should run, and what it should deliver.'; elements.message.value = text; textDrafts.set(chat.id, text); save(); resizeComposer(); render(); elements.message.focus(); }
  function agentItems(title, items) { const section = node('section', 'agent-detail-section'); section.append(node('h3', '', title)); const list = node('ul', 'agent-detail-list'); items.forEach((item) => list.append(node('li', '', item))); section.append(list); return section; }
  function marketplaceArt(id, kind, items) { const hero = node('section', `marketplace-art marketplace-art-${kind}`), hash = [...String(id)].reduce((sum, char) => sum + char.charCodeAt(0), 0); hero.style.setProperty('--art-position', `${20 + hash % 65}%`); hero.style.setProperty('--art-hue', `${hash % 35 - 17}deg`); const inner = node('div', 'marketplace-art-content'); inner.append(node('span', 'marketplace-art-caption', kind === 'agent' ? 'Agent workflow' : kind === 'connection' ? 'Example requests · coming soon' : 'Example request')); if (kind === 'agent') { const flow = node('ol', 'agent-flow'); for (const [index, text] of items.entries()) { const step = node('li'); step.append(icon(index === 0 ? 'clock' : index === items.length - 1 ? 'document' : 'search'), node('span', '', text)); flow.append(step); } inner.append(flow); } else for (const text of items) inner.append(node('p', 'marketplace-example', text)); hero.append(inner); return hero; }
  function openCatalogueDetail(kind, item) { persistDraft(); marketplaceDetail = { kind, item }; marketplaceTab = kind === 'agent' ? 'agents' : 'skills'; currentView = 'marketplace'; const url = new URL('/', location.href); url.searchParams.set('customize', '1'); url.searchParams.set('tab', marketplaceTab); url.searchParams.set('item', item.id); history.replaceState({}, '', url); render(); }
  function openAgentTemplate(template) { openCatalogueDetail('agent', template); }
  function agentTemplateContent(content, template) { content.append(node('h1', '', template.name), node('p', 'business-description', template.description), marketplaceArt(template.id, 'agent', [agentFrequency(template), workspaceExtension.templateSummary?.(template) || template.description, workspaceExtension.templateOutputLabel || 'Work report'])); const meta = node('div', 'template-meta'); meta.append(node('span', '', agentFrequency(template)), node('span', '', workspaceExtension.agentOutputLabel || 'In-app report')); content.append(meta); workspaceExtension.renderTemplateInputs?.(content,template); if (template.required_input?.length) content.append(agentItems('Before you begin', template.required_input)); const instructions = node('details', 'agent-technical'); instructions.append(node('summary', '', 'Agent instructions'), node('p', 'skill-instructions', template.instructions)); content.append(instructions, agentButton('New agent', () => startAgentChat(null, template), true)); }
  function renderCatalogueDetail(content) { const { kind, item } = marketplaceDetail; detailBreadcrumb(kind==='agent'?'Agents':'Skills',item.name,()=>showMarketplace(kind==='agent'?'agents':'skills')); content.classList.add('catalogue-detail-screen'); const back = agentButton(kind === 'agent' ? 'All agents' : 'All skills', () => { marketplaceDetail = null; const url = new URL(location.href); url.searchParams.delete('item'); history.replaceState({}, '', url); renderMarketplace(); }, false, 'chevron'); back.classList.add('return-parent'); content.append(back); if (kind === 'agent') agentTemplateContent(content, item); else { content.append(node('h1', '', item.name)); skillDetailContent(content, item, true); } }
  function renderMarketplace() {
    collectionNavigation();
    elements['chat-title'].textContent = marketplaceTab === 'agents' ? 'Agents marketplace' : marketplaceTab === 'skills' ? 'Skills' : marketplaceTab === 'yours' ? 'Your connections' : 'Connections';
    if (!marketplaceSelection && ['connections','yours'].includes(marketplaceTab)) { const id = new URL(location.href).searchParams.get('item'); if (id) marketplaceSelection = customConnections.find(item => item.id === id) || connectionCatalogue.find(item => item.id === id) || null; }
    if (!marketplaceDetail) { const id = new URL(location.href).searchParams.get('item'), kind = marketplaceTab === 'agents' ? 'agent' : marketplaceTab === 'skills' ? 'skill' : null, item = (kind === 'agent' ? agentTemplates : kind === 'skill' ? skillCatalogue : [])?.find((item) => item.id === id); if (item) marketplaceDetail = { kind, item }; }
    $('add-custom-connector').hidden = !['connections', 'yours'].includes(marketplaceTab) || Boolean(marketplaceSelection?.name); $('add-custom-connector').disabled = observing();
    $('marketplace-search-wrap').hidden = Boolean(marketplaceSelection?.name || marketplaceDetail); $('marketplace-search').placeholder = `Search ${marketplaceTab==='yours'?'connections':marketplaceTab}`; $('marketplace-search').setAttribute('aria-label',$('marketplace-search').placeholder); document.querySelector('#marketplace-view > .library-heading').hidden = Boolean(marketplaceSelection?.name || marketplaceDetail);
    $('marketplace-title').textContent = marketplaceTab === 'agents' ? 'Agents' : marketplaceTab === 'skills' ? 'Skills' : marketplaceTab === 'yours' ? 'Your connections' : marketplaceSelection?.name || 'Connections'; $('marketplace-description').textContent = marketplaceTab === 'agents' ? 'Rocoto agents for recurring work and individual investigations.' : 'Services and tools for your workspace.'; for (const [id, tab] of [['market-agents', 'agents'], ['market-skills', 'skills'], ['market-connections', 'connections']]) $(id).setAttribute('aria-pressed', String(marketplaceTab === tab)); renderCustomizeContext(); const content = $('marketplace-content'), filters = $('marketplace-filters'); content.replaceChildren(); filters.replaceChildren(); filters.hidden = marketplaceTab !== 'agents'; if (marketplaceError) content.append(node('p', 'workspace-help', marketplaceError));
    content.classList.toggle('catalogue-detail-screen', Boolean(marketplaceDetail)); if (marketplaceDetail && marketplaceDetail.kind === (marketplaceTab === 'agents' ? 'agent' : marketplaceTab === 'skills' ? 'skill' : 'connection')) { filters.hidden = true; renderCatalogueDetail(content); return; }
    if (marketplaceTab === 'skills') { renderSkillsCatalogue(content); return; }
    if (marketplaceTab === 'agents') { for (const [key, title] of [['industry', 'Industry'], ['use_case', 'Use case'], ['mode', 'Type'], ['cadence', 'Cadence']]) { const select = node('select'); select.title = title; select.setAttribute('aria-label', `Filter agents by ${title.toLowerCase()}`); const all = node('option', '', ({ industry: 'All industries', use_case: 'All use cases', mode: 'All types', cadence: 'All cadences' }[key])); all.value = ''; select.append(all); const choices = key === 'mode' ? ['ad_hoc', 'recurring'] : key === 'cadence' ? [...new Set((agentTemplates || []).map((item) => item.mode === 'ad_hoc' ? 'manual' : item.mode === 'ongoing' ? 'new_footage' : String(item.interval_seconds)))] : [...new Set((agentTemplates || []).map((item) => item[key]).filter(Boolean))]; choices.forEach((value) => { const option = node('option', '', key === 'cadence' && /^\d+$/.test(value) ? `Every ${cadenceLabel(value)}` : humanize(value)); option.value = value; select.append(option); }); select.value = skillFilters[key]; select.addEventListener('change', () => { skillFilters[key] = select.value; renderMarketplace(); }); filters.append(select); }  if (agentTemplates === null && !marketplaceError) content.append(node('p', 'workspace-help', 'Loading agents…')); const grid = node('div', 'marketplace-grid agent-catalogue-grid'); (agentTemplates || []).filter((item) => catalogueMatches(item) && (!skillFilters.industry || item.industry === skillFilters.industry) && (!skillFilters.use_case || item.use_case === skillFilters.use_case) && (!skillFilters.mode || (item.mode === 'ad_hoc' ? 'ad_hoc' : 'recurring') === skillFilters.mode) && (!skillFilters.cadence || (item.mode === 'ad_hoc' ? 'manual' : item.mode === 'ongoing' ? 'new_footage' : String(item.interval_seconds)) === skillFilters.cadence)).forEach((template) => { const card = node('button', 'marketplace-card'); card.type = 'button'; const logo = node('img'); logo.src = '/static/harness/brand/rocoto-mark.svg'; logo.alt = ''; const text = node('span'); text.append(node('strong', '', template.name), node('span', '', template.description), node('small', '', [agentFrequency(template), template.industry].filter(Boolean).join(' · '))); const dependencies = connectionStack(template.required_connections || []); if (dependencies) text.append(dependencies); card.append(logo, text); card.addEventListener('click', () => openAgentTemplate(template)); grid.append(catalogueItem(card, `Add ${template.name}`, () => startAgentChat(null, template))); }); content.append(grid); return; }
    if (marketplaceSelection?.name) { if (marketplaceSelection.custom) renderCustomConnectionDetail(content, marketplaceSelection); else renderConnectionDetail(content, marketplaceSelection); return; }
    renderConnectionsCatalogue(content);
  }

  function catalogueItem(card, label, action) { const item = node('div', 'marketplace-item'), add = node('button', 'catalogue-add'); add.type = 'button'; add.setAttribute('aria-label', label); add.append(icon('plus'), node('span', '', 'Add')); add.addEventListener('click', action); add.disabled = observing(); item.append(card, add); return item; }
  function customerConnection(item) { return !['internal', 'verification'].includes(item?.visibility || item?.presentation?.visibility); }
  function renderCustomizeContext() {
    for (const [id, tab] of [['customize-agents', 'agents'], ['customize-skills', 'skills'], ['customize-connections', 'connections']]) $(id).setAttribute('aria-current', marketplaceTab === tab ? 'page' : 'false');
    const list = $('customize-installed'); list.replaceChildren();
    const packages = nativePlugins.filter(item => item.installed && customerConnection(item)), connected = brokerConnections.filter(item => customerConnection(item) && item.status === 'connected' && (item.native_direct || !packages.some(plugin => plugin.id === item.provider)));
    const add = record => { const item = record.provider ? providerCatalogueEntry(record.provider) || record : connectionCatalogue.find(item=>item.id===record.id) || record, button = node('button', 'context-connection'); button.type = 'button'; button.setAttribute('aria-label', item.name || item.id); button.append(connectionLogo(item), node('span', '', item.name || item.id)); button.addEventListener('click', () => openConnectionDetail(item)); list.append(button); };
    packages.forEach(add);
    connected.forEach(add);
    for (const item of customConnections.filter(customerConnection)) { const button = node('button', 'context-connection'); button.type = 'button'; button.append(icon('at'), node('span', '', item.name)); button.addEventListener('click', () => openCustomConnector(item)); list.append(button); }
  }

  function connectionLogoUrl(item) { return safeUrl(item.logo_url || (item.logo ? `/static/harness/connection-brands/${item.logo}` : '')) || ''; }
  function connectionLogo(item, className = '') { const url = connectionLogoUrl(item); if (!url) { if(item.native_direct)return icon('document',className); const fallback = node('span', `connection-logo-fallback ${className}`, item.name?.[0] || '@'); return fallback; } const image = node('img', className); image.src = url; image.alt = ''; image.loading = 'lazy'; return image; }
  function brokerFor(item) { const id=item.broker_id || item.id; return brokerConnections.find(connection => (connection.provider === id || connection.id === id) && !(item.native_plugin && connection.native_direct)); }
  function providerCatalogueItem(connection) {
    const original=connectionCatalogue.find(item=>item.native_plugin && item.id===connection.provider);
    return {id:connection.native_direct?`provider:${connection.provider}`:connection.provider,broker_id:connection.provider,
      native_direct:connection.native_direct===true,name:connection.name,description:connection.description,visibility:connection.visibility,presentation:connection.presentation,
      category:connection.category || 'Connections',developer:connection.developer || (connection.provider==='openai-docs'?'OpenAI':''),
      logo_url:connection.logo_url || (connection.native_direct?original?.logo_url:undefined),source_url:connection.reference,
      prompts:[],capabilities:[],mcp_servers:[],apps:[]};
  }
  function providerCatalogueEntry(provider) { return connectionCatalogue.find(item=>item.native_direct && item.broker_id===provider) || connectionCatalogue.find(item=>item.id===provider); }
  function composerConnections() { return suggestedConnections().filter(item=>!item.native_plugin || !connectionCatalogue.some(direct=>direct.native_direct && direct.broker_id===item.id)); }
  function connectionStatus(item) { const broker=brokerFor(item);return broker?.status==='connected'?'Connected':broker?.available===false||broker?.status==='setup_required'?'Setup required':'Connect'; }
  function connectionSource(item) { if(item.native_direct)return `${item.developer || 'Provider'} · ${connectionStatus(item)==='Connect'?'Account connection':connectionStatus(item)}`;if(item.native_plugin && brokerConnections.some(connection=>connection.native_direct&&connection.provider===item.id))return `${item.developer || 'Plugin'} · Plugin`;return ''; }

  function suggestedConnections() { const priority = ['gmail', 'outlook-email', 'square', 'sumup', 'provider:google-drive', 'provider:google-docs']; return connectionCatalogue.filter(customerConnection).sort((a,b) => { const av = priority.indexOf(a.id), bv = priority.indexOf(b.id); return (av < 0 ? 100 : av) - (bv < 0 ? 100 : bv); }); }
  async function loadConnectionCatalogue() {
    if (catalogueLoading || !session) return; catalogueLoading = true; catalogueError = ''; const generation = sessionGeneration, pluginEpoch = nativePluginEpoch;
    const results = await Promise.allSettled([fetch('/api/plugins/catalogue').then(async response => { if (!response.ok) throw new Error('The connections catalogue is reconnecting.'); return response.json(); }), fetch('/api/connections').then(async response => { if (!response.ok) throw new Error('Connection status is reconnecting.'); return response.json(); }), fetch('/api/connections/custom').then(async response => { if (!response.ok) throw new Error('Custom connectors are reconnecting.'); return response.json(); }), fetch('/api/plugins/installed', {credentials:'same-origin',cache:'no-store'}).then(async response => { if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to manage plugins.' : 'Plugin status is reconnecting.'); return response.json(); })]);
    if (generation !== sessionGeneration) { catalogueLoading = false; return; }
    if (results[0].status === 'fulfilled') connectionCatalogue = (results[0].value.plugins || []).filter(customerConnection).map(item => ({ ...item, native_plugin:true, category: Array.isArray(item.category) ? item.category.join(', ') : item.category || 'Other' })); else catalogueError = results[0].reason.message;
    if (results[1].status === 'fulfilled') { brokerConnections = (results[1].value.connections || []).filter(customerConnection); connectionCatalogue=connectionCatalogue.filter(item=>item.native_plugin); for (const connection of brokerConnections) if (connection.native_direct || !connectionCatalogue.some(item => item.id === connection.provider)) connectionCatalogue.push(providerCatalogueItem(connection)); } else catalogueError ||= results[1].reason.message;
    if (results[2].status === 'fulfilled') { customConnections = (results[2].value.connections || []).filter(customerConnection).map(item => ({ ...item, custom: true })); customConnectorError = ''; } else customConnectorError = results[2].reason.message;
    if (pluginEpoch === nativePluginEpoch) {
      if (results[3].status === 'fulfilled') { const value = results[3].value; nativePlugins = (value.plugins || []).filter(item => typeof item.id === 'string' && item.installed === true && customerConnection(item)); nativePluginsAvailable = value.available === true; nativeInstallable = new Set(Array.isArray(value.installable_ids) ? value.installable_ids : []); if (Array.isArray(value.cleanup_pending_ids)) { nativePluginCleanup.clear(); value.cleanup_pending_ids.forEach(id => nativePluginCleanup.add(id)); } nativePluginError = ''; }
      else { nativePluginsAvailable = false; nativePluginError = results[3].reason.message; }
    }
    catalogueLoading = false; if(currentView==='plugins')renderPluginSettings();if (currentView === 'marketplace') renderMarketplace(); if (!$('attachment-menu').hidden) renderAttachmentConnections(); if (composerCommand?.kind === '@') renderCommandMenu();
  }
  function openConnectionDetail(item) { toggleAttachmentMenu(false); showMarketplace('connections'); marketplaceSelection = item; const url = new URL(location.href); url.searchParams.set('item', item.id); history.replaceState({}, '', url); renderMarketplace(); document.body.classList.remove('context-nav-open'); }
  function nativePluginFor(item) { return nativePlugins.find(plugin => plugin.id === item.id && plugin.installed); }
  function pluginWritable() { return Boolean(session?.authenticated && session.can_query && !session.read_only && !observing()); }
  function pluginInstallable(item) { return pluginWritable() && !nativePluginCleanup.has(item.id) && nativePluginsAvailable && nativeInstallable.has(item.id); }
  function connectionCard(item) {
    const card = node('button', 'marketplace-card'); card.type = 'button';card.dataset.connectionId=item.id; const copy = node('span'); copy.append(node('strong', '', item.name), node('span', '', item.description));const source=connectionSource(item);if(source)copy.append(node('small','connection-source-note',source)); card.append(connectionLogo(item), copy); card.addEventListener('click', () => openConnectionDetail(item));
    const installed = Boolean(nativePluginFor(item)), connected = brokerFor(item)?.status === 'connected', added = item.native_plugin ? installed : connected;
    const entry = catalogueItem(card, `${added ? 'Manage' : item.native_plugin ? 'Install' : 'Add'} ${item.name}`, () => item.native_plugin ? openNativePluginDialog(item) : openConnectionSetup(item)); entry.classList.add('connection-catalogue-item');
    const add = entry.querySelector('.catalogue-add'); add.replaceChildren(icon(added ? 'check' : 'plus')); add.title = item.native_plugin ? installed ? 'Installed' : 'Install plugin' : connected ? 'Connected' : 'Add';
    add.disabled = Boolean(observing() || session?.read_only || item.native_plugin && nativePluginOperations.has(item.id)); if (added) entry.classList.add('connection-added'); return entry;
  }
  function pluginAvailability(item) { return nativePluginError || (!nativePluginsAvailable ? 'Plugin installation is being prepared.' : !nativeInstallable.has(item.id) ? 'Installation is being prepared for this plugin.' : ''); }
  function pluginViews() { if (currentView === 'marketplace') renderMarketplace(); if (!$('attachment-menu').hidden) renderAttachmentConnections(); }
  function openNativePluginDialog(item) { connectionFlow = null; clearTimeout(connectionFlowTimer); nativePluginDialog = {item,generation:sessionGeneration,error:''}; renderNativePluginDialog(); }
  function renderNativePluginDialog() {
    const state = nativePluginDialog; if (!state || state.generation !== sessionGeneration) return;
    const item = state.item, installed = nativePluginFor(item), cleanup = nativePluginCleanup.has(item.id), operation = nativePluginOperations.get(item.id), dialog = $('connection-setup-dialog'), content = $('connection-setup-content');
    dialog.setAttribute('aria-label','Plugin installation'); content.replaceChildren(connectionLogo(item,'connection-setup-logo'),node('h2','',item.name),node('p','',item.description));
    const summary = [item.developer, item.skills?.length ? `${item.skills.length} skills` : '', installed?.version ? `Version ${installed.version}` : ''].filter(Boolean).join(' · '); if (summary) content.append(node('p','workspace-help',summary));
    if (cleanup) content.append(node('p','workspace-help','Finish removal to stop this plugin’s active work.'));
    else if (installed) { const status = node('p','plugin-installed-status'); status.append(icon('check'),node('span','','Installed')); content.append(status); }
    else content.append(node('p','workspace-help','Add this plugin’s skills and tools to your workspace.'));
    if (state.error) { const error = node('p','agent-form-error',state.error); error.setAttribute('role','alert'); content.append(error); }
    const availability = pluginAvailability(item); if (availability && (!installed || !nativePluginsAvailable)) content.append(node('p','workspace-help',availability));
    const actions = node('div','plugin-dialog-actions');
    if (installed || cleanup) { const remove = agentButton(operation ? 'Removing…' : cleanup ? 'Finish removal' : 'Remove plugin',() => changeNativePlugin(item,'remove'),false,'close'); remove.disabled = !pluginWritable() || Boolean(operation); actions.append(remove); }
    else { const install = agentButton(operation ? 'Installing…' : 'Install plugin',() => changeNativePlugin(item,'install'),true,'plus'); install.disabled = !pluginInstallable(item) || Boolean(operation); actions.append(install); }
    const broker = brokerFor(item); if (installed && broker) { const connect = agentButton(broker.status === 'connected' ? 'Manage connection' : 'Connect account',() => openConnectionSetup(item),false,'at'); connect.disabled = observing() || session?.read_only; actions.append(connect); }
    content.append(actions);
    if (installed && broker?.status !== 'connected' && (item.apps?.length || item.mcp_servers?.length)) content.append(node('p','workspace-help',broker ? 'Connect your account to use its information.' : 'Account setup is available through the plugin’s setup guide.'));
    if (!installed && availability || installed && !broker) { const url = safeUrl(item.source_url); if (url) { const source = node('a','agent-secondary','View plugin source'); source.href=url;source.target='_blank';source.rel='noopener noreferrer';source.prepend(icon('globe'));content.append(source); } }
    if (!dialog.open) dialog.showModal();
  }
  async function changeNativePlugin(item,action) {
    if (!pluginWritable() || action === 'install' && !pluginInstallable(item) || nativePluginOperations.has(item.id)) return;
    const generation = sessionGeneration, operation = {action,generation}, dialog = nativePluginDialog;
    nativePluginOperations.set(item.id,operation); ++nativePluginEpoch; ++skillsLoadEpoch; if (dialog) dialog.error=''; pluginViews(); renderNativePluginDialog();
    try {
      const response = await fetch(`/api/plugins/${encodeURIComponent(item.id)}/${action === 'install' ? 'install' : 'installation'}`,{method:action === 'install' ? 'POST' : 'DELETE',credentials:'same-origin',signal:AbortSignal.timeout(action === 'install' ? 450000 : 60000),headers:{'X-Rocoto-Page':'1',...(action === 'install' ? {'Content-Type':'application/json'} : {})},...(action === 'install' ? {body:'{}'} : {})});
      const data = await response.json(); if (generation !== sessionGeneration) return;
      if (action === 'remove' && data.removed === true && data.plugin_id === item.id) { nativePlugins = nativePlugins.filter(plugin => plugin.id !== item.id); const removed = new Set(installedSkills.filter(skill => skill.native_plugin && skill.plugin_id === item.id).map(skill => skill.id)); installedSkills = installedSkills.filter(skill => !removed.has(skill.id)); skillCatalogue = skillCatalogue?.filter(skill => !skill.native_plugin || skill.plugin_id !== item.id) ?? null; for (const chat of chats) if (chat.skill_drafts) chat.skill_drafts = chat.skill_drafts.filter(id => !removed.has(id)); save(); if (data.cleanup_confirmed === true) nativePluginCleanup.delete(item.id); else nativePluginCleanup.add(item.id); }
      if (!response.ok) { const error = new Error(action === 'remove' && nativePluginCleanup.has(item.id) ? 'Finish removal to confirm this plugin’s active work has stopped.' : response.status === 401 ? 'Sign in to manage plugins.' : response.status === 403 ? 'Open your own workspace to manage plugins.' : response.status === 503 ? 'Plugin installation is being prepared. Try again shortly.' : 'Plugin setup needs another attempt.'); error.status=response.status;throw error; }
      if (action === 'install') { if (data.id !== item.id || data.installed !== true) throw new Error('Plugin status needs a fresh check.'); nativePlugins = [...nativePlugins.filter(plugin => plugin.id !== item.id),data]; }
      else { if (data.removed !== true || data.plugin_id !== item.id || data.cleanup_confirmed !== true) throw new Error('Plugin status needs a fresh check.'); nativePlugins = nativePlugins.filter(plugin => plugin.id !== item.id); nativePluginCleanup.delete(item.id); }
    } catch (error) {
      if (generation !== sessionGeneration) return;
      if (error.status === 401 || error.status === 403) { nativePluginsAvailable=false; nativePluginError=error.message; if (error.status===401)nativePlugins=[]; }
      let confirmed = false;
      if (!error.status) {
        if (action === 'remove') nativePluginCleanup.add(item.id);
        const epoch = nativePluginEpoch;
        try { const check = await fetch('/api/plugins/installed',{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(10000)}), value = await check.json();
          if (check.ok && generation === sessionGeneration && epoch === nativePluginEpoch) { nativePlugins = (value.plugins || []).filter(plugin => typeof plugin.id === 'string' && plugin.installed === true); nativePluginsAvailable = value.available === true; nativeInstallable = new Set(Array.isArray(value.installable_ids) ? value.installable_ids : []); if (Array.isArray(value.cleanup_pending_ids)) { nativePluginCleanup.clear(); value.cleanup_pending_ids.forEach(id => nativePluginCleanup.add(id)); } confirmed = action === 'install' ? Boolean(nativePluginFor(item)) : !nativePluginFor(item) && Array.isArray(value.cleanup_pending_ids) && !nativePluginCleanup.has(item.id); }
        } catch { /* The explicit retry stays available after a failed status read. */ }
      }
      if (generation === sessionGeneration && nativePluginDialog === dialog && dialog) dialog.error=confirmed ? '' : error.status ? error.message : 'Plugin status is reconnecting. Try again to check its installation.';
    } finally {
      if (generation === sessionGeneration && nativePluginOperations.get(item.id) === operation) { nativePluginOperations.delete(item.id); ++nativePluginEpoch; pluginViews(); if (nativePluginDialog === dialog) renderNativePluginDialog(); await loadSkills(); }
    }
  }
  function renderConnectionsCatalogue(content) {
    content.classList.remove('connection-detail-screen'); if (catalogueError) content.append(node('p', 'workspace-help', catalogueError)); if (nativePluginError) content.append(node('p','workspace-help',nativePluginError)); if (catalogueLoading && !connectionCatalogue.length) content.append(node('p', 'workspace-help', 'Loading connections…'));
    const custom = customConnections.filter(item => `${item.name} ${item.summary || item.description || ''}`.toLowerCase().includes(connectionSearch.toLowerCase()));
    if (customConnectorError) content.append(node('p', 'workspace-help', customConnectorError));
    if (custom.length) { const section = node('section', 'connection-category custom-connectors'); section.append(node('h2', '', 'Custom connectors')); const grid = node('div', 'marketplace-grid'); custom.forEach(item => grid.append(customConnectorCard(item))); section.append(grid); content.append(section); }
    let items = suggestedConnections().filter(item => `${item.name} ${item.description} ${item.category}`.toLowerCase().includes(connectionSearch.toLowerCase())); if (marketplaceTab === 'yours') items = items.filter(item => nativePluginFor(item) || nativePluginCleanup.has(item.id) || brokerFor(item)?.status === 'connected');
    if (!items.length && !custom.length && !catalogueLoading) { content.append(node('p', 'workspace-help', marketplaceTab === 'yours' ? 'Your connected apps appear here.' : 'Try another connection name.')); return; }
    const categories = marketplaceTab === 'yours' || connectionSearch ? [''] : [...new Set(items.map(item => item.category))];
    for (const category of categories) { const section = node('section', 'connection-category'); if (category) section.append(node('h2', '', category)); const grid = node('div', 'marketplace-grid'); items.filter(item => !category || item.category === category).forEach(item => grid.append(connectionCard(item))); section.append(grid); content.append(section); }
  }
  function renderConnectionDetail(content, item) {
    content.classList.add('connection-detail-screen'); detailBreadcrumb('Connections',item.name,() => { marketplaceSelection=null;const url=new URL(location.href);url.searchParams.delete('item');history.replaceState({},'',url);renderMarketplace(); });
    const hero = node('header', 'connection-hero'); hero.append(connectionLogo(item, 'connection-hero-logo')); const heading = node('div', 'connection-hero-row'), copy = node('div'); copy.append(node('h1', '', item.name), node('p', '', item.description)); const actions = node('div', 'connection-hero-actions'), share = agentButton('Copy link', async () => { try { await navigator.clipboard.writeText(location.href); share.replaceChildren(icon('check'), document.createTextNode('Copied')); } catch { notice('Copy the page address to share this connection.'); } }, false, 'link'); const installed = nativePluginFor(item), cleanup = nativePluginCleanup.has(item.id), operation = nativePluginOperations.get(item.id), connect = agentButton(item.native_plugin ? operation ? operation.action === 'install' ? 'Installing…' : 'Removing…' : cleanup ? 'Finish removal' : installed ? 'Installed' : 'Install plugin' : connectionStatus(item), () => item.native_plugin ? openNativePluginDialog(item) : openConnectionSetup(item), true, (item.native_plugin ? installed : brokerFor(item)?.status === 'connected') ? 'check' : 'plus'); connect.disabled = observing() || session?.read_only || Boolean(operation) || item.native_plugin && !installed && !cleanup && !pluginInstallable(item); if (item.native_plugin && !installed) connect.title=pluginAvailability(item); actions.append(share, connect); heading.append(copy, actions); hero.append(heading); content.append(hero);if(item.native_direct)content.append(node('p','workspace-help connection-source-note',`${item.developer || 'Provider'} MCP · ${connectionStatus(item)==='Connect'?'Authorize your account':connectionStatus(item)}`));
    if (item.prompts?.length) { const art = node('section', 'marketplace-art connection-examples'), prompts = node('div', 'connection-example-list'); for (const prompt of item.prompts.slice(0, 3)) { const button = node('button', 'connection-example'); button.type = 'button'; const label = node('span'); label.append(connectionLogo(item), node('strong', '', item.name), document.createTextNode(' ' + (typeof prompt === 'string' ? prompt : prompt.prompt || prompt.text || ''))); button.append(label, icon('chevron')); button.addEventListener('click', () => { if (brokerFor(item)?.status !== 'connected') { openConnectionSetup(item); return; } const chat = activeChat(); if (chat && !chat.parent_id) chooseChat(chat); else newChat(); elements.message.value = `@${item.name} ${typeof prompt === 'string' ? prompt : prompt.prompt || prompt.text || ''}`; resizeComposer(); elements.message.focus(); }); prompts.append(button); } art.append(prompts); content.append(art); }
    if (item.long_description) { const description = node('div', 'connection-long-description'); renderText(description, item.long_description); content.append(description); }
    if (item.native_plugin && (item.apps?.length || item.mcp_servers?.length)) { const account = node('section','connection-account-state'), broker = brokerFor(item), label = node('div'); label.append(node('strong','','Account'),node('p','workspace-help',broker?.status === 'connected' ? 'Connected' : broker ? 'Authorize your account to use this connection.' : 'Account setup is being prepared.')); const setup=agentButton(broker ? broker.status === 'connected' ? 'Manage connection' : 'Connect account' : 'Setup options',() => openConnectionSetup(item),false,'at'); setup.disabled=observing() || session?.read_only;account.append(label,setup);content.append(account); }
    if (item.apps?.length) { const apps = node('section', 'connection-details-section'); apps.append(node('h2', '', `Apps ${item.apps.length}`)); for (const app of item.apps) { const row = node('div', 'connection-app-row'); const copy = node('div'); copy.append(node('strong', '', app.name === item.id ? item.name : app.name || item.name)); if (app.description) copy.append(node('p', '', app.description)); row.append(connectionLogo(item), copy); apps.append(row); } content.append(apps); }
    const related = connectionSkills(item), bundled = item.skills || []; if (related.length || bundled.length) { const skills = node('section', 'connection-details-section'); skills.append(node('h2', '', `Skills ${related.length + bundled.length}`)); for (const skill of related) { const button = agentButton(skill.name, () => openReusableSkill(skill), false, 'skill'); button.classList.add('connection-skill'); button.append(node('small', '', skill.description)); skills.append(button); } for (const skill of bundled) { const row = node('button', 'bundled-skill'); row.type='button'; row.append(icon('skill'), node('span', '', skill.name)); if(skill.description)row.append(node('small','',skill.description));row.addEventListener('click',()=>openBundledSkill(item,skill));skills.append(row); } content.append(skills); }
    const information = node('section', 'connection-details-section'); information.append(node('h2', '', 'Information')); const facts = node('dl', 'connection-information'); const capabilities = Array.isArray(item.capabilities) ? item.capabilities.join(', ') : item.capabilities; for (const [label,value] of [['Capabilities', capabilities], ['Developer', item.developer], ['Category', item.category], ['Version', item.version]]) if (value) facts.append(node('dt', '', label), node('dd', '', value)); for (const [label,value] of [['Website',item.website_url],['Privacy Policy',item.privacy_policy_url],['Terms of Service',item.terms_url],['Source',item.source_url]]) { const url = safeUrl(value); if (!url) continue; const link = node('a'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.setAttribute('aria-label',label); link.append(icon('globe')); const valueNode = node('dd'); valueNode.append(link); facts.append(node('dt','',label),valueNode); } information.append(facts); content.append(information);
  }
  function customConnectorWritable(item) { return pluginWritable() && !(item?.organization_shared === true && item.can_configure !== true); }
  const customConnectorKinds = { sql: 'SQL database', api: 'Private API', sftp: 'SFTP', cli: 'CLI tool', mcp: 'MCP server', local: 'Local files', other: 'Other' };
  function clearCustomCredentialInputs() { document.querySelectorAll('.custom-secret-field input').forEach(input => { input.value = ''; }); }
  function customConnectorStatus(item) { const status=workspaceExtension.connectorStatus?.(item); if(status)return status; return ({active:'Connected',needs_verification:'Verification required',revoked:'Access removed'})[item.active_status] || (['draft', 'prepared'].includes(item.status || 'draft') ? 'Setup in progress' : humanize(item.status)); }
  function customConnectorCard(item) {
    const entry = node('div', 'marketplace-item connection-catalogue-item custom-connector-card'), card = node('button', 'marketplace-card'), copy = node('span'); card.type = 'button';
    copy.append(node('strong', '', item.name), node('span', '', item.summary || item.description || 'Continue preparing this connector.'), node('small', 'custom-connector-status', `${customConnectorKinds[item.kind] || 'Custom connector'} · ${customConnectorStatus(item)}`));
    card.append(icon(item.kind === 'sftp' ? 'folder' : 'at'), copy, icon('chevron')); card.addEventListener('click', () => openCustomConnector(item)); entry.append(card); return entry;
  }
  function openCustomConnectorDialog(opener, { collection = null, ...options } = {}) {
    if (observing() || session?.read_only || !session?.can_query) return;
    customConnectorOpener = opener || document.activeElement; toggleAttachmentMenu(false); closeCommandMenu(); $('custom-connector-form').reset(); $('custom-connector-error').hidden = true; $('create-custom-connector').disabled = false;
    customConnectorScope={collection:collection || activeChat()?.dataset || 'company',generation:sessionGeneration};
    const fields=workspaceExtension.connectorDialog?.(options)||{};
    $('custom-connector-title').textContent=fields.title||'Add Custom Connector';
    $('custom-connector-intro').textContent=fields.introduction||'Describe your system and what you want to do. Rocoto will prepare the connector with you in a setup chat.';
    $('custom-connector-kind').value=fields.kind||'api';
    $('custom-connector-name').placeholder=fields.namePlaceholder||'Warehouse ERP';
    $('custom-connector-description').placeholder=fields.descriptionPlaceholder||workspaceExtension.connectorPlaceholder||'Read records from our business system and help with the next step.';
    $('custom-connector-dialog').showModal(); $('custom-connector-description').focus();
  }
  async function createCustomConnector(event) {
    event.preventDefault(); if (customConnectorCreating || observing() || session?.read_only || !session?.can_query || customConnectorScope?.generation !== sessionGeneration || !$('custom-connector-form').reportValidity()) return;
    const description = $('custom-connector-description').value.trim(); if (!description) { $('custom-connector-description').focus(); return; }
    const payload = { name: $('custom-connector-name').value.trim(), description, kind: $('custom-connector-kind').value, collection: customConnectorScope.collection }, generation = sessionGeneration;
    customConnectorCreating = true; $('create-custom-connector').disabled = true; $('custom-connector-error').hidden = true;
    try {
      const response = await request('/api/connections/custom', payload), item = await response.json(); if (generation !== sessionGeneration) return;
      if (!item.id || !item.chat_id) throw new Error('The connector setup is reconnecting. Try again.');
      customConnections.push({ ...item, custom: true }); $('custom-connector-dialog').close(); await loadServerChats(); if (generation === sessionGeneration) await pollChild(item.chat_id, true);
    } catch (error) { if (generation === sessionGeneration) { $('custom-connector-error').textContent = error.message; $('custom-connector-error').hidden = false; } }
    finally { customConnectorCreating = false; $('create-custom-connector').disabled = false; }
  }
  function openCustomConnector(item) {
    workspaceExtension.resetConnectorDetails?.(); toggleAttachmentMenu(false); ++customDetailGeneration; customConnectorLoading = false; showMarketplace('connections'); marketplaceSelection = { ...item, custom: true, details_loaded: false }; renderMarketplace();
    const url = new URL(location.href); url.searchParams.set('item', item.id); history.replaceState({}, '', url); document.body.classList.remove('context-nav-open');
  }
  async function loadCustomConnector(item) {
    const generation = ++customDetailGeneration, sessionId = sessionGeneration; customConnectorLoading = true;
    try {
      const response = await fetch(`/api/connections/custom/${encodeURIComponent(item.id)}`); if (!response.ok) throw new Error('Connector setup is reconnecting. Open it again to retry.');
      const detail = await response.json(); if (generation !== customDetailGeneration || sessionId !== sessionGeneration || marketplaceSelection?.id !== item.id) return;
      marketplaceSelection = { ...item, ...detail, custom: true, details_loaded: true }; const index = customConnections.findIndex(connection => connection.id === item.id); if (index >= 0) customConnections[index] = marketplaceSelection;
    } catch (error) { if (generation === customDetailGeneration && sessionId === sessionGeneration && marketplaceSelection?.id === item.id) marketplaceSelection = { ...item, details_loaded: true, detail_error: error.message }; }
    finally { if (generation === customDetailGeneration) { customConnectorLoading = false; if (sessionId === sessionGeneration && currentView === 'marketplace' && marketplaceSelection?.id === item.id) renderMarketplace(); } }
  }
  async function continueCustomConnector(item) {
    if (observing() || !item.chat_id) return; await loadServerChats(); await pollChild(item.chat_id, true);
  }
  async function customExecutionAction(item, action, payload, button) {
    if (!customConnectorWritable(item)) return;
    const generation=sessionGeneration, detail=customDetailGeneration, selection=marketplaceSelection, view=currentView;
    const targetContent=action==='target'?$('connection-setup-content').firstElementChild:null;
    const current=()=>generation===sessionGeneration&&detail===customDetailGeneration&&selection===marketplaceSelection&&view===currentView;
    button.disabled=true;
    if(action==='test'){customExecutionPending.set(item.id,generation);renderMarketplace();}
    try {
      await request(`/api/connections/custom/${encodeURIComponent(item.id)}/execution/${action}`,payload);
      if(!current())return;
      if(action==='target'&&$('connection-setup-content').firstElementChild===targetContent)$('connection-setup-dialog').close();
      await loadCustomConnector(item);
    } catch(error) { if(current())notice(error.message); }
    finally { if(generation===sessionGeneration){if(action==='test')customExecutionPending.delete(item.id);button.disabled=!customConnectorWritable(item);if(current())refreshCustomExecution(item);} }
  }
  async function refreshCustomExecution(item) {
    clearTimeout(customExecutionTimer);
    if(currentView!=='marketplace'||marketplaceSelection?.id!==item.id)return;
    const generation=sessionGeneration,detail=customDetailGeneration;
    try {
      const response=await fetch(`/api/connections/custom/${encodeURIComponent(item.id)}/execution`);
      if(!response.ok)return;
      const setup=await response.json();
      if(generation!==sessionGeneration||detail!==customDetailGeneration||currentView!=='marketplace'||marketplaceSelection?.id!==item.id)return;
      marketplaceSelection.execution_setup=setup;
      const section=document.querySelector('.custom-execution'),replacement=document.createDocumentFragment();
      if(section){renderCustomExecution(replacement,marketplaceSelection);section.replaceWith(replacement);}
    } catch { /* The saved setup stays available while status reconnects. */ }
    finally {if(generation===sessionGeneration&&detail===customDetailGeneration&&currentView==='marketplace'&&marketplaceSelection?.id===item.id)customExecutionTimer=setTimeout(()=>refreshCustomExecution(item),1500);}
  }
  function reviewCustomTarget(item, opener) {
    if(!customConnectorWritable(item) || !item.execution_setup?.proposal?.current)return;
    const proposal=item.execution_setup.proposal, target=proposal.target, dialog=$('connection-setup-dialog'), content=$('connection-setup-content');
    nativePluginDialog=null; connectionFlow=null; clearTimeout(connectionFlowTimer);
    dialog.setAttribute('aria-label','Review connection access');content.replaceChildren();
    const mark=node('span','custom-connector-mark');mark.append(icon('at'));content.append(mark,node('h2','','Review connection access'),node('p','',proposal.reason));
    const facts=node('dl','connection-information');facts.append(node('dt','','Service'),node('dd','',target.origin),node('dt','','Access'),node('dd','','Read'));
    const names=(item.secret_fields || []).filter(field=>[target.authentication.credential,target.authentication.username_field,target.authentication.password_field].includes(field.name)).map(field=>field.label || humanize(field.name));
    facts.append(node('dt','','Credentials'),node('dd','',names.length?names.join(', '):'Public API'));content.append(facts);
    const operations=node('div','custom-access-operations');for(const operation of target.operations){const row=node('div');row.append(icon('globe'),node('code','',`${operation.method} ${operation.path}`));operations.append(row);}content.append(operations,node('p','workspace-help','Approve this destination, then run the connector checks.'));
    const actions=node('div','custom-access-actions'), cancel=agentButton('Cancel',()=>dialog.close(),false,'close'), approve=agentButton('Approve access',()=>customExecutionAction(item,'target',{proposal_id:proposal.id},approve),true,'check');actions.append(cancel,approve);content.append(actions);
    dialog.addEventListener('close',()=>opener?.isConnected&&opener.focus(),{once:true});dialog.showModal();
  }
  function renderCustomExecution(content,item) {
    const setup=item.execution_setup;if(!setup?.tools?.length)return;
    const section=node('section','connection-details-section custom-execution');section.append(node('h2','','Connection access'));
    for(const tool of setup.tools){const row=node('div','custom-read-tool');row.append(icon('search'),node('strong','',humanize(tool.name)),node('p','workspace-help',tool.description));section.append(row);}
    const proposal=setup.proposal, last=setup.last_test, actions=node('div','custom-access-actions');
    const working=customExecutionPending.get(item.id)===sessionGeneration||last?.status==='pending'||['running','cleanup_uncertain'].includes(setup.execution_status);
    if(proposal?.current){const review=agentButton('Review access',()=>reviewCustomTarget(item,review),true,'lock');review.disabled=!customConnectorWritable(item);actions.append(review);}
    if(setup.target) {
      section.append(node('p','workspace-help',`${setup.target.origin} · Read access`));
      const check=agentButton('Run checks',()=>customExecutionAction(item,'test',{expected_revision:item.revision},check),false,'play');
      check.disabled=!customConnectorWritable(item)||!setup.runtime_available||working;actions.append(check);
      if(working){const stop=agentButton('Stop checks',()=>customExecutionAction(item,'stop',{},stop),false,'stop');stop.disabled=!customConnectorWritable(item);actions.append(stop);}
      if(!setup.runtime_available)section.append(node('p','workspace-help','Prepare the connector here. Runtime setup is being completed.'));
    }
    if(last){section.append(node('p','workspace-help',({pending:'Checking the connection…',passed:'Connection checks passed.',failed:'Review the setup chat to repair the connection.',stopped:'Connection checks stopped.',cleanup_uncertain:'Finishing the earlier connection check.',invalidated:'Run checks for the updated connection.',expired:'Run the connection checks again.'})[last.status]||'Review the connection check.'));
      if(last.status==='passed'&&last.revision===item.revision&&setup.active_revision!==item.revision){const activate=agentButton('Activate connection',()=>customExecutionAction(item,'activate',{test_id:last.id,expected_active_generation:setup.active_generation},activate),true,'check');activate.disabled=!customConnectorWritable(item)||!setup.runtime_available;actions.append(activate);}}
    if(setup.active_status==='active'){const revoke=agentButton('Remove access',()=>customExecutionAction(item,'revoke',{},revoke),false,'close');revoke.disabled=!customConnectorWritable(item);actions.append(revoke);}
    section.append(actions);content.append(section);
    clearTimeout(customExecutionTimer);customExecutionTimer=setTimeout(()=>refreshCustomExecution(item),1500);
  }
  function customSecretField(item, field) {
    const form = node('form', 'custom-secret-field'), label = node('label'), labelText = field.label || humanize(field.name), input = node('input'), state = node('span', 'custom-secret-state'), actions = node('div', 'custom-secret-actions'), message = node('p', 'workspace-help');
    input.id = `custom-secret-${field.name}`; input.type = 'password'; input.autocomplete = 'new-password'; input.spellcheck = false; input.maxLength = 16384; input.required = true; input.disabled = !customConnectorWritable(item); label.htmlFor = input.id; label.append(node('strong', '', labelText));
    const save = agentButton(`Save ${labelText}`, null, false, 'check'), remove = agentButton(`Remove ${labelText}`, null, false, 'close'); save.type = 'submit'; save.disabled = remove.disabled = !customConnectorWritable(item); save.setAttribute('aria-label', `Save ${labelText}`); remove.setAttribute('aria-label', `Remove ${labelText}`); save.replaceChildren(icon('check'), document.createTextNode('Save')); remove.replaceChildren(icon('close'), document.createTextNode('Remove'));
    const update = () => { state.textContent = field.configured ? 'Configured' : 'Awaiting credential'; remove.hidden = !field.configured; input.placeholder = field.configured ? 'Enter a replacement credential' : 'Enter credential'; }; update();
    message.setAttribute('role', 'status'); form.append(label, state); if (field.description) form.append(node('p', 'workspace-help', field.description)); actions.append(input, save, remove); form.append(actions, message);
    async function submitCredential(revoke) {
      if (!customConnectorWritable(item) || !revoke && !form.reportValidity()) return; const value = input.value, generation = sessionGeneration; input.value = ''; save.disabled = remove.disabled = true; message.textContent = '';
      try {
        const base = `/api/connections/custom/${encodeURIComponent(item.id)}/secrets`;
        const response = revoke ? await fetch(`${base}/${encodeURIComponent(field.name)}`, { method: 'DELETE', headers: { 'X-Rocoto-Page': '1' } }) : await request(base, { name: field.name, value });
        if (!response.ok) throw new Error(); if (generation !== sessionGeneration) return; const metadata = await response.json(); field.configured = metadata.configured === true; update(); message.textContent = revoke ? 'Credential removed.' : 'Credential saved privately.';
      } catch { if (generation === sessionGeneration) message.textContent = revoke ? 'Credential removal needs another attempt.' : 'Credential setup needs another attempt.'; }
      finally { input.value = ''; save.disabled = remove.disabled = !customConnectorWritable(item); }
    }
    form.addEventListener('submit', event => { event.preventDefault(); submitCredential(false); }); remove.addEventListener('click', () => submitCredential(true)); return form;
  }
  function renderCustomConnectionDetail(content, item) {
    content.classList.add('connection-detail-screen'); detailBreadcrumb('Connections',item.name,() => { marketplaceSelection=null;const url=new URL(location.href);url.searchParams.delete('item');history.replaceState({},'',url);renderMarketplace(); });
    const hero = node('header', 'connection-hero'), mark = node('span', 'custom-connector-mark connection-hero-logo'); mark.append(icon(item.kind === 'sftp' ? 'folder' : 'at')); hero.append(mark); const row = node('div', 'connection-hero-row'), heading = node('div'), actions = node('div', 'connection-hero-actions'); heading.append(node('h1', '', item.name), node('p', '', item.summary || item.description || 'Prepare your connection in its setup chat.'));
    const resume = agentButton('Continue setup', () => continueCustomConnector(item), true, 'chat'); resume.disabled = observing() || !item.chat_id; actions.append(resume); row.append(heading, actions); hero.append(row); content.append(hero);
    const facts = node('dl', 'connection-information'); facts.append(node('dt', '', 'Type'), node('dd', '', customConnectorKinds[item.kind] || 'Custom connector')); if (item.kind !== 'sftp' || !item.storage_setup) facts.append(node('dt', '', 'Status'), node('dd', '', customConnectorStatus(item))); content.append(facts);
    if (!item.details_loaded) { content.append(node('p', 'workspace-help', 'Loading setup…')); if (!customConnectorLoading) loadCustomConnector(item); return; }
    if (item.detail_error) { content.append(node('p', 'workspace-help', item.detail_error), agentButton('Retry', () => { item.details_loaded = false; renderMarketplace(); }, false, 'refresh')); return; }
    renderCustomExecution(content,item); workspaceExtension.renderConnectorDetails?.(content,item);
    if (item.instructions) { const setup = node('section', 'connection-details-section'); setup.append(node('h2', '', 'Setup')); const instructions = node('div'); renderText(instructions, item.instructions); setup.append(instructions); content.append(setup); }
    const fields = (item.secret_fields || []).filter(field => /^[A-Z][A-Z0-9_]{0,63}$/.test(field.name));
    if (fields.length) { const credentials = node('section', 'connection-details-section custom-credentials'); credentials.append(node('h2', '', 'Credentials'), node('p', 'workspace-help', 'Credentials are saved privately for this connector.')); fields.forEach(field => credentials.append(customSecretField(item, field))); content.append(credentials); }
    else content.append(node('p', 'workspace-help custom-credential-note', 'The setup chat will identify the access and credentials this connection needs.'));
    const url = safeUrl(item.package_url); if (url && new URL(url).origin === location.origin) { const download = node('a', 'agent-secondary custom-connector-download', 'Download connector'); download.href = url; download.prepend(icon('download')); content.append(download); }
    if (item.files?.length) { const files = node('details', 'custom-connector-files'), summary = node('summary'); summary.append(icon('document'), node('span', '', `${item.files.length} connector ${item.files.length === 1 ? 'file' : 'files'}`), icon('chevron')); files.append(summary); for (const file of item.files) { const row = node('div'); row.append(node('span', '', file.path), node('small', '', fileSize(file.size_bytes))); files.append(row); } content.append(files); }
  }
  function openBundledSkill(plugin, skill) { const installed = installedSkills.find(item => item.native_plugin && item.plugin_id === plugin.id && item.name === skill.name); if (installed) { openReusableSkill(installed); return; } $('skill-title').textContent=skill.name;document.querySelector('#skill-dialog .dialog-header p').textContent=plugin.developer || plugin.name;const content=$('skill-content');content.replaceChildren(node('p','business-description',skill.description || ''),node('h3','','Skill summary'));const summary=node('div','skill-instructions');renderText(summary,skill.summary || skill.description || '');content.append(summary);const url=safeUrl(skill.source_url);if(url){const source=node('a','agent-secondary','View skill instructions');source.href=url;source.target='_blank';source.rel='noopener noreferrer';source.prepend(icon('globe'));content.append(source);}$('skill-dialog').showModal(); }
  function clearConnectionCredential() {
    const input=$('connection-token');if(!input)return;input.value='';input.type='password';const reveal=$('connection-token-reveal');if(reveal){reveal.setAttribute('aria-label','Show access token');reveal.setAttribute('aria-pressed','false');reveal.replaceChildren(icon('eye'));}input.dispatchEvent(new Event('input'));
  }
  function openConnectionSetup(item) {
    clearConnectionCredential(); nativePluginDialog = null; $('connection-setup-dialog').setAttribute('aria-label','Connection setup');
    const dialog = $('connection-setup-dialog'), content = $('connection-setup-content'), broker = brokerFor(item); content.replaceChildren(connectionLogo(item, 'connection-setup-logo'), node('h2', '', item.name), node('p', '', item.description)); connectionFlow = null; clearTimeout(connectionFlowTimer);
    if (broker) {
      const bearer=broker.authentication==='bearer',setupAnchor=content.firstElementChild,connected = broker.status === 'connected', generation = sessionGeneration,current=()=>generation===sessionGeneration&&dialog.open&&content.firstElementChild===setupAnchor, currentMode = broker.access_mode === 'read_write' ? 'read_write' : 'read';
      content.append(node('p','workspace-help', connected ? `Connected · ${currentMode === 'read_write' ? 'read and write access' : 'read access'}` : broker.setup_reason==='registered_client_required' ? 'Your workspace owner can enable Google account sign-in.' : broker.available === false ? 'Account setup is being prepared.' : bearer ? 'Use your access token to connect this account.' : broker.authentication === 'oauth' ? 'Authorize your account to use this connection in Rocoto.' : 'Add this source to your workspace.'));
      const modes = ['read','read_write'].filter(mode => (broker.access_modes || ['read']).includes(mode)), select = node('select');
      select.id = 'connection-access-mode'; select.setAttribute('aria-label','Account access'); select.disabled = !pluginWritable() || broker.available === false;
      for (const mode of modes) { const option = node('option','',mode === 'read' ? 'Read access' : broker.write_available === true ? 'Read and write access' : 'Read and write · setup required'); option.value = mode; option.disabled = mode === 'read_write' && broker.write_available !== true; select.append(option); }
      select.value = currentMode;
      if (modes.length > 1) { const field = node('label','connection-access-field','Account access'); field.htmlFor = select.id; field.append(select); content.append(field); }
      else content.append(node('p','connection-scope-note','Read access'));
      const scope = node('p','workspace-help connection-scope-note'), updateScope = () => { scope.textContent = select.value === 'read_write' ? 'Changes require your approval in chat.' : 'Read the content you authorize.'; };
      updateScope(); content.append(scope);
      let tokenInput=null,reveal=null,busy=false;const form=bearer?node('form','connection-token-form'):null;
      const allowed=()=>pluginWritable()&&broker.available!==false,validToken=()=>!bearer||/^[A-Za-z0-9._~+/=-]{1,16384}$/.test(tokenInput.value);
      if(bearer){
        form.noValidate=true;const label=node('label','credential-input-label','Access token'),row=node('div','credential-input-row');tokenInput=node('input');tokenInput.id='connection-token';tokenInput.type='password';tokenInput.autocomplete='new-password';tokenInput.spellcheck=false;tokenInput.maxLength=16384;tokenInput.required=true;tokenInput.disabled=!allowed();tokenInput.setAttribute('aria-describedby','connection-token-security');label.htmlFor=tokenInput.id;
        reveal=node('button','icon-button credential-reveal');reveal.id='connection-token-reveal';reveal.type='button';reveal.disabled=!allowed();reveal.setAttribute('aria-label','Show access token');reveal.setAttribute('aria-pressed','false');reveal.append(icon('eye'));reveal.addEventListener('click',()=>{const show=tokenInput.type==='password';tokenInput.type=show?'text':'password';reveal.setAttribute('aria-label',show?'Hide access token':'Show access token');reveal.setAttribute('aria-pressed',String(show));reveal.replaceChildren(icon(show?'eye-off':'eye'));});row.append(tokenInput,reveal);
        const security=node('p','credential-security');security.id='connection-token-security';security.append(icon('lock'),node('span','','Sent securely to this connection’s credential store.'));form.append(label,row,security);content.append(form);
      }
      const submitConnection=async event=>{
        event?.preventDefault();if(!current()||busy||!allowed()||!validToken()||connect.disabled)return;
        busy=true;connect.disabled=true;select.disabled=true;content.querySelectorAll('.agent-form-error').forEach(el=>el.remove());
        const payload={access_mode:select.value||'read'};if(bearer){payload.token=tokenInput.value;clearConnectionCredential();tokenInput.disabled=true;reveal.disabled=true;}
        try {
          const pending=bearer?fetch(`/api/connections/${encodeURIComponent(broker.provider)}/connect`,{method:'POST',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify(payload)}):request(`/api/connections/${encodeURIComponent(broker.provider)}/connect`,payload);if(bearer)payload.token='';
          const response=await pending;if(!current())return;
          if(!response.ok){if(response.status===401)showSession('Open your workspace to continue.');throw new Error();}
          const flow=await response.json();if(!current())return;connectionFlow={...flow,item,generation,anchor:setupAnchor};renderConnectionFlow();
        } catch(error) { if(!current())return;busy=false;content.append(node('p','agent-form-error',bearer?'Connection setup needs another attempt. Enter your access token to retry.':error.message));select.disabled=!allowed();if(bearer){tokenInput.disabled=!allowed();reveal.disabled=!allowed();}syncAccess(); }
      };
      const connect=agentButton(connected?'Update access':'Connect',bearer?null:submitConnection,true,connected?'refresh':'plus');
      const syncAccess=()=>{connect.disabled=busy||!allowed()||!validToken()||connected&&!bearer&&select.value===currentMode||select.value==='read_write'&&broker.write_available!==true;updateScope();};
      select.addEventListener('change',syncAccess);if(bearer){tokenInput.addEventListener('input',syncAccess);connect.type='submit';form.addEventListener('submit',submitConnection);form.addEventListener('connection-flow-ended',()=>{if(!current())return;busy=false;clearConnectionCredential();select.disabled=!allowed();tokenInput.disabled=!allowed();reveal.disabled=!allowed();syncAccess();});form.append(connect);}else content.append(connect);syncAccess();
      if (connected) { const remove = agentButton('Remove connection',async()=>{ remove.disabled = true; try { const response = await fetch(`/api/connections/${encodeURIComponent(broker.provider)}`,{method:'DELETE',headers:{'X-Rocoto-Page':'1'}}); if(!response.ok)throw new Error('Connection removal is reconnecting. Try again.'); if(!current())return; await loadConnectionCatalogue(); if(current())dialog.close(); } catch(error) { if(!current())return; content.append(node('p','agent-form-error',error.message)); remove.disabled = !pluginWritable(); } },false,'close'); remove.disabled = !pluginWritable(); content.append(remove); }
    }
    else { content.append(node('p','workspace-help','Rocoto setup is being prepared for this connection.')); const url=safeUrl(item.source_url);if(url){const link=node('a','agent-secondary','View connection source');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.prepend(icon('globe'));content.append(link);} }
    const guides = (item.connection_setup?.instructions || []).filter(guide => /\/(README|SETUP|INSTALL|AUTHENTICATION)\.md$/i.test(guide.path || ''));
    guides.sort((a, b) => a.path.split('/').length - b.path.split('/').length);
    if (guides.length) { const url = safeUrl(guides[0].source_url); if (url) { const link = node('a', 'agent-secondary', 'Connection setup guide'); link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.prepend(icon('document')); content.append(link); } }
    if (!dialog.open) dialog.showModal();
  }
  function renderConnectionFlow() { const flow=connectionFlow;if(!flow||flow.generation!==sessionGeneration||flow.anchor!==$('connection-setup-content').firstElementChild)return;const content=$('connection-setup-content');content.querySelectorAll('.connection-flow-status,.connection-authorize').forEach(el=>el.remove()); const status=node('p','connection-flow-status workspace-help',({connected:'Connected',connecting:'Connecting…',awaiting_authorization:'Ready to authorize your account.',completing:'Completing connection…',failed:'Connection needs another attempt.',interrupted:'Start connection setup again.',expired:'Start connection setup again.'}[flow.status]||humanize(flow.status)));content.append(status);if(flow.authorization_url){const url=safeUrl(flow.authorization_url);if(url&&new URL(url).protocol==='https:'){const link=node('a','agent-primary connection-authorize','Authorize account');link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.prepend(icon('globe'));content.append(link);}}
    if(flow.status==='connected'){loadConnectionCatalogue();return;}if(['failed','expired','interrupted','declined','disconnected','reconnect_required'].includes(flow.status)){content.querySelector('.connection-token-form')?.dispatchEvent(new Event('connection-flow-ended'));return;}clearTimeout(connectionFlowTimer);connectionFlowTimer=setTimeout(async()=>{if(connectionFlow!==flow||flow.generation!==sessionGeneration||flow.anchor!==$('connection-setup-content').firstElementChild||!$('connection-setup-dialog').open)return;try{const response=await fetch(`/api/connections/flows/${encodeURIComponent(flow.flow_id)}`);if(!response.ok)throw new Error();const data=await response.json();if(connectionFlow!==flow||flow.generation!==sessionGeneration||flow.anchor!==$('connection-setup-content').firstElementChild||!$('connection-setup-dialog').open)return;connectionFlow={...flow,...data};renderConnectionFlow();}catch{status.textContent='Connection status is reconnecting. Open setup again to retry.';}},1500);
  }
  function renderAttachmentConnections() { const list=$('attachment-connections');list.replaceChildren();list.append(node('div','attachment-category','Connections'));const connected=composerConnections().filter(item=>brokerFor(item)?.status==='connected'||nativePluginFor(item)), entries=[...connected,...composerConnections().filter(item=>!connected.includes(item))].slice(0,6);for(const item of entries){const button=node('button');button.type='button';button.append(connectionLogo(item),node('span','',item.name));button.addEventListener('click',()=>{toggleAttachmentMenu(false);if(brokerFor(item)?.status==='connected'){const value=elements.message.value;elements.message.value=value+(value&&!/\s$/.test(value)?' ':'')+'@'+item.name+' ';resizeComposer();elements.message.focus();}else openConnectionSetup(item);});list.append(button);}const browse=node('button');browse.type='button';browse.append(icon('grid'),node('span','','Browse connections'));browse.addEventListener('click',()=>{toggleAttachmentMenu(false);showMarketplace('connections');});list.append(browse); const custom=node('button');custom.type='button';custom.disabled=observing();custom.append(icon('plus'),node('span','','Add Custom Connector'));custom.addEventListener('click',()=>openCustomConnectorDialog($('attachment-toggle')));list.append(custom); }

  function connectionStack(dependencies) {
    if (!dependencies.length) return null; const stack = node('span', 'connection-stack');
    for (const dependency of dependencies.slice(0, 3)) { const provider = typeof dependency === 'string' ? dependency : dependency.provider, brand = connectionCatalogue.find((item) => item.id === String(provider).toLowerCase() || item.name.toLowerCase() === String(provider).toLowerCase()); if (brand) { const logo = node('img'); logo.src = connectionLogoUrl(brand); logo.alt = brand.name; logo.title = `${brand.name} connection required`; stack.append(logo); } else { const label = node('span', 'connection-initial', String(provider || '?').slice(0, 1).toUpperCase()); label.title = `${provider} connection required`; stack.append(label); } }
    if (dependencies.length > 3) stack.append(node('span', '', `+${dependencies.length - 3}`)); return stack;
  }
  async function loadSkills() {
    const generation = sessionGeneration, epoch = ++skillsLoadEpoch; try { const responses = await Promise.all([fetch('/api/skills/catalogue'), fetch('/api/skills')]); if (!responses.every((response) => response.ok)) throw new Error('Skills are reconnecting.'); const [catalogue, installed] = await Promise.all(responses.map((response) => response.json())); if (generation !== sessionGeneration || epoch !== skillsLoadEpoch) return; skillCatalogue = catalogue.skills || []; installedSkills = installed.skills || []; skillsError = '';
      const available = new Set(installedSkills.map(skill => skill.id)); for (const chat of chats) if (chat.skill_drafts) chat.skill_drafts = chat.skill_drafts.filter(id => !id.startsWith('native-skill:') || available.has(id)); save(); }
    catch (error) { if (generation !== sessionGeneration || epoch !== skillsLoadEpoch) return; skillsError = error.message; }
    if (currentView === 'marketplace') renderMarketplace(); if (composerCommand) renderCommandMenu(); renderSelectedSkills(); if (!$('skills-submenu').hidden) renderSkillsSubmenu();
  }
  function renderSkillsCatalogue(content) {
     if (skillsError) content.append(node('p', 'workspace-help', skillsError)); if (skillCatalogue === null && !skillsError) content.append(node('p', 'workspace-help', 'Loading skills…'));
    const grid = node('div', 'marketplace-grid'); for (const skill of (skillCatalogue || []).filter(catalogueMatches)) { const card = node('button', 'marketplace-card'); card.type = 'button'; const logo = node('img'); logo.src = '/static/harness/brand/rocoto-mark.svg'; logo.alt = ''; const text = node('span'); text.append(node('strong', '', skill.name), node('span', '', skill.description), node('small', '', `${skill.publisher || 'Rocoto'} · ${skill.installed ? skill.installed_revision !== skill.latest_revision ? 'Update available' : 'Installed' : 'Available'}`)); const dependencies = connectionStack(skill.required_connections || []); if (dependencies) text.append(dependencies); card.append(logo, text); card.addEventListener('click', () => openCatalogueDetail('skill', skill)); grid.append(catalogueItem(card, `${skill.installed ? 'Use' : 'Add'} ${skill.name}`, () => openReusableSkill(skill))); } content.append(grid, agentButton('Add New Skill', createSkillChat));
  }
  function openReusableSkill(skill) { document.querySelector('#skill-dialog .dialog-header p').textContent=skill.publisher || 'Rocoto'; $('skill-title').textContent = skill.name; const content = $('skill-content'); content.replaceChildren(); skillDetailContent(content, skill, false); $('skill-dialog').showModal(); }
  function skillDetailContent(content, skill, screen) {
    if (skill.native_plugin) {
      content.append(node('p', 'business-description', skill.description), marketplaceArt(skill.id, 'skill', [`/${skillCommand(skill)}`]));
      content.append(node('p', 'workspace-help', `${skill.publisher} · Version ${skill.package_version}`));
      const source = safeUrl(skill.source_url); if (source) { const link = node('a', 'agent-secondary', 'View skill instructions'); link.href = source; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.prepend(icon('document')); content.append(link); }
      const action = agentButton(`Use /${skillCommand(skill)}`, () => { if (!screen) $('skill-dialog').close(); const chat = activeChat(); if (chat && !chat.parent_id) chooseChat(chat); else newChat(); selectSkill(skill.id); }, true, 'skill');
      action.disabled = !session?.can_query || observing() || !installedSkills.some(item => item.id === skill.id); content.append(action); return;
    }
    content.append(node('p', 'business-description', skill.description), marketplaceArt(skill.id, 'skill', [`Use /${skillCommand(skill)} for this investigation.`]));
    if (skill.required_connections?.length) { content.append(node('h3', '', 'Required connections')); for (const connection of skill.required_connections) content.append(node('p', 'workspace-help', `${connection.provider} · ${(connection.scopes || []).join(', ')}`)); }
    const instructions = node('details', 'agent-technical'); instructions.append(node('summary', '', 'Skill instructions'), node('p', 'skill-instructions', skill.instructions)); if (skill.required_tools?.length) instructions.append(node('h3', '', 'Tools'), node('p', 'workspace-help', skill.required_tools.join(', '))); instructions.append(node('p', 'workspace-help', `${skill.publisher || 'Rocoto'} · Version ${skill.revision}`)); content.append(instructions);
    const installed = skill.installed && skill.installed_revision === skill.latest_revision, action = agentButton(installed ? 'Use skill' : skill.installed ? 'Update skill' : 'Add to Rocoto', async () => {
      if (!session?.can_query) { if (!screen) $('skill-dialog').close(); showSession('Enter your access code to install and use skills.'); return; }
      if (!installed) { action.disabled = true; try { await request('/api/skills/install', { skill_id: skill.id, expected_revision: skill.revision }); await loadSkills(); if (screen) { marketplaceDetail.item = skillCatalogue.find((item) => item.id === skill.id) || skill; renderMarketplace(); } else $('skill-dialog').close(); } catch (error) { content.append(node('p', 'agent-form-error', error.message)); action.disabled = false; } }
      else { if (!screen) $('skill-dialog').close(); const chat = activeChat(); if (chat && !chat.parent_id) chooseChat(chat); else newChat(); selectSkill(skill.id); }
    }, true); action.disabled = observing(); content.append(action);
  }
  function renderSelectedSkills() { const container = $('selected-skills'); container.replaceChildren(); const ids = activeChat()?.skill_drafts || []; container.hidden = !ids.length || homeMode === 'search'; for (const id of ids) { const skill = installedSkills.find((item) => item.id === id), chip = node('span', 'selected-skill'); const open = node('button', 'selected-skill-open'); open.type = 'button'; open.append(icon('skill'), node('span', '', `/${skill ? skillCommand(skill) : id}`)); open.addEventListener('click', () => { if (skill) openReusableSkill(skill); }); chip.append(open); const remove = node('button', 'icon-button'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove skill ${skill?.name || id}`); remove.append(icon('close')); remove.disabled=draftAdmitting();remove.addEventListener('click', () => { if(draftAdmitting())return;activeChat().skill_drafts = activeChat().skill_drafts.filter((item) => item !== id); save(); renderSelectedSkills(); }); chip.append(remove); container.append(chip); } }
  function consumeMention() { if (!attachmentMention) return; const { start, end } = attachmentMention; elements.message.value = elements.message.value.slice(0, start) + elements.message.value.slice(end); elements.message.setSelectionRange(start, start); }
  function draftAdmitting(chatId=activeId){return Boolean(running.get(chatId)?.admitting);}
  function selectSkill(id) { if (observing()||draftAdmitting()) return; const chat = activeChat() || newChat(); chat.skill_drafts ||= []; if (!chat.skill_drafts.includes(id)) { if (chat.skill_drafts.length >= 8) { notice('Choose up to 8 skills for a message.'); return; } chat.skill_drafts.push(id); } consumeMention(); toggleAttachmentMenu(false); save(); renderSelectedSkills(); resizeComposer(); elements.message.focus(); }
  function renderSkillsSubmenu() { const list = $('installed-skill-options'); list.replaceChildren(); const filter = attachmentMention?.query?.toLowerCase() || ''; for (const skill of installedSkills.filter((item) => !filter || `${item.name} ${item.description}`.toLowerCase().includes(filter))) { const button = node('button'); button.type = 'button'; button.append(icon('skill'), node('span', '', `/${skillCommand(skill)}`)); button.title = skill.description || skill.name; button.addEventListener('click', () => selectSkill(skill.id)); list.append(button); } if (!list.childElementCount) list.append(node('p', 'workspace-help', installedSkills.length ? 'Try another skill name.' : 'Installed skills appear here.')); }
  function openSkillsSubmenu() { $('skills-submenu').hidden = false; $('attach-skills').setAttribute('aria-expanded', 'true'); renderSkillsSubmenu(); }
  function closeSkillsSubmenu() { $('skills-submenu').hidden = true; $('attach-skills').setAttribute('aria-expanded', 'false'); }
  async function createSkillChat() { if (!session?.can_query) { toggleAttachmentMenu(false); showSession('Enter your access code to create a skill.'); return; } consumeMention(); toggleAttachmentMenu(false); const chat = newChat(); chat.dataset ||= scopes()[0]?.id || ''; elements.message.value = 'Create a skill. Help me define its name, purpose, instructions and any required tools or connections, then save the agreed definition.'; resizeComposer(); await submit({ preventDefault() {} }); }
  const skillCommand = (skill) => skill.command || skill.slug || String(skill.name || skill.id).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  function closeCommandMenu() { $('command-menu').hidden = true; elements.message.setAttribute('aria-expanded', 'false'); composerCommand = null; commandIndex = 0; elements.message.removeAttribute('aria-activedescendant'); }
  function consumeCommand() { if (!composerCommand) return; const { start, end } = composerCommand; elements.message.value = elements.message.value.slice(0, start) + elements.message.value.slice(end); elements.message.setSelectionRange(start, start); }
  function chooseCommand(action) { consumeCommand(); closeCommandMenu(); action(); resizeComposer(); }
  function connectionSkills(connection) { return (skillCatalogue || []).filter(skill => (skill.required_connections || []).some(required => String(required.provider).toLowerCase() === (connection.broker_id || connection.id))); }
  function renderCommandMenu() { elements.message.setAttribute('aria-expanded', 'true'); const menu = $('command-menu'); menu.replaceChildren(); if (!composerCommand) return; const query = composerCommand.query.toLowerCase(), options = [];
    if (composerCommand.kind === '/') { for (const skill of installedSkills) if (!query || `${skillCommand(skill)} ${skill.name}`.toLowerCase().includes(query)) options.push({ name: `/${skillCommand(skill)}`, description: skill.description, symbol: 'skill', action: () => selectSkill(skill.id) }); if (!query || 'create-skill'.includes(query)) options.push({ name: '/create-skill', description: 'Create a reusable skill in chat', symbol: 'plus', action: createSkillChat }); options.push({ name: 'Browse Skills', description: 'Open the skills marketplace', symbol: 'grid', action: () => showMarketplace('skills') }); }
    else if (composerCommand.connection) { const connection = composerCommand.connection; for (const skill of connectionSkills(connection)) options.push({ name: `/${skillCommand(skill)}`, description: skill.description, symbol: 'skill', logo_url: connectionLogoUrl(connection), action: () => installedSkills.some(item => item.id === skill.id) ? selectSkill(skill.id) : openReusableSkill(skill) }); options.push({ name: `${connection.name} connection`, description: 'Capabilities and setup status', symbol: connection.native_direct ? 'document' : 'at', logo_url: connectionLogoUrl(connection), action: () => openConnectionDetail(connection) }); }
    else { for (const connection of composerConnections()) if (!query || connection.name.toLowerCase().includes(query)) options.push({ name: `@${connection.name}`, description: connection.description, symbol: connection.native_direct ? 'document' : 'at', logo_url: connectionLogoUrl(connection), connection }); }
    menu.append(node('div', 'command-menu-label', composerCommand.kind === '/' ? 'Skills' : composerCommand.connection ? `${composerCommand.connection.name} skills` : 'Connections')); if (composerCommand.connection) { const back = node('button', 'command-back', 'All connections'); back.type = 'button'; back.prepend(icon('chevron')); back.addEventListener('click', () => { delete composerCommand.connection; commandIndex = 0; renderCommandMenu(); }); menu.append(back); } options.forEach((option, index) => { const button = node('button', 'command-option'); button.type = 'button'; button.id = `command-option-${index}`; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', String(index === commandIndex)); if (option.logo_url) { const image = node('img'); image.src = option.logo_url; image.alt = ''; button.append(image); } else button.append(icon(option.symbol)); const copy = node('span'); copy.append(node('strong', '', option.name), node('small', '', option.description)); button.append(copy); if (option.connection) { button.dataset.submenu = 'connection'; button.append(icon('chevron')); } button.addEventListener('click', () => { if (option.connection) { composerCommand.connection = option.connection; commandIndex = 0; renderCommandMenu(); } else chooseCommand(option.action); }); menu.append(button); });
    if (!options.length) menu.append(node('p', 'workspace-help', 'Try another connection name.')); menu.hidden = false; elements.message.setAttribute('aria-activedescendant', `command-option-${commandIndex}`);
  }
  function updateMentionMenu() { if (homeMode !== 'chat' || observing()) return; const end = elements.message.selectionStart, before = elements.message.value.slice(0, end), match = before.match(/(?:^|\s)([/@])([^\s/@]*)$/); if (!match) { if (composerCommand) closeCommandMenu(); return; } const next = { kind: match[1], start: end - match[2].length - 1, end, query: match[2] }; if (!composerCommand || composerCommand.query !== next.query || composerCommand.kind !== next.kind) commandIndex = 0; composerCommand = next; toggleAttachmentMenu(false); renderCommandMenu(); }
  function handleCommandMenu(event) { if ($('command-menu').hidden || event.isComposing) return false; if (event.key === 'Escape') { event.preventDefault(); closeCommandMenu(); return true; } const buttons = [...$('command-menu').querySelectorAll('.command-option')]; if (event.key === 'ArrowRight' && buttons[commandIndex]?.dataset.submenu) { event.preventDefault(); buttons[commandIndex].click(); return true; } if (event.key === 'ArrowLeft' && composerCommand?.connection) { event.preventDefault(); delete composerCommand.connection; commandIndex = 0; renderCommandMenu(); return true; } if (!buttons.length || !['ArrowDown', 'ArrowUp', 'Enter', 'Tab'].includes(event.key)) return false; event.preventDefault(); if (['Enter', 'Tab'].includes(event.key)) { buttons[commandIndex]?.click(); return true; } commandIndex = (commandIndex + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length; buttons.forEach((button, index) => button.setAttribute('aria-selected', String(index === commandIndex))); elements.message.setAttribute('aria-activedescendant', buttons[commandIndex].id); buttons[commandIndex].scrollIntoView({ block: 'nearest' }); return true; }
  function handleComposerMenu(event) {
    if (event.isComposing) return false;
    const toggle = $('attachment-toggle'), inSubmenu = Boolean(event.target.closest('#skills-submenu'));
    if (event.target === toggle && ['ArrowDown', 'ArrowUp'].includes(event.key)) { event.preventDefault(); toggleAttachmentMenu(true); const buttons = [...$('attachment-menu').querySelectorAll(':scope > button, #attach-skills, #attachment-connections button')]; buttons[event.key === 'ArrowUp' ? buttons.length - 1 : 0]?.focus(); return true; }
    if ($('attachment-menu').hidden) return false;
    if (event.key === 'ArrowRight' && event.target === $('attach-skills')) { event.preventDefault(); openSkillsSubmenu(); $('skills-submenu').querySelector('button:not(:disabled)')?.focus(); return true; }
    if (inSubmenu && ['ArrowLeft', 'Escape'].includes(event.key)) { event.preventDefault(); closeSkillsSubmenu(); $('attach-skills').focus(); closeSkillsSubmenu(); return true; }
    if (event.key === 'Escape') { event.preventDefault(); toggleAttachmentMenu(false, true); return true; }
    const scope = inSubmenu ? $('skills-submenu') : $('attachment-menu'), selector = inSubmenu ? 'button' : ':scope > button, #attach-skills, #attachment-connections button';
    const buttons = [...scope.querySelectorAll(selector)].filter(button => !button.disabled && button.getClientRects().length);
    if (!buttons.length || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return false;
    event.preventDefault(); const index = buttons.indexOf(event.target); const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1 : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length; buttons[next]?.focus(); return true;
  }
  function businessMapMetadata(value){
    if(!value||!/^map-[a-f0-9]{32}$/.test(value.id||'')||typeof value.collection!=='string'||!/^[a-zA-Z0-9_-]{1,128}$/.test(value.collection))return null;
    return {id:value.id,collection:value.collection,title:typeof value.title==='string'?value.title.slice(0,240):'Neighborhood map',created_at:value.created_at,stale:value.stale===true};
  }
  function invalidateBusinessMaps(collection){for(const key of businessMaps.keys())if(JSON.parse(key)[1]===collection)businessMaps.delete(key);}
  function resetBusinessMaps(){for(const root of document.querySelectorAll('.business-map'))window.RocotoBusinessMap.dispose(root);businessMaps.clear();if($('business-map-dialog').open)$('business-map-dialog').close();$('business-map-expanded').replaceChildren();mapDialogOpener=null;}
  function expandBusinessMap(entry,opener){if(!entry.data)return;mapDialogOpener=opener;const root=$('business-map-expanded');root.replaceChildren();if(entry.status==='stale')root.append(node('p','workspace-help','Saved map · The business location has changed.'));root.append(window.RocotoBusinessMap.render(entry.data,{node,icon}));$('business-map-dialog').showModal();$('close-business-map').focus();}
  function businessMapSurface(collection,metadata=null,revision=''){
    const root=node('section','business-map-surface');if(!session||!collection)return root;
    const key=JSON.stringify([sessionGeneration,collection,metadata?.id||'latest',metadata?'':revision]),generation=sessionGeneration;let entry=businessMaps.get(key);
    const paint=()=>{root.replaceChildren();root.hidden=entry.status==='empty';if(root.hidden)return;if(entry.error){root.append(node('p','workspace-help',entry.error),agentButton('Retry',()=>{businessMaps.delete(key);root.replaceWith(businessMapSurface(collection,metadata,revision));},false,'refresh'));return;}if(!entry.loaded){root.append(node('p','workspace-help','Loading neighborhood map…'));return;}if(!entry.data){root.append(node('p','workspace-help','Prepare a map for the current business location in chat.'));return;}if(entry.status==='stale')root.append(node('p','workspace-help','Saved map · The business location has changed.'));let rendered;rendered=window.RocotoBusinessMap.render(entry.data,{node,icon,onExpand:()=>expandBusinessMap(entry,rendered.querySelector('.business-map-heading button'))});root.append(rendered);};
    if(!entry){entry={loaded:false,status:'loading',data:null,error:''};businessMaps.set(key,entry);if(businessMaps.size>12)businessMaps.delete(businessMaps.keys().next().value);const url=new URL('/api/business/map',location.origin);url.searchParams.set('collection',collection);if(metadata)url.searchParams.set('map_id',metadata.id);entry.promise=(async()=>{try{const response=await fetch(url.pathname+url.search,{cache:'no-store'});if(!response.ok)throw new Error();const result=await response.json();if(generation!==sessionGeneration||businessMaps.get(key)!==entry)return;const record=businessMapMetadata(result.map);if(!['ready','empty','stale'].includes(result.status)||result.status!=='empty'&&(!record||record.collection!==collection||metadata&&record.id!==metadata.id))throw new Error();entry.status=result.status;entry.data=result.data||null;entry.loaded=true;}catch{if(generation===sessionGeneration&&businessMaps.get(key)===entry){entry.loaded=true;entry.error='The neighborhood map is reconnecting. Try again.';}}})();}
    paint();if(!entry.loaded)entry.promise?.then(()=>{if(generation===sessionGeneration&&root.isConnected&&businessMaps.get(key)===entry)paint();});return root;
  }
  function showBusiness(collection = null) { persistDraft(); clearTimeout(agentsTimer); currentView = 'business'; businessCollection = collection ?? (businessCollection || currentCollection()); businessData = null; businessError = ''; const url = new URL('/', location.href); url.searchParams.set('settings', 'business'); if (businessCollection) url.searchParams.set('collection', businessCollection); history.replaceState({}, '', url); render(); setSidebar(false); loadBusiness(); }
  async function loadBusiness() { const generation = ++businessGeneration, identity=sessionGeneration, environment = session?.environment?.id; if (!environment || currentView !== 'business') return; if (businessEnvironment && businessEnvironment !== environment) businessData = null; businessEnvironment = environment; businessLoading = Boolean(businessCollection); businessError = ''; renderBusiness(); if (!businessCollection) return; workspaceExtension.loadBusinessContext?.(businessCollection); try { const response = await fetch(`/api/business?collection=${encodeURIComponent(businessCollection)}`); if (!response.ok) throw new Error('Business context is reconnecting. Retry to load it.'); const data = await response.json(); if (generation !== businessGeneration || identity!==sessionGeneration || environment !== session?.environment?.id) return;invalidateBusinessMaps(businessCollection); businessData = data; } catch (error) { if (generation === businessGeneration && identity===sessionGeneration) businessError = error.message; } finally { if (generation === businessGeneration && identity===sessionGeneration) { businessLoading = false; if (currentView === 'business') renderBusiness(); } } }
  async function openOnboarding() {
    if (!session?.can_query || observing() || currentView !== 'business') return;
    const epoch=++onboardingEpoch,generation=sessionGeneration,collection=businessCollection,revision=businessGeneration;
    const current=()=>epoch===onboardingEpoch&&generation===sessionGeneration&&revision===businessGeneration&&collection===businessCollection&&currentView==='business'&&session?.can_query&&!observing();
    try {
      const response=await request('/api/organization/chats',{collection,purpose:'organization'}),data=await response.json();
      if(!current())return;
      if(typeof data.id!=='string'||!data.id)throw new Error('Business onboarding is reconnecting. Try again.');
      onboardingChatId=data.id;const frame=$('business-chat-frame'),url=`/?chat=${encodeURIComponent(data.id)}&drawer=1`;
      if(frame.getAttribute('src')!==url)frame.src=url;$('business-chat-drawer').hidden=false;document.body.classList.add('onboarding-open');
    } catch(error) { if(current())notice(error.message); }
  }
  function renderBusiness() {
    $('stats-collection-label').textContent = demoEnvironment() ? scopeLabel(businessCollection) : '';
    const content = $('business-content'), message = $('business-message'); content.replaceChildren(); message.replaceChildren(); message.hidden = false; if (!businessCollection) { message.textContent = 'Choose a collection to open its business context.'; return; } if (businessError) message.append(node('span', '', businessError), agentButton('Retry', loadBusiness)); else if (businessLoading) message.textContent = 'Loading business context…'; else message.hidden = true; if (!businessData) return;
    const data = businessData, profile = data.profile || {}, layout = node('div','business-layout'), facts = node('section','business-panel');
    if(!Object.values(profile).some(value=>Array.isArray(value)?value.length:typeof value==='string'&&value.trim())){const setup=agentButton('Edit in chat',openOnboarding,false,'edit'),edit=agentButton('Edit business',()=>openBusinessEditor(data,edit),false,'edit');setup.disabled=edit.disabled=observing()||!session?.can_query;content.append(workspaceEmpty('factory','Business details','Add your name, website and location.',[setup,edit]));workspaceExtension.renderBusinessSections?.(content,businessCollection);return;}
    const summary=window.RocotoBusinessSummary.renderProfile ? window.RocotoBusinessSummary.renderProfile(data,{node,icon,safeUrl,sourceIcon}) : window.RocotoBusinessSummary.render(data,{node,icon,safeUrl,sourceIcon});
    const actions=summary.querySelector('.business-profile-actions')||node('div','business-profile-actions');if(!actions.isConnected&&!summary.contains(actions))summary.append(actions);const editChat=agentButton('Edit in chat',openOnboarding,false,'edit');editChat.disabled=observing()||!session?.can_query;actions.append(editChat);facts.append(summary,businessMapSurface(businessCollection,null,data.revision));
    if(!window.RocotoBusinessSummary.renderProfile)for(const [key,label] of [['operations','Operations'],['assumptions','Assumptions to review']])if(profile[key]?.length){facts.append(node('h3','',label));const list=node('ul','business-facts');profile[key].forEach(value=>list.append(node('li','',value)));facts.append(list);}
    workspaceExtension.renderBusinessContext?.(facts,data);
    const edit=agentButton('Edit business',()=>openBusinessEditor(data,edit),false,'edit');edit.disabled=observing()||!session?.can_query;actions.append(edit);layout.append(facts);
    content.append(layout);workspaceExtension.renderBusinessSections?.(content,businessCollection); if (observing()) content.querySelectorAll('input, textarea, select, button').forEach(control=>{if(!control.closest('.business-map-surface'))control.disabled=true;});
  }
  function closeBusinessEditor(){const saved=businessEditor;if(!saved)return;businessEditor=null;saved.dialog.close();saved.dialog.remove();if(saved.opener?.isConnected&&currentView==='business')saved.opener.focus();}
  function openBusinessEditor(data,opener){
    if(currentView!=='business'||!session?.can_query||observing()||!businessCollection||data!==businessData)return;
    closeBusinessEditor();const generation=sessionGeneration,collection=businessCollection,revision=businessGeneration,dialog=node('dialog','agent-editor business-edit-dialog'),form=node('form'),header=node('header','agent-editor-header'),body=node('div','agent-editor-body'),footer=node('footer','agent-editor-footer'),fields={};
    dialog.id='business-edit-dialog';dialog.setAttribute('aria-labelledby','business-edit-title');const title=node('h2','','Edit business');title.id='business-edit-title';const close=agentButton('Close',closeBusinessEditor,false,'close');close.setAttribute('aria-label','Close business editor');header.append(title,close);
    const current=()=>businessEditor?.dialog===dialog&&dialog.open&&generation===sessionGeneration&&collection===businessCollection&&revision===businessGeneration&&currentView==='business'&&session?.can_query&&!observing();
    businessEditor={dialog,opener,current};
    for(const[key,label,max,multiline]of [['name','Business name',200],['type','Business type',200],['website','Website',2000],['location','Location',500],['description','Description',6000,true],['operations','Operations · one per line',16032,true],['source_urls','Source URLs · one per line',48024,true],['assumptions','Assumptions to review · one per line',16032,true]]){
      const field=node('label','agent-field'),input=node(multiline?'textarea':'input');input.name=key;input.maxLength=max;input.value=Array.isArray(data.profile?.[key])?data.profile[key].join('\n'):data.profile?.[key]||'';if(multiline)input.rows=key==='description'?4:3;field.append(node('span','',label),input);body.append(field);fields[key]=input;
    }
    const error=node('p','agent-form-error'),cancel=agentButton('Cancel',closeBusinessEditor),save=agentButton('Save changes',()=>{},true,'check');error.hidden=true;error.setAttribute('role','alert');save.type='submit';body.append(error);footer.append(cancel,save);form.append(header,body,footer);dialog.append(form);document.body.append(dialog);
    dialog.addEventListener('close',()=>{if(businessEditor?.dialog===dialog)closeBusinessEditor();});
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(!current()||save.disabled)return;save.disabled=true;error.hidden=true;
      const profile=Object.fromEntries(Object.entries(fields).map(([key,input])=>[key,['operations','source_urls','assumptions'].includes(key)?input.value.split('\n').map(line=>line.trim()).filter(Boolean):input.value.trim()]));
      try{await request('/api/business/profile',{collection,expected_revision:data.revision,profile});if(!current())return;invalidateBusinessMaps(collection);closeBusinessEditor();await loadBusiness();}
      catch(failure){if(current()){error.textContent=failure.message;error.hidden=false;save.disabled=false;}}
    });
    dialog.showModal();fields.name.focus();
  }
  function showStats(){persistDraft();clearTimeout(agentsTimer);setProfileMenu(false);currentView='stats';history.replaceState({},'','/?settings=stats');render();setSidebar(false);loadStatsActivity();}
  function statsAllowed(){return session?.role==='owner'&&!observing();}
  function renderStatsActivity(){
    const root=$('stats-activity');root.replaceChildren();root.hidden=false;if(!statsAllowed()){root.append(node('p','workspace-help','Organization activity is available to organization admins.'));return;}
    root.append(node('h2','','Organization activity'),node('p','stats-activity-scope',demoEnvironment()?`All demo collections · ${statsActivity?.identity?.name||session.environment.name}`:statsActivity?.identity?.name||session.environment.name));
    const content=window.RocotoProfileSettings.render(statsActivity,{node,icon,scope:'organisation',mode:profileMode,loading:statsActivityLoading,error:statsActivityError,onMode:value=>{profileMode=value;},onAgent:id=>showAgents(id)});content.querySelector('.profile-settings-title')?.remove();content.querySelector('.profile-identity')?.remove();root.append(content);
  }
  async function loadStatsActivity(){
    const epoch=++statsActivityEpoch,generation=sessionGeneration;statsActivity=null;statsActivityError='';statsActivityLoading=statsAllowed();renderStatsActivity();if(!statsAllowed())return;
    try{const response=await fetch('/api/profile/activity?scope=organisation&days=364',{cache:'no-store'});if(!response.ok)throw new Error('Organization activity is available to organization admins.');const data=await response.json();if(epoch!==statsActivityEpoch||generation!==sessionGeneration||!statsAllowed())return;if(data.scope!=='organisation'||data.organisation_allowed!==true)throw new Error('Refresh Stats to load organization activity.');statsActivity=data;}
    catch(error){if(epoch===statsActivityEpoch&&generation===sessionGeneration)statsActivityError=error.message;}
    finally{if(epoch===statsActivityEpoch&&generation===sessionGeneration){statsActivityLoading=false;if(currentView==='stats')renderStatsActivity();}}
  }

  async function loadWorkspaceCapabilities(dataset = 'company') {
    if (!session || workspaceCapabilityRequests.has(dataset)) return;
    const cached = workspaceCapabilities.get(dataset); if (cached && Date.now() - cached.loaded < 30000) return;
    const generation = sessionGeneration; workspaceCapabilityRequests.set(dataset, generation);
    try { const response = await fetch(`/api/workspace/capabilities?dataset=${encodeURIComponent(dataset)}`); if (!response.ok) return; const data = await response.json(); if (generation !== sessionGeneration) return; workspaceCapabilities.set(dataset, { data, loaded: Date.now() }); renderAgentSetupNotice(); }
    catch { /* The saved configuration stays available while readiness reconnects. */ }
    finally { if (workspaceCapabilityRequests.get(dataset) === generation) workspaceCapabilityRequests.delete(dataset); }
  }
  function renderAgentSetupNotice() {
    const notice = $('agent-setup-notice'), chat = activeChat(), setup = chat?.agent_setup, assigned = agents.find(item => item.id === setup?.agent_id), readiness = assigned?.execution || workspaceCapabilities.get(chat?.dataset || 'company')?.data?.recurring_agents;
    const visible = currentView === 'chat' && homeMode === 'chat' && !chat?.parent_id && !observing() && setup && setup.mode !== 'ad_hoc' && readiness?.ready !== true;
    notice.hidden = !visible; if (!visible) { notice.replaceChildren(); return; }
    const copy = node('span', '', readiness?.notice || 'Connect the inputs for this agent to start scheduled runs.'); notice.replaceChildren(icon('clock'), copy);
    workspaceExtension.appendSetupActions?.(notice,setup,chat.dataset);
  }

  function agentExecutionReady(agent) { return agent.mode === 'ad_hoc' || (agent.execution?.ready ?? agentCapabilities.recurring_agents?.ready) === true; }

  let fileSearch = '', fileRename = null; const selectedGalleryFiles = new Set();
  function resetFiles() {
    selectedGalleryFiles.clear(); fileRename = null; $('rename-file-dialog').close(); $('file-question').value = '';
    filePreviewController?.abort();filePreviewController=null;
    ++filesGeneration; ++filePreviewGeneration; filesScope = ''; filesData = []; filesLoading = false; filesError = ''; selectedFileId = null; filePreview = null; filePreviewLoading = false; filePreviewError = ''; filesPaneOpen = false;
    $('chat-files-panel').hidden = true; $('chat-files-toggle').setAttribute('aria-expanded', 'false'); document.body.classList.remove('files-panel-open','workspace-file-viewer'); $('chat-files-list').replaceChildren(); $('file-preview').replaceChildren(); $('generated-files')?.replaceChildren();
  }
  function currentFilesScope() { return currentView === 'chat' ? `${sessionGeneration}:chat:${activeId || ''}` : `${sessionGeneration}:surface:${workspaceExtension.filesSurface?.()?.collection || ''}`; }
  function updateFilesSurface() {
    const available = currentView === 'chat' || Boolean(workspaceExtension.filesSurface?.()?.active);
    $('chat-files-toggle').hidden = currentView !== 'chat';
    if (!available) filesPaneOpen = false;
    const scope = currentFilesScope();
    if (filesScope !== scope) { filePreviewController?.abort();filePreviewController=null; ++filesGeneration; ++filePreviewGeneration; filesScope = scope; filesData = []; filesLoading = false; filesError = ''; selectedFileId = null; filePreview = null; filePreviewLoading = false; filePreviewError = ''; renderFilePreview(); if (filesPaneOpen || Boolean(workspaceExtension.filesSurface?.()?.active)) loadFiles(); }
    if(!filesPaneOpen)pauseMedia($('file-preview'));$('chat-files-panel').hidden = !filesPaneOpen; $('chat-files-toggle').setAttribute('aria-expanded', String(filesPaneOpen)); document.body.classList.toggle('files-panel-open', filesPaneOpen && currentView !== 'library'); document.body.classList.toggle('workspace-file-viewer', filesPaneOpen && currentView === 'library'); $('file-question-form').hidden = !filesPaneOpen || currentView !== 'library'; $('file-question').disabled = $('file-question-send').disabled = !pluginWritable();
  }
  function setFilesPane(open, opener = null) { if(!open){filePreviewController?.abort();filePreviewController=null;++filePreviewGeneration;filePreviewLoading=false;} if(open) workspaceExtension.closeOverlay?.(); filesPaneOpen = open; if (open) { filesOpener = opener || $('chat-files-toggle'); updateFilesSurface(); loadFiles(); $('close-chat-files').focus(); } else { updateFilesSurface(); if (filesOpener?.isConnected && !filesOpener.hidden) filesOpener.focus(); else if (currentView === 'chat') $('chat-context-toggle').focus(); else workspaceExtension.focusFiles?.(); } }
  async function loadFiles() {
    if (!session || filesLoading) return;
    const scope = currentFilesScope(), generation = ++filesGeneration, params = new URLSearchParams(); filesScope = scope;
    if (currentView === 'chat') { if (!activeId) return; params.set('chat_id', activeId); if (activeChat()?.dataset) params.set('dataset', activeChat().dataset); }
    else if (workspaceExtension.filesSurface?.()?.collection) params.set('dataset', workspaceExtension.filesSurface().collection);
    filesLoading = true; filesError = ''; if (!filesData.length) renderGeneratedFiles();
    try { const response = await fetch(`/api/files?${params}`); if (!response.ok) throw new Error('Files are reconnecting. Refresh to try again.'); const data = await response.json(); if (generation !== filesGeneration || scope !== currentFilesScope()) return; const next = Array.isArray(data.files) ? data.files : []; if (JSON.stringify(next) !== JSON.stringify(filesData)) { const priorSelected=filesData.find(file=>file.id===selectedFileId);filesData = next; if (selectedFileId && !filesData.some(file => file.id === selectedFileId)) { selectedFileId = null; filePreview = null; ++filePreviewGeneration; renderFilePreview(); } else if(selectedFileId){const selected=filesData.find(file=>file.id===selectedFileId);if(priorSelected?.status!=='ready'&&selected?.status==='ready')openGeneratedFile(selected,filesOpener);else renderFilePreview();} renderGeneratedFiles(); } }
    catch (error) { if (generation === filesGeneration) filesError = error.message; }
    finally { if (generation === filesGeneration) { filesLoading = false; renderFilesMessage(); if (!filesData.length) renderGeneratedFiles(); } }
  }
  function renderFilesMessage() { const message = filesError || (filesLoading ? 'Loading files…' : ''); $('files-message').textContent = message; workspaceExtension.renderFilesStatus?.(message); }
  function generatedFileCard(file) { const button = node('button', 'generated-file'); button.type = 'button'; button.setAttribute('aria-pressed', String(selectedFileId === file.id)); const copy = node('span', 'generated-file-copy'); copy.append(node('strong', '', file.title || file.name), node('small', '', file.kind==='provider_resource'?(file.provider_name||'Provider resource'):file.name)); button.append(icon(file.kind === 'media_export' ? 'archive' : 'document'), copy, node('small', 'generated-file-size', file.status && file.status !== 'ready' ? humanize(file.status) : fileSize(file.size_bytes))); button.addEventListener('click', () => openGeneratedFile(file, button)); return button; }
  function fileFormat(file) { const suffix = (file.name || '').split('.').at(-1).toLowerCase(); return ({docx:'Word',doc:'Word',pdf:'PDF',xlsx:'Excel',xls:'Excel',csv:'CSV',md:'Markdown',txt:'Text',png:'Image',jpg:'Image',jpeg:'Image',zip:'ZIP'})[suffix] || (file.kind === 'provider_resource' ? 'Document' : suffix.toUpperCase() || 'File'); }
  function workspaceFileCard(file) {
    const card=node('article','generated-file workspace-file-tile'), open=node('button','workspace-file-open'), format=fileFormat(file), title=file.title || file.name; card.dataset.fileId=file.id; open.type='button';open.setAttribute('aria-label',title);open.append(node('strong','workspace-file-title',title));const mark=node('span','workspace-file-format');mark.dataset.format=format;mark.append(icon(file.kind==='media_export'?'archive':/Excel|CSV/.test(format)?'grid':'document'),node('span','',format));open.append(mark);
    const footer=node('span','workspace-file-footer'), opened=file.last_opened_at,stamp=opened || file.created_at,date=new Date(typeof stamp==='number'?stamp*1000:stamp);footer.append(node('small','',Number.isFinite(date.getTime())?`${opened?'Opened':'Created'} ${date.toLocaleDateString(undefined,{day:'numeric',month:'short'})}`:file.status==='ready'?'Ready':humanize(file.status)));open.append(footer);open.addEventListener('click',()=>openGeneratedFile(file,open));card.append(open);
    const provider=node('span','workspace-file-provider');provider.title=file.provider_name || 'Saved in Rocoto';provider.setAttribute('aria-label',provider.title);const providerLogo=connectionCatalogue.find(item=>item.id===file.provider || item.name===file.provider_name);if(providerLogo)provider.append(connectionLogo(providerLogo));else provider.append(icon(file.kind==='provider_resource'?'globe':'document'));card.append(provider);
    const selected=node('label','workspace-file-select'), check=node('input');check.type='checkbox';check.checked=selectedGalleryFiles.has(file.id);check.setAttribute('aria-label',`Select ${title}`);selected.append(check);const update=()=>{card.classList.toggle('is-selected',check.checked);check.checked?selectedGalleryFiles.add(file.id):selectedGalleryFiles.delete(file.id);};check.addEventListener('change',update);update();card.append(selected);
    const menu=node('details','workspace-file-menu'), summary=node('summary');summary.setAttribute('aria-label',`Actions for ${title}`);summary.append(icon('more'));const actions=node('div','workspace-file-menu-items');const about=agentButton('Chat about this',()=>{menu.open=false;chatAboutFile(file);},false,'edit');about.disabled=!pluginWritable();actions.append(about);const download=safeUrl(file.download_url);if(download&&new URL(download).origin===location.origin){const link=node('a','','Download');link.href=download;link.download=file.name || '';link.prepend(icon('download'));actions.append(link);}const rename=agentButton('Rename',()=>{menu.open=false;openFileRename(file,summary);},false,'edit');rename.disabled=!pluginWritable();actions.append(rename);menu.append(summary,actions);menu.addEventListener('toggle',()=>{if(!menu.open)return;for(const other of document.querySelectorAll('.workspace-file-menu[open]'))if(other!==menu)other.open=false;actions.style.transform='';const box=actions.getBoundingClientRect(),rail=matchMedia('(max-width:760px)').matches?56:72,left=Math.min(innerWidth-box.width-8,Math.max(rail+8,box.left));actions.style.transform=`translateX(${left-box.left}px)`;});menu.addEventListener('keydown',event=>{if(event.key==='Escape'){event.stopPropagation();menu.open=false;summary.focus();}});card.append(menu);return card;
  }
  async function markFileOpened(file) {
    if(!pluginWritable())return;const generation=sessionGeneration,scope=currentFilesScope();
    try{const response=await fetch(`/api/files/${encodeURIComponent(file.id)}/opened`,{method:'POST',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:'{}'});if(!response.ok)return;const data=await response.json(),record=data.file || data;if(generation!==sessionGeneration||scope!==currentFilesScope()||record.id!==file.id)return;const saved=filesData.find(item=>item.id===file.id);if(saved)Object.assign(saved,record);renderGeneratedFiles();}catch{}
  }
  function openFileRename(file,opener) { if(!pluginWritable())return;fileRename={id:file.id,generation:sessionGeneration,scope:currentFilesScope(),opener};$('rename-file-input').value=file.title || file.name;$('rename-file-error').textContent='';$('rename-file-save').disabled=false;$('rename-file-dialog').showModal();$('rename-file-input').focus();$('rename-file-input').select(); }
  async function renameFile(event) {
    event.preventDefault();const state=fileRename,title=$('rename-file-input').value.trim();if(!state||!title||!pluginWritable())return;$('rename-file-save').disabled=true;
    try{const response=await fetch(`/api/files/${encodeURIComponent(state.id)}`,{method:'PATCH',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify({title})});if(!response.ok)throw new Error();const data=await response.json(),record=data.file || data;if(fileRename!==state||state.generation!==sessionGeneration||state.scope!==currentFilesScope()||record.id!==state.id)return;const saved=filesData.find(item=>item.id===state.id);if(saved)Object.assign(saved,record);$('rename-file-dialog').close();renderGeneratedFiles();renderFilePreview();}catch{if(fileRename===state)$('rename-file-error').textContent='The title is reconnecting. Try saving it again.';}finally{if(fileRename===state)$('rename-file-save').disabled=false;}
  }
  function chatAboutFile(file,question='') { if(!pluginWritable())return;setFilesPane(false);const chat=newChat();chat.dataset=file.dataset || workspaceExtension.filesSurface?.()?.collection || chat.dataset;elements.message.value=question.trim()?`${question.trim()}\n\nUse the saved file “${file.title || file.name}” (file ID ${file.id}).`:`Use the saved file “${file.title || file.name}” (file ID ${file.id}): `;resizeComposer();elements.message.focus();save();if(question.trim())$('composer').requestSubmit(); }

  function renderGeneratedFiles() {
    renderFilesMessage(); for (const id of ['chat-files-list', 'generated-files']) { const container = $(id); if(!container)continue; container.replaceChildren(); if (!filesData.length && !filesLoading && !filesError) { const empty = node('div', 'files-empty'); empty.append(icon('document'), node('strong', '', 'Files from your chats'), node('p', '', currentView === 'chat' ? 'Ask for a report, spreadsheet or document. The files created in this chat appear here.' : 'Reports, documents and editing packages appear here as they are created.')); container.append(empty); } for (const file of filesData.filter(file => id !== 'generated-files' || `${file.title || ''} ${file.name || ''}`.toLowerCase().includes(fileSearch))) container.append(id === 'generated-files' ? workspaceFileCard(file) : generatedFileCard(file)); }
  }
  async function openGeneratedFile(file, opener) {
    workspaceExtension.cancelPending?.();pauseMedia(document);
    filePreviewController?.abort();
    if(selectedFileId!==file.id)$('file-question').value='';filesPaneOpen = true; filesOpener = opener; updateFilesSurface(); if(currentView === 'library') markFileOpened(file);
    const controller=new AbortController();filePreviewController=controller;
    if (!filesData.some(item=>item.id===file.id)) filesData.push(file); selectedFileId = file.id; filePreview = null; filePreviewError = ''; filePreviewLoading = file.kind !== 'media_export' && (file.kind !== 'provider_resource' || Boolean(file.preview_format)) && file.status === 'ready'; const generation = ++filePreviewGeneration, scope = currentFilesScope(); updateFilesSurface(); renderGeneratedFiles(); renderFilePreview(); $('close-chat-files').focus();
    if (!filePreviewLoading) return;
    const current=()=>generation===filePreviewGeneration&&scope===currentFilesScope()&&!controller.signal.aborted;
    try {
      const response = await fetch(`/api/files/${encodeURIComponent(file.id)}/content`,{signal:controller.signal});
      if (!response.ok) throw new Error('This preview is reconnecting. Open the file again to retry.');
      const data = await response.json(); if (!current()) return; filePreview = data;
      if(file.kind==='document'&&/\.(docx|pdf|xlsx)$/i.test(file.name||'')){
        const status=await fetch(`/api/files/${encodeURIComponent(file.id)}/page-preview`,{signal:controller.signal});
        if(!current())return;
        if(status.ok){
          let preview=await status.json();if(!current())return;
          if(preview.preview_status!=='ready'&&preview.file?.page_preview_available&&!observing()){
            const prepared=await fetch(`/api/files/${encodeURIComponent(file.id)}/preview`,{method:'POST',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:'{}',signal:AbortSignal.any([controller.signal,AbortSignal.timeout(180000)])});
            if(!current())return;
            if(!prepared.ok)throw new Error('Open the file again to prepare its preview. The original is ready to download.');
            preview=await prepared.json();if(!current())return;
          }
          if(preview.preview_status==='ready')filePreview=preview;
        }
      }
    }
    catch (error) { if (current()) filePreviewError = error.message; }
    finally { if (current()) { filePreviewLoading = false; renderFilePreview(); } }
  }
  function previewTable(text, name) { const table=node('table','file-table'), body=node('tbody'),rows=[];let row=[],field='',quoted=false;const separator=String(name).endsWith('.tsv')||(!text.includes(',')&&text.includes('\t'))?'\t':',';for(let i=0;i<text.length&&rows.length<1000;i++){const char=text[i];if(char==='"'){if(quoted&&text[i+1]==='"'){field+='"';i++;}else quoted=!quoted;}else if(char===separator&&!quoted){row.push(field);field='';}else if(char==='\n'&&!quoted){row.push(field.replace(/\r$/,''));rows.push(row);row=[];field='';}else field+=char;}if(field||row.length){row.push(field);rows.push(row);}rows.forEach((values,index)=>{const tr=node('tr');values.slice(0,100).forEach(value=>tr.append(node(index?'td':'th','',value)));body.append(tr);});table.append(body);return table; }
  function renderFilePreview() {
    const container = $('file-preview'), savedFile = filesData.find(file => file.id === selectedFileId), file = savedFile && {...savedFile,...(filePreview?.file?.id===savedFile.id?filePreview.file:{})}; pauseMedia(container);container.replaceChildren(); container.hidden = !file; $('chat-files-panel').classList.toggle('file-viewer-open',Boolean(file)); $('files-panel-title').textContent=file?.title || file?.name || 'Files'; const full = currentView === 'library'; $('files-panel-title').hidden = full && Boolean(file); $('file-selector-wrap').hidden = !full || !file; $('full-file-actions').hidden = !full || !file; $('full-file-actions').replaceChildren(); if(full && file){const select=$('file-selector');select.replaceChildren();for(const record of filesData){const option=node('option','',record.title || record.name);option.value=record.id;select.append(option);}select.value=file.id;} $('chat-files-list').hidden=Boolean(file); $('refresh-chat-files').hidden=Boolean(file); if (!file) return;
    const heading = node('header', 'file-preview-heading'); heading.append(node('h3', '', file.title || file.name), node('p', '', [file.name, fileSize(file.size_bytes)].filter(Boolean).join(' · '))); heading.hidden=true;container.append(heading);
    const actions = node('div', 'file-preview-actions'), downloadUrl = safeUrl(file.download_url || '');
    if (file.status === 'ready' && downloadUrl && new URL(downloadUrl).origin === location.origin) { const download = node('a', 'agent-secondary', 'Download'); download.id = 'file-download'; download.prepend(icon('download')); download.href = downloadUrl; download.download = file.name || ''; actions.append(download); }
    const text = filePreview?.content;
    if(file.kind==='provider_resource')providerResourceActions(file,actions);
    if (file.copyable && typeof text === 'string' && new TextEncoder().encode(text).length <= 100 * 1024) { const copy = agentButton('Copy', async () => { try { await navigator.clipboard.writeText(text); copy.replaceChildren(icon('check'), document.createTextNode('Copied')); } catch { copy.replaceChildren(icon('copy'), document.createTextNode('Select text to copy')); const content = $('file-preview-content'); if (content) { const range = document.createRange(); range.selectNodeContents(content); const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range); } } }, false, 'copy'); copy.id = 'file-copy'; if(file.copy_source)copy.title=file.copy_source==='original_source'?'Copy source text':'Copy extracted text'; actions.append(copy); }
    const edits=agentButton('Request edits',()=>{if(observing())return;const chat=chats.find(item=>item.id===file.chat_id);if(chat&&!chat.parent_id)chooseChat(chat);else if(activeChat()?.parent_id)newChat();elements.message.value=`Update the file “${file.name}” (file ID ${file.id}): `;if(matchMedia('(max-width:1100px)').matches)setFilesPane(false);resizeComposer();elements.message.focus();},false,'edit');edits.disabled=observing()||Boolean(activeChat()?.parent_id);if(!full)actions.prepend(edits);const zoom=node('select','file-zoom');zoom.setAttribute('aria-label','Document zoom');for(const [value,label] of [['fit','Fit'],['1','100%'],['1.25','125%'],['1.5','150%']]){const option=node('option','',label);option.value=value;zoom.append(option);}zoom.addEventListener('change',()=>{const preview=$('file-preview-content');if(preview){const pdf=preview.querySelector('iframe');if(pdf)pdf.src=`/api/files/${encodeURIComponent(file.id)}/preview#toolbar=0&navpanes=0&${zoom.value==='fit'?'view=FitV':`zoom=${Number(zoom.value)*100}`}`;else {preview.style.setProperty('--document-scale',zoom.value==='fit'?'1':zoom.value);preview.classList.toggle('pages-actual-size',zoom.value!=='fit');}}});zoom.hidden=(file.representation==='native_original'&&!file.preview_format&&!['image','pages'].includes(file.inline_preview_type))||(file.kind==='provider_resource'&&!file.preview_format);actions.append(zoom);if(full){$('full-file-actions').append(actions);for(const action of actions.querySelectorAll('button,a')){action.setAttribute('aria-label',action.textContent.trim());action.title=action.textContent.trim();}}else container.append(actions);
    if (filePreviewLoading || filePreviewError) { container.append(node('p', 'workspace-help', filePreviewError || 'Loading preview…')); return; }
    if(workspaceExtension.renderFilePreview?.(container,file))return;
    if(file.kind==='provider_resource'){const url=providerResourceUrl(file),caption=node('p','workspace-help',[file.provider_name||'Provider resource',url?new URL(url).hostname:'',typeof text==='string'?'Saved text preview':''].filter(Boolean).join(' · '));container.append(caption);if(typeof text==='string'){const viewport=node('div','file-page-viewport'),preview=node('pre','file-preview-content');preview.id='file-preview-content';preview.textContent=text;viewport.append(preview);container.append(viewport);}else{const detail=node('div','files-empty');detail.append(icon('document'),node('strong','',file.title||file.name),node('p','',file.description||'Open this resource with its provider.'));container.append(detail);}return;}

    if(file.inline_preview_type==='pages'&&Array.isArray(filePreview?.pages)&&filePreview.pages.length>0&&filePreview.pages.length<=40){
      const viewport=node('div','file-pages-viewport');viewport.id='file-preview-content';viewport.setAttribute('role','region');viewport.setAttribute('aria-label','Document pages');viewport.tabIndex=0;
      for(const [index,page] of filePreview.pages.entries()){
        if(page.page!==index+1||!Number.isInteger(page.width)||!Number.isInteger(page.height)||page.width<1||page.height<1||page.width>1600||page.height>1600)continue;
        const figure=node('figure','file-preview-page'),image=node('img');figure.style.setProperty('--page-ratio',String(page.width/page.height));figure.style.setProperty('--page-pixel-width',`${page.width}px`);image.src=`/api/files/${encodeURIComponent(file.id)}/pages/${page.page}`;image.alt=`${file.title||file.name}, page ${page.page}`;image.width=page.width;image.height=page.height;image.decoding='async';image.loading=index?'lazy':'eager';image.referrerPolicy='no-referrer';figure.append(image,node('figcaption','',`Page ${page.page} of ${filePreview.pages.length}`));viewport.append(figure);
      }
      container.append(viewport);return;
    }
    if(nativeMediaType(file)){const viewport=node('div','file-media-viewport');viewport.id='file-preview-content';viewport.append(nativeMediaPlayer(file));container.append(viewport);return;}
    if(file.inline_preview_type==='image'&&['image/png','image/jpeg','image/webp'].includes(file.media_type)&&file.status==='ready'){const viewport=node('div','file-page-viewport'),preview=node('div','file-preview-content'),image=node('img');preview.id='file-preview-content';image.alt=file.title||file.name;image.src=`/api/files/${encodeURIComponent(file.id)}/preview`;image.referrerPolicy='no-referrer';image.decoding='async';image.style.maxWidth='100%';image.style.height='auto';image.style.display='block';preview.append(image);viewport.append(preview);container.append(viewport);return;}
    if(file.representation==='native_original'&&file.media_type==='application/pdf'){const fallback=node('div','files-empty');fallback.append(icon('document'),node('strong','','PDF ready to download'),node('p','','Download the original PDF to view its pages.'));container.append(fallback);return;}
    if(file.media_type==='application/pdf'&&file.status==='ready'){const viewport=node('div','file-pdf-viewport');viewport.id='file-preview-content';const frame=node('iframe','file-pdf-frame');frame.title=file.title || file.name;frame.src=`/api/files/${encodeURIComponent(file.id)}/preview#toolbar=0&navpanes=0&view=FitV`;viewport.append(frame);container.append(viewport);return;}
    if (typeof text === 'string') { const format=file.preview_format || filePreview?.file?.preview_format, markdown=format==='markdown'||file.media_type==='text/markdown', tabular=format==='table'||file.media_type==='text/csv'; const preview=node(markdown||tabular?'div':'pre','file-preview-content');preview.id='file-preview-content';if(markdown)renderText(preview,text);else if(tabular)preview.append(previewTable(text,file.name));else preview.textContent=text;const viewport=node('div','file-page-viewport');viewport.append(preview);container.append(viewport); }
    else container.append(node('p', 'workspace-help', file.status === 'ready' ? 'Download this file to open its full contents.' : `${humanize(file.status)} file.`));
  }
  function fileSize(value) { if (value === null || value === undefined || !Number.isFinite(Number(value))) return ''; const bytes = Number(value); return bytes >= 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : bytes >= 1e6 ? `${(bytes / 1e6).toFixed(1)} MB` : bytes >= 1e3 ? `${Math.round(bytes / 1e3)} KB` : `${bytes} B`; }

  function agentFrequency(agent) { if (agent.frequency) return agent.frequency; return agent.mode === 'ad_hoc' ? 'On demand' : workspaceExtension.agentFrequency?.(agent) || `Every ${cadenceLabel(agent.interval_seconds)}`; }
  const agentModeLabel = (mode) => ({ ad_hoc: 'Ad hoc', scheduled: 'Scheduled', ongoing: 'Ongoing' }[mode] || humanize(mode));
  const agentStateLabel = (state) => ({ needs_attention: 'Needs attention' }[state] || humanize(state));
  const money = (amount) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(amount);
  function agentDate(value) { if (!value) return ''; const date = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value); return Number.isNaN(date.getTime()) ? '' : new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date); }
  function cadenceLabel(seconds) { const value = Number(seconds); return value >= 604800 && value % 604800 === 0 ? `${value / 604800} ${value === 604800 ? 'week' : 'weeks'}` : value >= 86400 && value % 86400 === 0 ? `${value / 86400} ${value === 86400 ? 'day' : 'days'}` : value >= 3600 && value % 3600 === 0 ? `${value / 3600} ${value === 3600 ? 'hour' : 'hours'}` : value >= 60 && value % 60 === 0 ? `${value / 60} ${value === 60 ? 'minute' : 'minutes'}` : `${value} seconds`; }
  function agentButton(label, handler, primary = false, symbol = null) { const button = node('button', primary ? 'agent-primary' : 'agent-secondary'); button.type = 'button'; const symbolName = symbol || workspaceExtension.buttonIcon?.(label) || ({ 'New agent': 'plus', 'Edit in chat': 'edit', 'Retry': 'refresh', 'Save draft': 'document', 'Restore': 'archive', 'See detailed breakdown': 'chart', 'Add New Skill': 'plus', 'Use skill': 'skill', 'Add to Rocoto': 'plus', 'Update skill': 'refresh', 'Back to files': 'folder', 'All connections': 'chevron' }[label]); if (symbolName) button.append(icon(symbolName)); button.append(document.createTextNode(label)); button.addEventListener('click', handler); return button; }
  function agentState(agent) { const state = node('span', `agent-state agent-state-${agent.status}`, agentStateLabel(agent.status)); return state; }
  const conversationAgentId = chat => chat?.kind==='agent_conversation' ? chat.agent_id || chat.agent_setup?.agent_id || null : null;
  function agentCardMetadata(value) {
    if(!value || typeof value!=='object' || Array.isArray(value) || !/^[A-Za-z0-9_-]{1,128}$/.test(value.id||'') || !/^[A-Za-z0-9_-]{1,128}$/.test(value.chat_id||'') || typeof value.title!=='string' || !value.title.trim() || value.title.length>100 || !['ad_hoc','scheduled','ongoing'].includes(value.mode) || !['draft','active','paused','completed','needs_attention','expired'].includes(value.status))return null;
    return {id:value.id,chat_id:value.chat_id,title:value.title,mode:value.mode,status:value.status};
  }
  function normalizeAgentCards(values){return Array.isArray(values)?values.slice(0,20).map(agentCardMetadata).filter(Boolean):[];}
  function agentConversationCard(agent){const link=node('a','agent-conversation-card');link.href=`/?agents=1&agent=${encodeURIComponent(agent.id)}`;link.append(icon('agent'),node('strong','',agent.title),node('span','',agentModeLabel(agent.mode)),icon('chevron'));link.addEventListener('click',event=>{event.preventDefault();showAgents(agent.id);});return link;}
  /* A phone alert links to /?chat={chat}&alert={alert}. The chat opens on that alert and holds it until the person's first input. */
  function releaseLinkedAlert(){if(!linkedAlert||!linkedAlertShown)return;linkedAlert=null;const url=new URL(location.href);if(url.searchParams.has('alert')){url.searchParams.delete('alert');history.replaceState({},'',url);}}
  function showLinkedAlert(){
    if(!linkedAlert||currentView!=='chat')return;const message=activeChat()?.messages.find(item=>item.alert?.id===linkedAlert);if(!message)return;
    const article=elements.messages.querySelector(`[data-message-id="${CSS.escape(String(message.id))}"]`);if(!article)return;
    elements.conversation.scrollTop+=article.getBoundingClientRect().top-elements.conversation.getBoundingClientRect().top-12;if(document.activeElement!==article){article.tabIndex=-1;article.focus({preventScroll:true});}linkedAlertShown=true;
  }
  function activePhoneAgentId(){const chat=currentView==='chat'?activeChat():null,id=conversationAgentId(chat);if(!id||!session||chat.parent_id)return null;return window.RocotoHarnessShell.classFor(harness,agents.find(item=>item.id===id)?.agent_kind||chat.agent_setup?.agent_kind)?.authority==='participant_reach'?id:null;}
  function phoneParticipantName(memberId){if(memberId&&memberId===session?.principal?.id)return 'You';return agentPhoneBlocks.get(conversationAgentId(activeChat()))?.participants?.find(item=>item.member_id===memberId)?.name||teamMembers().find(item=>item.id===memberId)?.display_name||'Participant';}
  async function loadAgentPhone(id){
    if(!id||agentPhoneLoading===id)return;const generation=sessionGeneration;agentPhoneLoading=id;let block=null;
    try{const response=await fetch(`/api/agents/${encodeURIComponent(id)}/phone`,{cache:'no-store'});if(response.ok)block=await response.json();}catch{}
    if(agentPhoneLoading===id)agentPhoneLoading=null;if(generation!==sessionGeneration)return;
    saveAgentPhone(id,block&&block.agent_id===id&&Array.isArray(block.participants)?block:null);
  }
  function saveAgentPhone(id,block){const changed=JSON.stringify(block)!==JSON.stringify(agentPhoneBlocks.get(id)??null);agentPhoneBlocks.set(id,block);if(changed&&activePhoneAgentId()===id)render();}
  function renderPhoneContactNotice(){
    let notice=$('phone-contact-notice');const id=activePhoneAgentId(),block=id?agentPhoneBlocks.get(id):null,own=block?.participants.find(item=>item.is_self&&item.verification!=='verified'&&item.contact_request?.status==='open'),visible=Boolean(own)&&homeMode==='chat'&&!observing()&&block.read_only!==true;
    if(!notice){if(!visible)return;notice=node('section','agent-setup-notice phone-contact-notice');notice.id='phone-contact-notice';notice.setAttribute('aria-label','Contact card');$('agent-setup-notice').after(notice);}
    notice.hidden=!visible;if(!visible){notice.replaceChildren();delete notice.dataset.signature;return;}
    const title=agents.find(item=>item.id===id)?.title||activeChat()?.title||'this agent',signature=JSON.stringify([id,title,own.member_id,own.verification]);if(notice.dataset.signature===signature)return;notice.dataset.signature=signature;
    const add=agentButton(own.verification==='in_progress'?'Enter security code':'Add a number',()=>{if(phoneContactCard)return;const current=agentPhoneBlocks.get(id)||block;phoneContactCard=window.RocotoMessaging.openContactCard({block:current,participant:current.participants.find(item=>item.member_id===own.member_id)||own,agentTitle:title,node,icon,onClose:()=>{phoneContactCard=null;loadAgentPhone(id);}});},true,'chat');
    notice.replaceChildren(icon('chat'),node('span','',`Add a number to get alerts from ${title} on your phone.`),add);
  }
  function syncAgentPhone(){const id=activePhoneAgentId();if(id&&agentPhoneActive!==id)loadAgentPhone(id);agentPhoneActive=id;renderPhoneContactNotice();}
  function agentInfoPhone(selected,detail){
    const state=agentInfo;if(!state||state.body!==detail)return null;
    if(window.RocotoHarnessShell.classFor(harness,selected.agent_kind)?.authority!=='participant_reach'){state.phone?.destroy();state.phone=null;return null;}
    if(!state.phone)state.phone=window.RocotoMessaging.mountPhone({agentId:selected.id,agentTitle:selected.title,node,icon,readOnly:observing(),onChange:block=>{if(agentInfo===state)saveAgentPhone(selected.id,block);}});
    else{state.phone.setTitle(selected.title);state.phone.setReadOnly(observing());}
    return state.phone.element;
  }
  function renderAgentHeader(){
    let button=$('agent-info-button');if(!button){button=node('button','icon-button');button.id='agent-info-button';button.type='button';button.setAttribute('aria-label','Agent info');button.title='Agent info';button.append(icon('info'));button.addEventListener('click',()=>{const id=conversationAgentId(activeChat());if(id)openAgentInfo(id,button);});document.querySelector('.topbar').append(button);}
    let meta=$('agent-chat-meta');if(!meta){meta=node('span','agent-chat-meta');meta.id='agent-chat-meta';elements['chat-title'].after(meta);}
    const id=currentView==='chat'?conversationAgentId(activeChat()):null,agent=agents.find(item=>item.id===id);button.hidden=!id;meta.hidden=!id;meta.textContent=agent?[agentStateLabel(agent.status),agentFrequency(agent)].filter(Boolean).join(' · '):'';
    syncAgentPhone();
  }
  function showAgents(id = null) {
    ++agentNavigationEpoch;closeAgentInfo();persistDraft();currentView='agents';selectedAgentId=id;const url=new URL(location.href);for(const key of ['chat','collection','business','stats','settings','customize','tab'])url.searchParams.delete(key);workspaceExtension.clearRoute?.(url);url.searchParams.set('agents','1');if(id)url.searchParams.set('agent',id);else url.searchParams.delete('agent');history.replaceState({},'',url);render();setSidebar(false);document.body.classList.remove('agents-list-open');loadAgents();
  }
  async function openAgentConversation(id){
    const generation=sessionGeneration,epoch=agentNavigationEpoch,current=()=>generation===sessionGeneration&&epoch===agentNavigationEpoch&&currentView==='agents'&&selectedAgentId===id;
    try{const response=await fetch(`/api/agents/${encodeURIComponent(id)}`);if(!response.ok)throw new Error('This agent is reconnecting. Open it again to retry.');const data=await response.json(),agent=data.agent||data;if(!current())return;if(agent.id!==id || !/^[A-Za-z0-9_-]{1,128}$/.test(agent.chat_id||''))throw new Error('This agent conversation is preparing. Open it again to retry.');const result=await fetch(`/api/chats/${encodeURIComponent(agent.chat_id)}`);if(!result.ok)throw new Error('This agent conversation is reconnecting. Open it again to retry.');const record=await result.json();if(!current())return;if(record.id!==agent.chat_id||record.agent_id!==id||record.kind!=='agent_conversation')throw new Error('Refresh this agent to open its current conversation.');const chat=mergeRemoteChat(record);if(!chat)throw new Error('Open this agent from its owner workspace.');const index=agents.findIndex(item=>item.id===id);if(index<0)agents.push(agent);else agents[index]=agent;save();chooseChat(chat);}
    catch(error){if(current()){agentsError=error.message;renderAgents();}}
  }
  async function loadAgents(forSidebar = false) {
    clearTimeout(agentsTimer);const onScreen=()=>currentView==='agents'||currentView==='chat'&&Boolean(conversationAgentId(activeChat()));if(agentsLoading||(!forSidebar&&!onScreen())||(!session?.can_query&&!observing())||document.hidden)return;
    const generation=sessionGeneration,environmentId=session.environment.id;if(agentsEnvironment!==environmentId){if(agentsEnvironment){agentPhoneBlocks.clear();agentPhoneActive=null;}agents=[];agentCapabilities={};agentDetailSignature=null;agentsEnvironment=environmentId;}agentsLoading=true;
    try{const response=await fetch('/api/agents');if(!response.ok)throw new Error(response.status===401||response.status===403?'Open your workspace to view agents.':'Agents are reconnecting. Try again in a moment.');const data=await response.json();if(generation!==sessionGeneration||session?.environment.id!==environmentId)return;agents=Array.isArray(data.agents)?data.agents:[];agentCapabilities=data.capabilities||{};agentsError='';if(currentView==='agents'&&selectedAgentId)await openAgentConversation(selectedAgentId);}
    catch(error){if(generation===sessionGeneration)agentsError=error.message;}
    finally{if(generation===sessionGeneration){agentsLoading=false;if(onScreen()){if(currentView==='agents')renderAgents();renderSidebar();renderAgentHeader();renderAgentSetupNotice();if(currentView==='chat'&&conversationAgentId(activeChat())&&!running.has(activeId))pollChild(activeId);if(agentInfo)refreshAgentInfo();if($('agent-editor').open)workspaceExtension.updateAgentEditor?.();if((session?.can_query||observing())&&!document.hidden)agentsTimer=setTimeout(loadAgents,4000);}else if(forSidebar)renderSidebar();}}
  }
  function closeAgentInfo(){const state=agentInfo;if(!state)return;agentInfo=null;state.phone?.destroy();state.phone=null;if(state.dialog.open)state.dialog.close();state.dialog.remove();if(state.opener?.isConnected)state.opener.focus();}
  async function openAgentInfo(id,opener){
    closeAgentInfo();const generation=sessionGeneration,view=currentView,chatId=activeId,dialog=node('dialog','agent-editor agent-info-dialog'),header=node('header','agent-editor-header'),title=node('h2','','Agent info'),body=node('div','agent-editor-body');dialog.id='agent-info-dialog';title.id='agent-info-title';dialog.setAttribute('aria-labelledby',title.id);const close=agentButton('Close',closeAgentInfo,false,'close');close.setAttribute('aria-label','Close agent info');header.append(title,close);dialog.append(header,body);document.body.append(dialog);const state={id,dialog,body,title,opener,signature:null,current:()=>agentInfo===state&&generation===sessionGeneration&&currentView===view&&activeId===chatId};agentInfo=state;dialog.addEventListener('close',()=>{if(agentInfo===state)closeAgentInfo();});body.append(node('p','workspace-help','Loading agent…'));dialog.showModal();
    try{const response=await fetch(`/api/agents/${encodeURIComponent(id)}`);if(!response.ok)throw new Error('This agent is reconnecting. Open its info again to retry.');const result=await response.json(),agent=result.agent||result;if(!state.current())return;if(agent.id!==id)throw new Error('Open this agent again to load its details.');const index=agents.findIndex(item=>item.id===id);if(index<0)agents.push(agent);else agents[index]=agent;refreshAgentInfo();}
    catch(error){if(state.current())body.replaceChildren(node('p','agent-form-error',error.message));}
  }
  function refreshAgentInfo(){const state=agentInfo;if(!state?.current())return;const selected=agents.find(item=>item.id===state.id);if(!selected)return;const signature=JSON.stringify([selected,agentCapabilities,observing(),session?.can_query]);if(signature===state.signature)return;state.signature=signature;state.title.textContent=selected.title;renderAgentDetails(selected,state.body);}
  function agentFrequencyForm(selected){
    const form=node('form','agent-frequency-form'),interval=node('input'),unit=node('div','agent-frequency-units'),limit=node('input'),label=node('label','agent-field'),runs=node('label','agent-field'),error=node('p','agent-form-error'),save=agentButton('Save frequency',()=>{},true,'check');interval.type='number';interval.min='1';interval.step='1';interval.required=true;const units=[['Seconds',1],['Minutes',60],['Hours',3600],['Days',86400]],factor=[...units].reverse().find(([,value])=>selected.interval_seconds%value===0)?.[1]||1;let unitSeconds=factor;unit.setAttribute('role','group');unit.setAttribute('aria-label','Frequency unit');for(const [name,value]of units){const button=agentButton(name,()=>{if(agentMutation)return;unitSeconds=value;for(const option of unit.children)option.setAttribute('aria-pressed',String(option===button));});button.setAttribute('aria-pressed',String(value===factor));unit.append(button);}interval.value=selected.interval_seconds/factor;label.append(node('span','','Run every'),interval);const unitLabel=node('div','agent-field');unitLabel.append(node('span','','Frequency unit'),unit);limit.type='number';limit.min='1';limit.max='20';limit.step='1';limit.required=true;limit.value=selected.max_runs;runs.append(node('span','',selected.grant?.kind==='standing'?'Runs / day':'Run limit'),limit);save.type='submit';error.hidden=true;form.append(label,unitLabel,runs,node('p','agent-field-help','Save the schedule as a draft, then activate it.'),error,save);const state=agentInfo,locked=observing()||!session?.can_query||selected.status==='active'||(selected.runs||[]).some(run=>run.status==='running');for(const control of [interval,...unit.children,limit,save])control.disabled=locked;if(locked&&!observing())form.append(node('p','agent-field-help','Pause the agent to change its schedule.'));
    form.addEventListener('submit',async event=>{event.preventDefault();if(locked||agentMutation||!state?.current()||!form.reportValidity())return;const seconds=Number(interval.value)*unitSeconds;if(!Number.isSafeInteger(seconds)||seconds<(selected.mode==='ongoing'?10:60)||seconds>604800){error.textContent='Choose a frequency between '+(selected.mode==='ongoing'?'10 seconds':'1 minute')+' and 7 days.';error.hidden=false;return;}const payload={};for(const key of ['title','instruction','description','agent_kind','dataset','input_kind','mode','interval_seconds','max_runs','output','skill_ids'])if(selected[key]!==undefined)payload[key]=selected[key];Object.assign(payload,workspaceExtension.agentDefinitionData?.(selected),{interval_seconds:seconds,max_runs:Number(limit.value),expected_revision:selected.revision});agentMutation=true;save.disabled=true;error.hidden=true;try{const response=await request(`/api/agents/${encodeURIComponent(selected.id)}/update`,payload),result=await response.json();if(!state.current())return;const updated=result.agent||result;if(updated.id!==selected.id)throw new Error('Refresh this agent to confirm its saved schedule.');Object.assign(selected,updated);state.signature=null;refreshAgentInfo();renderAgentHeader();}catch(failure){if(state.current()){error.textContent=failure.message;error.hidden=false;save.disabled=false;}}finally{agentMutation=false;}});return form;
  }
  function renderAgents() {
    collectionNavigation(); const hasDetail=Boolean(selectedAgentId); document.querySelector('.agents-heading').hidden=hasDetail; $('context-new-agent').hidden=hasDetail; $('agent-context').hidden=true;document.body.classList.remove('agents-context-active');
    const list = $('agent-list'), detail = $('agent-detail'), layout = $('agents-layout'), message = $('agents-message'), create = $('create-agent');
    if (agentsEnvironment && agentsEnvironment !== session?.environment.id) { agents = []; agentCapabilities = {}; agentDetailSignature = null; }
    create.hidden = !session?.can_query; $('context-new-agent').disabled = observing(); message.replaceChildren(); message.hidden = false;
    if (!session?.can_query && !observing()) { layout.hidden = true; const entry = node('div', 'agents-entry'); entry.append(icon('agent'), node('h2', '', 'Your agents'), node('p', '', 'Use your access code to save investigations, set their schedule, and review each run.'), agentButton('Enter access code', () => showSession('Enter your access code to manage agents.'), true)); message.append(entry); return; }
    if (agentsError) { message.append(node('span', '', agentsError), agentButton('Retry', loadAgents)); } else message.hidden = true;
    if (!agents.length) { list.replaceChildren(); layout.hidden = true; if (agentsLoading) { message.hidden = false; message.textContent = 'Loading agents…'; } else if (!agentsError) { message.hidden = false; const empty = node('div', 'agents-entry'); if (observing()) { empty.append(node('h2', '', 'Your agents'), node('p', '', 'Saved agents appear here.')); message.append(empty); return; } const artwork = node('div', 'agent-empty-art'), copy = node('div', 'agent-empty-copy'); copy.append(icon('agent'),node('h2','','Your agents'),node('p', '', 'Turn a question into a regular investigation.'), agentButton('New agent', () => startAgentChat(), true, 'plus')); artwork.append(copy); empty.append(artwork, node('small', 'agent-art-credit', 'Claude Monet · Water Lilies, 1906 · Art Institute of Chicago')); message.append(empty); } return; }
    layout.hidden = false; layout.classList.toggle('has-selection', Boolean(selectedAgentId));
    list.replaceChildren();
    if(selectedAgentId){detail.replaceChildren(node('p','workspace-help',agentsError||'Opening agent conversation…'));return;}
    detail.replaceChildren();const grid=node('div','agent-collection-grid');for(const agent of agents.filter(agent=>`${agent.title} ${agent.description||''}`.toLowerCase().includes($('agents-search').value.toLowerCase()))){const item=node('div','agent-collection-item'),button=node('button','agent-collection-card'),info=node('button','icon-button agent-card-info');button.type=info.type='button';button.append(icon('agent'),node('strong','',agent.title),node('p','',agent.description||agent.summary||''),node('small','',agentFrequency(agent)));button.addEventListener('click',()=>showAgents(agent.id));info.setAttribute('aria-label',`About ${agent.title}`);info.title=`About ${agent.title}`;info.append(icon('info'));info.addEventListener('click',()=>openAgentInfo(agent.id,info));item.append(button,info);grid.append(item);}if(!grid.childElementCount)detail.append(workspaceEmpty('search','Search agents','Try another name or description.'));else detail.append(grid);agentDetailSignature=null;
  }
  function renderAgentDetails(selected,detail){
    const scrolled=detail.scrollTop,phoneFocus=agentInfo?.body===detail&&agentInfo.phone?.element.contains(document.activeElement)?document.activeElement:null;
    detail.replaceChildren();
    const header = node('div', 'agent-detail-header'), titles = node('div'); titles.append(node('span', 'agent-detail-mode', agentModeLabel(selected.mode))); header.append(titles, agentState(selected)); detail.append(header);
    const actions = node('div', 'agent-actions'), ready = agentExecutionReady(selected);
    if (selected.status === 'active') actions.append(agentButton('Pause', () => agentAction(selected, 'pause'), false, 'pause'));
    else if (!(workspaceExtension.agentReviewRequired?.(selected)) && (selected.grant?.kind==='standing' || Number(selected.runs_started || 0) < Number(selected.max_runs))) { if (selected.mode !== 'ad_hoc') actions.append(agentButton('Activate', () => agentAction(selected, 'activate'), true, 'play')); actions.append(agentButton('Run once', () => agentAction(selected, 'run'), selected.mode === 'ad_hoc', 'play')); }
    if (!ready && selected.status !== 'active') for (const button of actions.querySelectorAll('button')) { button.disabled = true; button.title = selected.execution?.notice || agentCapabilities.recurring_agents?.notice || 'Connect the inputs for this agent to start scheduled runs.'; }
    const edit = agentButton(workspaceExtension.agentReviewRequired?.(selected) ? 'Review in chat' : 'Edit in chat', () => startAgentChat(selected), false, 'edit'); edit.disabled = selected.status === 'active' || (selected.runs || []).some((run) => run.status === 'running'); if (edit.disabled) edit.title = 'Pause the agent before editing'; actions.append(edit); if (observing()) actions.querySelectorAll('button').forEach((button) => { button.disabled = true; button.title = 'Viewing this person’s agent'; }); detail.append(actions);
    if (!ready) { const readiness = node('div', 'agent-readiness-note'); readiness.append(icon('clock'), node('p', '', selected.execution?.notice || agentCapabilities.recurring_agents?.notice || 'Connect the inputs for this agent to start scheduled runs.')); workspaceExtension.appendSetupActions?.(readiness,selected,selected.dataset); detail.append(readiness); }
    if (selected.description || selected.summary) detail.append(node('p', 'agent-outcome', selected.description || selected.summary)); if (selected.display_instructions || selected.instruction) { const instruction = node('details', 'agent-technical raw-agent-instructions'); instruction.append(node('summary', '', 'Agent instructions'), node('p', 'agent-instruction', selected.display_instructions || selected.instruction)); detail.append(instruction); } workspaceExtension.renderAgentSummary?.(detail,selected); const schedule = node('p', 'agent-frequency'); schedule.append(icon('clock'), document.createTextNode(agentFrequency(selected))); detail.append(schedule); const technical = node('details', 'agent-technical'), technicalTitle = node('summary', '', 'Technical details'); technicalTitle.prepend(icon('settings')); technical.append(technicalTitle); detail.append(technical);
    workspaceExtension.renderAgentDetails?.(selected,detail,technical);
    const settings = node('dl', 'agent-settings');
    const setting = (label, value) => { const pair = node('div'); pair.append(node('dt', '', label), node('dd', '', value)); settings.append(pair); };
    setting(workspaceExtension.collectionFieldLabel || 'Collection', `${session.environment.name} / ${scopeLabel(selected.dataset)}`);
    setting('Schedule', selected.mode === 'ad_hoc' ? 'Run when requested' : `${workspaceExtension.agentScheduleVerb?.(selected) || 'Run'} every ${cadenceLabel(selected.interval_seconds)}`);
    setting(selected.grant?.kind==='standing'?'Daily run limit':'Run limit', selected.grant?.kind==='standing'?`${selected.max_runs} / day`:`${Number(selected.runs_started) || 0} of ${selected.max_runs} started`);
    workspaceExtension.renderAgentSettings?.(selected,setting);
    if (selected.next_run_at && selected.status === 'active') setting(workspaceExtension.agentNextRunLabel?.(selected) || 'Next run', agentDate(selected.next_run_at));
    if (selected.expires_at) setting('Access expires', agentDate(selected.expires_at)); setting('Revision', String(selected.revision)); technical.append(settings);
    workspaceExtension.renderAgentInputNotice?.(detail,selected,ready);
    const phone=agentInfoPhone(selected,detail);if(phone)detail.append(phone);
    if(selected.mode!=='ad_hoc')detail.append(agentFrequencyForm(selected));
    const output = node('section', 'agent-detail-section'); output.append(node('h3', '', 'Output')); const report = node('div', 'agent-output'); report.append(icon('document'), node('span', '', workspaceExtension.agentOutputLabel || 'In-app report')); output.append(report); detail.append(output); const rawInstructions = detail.querySelector('.raw-agent-instructions'); detail.append(technical); if (rawInstructions) detail.append(rawInstructions);
    const runs = node('section', 'agent-detail-section agent-runs'); runs.append(node('h3', '', 'Runs'));
    const runHistory = Array.isArray(selected.runs) ? [...selected.runs].sort((a, b) => Number(b.created_at) - Number(a.created_at)) : selected.last_run ? [selected.last_run] : [];
    if (!runHistory.length) runs.append(node('p', 'agent-field-help', workspaceExtension.runHistoryHint || 'Each run appears here with its report and status.'));
    for (const [index, run] of runHistory.entries()) {
      const row = node('div', 'agent-run'), heading = node('div', 'agent-run-heading');
      if (run.chat_id) { const link = node('a', 'agent-run-link', `Run ${runHistory.length - index}`); link.href = `/?chat=${encodeURIComponent(run.chat_id)}`; link.append(icon('chevron')); link.addEventListener('click', async (event) => { event.preventDefault(); await openChatId(run.chat_id); const chat = chats.find((item) => item.id === run.chat_id); if (chat) { chat.agent_id = selected.id; save(); if (run.status === 'running') pollChild(chat.id); } }); heading.append(link); }
      else heading.append(node('strong', '', `Run ${runHistory.length - index}`));
      heading.append(node('span', 'agent-run-status', agentStateLabel(run.status))); row.append(heading, node('small', '', [agentDate(run.created_at), workspaceExtension.runSummary?.(run) || ''].filter(Boolean).join(' · '))); if (run.error) row.append(node('p', 'agent-run-error', run.error)); runs.append(row);
    }
    detail.append(runs); workspaceExtension.renderAgentCommunicationNotice?.(detail,selected);
    detail.scrollTop=scrolled;if(phoneFocus?.isConnected)phoneFocus.focus({preventScroll:true});
  }
  function openAgentEditor(agent = null) {
    if (!session?.can_query) { showSession('Enter your access code to create an agent.'); return; }
    agentEditingId = agent?.id || null; agentEditingRevision = agent?.revision ?? null; agentEditorOpener = document.activeElement; $('agent-editor-title').textContent = agent ? 'Edit agent' : 'New agent'; $('agent-title').value = agent?.title || ''; $('agent-instruction').value = agent?.instruction || ''; $('agent-company-label').textContent = session.environment.name;
    const collection = $('agent-dataset'); collection.replaceChildren(); const blank = node('option', '', 'Choose a collection'); blank.value = ''; blank.disabled = true; collection.append(blank); for (const scope of scopes()) { const option = node('option', '', scope.label); option.value = scope.id; collection.append(option); } collection.value = agent?.dataset || activeChat()?.dataset || '';
    const mode = agent?.mode || 'ad_hoc'; for (const radio of document.querySelectorAll('[name="agent-mode"]')) radio.checked = radio.value === mode;
    $('agent-interval').value = agent?.interval_seconds || (mode === 'ongoing' ? 10 : 60); $('agent-max-runs').value = agent?.max_runs || 1; workspaceExtension.openAgentEditor?.(agent);$('agent-grant-description').textContent=agent?.grant&&agent.grant.kind!=='standing'?'This saved grant keeps its reviewed run count and access expiry.':mode==='ad_hoc'?'Run once when you approve it.':'Recurring work uses a daily run allowance.'; $('agent-form-error').hidden = true; $('save-agent').disabled = false; updateAgentMode(); $('agent-editor').showModal(); $('agent-title').focus();
  }
  function updateAgentMode() {
    const mode = document.querySelector('[name="agent-mode"]:checked').value, minimum = mode === 'ongoing' ? 10 : 60, interval = $('agent-interval'); interval.min = String(minimum); interval.required = mode !== 'ad_hoc'; if (Number(interval.value) < minimum) interval.value = minimum;
    $('agent-max-runs').disabled = mode === 'ad_hoc'; if (mode === 'ad_hoc') $('agent-max-runs').value = '1'; $('agent-cadence-field').hidden = mode === 'ad_hoc'; $('agent-cadence-label').textContent = mode === 'ongoing' ? 'Check every' : 'Run every'; $('agent-cadence-help').textContent = `Minimum ${minimum} seconds.`;
    $('agent-mode-description').textContent = workspaceExtension.agentModeDescription?.(mode) || (mode === 'ad_hoc' ? 'Runs once when you choose Run once.' : 'Runs on the saved schedule when its inputs are ready.');
    workspaceExtension.updateAgentEditor?.();
  }
  async function saveAgent(event) {
    event.preventDefault(); if (agentMutation) return; const form = $('agent-form'); if (!form.reportValidity()) return;
    const mode = document.querySelector('[name="agent-mode"]:checked').value, payload = { title: $('agent-title').value.trim(), instruction: $('agent-instruction').value.trim(), dataset: $('agent-dataset').value, mode, interval_seconds: mode === 'ad_hoc' ? 60 : Number($('agent-interval').value), max_runs: Number($('agent-max-runs').value), ...workspaceExtension.agentEditorData?.(), output: 'report' };
    if (!payload.title || !payload.instruction) { $('agent-form-error').textContent = 'Add a title and instructions to save this agent.'; $('agent-form-error').hidden = false; return; }
    agentMutation = true; $('save-agent').disabled = true; $('agent-form-error').hidden = true;
    try { const response = await request(agentEditingId ? `/api/agents/${encodeURIComponent(agentEditingId)}/update` : '/api/agents', { ...payload, ...(agentEditingId ? { expected_revision: agentEditingRevision } : {}) }), data = await response.json(), agent = data.agent || data; selectedAgentId = agent.id || agentEditingId; agentDetailSignature = null; $('agent-editor').close(); await loadAgents(); }
    catch (error) { $('agent-form-error').textContent = error.message; $('agent-form-error').hidden = false; }
    finally { agentMutation = false; $('save-agent').disabled = false; }
  }
  async function agentAction(agent, action) {
    if(agentMutation||observing()||!session?.can_query||action!=='pause'&&!agentExecutionReady(agent))return;const generation=sessionGeneration,state=agentInfo;agentMutation=true;state?.body.querySelectorAll('button:not(.phone-block button)').forEach(button=>{button.disabled=true;});
    try{await request(`/api/agents/${encodeURIComponent(agent.id)}/${action}`,action==='pause'?{}:{expected_revision:agent.revision});if(generation!==sessionGeneration)return;agentsError='';await loadAgents();if(state?.current()){state.signature=null;const response=await fetch(`/api/agents/${encodeURIComponent(agent.id)}`);if(!response.ok)throw new Error('Refresh this agent to confirm its state.');const data=await response.json();if(!state.current())return;Object.assign(agent,data.agent||data);refreshAgentInfo();}}
    catch(error){if(generation===sessionGeneration){agentsError=error.message;if(state?.current()){state.signature=null;refreshAgentInfo();state.body.append(node('p','agent-form-error',error.message));}else notice(error.message);}}
    finally{agentMutation=false;if(generation===sessionGeneration){renderAgentHeader();}}
  }
  function resizeComposer() { persistDraft(); elements.message.style.height = 'auto'; elements.message.style.height = `${Math.min(elements.message.scrollHeight, 180)}px`; }
  function updateMode() {
    renderContextRing();renderFundingNotice();workspaceExtension.renderChatContext?.();
    elements.mode.value = 'chat'; elements.dataset.closest('label').hidden = true;
    const drafts = attachmentDrafts.get(activeId) || [], hasContent = elements.message.value.trim() || drafts.length || workspaceExtension.hasDraft?.(activeId), busy = isBusy();
    const dataset = activeChat()?.dataset || ''; elements.dataset.value = dataset; $('workspace-label').textContent = dataset ? scopeLabel(dataset) : workspaceExtension.collectionPrompt?.() || 'Choose a collection'; $('environment-label').textContent = demoEnvironment() ? 'Company' : session?.environment?.name || 'Company';
    $('attachment-toggle').hidden = homeMode === 'search'; if (homeMode === 'search') toggleAttachmentMenu(false); workspaceExtension.setComposerReadOnly?.(observing()||draftAdmitting());elements.message.readOnly = observing() || Boolean(running.get(activeId)?.admitting); $('attachment-toggle').disabled = observing()||draftAdmitting();for(const remove of document.querySelectorAll('#attachment-previews button[aria-label^="Remove"],#selected-skills button[aria-label^="Remove"]'))remove.disabled=observing()||draftAdmitting(); if (observing()) { elements.send.disabled = true; elements.message.placeholder = 'Viewing this person’s workspace'; return; } elements.message.placeholder = workspaceExtension.composerPlaceholder?.(homeMode) || 'Ask anything…';
    if(workspaceExtension.updateAlternateComposer?.())return;
    $('composer-note').textContent = busy ? 'Write a follow-up while Rocoto works.' : workspaceExtension.composerNote || 'Ask a question or describe a task.';
    elements.send.setAttribute('aria-label', busy && !hasContent ? 'Stop response' : busy ? 'Queue message' : 'Send message'); elements.send.replaceChildren(icon(busy && !hasContent ? 'stop' : 'arrow')); elements.send.disabled = drafts.some((item) => item.uploading) || Boolean(running.get(activeId)?.admitting);
  }
  function renderAttachments() {
    persistDraft();
    renderSelectedSkills(); workspaceExtension.renderComposerAttachments?.();
    const container = $('attachment-previews'), drafts = attachmentDrafts.get(activeId) || []; container.replaceChildren(); container.hidden = !drafts.length || homeMode === 'search';
    drafts.forEach((item) => { const preview = node('div', 'attachment-preview'), img = node('img'); img.src = item.preview || item.url; img.alt = item.name || 'Attached image'; const remove = node('button', 'icon-button'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove attachment ${item.name}`); remove.append(icon('close')); remove.disabled=draftAdmitting();remove.addEventListener('click', () => { if(draftAdmitting())return;attachmentDrafts.set(activeId, (attachmentDrafts.get(activeId) || []).filter((other) => other.localId !== item.localId)); renderAttachments(); updateMode(); }); preview.append(img, remove, node('span', '', item.uploading ? 'Uploading' : item.name)); container.append(preview); });
  }
  async function pasteImages(event) {
    if (homeMode === 'search') return;
    const files = Array.from(event.clipboardData?.items || []).filter((item) => item.kind === 'file' && item.type.startsWith('image/')).map((item) => item.getAsFile()).filter(Boolean); if (!files.length) return; event.preventDefault();
    await uploadImages(files);
  }
  async function uploadImages(files) {
    if(draftAdmitting())return;
    if (!session?.can_query) { showSession('Enter your access code to attach images.'); return; }
    const chat = activeChat() || newChat(), generation=sessionGeneration;
    for (const file of files) {
      const item = { localId: uuid(), name: file.name || 'Pasted image', uploading: true, preview: '' }; const drafts = attachmentDrafts.get(chat.id) || []; drafts.push(item); attachmentDrafts.set(chat.id, drafts);
      const reader = new FileReader(); reader.onload = () => { if(generation!==sessionGeneration)return;item.preview = reader.result; if (chat.id === activeId) renderAttachments(); }; reader.readAsDataURL(file); renderAttachments(); updateMode();
      try {
        const form = new FormData(); form.append('file', file, item.name); const response = await fetch('/api/attachments', { method: 'POST', headers: { 'X-Rocoto-Page': '1' }, body: form }); if (!response.ok) throw new Error('Image upload ended. Paste the image again to retry.'); const data = await response.json(); if(generation!==sessionGeneration)return;if (!data.id || !data.url) throw new Error('Image upload is still preparing. Paste the image again to retry.'); Object.assign(item, data, { uploading: false });
      } catch (error) { if(generation!==sessionGeneration)return;attachmentDrafts.set(chat.id, (attachmentDrafts.get(chat.id) || []).filter((other) => other.localId !== item.localId)); notice(error.message); }
      persistDraft(chat.id,false);if (chat.id === activeId) { renderAttachments(); updateMode(); }
    }
  }

  function toggleAttachmentMenu(open, restoreFocus = false) { if(open&&draftAdmitting())return; $('attachment-menu').hidden = !open; $('attachment-toggle').setAttribute('aria-expanded', String(open)); if (open) { closeCommandMenu(); renderAttachmentConnections(); if (!connectionCatalogue.length) loadConnectionCatalogue(); } else { closeSkillsSubmenu(); attachmentMention = null; menuIndex = -1; elements.message.removeAttribute('aria-activedescendant'); if (restoreFocus) $('attachment-toggle').focus(); } }

  const steeringQueued = new Set();
  let queueRenderState = [];
  function queueIcon(name, className = '') {
    const svg = icon('', className), path = document.createElementNS(svg.namespaceURI, 'path'); svg.setAttribute('viewBox', '0 0 24 24');
    path.setAttribute('d', { queued: 'M4 4v12a3 3 0 0 0 3 3h13m-4-4 4 4-4 4M10 5h8M10 10h5', steer: 'M4 5v5a4 4 0 0 0 4 4h12m-5-5 5 5-5 5', trash: 'M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7M14 10v7', more: 'M5 12h.01M12 12h.01M19 12h.01' }[name]);
    if (name === 'more') path.setAttribute('stroke-width', '3');
    svg.replaceChildren(path); return svg;
  }
  function queueWritable(chatId = activeId) {
    const chat = chats.find(item => item.id === chatId);
    return Boolean(session?.can_query && !observing() && chat && !chat.parent_id && !chat.read_only && visibleChats().includes(chat));
  }
  function queueEditable(item) {
    return queueWritable(item.chat_id) && activeId === item.chat_id && queued.includes(item) && !steeringQueued.has(item) && !elements.message.value && !attachmentDrafts.get(activeId)?.length && !workspaceExtension.hasDraft?.(activeId) && !activeChat()?.skill_drafts?.length;
  }
  function editQueuedMessage(item) {
    if (!queueEditable(item)) return;
    const chat = activeChat(), attachments = (item.attachments || []).map(attachment => ({ ...attachment, localId: uuid(), uploading: false }));
    // Move the complete queued message together before rendering or admitting another send.
    attachmentDrafts.set(chat.id, attachments); workspaceExtension.editQueued?.(chat.id,item); chat.skill_drafts = [...(item.skill_ids || [])];
    elements.message.value = item.query; textDrafts.set(chat.id, item.query); queued.splice(queued.indexOf(item), 1);
    saveQueue(); workspaceExtension.saveDrafts?.(); save(); renderAttachments(); renderQueue(); resizeComposer(); updateMode(); elements.message.focus();
  }
  function queuedThumbnail(item) {
    const attachment=(item.attachments || []).find(value=>value?.url);
    if(!attachment)return workspaceExtension.queuedThumbnail?.(item) || null;
    const value=attachment.url;
    try { const url=new URL(value,location.href);if(url.origin!==location.origin || url.username || url.password || !/^\/attachments\//.test(url.pathname))return null; } catch{return null;}
    const image=node('img','queued-thumbnail');image.src=value;image.alt=attachment.name || 'Attached image';return image;
  }
  function renderQueue() {
    const container = $('message-queue'), visible = queued.filter(item => item.chat_id === activeId);
    const state = [activeId, homeMode, sessionGeneration, queueWritable(), observing(), Boolean(activeChat()?.read_only), Boolean(activeChat()?.parent_id), isBusy(), ...visible.flatMap(item => [item, steeringQueued.has(item)])];
    if (state.length === queueRenderState.length && state.every((value, index) => value === queueRenderState[index])) return;
    queueRenderState = state; container.replaceChildren(); container.hidden = !visible.length || homeMode === 'search';
    container.setAttribute('aria-label', `${visible.length} ${visible.length === 1 ? 'message' : 'messages'} queued`);
    if (!visible.length) return;
    if (!isBusy()) { const heading = node('div', 'queue-heading'), next = node('button', 'text-button', 'Send next'); heading.append(node('span', '', `${visible.length} ${visible.length === 1 ? 'message' : 'messages'} queued`)); next.type = 'button'; next.disabled = !queueWritable() || steeringQueued.has(visible[0]); next.addEventListener('click', () => drainQueue(visible[0].id)); heading.append(next); container.append(heading); }
    for (const item of visible) {
      const row = node('div', 'queued-message'), text = node('span', 'queued-text', item.query), remove = node('button', 'icon-button queued-remove'), more = node('button', 'icon-button queued-more');
      const pending = steeringQueued.has(item); row.append(queueIcon('queued', 'queued-leading-icon')); const thumbnail = queuedThumbnail(item); if (thumbnail) row.append(thumbnail); text.title = item.query; row.append(text);
      if (isBusy(item.chat_id)) { const steer = node('button', 'text-button steer-button'); steer.type = 'button'; steer.append(queueIcon('steer'), node('span', '', 'Steer')); steer.disabled = pending || !queueWritable(item.chat_id); steer.addEventListener('click', () => steerMessage(item)); row.append(steer); }
      remove.type = 'button'; remove.disabled = pending || !queueWritable(item.chat_id); remove.setAttribute('aria-label', `Remove queued message: ${item.query.slice(0, 60)}`); remove.append(queueIcon('trash')); remove.addEventListener('click', () => { if (steeringQueued.has(item) || !queued.includes(item) || !queueWritable(item.chat_id)) return; queued.splice(queued.indexOf(item), 1); saveQueue(); renderQueue(); });
      const menu = node('div', 'queued-menu'), edit = node('button', 'queued-edit'); menu.setAttribute('popover', 'auto'); edit.type = 'button'; edit.setAttribute('aria-label', 'Edit queued message'); edit.append(icon('edit'), node('span', '', 'Edit')); edit.addEventListener('click', () => { menu.hidePopover(); editQueuedMessage(item); }); menu.append(edit);
      more.type = 'button'; more.setAttribute('aria-label', 'Queued message options'); more.setAttribute('aria-expanded', 'false'); more.append(queueIcon('more')); more.addEventListener('click', () => { if (menu.matches(':popover-open')) { menu.hidePopover(); return; } edit.disabled = !queueEditable(item); edit.title = edit.disabled ? 'Send or clear the current draft to edit a queued message.' : ''; const rect = more.getBoundingClientRect(); menu.style.left = `${Math.max(8, Math.min(rect.right - 168, innerWidth - 176))}px`; menu.style.top = `${rect.bottom + 4}px`; menu.showPopover(); }); menu.addEventListener('toggle', event => more.setAttribute('aria-expanded', String(event.newState === 'open')));
      row.append(remove, more, menu); container.append(row);
    }
  }
  function drainQueue(id, chatId = activeId) { const chat = chats.find((candidate) => candidate.id === chatId); if (!chat || !queueWritable(chatId) || isBusy(chatId) || !queued.length) return; if (!session.can_query) { if (id) showSession('Enter your access code to send this queued message.'); return; } const index = id ? queued.findIndex((item) => item.id === id && item.chat_id === chatId) : queued.findIndex((item) => item.chat_id === chatId); if (index < 0 || steeringQueued.has(queued[index])) return; const [item] = queued.splice(index, 1); saveQueue(); renderQueue(); runQuery(chat, item.query, 'chat', item.dataset ?? chat.dataset ?? '', item.attachments || [], workspaceExtension.messageData?.(item) || {}, item.skill_ids || []); }
  async function steerMessage(item) {
    if (!session?.can_query) { showSession('Enter your access code to continue this investigation.'); return; }
    if (!queueWritable(item.chat_id) || !queued.includes(item) || steeringQueued.has(item) || !isBusy(item.chat_id)) return;
    const generation = sessionGeneration; steeringQueued.add(item); renderQueue();
    try {
      await request(`/api/chat/${encodeURIComponent(item.chat_id)}/steer`, { message: item.query, attachments: (item.attachments || []).map(attachment => attachment.id), ...workspaceExtension.requestData?.(item), skill_ids: item.skill_ids || [] });
      if (generation !== sessionGeneration) return;
      queued = queued.filter(other => other !== item); saveQueue(); const chat = chats.find(candidate => candidate.id === item.chat_id);
      if (chat) { const message = { id: uuid(), role: 'user', created_at: new Date().toISOString(), text: item.query, attachments: item.attachments || [], ...workspaceExtension.messageData?.(item), skill_ids: item.skill_ids || [], steer: true }; chat.messages.push(message); save(); if (activeId === chat.id) updateMessage(chat, message); }
    } catch (error) { if (generation === sessionGeneration) notice(error.message); }
    finally { steeringQueued.delete(item); if (generation === sessionGeneration) renderQueue(); }
  }
  async function stopChat(chatId = activeId) { const operation = running.get(chatId); if (!isBusy(chatId) || operation?.stopRequested) return; if (operation) operation.stopRequested = true; try { await request(`/api/chat/${encodeURIComponent(chatId)}/stop`, {}); if (operation) operation.answer.stopped = true; else { const chat = chats.find((item) => item.id === chatId); if (chat) { chat.remote_running = false; renderSidebar(); renderQueue(); updateMode(); } } } catch (error) { if (operation) operation.stopRequested = false; notice(error.message); } }
  async function request(url, data) {
    const response = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Rocoto-Page': '1' }, body: JSON.stringify(data) });
    if (!response.ok) { let detail = ''; try { const error = await response.json(); detail = error.detail || error.message || ''; if (typeof detail !== 'string') detail = 'Please check your message and try again.'; } catch {} if (response.status === 401 && !url.startsWith('/api/session')) showSession('Open your workspace to continue.'); throw new Error(detail || `Request ended with status ${response.status}.`); }
    return response;
  }
  async function submit(event) {
    event.preventDefault(); if (activeChat()?.parent_id) return; if (homeMode === 'search') { await workspaceExtension.submitAlternate?.(); return; } let query = elements.message.value.trim(); const drafts = attachmentDrafts.get(activeId) || []; if (drafts.some((item) => item.uploading)) return;
    const extras = workspaceExtension.composerData?.(activeId) || {}, skillIds = [...(activeChat()?.skill_drafts || [])]; if (!query && !drafts.length && !workspaceExtension.hasDraft?.(activeId)) { if (isBusy()) await stopChat(); return; }
    if (demoEnvironment() && !activeChat()?.dataset) { if(founderAllowed())openFounder();return; }
    if (!session?.can_query) { showSession('Enter your access code to open this workspace.'); return; }
    const chat = activeChat() || newChat(), attachments = drafts.map(({ id, url, name, mime }) => ({ id, url, name, mime }));persistDraft();
    if(fundingNotice()){renderFundingNotice();return;}
    query ||= workspaceExtension.defaultMessage?.(extras) || 'Look at the attached image.';toggleAttachmentMenu(false);
    if (isBusy(chat.id) || queued.some((item) => item.chat_id === chat.id)) { if(running.get(chat.id)?.admitting)return;queued.push({ id: uuid(), chat_id: chat.id, query, attachments, ...extras, skill_ids: skillIds, dataset: chat.dataset || '' }); saveQueue();clearSentDraft(chat.id);renderQueue(); if (!isBusy(chat.id)) drainQueue(); return; }
    await runQuery(chat, query, 'chat', chat.dataset || '', attachments, extras, skillIds, true);
  }
  function continueAgentRun(report) {
    const chat = { id: uuid(), title: 'New chat', kind: 'chat', messages: [], thread_id: null, dataset: report.dataset || '', environment_id: report.environment_id, owner_principal_id: report.owner_principal_id || principalId(), continuation_of: report.id, continuation_title: report.title, followup_run_chat_id: report.id, updated: Date.now() };
    chats.unshift(chat);
    // Messages queued on the report continue in one new conversation, in their original order.
    queued.forEach((item) => { if (item.chat_id === report.id) item.chat_id = chat.id; }); saveQueue();
    chat.skill_drafts = report.skill_drafts || []; report.skill_drafts = [];
    const attachments = attachmentDrafts.get(report.id); if (attachments) { attachmentDrafts.set(chat.id, attachments); attachmentDrafts.delete(report.id); }
    workspaceExtension.moveDraft?.(report.id,chat.id);
    const draft = activeId === report.id ? elements.message.value : textDrafts.get(report.id); if (draft) textDrafts.set(chat.id, draft); textDrafts.delete(report.id);
    if (activeId === report.id) chooseChat(chat); else renderSidebar(); textDrafts.delete(report.id); save(); return chat;
  }
  async function runQuery(chat, query, mode, dataset, attachments = [], extras = {}, skillIds = [], composerDraft = false) {
    mode = 'chat';
    if (chat.parent_id) return;
    if (chat.kind === 'agent_run' && !isBusy(chat.id)) { chat = continueAgentRun(chat); dataset = chat.dataset; }
    if (!chat.messages.length && chat.kind!=='organization_chat') chat.title = 'New chat';
    const user = { id: uuid(), role: 'user', created_at: new Date().toISOString(), text: query, attachments, ...extras, skill_ids: skillIds }, answer = { id: uuid(), role: 'assistant', text: '', tools: [], results: [], pending: true, mode, query };
    const generation=sessionGeneration,operation = { answer, stopRequested: false, admitting:composerDraft }; running.set(chat.id, operation); chat.remote_running = false; chat.messages.push(user, answer); chat.turn_anchor={user:user.id,answer:answer.id}; chat.unread_completion=false; chat.updated = Date.now(); updateMode(); if (chat.id === activeId) { render(); anchorSubmittedTurn(chat); } else renderSidebar(); save();
    try {
        const response = await request('/api/chat', { message: query, chat_id: chat.id, attachments: attachments.map((item) => item.id), ...workspaceExtension.requestData?.(extras), skill_ids: skillIds, ...(dataset ? { dataset } : {}), ...(chat.agent_setup && !chat.participant ? { agent_setup: chat.agent_setup } : {}), ...(chat.thread_id ? { thread_id: chat.thread_id } : chat.followup_run_chat_id ? { followup_run_chat_id: chat.followup_run_chat_id } : {}) });
        if(generation!==sessionGeneration)return;operation.admitting=false;if(composerDraft)clearSentDraft(chat.id);updateMode();chat.server_saved = true; if (!response.body) throw new Error('The response stream ended before it opened.');
        const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '', doneEvent = false;
        function receive(line) {
          if (!line.trim() || generation!==sessionGeneration) return; const event = JSON.parse(line);
          if(event.type==='agent'){const agent=agentCardMetadata(event.agent);if(agent){answer.agents ||= [];const index=answer.agents.findIndex(item=>item.id===agent.id);if(index<0)answer.agents.push(agent);else answer.agents[index]=agent;if(agent.chat_id===chat.id){chat.kind='agent_conversation';chat.title=agent.title;chat.agent_id=agent.id;chat.agent_setup={agent_id:agent.id,mode:agent.mode};if(window.RocotoHarnessShell.classFor(harness,event.agent.agent_kind))chat.agent_kind=event.agent.agent_kind;if(activeId===chat.id&&currentView==='chat'){selectedAgentId=agent.id;const url=new URL(location.href);url.searchParams.delete('chat');url.searchParams.set('agents','1');url.searchParams.set('agent',agent.id);history.replaceState({},'',url);render();loadAgents();}}}}
          if(event.type==='channel_delivery'){answer.channel_delivery=event.delivery;chat.waiting_for_reply=Boolean(event.delivery?.waiting_for_reply);}
          if(event.type==='business_map')answer.business_map=businessMapMetadata(event.map);
          workspaceExtension.receiveEvent?.(event,answer);
          if (event.type === 'timing') { for (const key of ['started_at', 'completed_at', 'elapsed_ms']) if (event[key] !== undefined) answer[key]=event[key]; }
          if (['approval_request','approval_resolved'].includes(event.type)) { const request=nativeApprovalMetadata(event.request);if(request){answer.native_approvals ||= [];const index=answer.native_approvals.findIndex(item=>item.id===request.id);if(index<0)answer.native_approvals.push(request);else answer.native_approvals[index]=request;} }
          if(event.type==='user_input_request'){chat.unread_question=true;renderSidebar();}
          if (['user_input_request','user_input_resolved'].includes(event.type)) { const request = nativeQuestionMetadata(event.request); if (request) { answer.native_questions ||= []; const index = answer.native_questions.findIndex(item => item.id === request.id); if (index < 0) answer.native_questions.push(request); else answer.native_questions[index] = request; } }
          if (['credential_request', 'credential_request_resolved'].includes(event.type)) { const request=credentialMetadata(event.request); if(request) { answer.credential_requests ||= []; const index=answer.credential_requests.findIndex(item=>item.id===request.id); if(index<0)answer.credential_requests.push(request);else answer.credential_requests[index]=request; } }
          if (event.type === 'artifact' && event.artifact?.id) { answer.artifacts ||= []; const index=answer.artifacts.findIndex(file=>file.id===event.artifact.id); if(index<0)answer.artifacts.push(event.artifact);else answer.artifacts[index]=event.artifact; }
          if (event.type === 'tool_start') answer.tools.push({ id: event.id, name: event.name, arguments: event.arguments, status: 'running' });
          if (event.type === 'tool_end') { const tool = [...answer.tools].reverse().find((item) => item.status === 'running' && (event.id ? item.id === event.id : item.name === event.name)); if (tool) { tool.status = event.success ? 'complete' : 'ended'; if (typeof event.output === 'string') tool.output = event.output; tool.output_truncated = Boolean(event.output_truncated); Object.assign(tool,workspaceExtension.toolData?.({...tool,...event})); } }
          if (event.type === 'results') { const known = new Map(answer.results.map((result) => [result.id, result])); for (const result of event.results || []) if (!known.has(result.id)) { answer.results.push(result); known.set(result.id, result); } else Object.assign(known.get(result.id), result); }
          if (event.type === 'title' && typeof event.title === 'string' && event.title.trim()) { chat.title = event.title.trim(); if (chat.id === activeId && currentView==='chat'){if(conversationAgentId(chat))detailBreadcrumb('Agents',chat.title,()=>showAgents());else elements['chat-title'].textContent=chat.title;} renderSidebar(); }
          if (event.type === 'delta') answer.text += event.text || '';
          if (event.type === 'progress') { answer.progress ||= []; const entry = answer.progress.find((item) => item.id === event.item_id); if (entry) entry.text += event.text || ''; else answer.progress.push({ id: event.item_id, text: event.text || '', activity: event.activity }); if (entry && event.activity) entry.activity = event.activity; }
          if (event.type === 'usage' && event.context) { chat.context = event.context; renderContextRing(); }
          if (event.type === 'subchat') registerChild(event, answer, chat);
          if (event.type === 'done') { operation.completed=event.status==='completed'; if(operation.completed)chat.unread_completion=true;for (const key of ['started_at', 'completed_at', 'elapsed_ms']) if (event[key] !== undefined) answer[key]=event[key]; doneEvent = true; if (event.thread_id) chat.thread_id = event.thread_id; if (event.status === 'interrupted') answer.stopped = true; }
          if (event.type === 'error') throw new Error(event.message || 'The agent run ended. Try your question again.');
          updateMessage(chat, answer);
        }
        try { for (;;) { const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true }); let boundary; while ((boundary = buffer.indexOf('\n')) >= 0) { const line = buffer.slice(0, boundary); buffer = buffer.slice(boundary + 1); receive(line); } } buffer += decoder.decode(); if (buffer.trim()) receive(buffer); }
        catch (error) { await reader.cancel().catch(() => {}); throw error; }
        if (!doneEvent && !operation.stopRequested) throw new Error('Your partial answer is saved. Reconnect to continue.');
    } catch (error) { if (!operation.stopRequested) { answer.error = true; answer.text = [answer.text, error instanceof Error ? error.message : 'The request ended. Try again.'].filter(Boolean).join('\n\n'); } }
    finally { answer.pending = false; if (operation.stopRequested) answer.stopped = true; if(running.get(chat.id)===operation)running.delete(chat.id); if(generation===sessionGeneration){if(operation.completed&&!answer.error&&!answer.stopped)window.RocotoWorkspaceSettings.notify(browserPreferences,chat.id,()=>{if(generation===sessionGeneration)openChatId(chat.id);});updateMode(); updateMessage(chat, answer); save(); renderSidebar(); renderQueue();refreshUsage();} if (generation===sessionGeneration && !answer.error && !answer.stopped) drainQueue(null, chat.id); }
  }
  function showSession(message = '') { persistDraft();workspaceExtension.resetScope?.();resetBusinessMaps();pauseMedia(document);resetMcp(); closeCredentialRequest(false); closeNativeQuestion(false); clearCustomCredentialInputs(); clearConnectionCredential(); $('custom-connector-form').reset(); clearTimeout(statusTimer); clearTimeout(agentsTimer); document.querySelectorAll('dialog[open]').forEach((dialog) => dialog.close()); $('business-chat-drawer').hidden = true; $('business-chat-frame').removeAttribute('src'); session = null;browserPreferences=window.RocotoWorkspaceSettings.defaults;window.RocotoWorkspaceSettings.apply(browserPreferences); document.body.classList.add('session-pending'); $('session-screen').hidden = false; $('session-message').textContent = message; }
  function renderOperator() { const operator = session?.operator || status?.operator, name = session?.principal?.display_name || operator?.name || session?.environment?.name || 'Rocoto'; $('account-button').setAttribute('aria-label', `${name} account menu`); $('account-button').title = `${name} account menu`; $('workspace-owner').textContent = name; $('workspace-avatar').textContent = name.charAt(0); $('workspace-person').textContent = demoEnvironment() ? 'Demo Person' : session?.environment?.name || 'Workspace'; }
  function contextText() { const context = activeChat()?.context; if (!context || !Number.isFinite(context.used_tokens)) return 'Available after a response'; const used = new Intl.NumberFormat().format(context.used_tokens); return Number.isFinite(context.window_tokens) && context.window_tokens > 0 ? `${used} of ${new Intl.NumberFormat().format(context.window_tokens)} tokens · latest model request` : `${used} tokens · latest model request`; }
  function renderContextRing() { const context = activeChat()?.context, ratio = context && Number.isFinite(context.used_tokens) && Number.isFinite(context.window_tokens) && context.window_tokens > 0 ? Math.max(0, Math.min(1, context.used_tokens / context.window_tokens)) : 0, circle = $('context-ring').querySelector('.context-value'); circle.style.strokeDasharray = `${ratio * 50.265} 50.265`; $('context-tooltip').textContent = contextText(); $('context-ring').setAttribute('aria-label', `Context and usage: ${contextText()}`); }
  function preferencesIdentity(){return session?.principal?.id&&session?.environment?.id?JSON.stringify([session.environment.id,session.principal.id]):'';}
  function savePreferences(value){const identity=preferencesIdentity();if(!identity)return;if(!window.RocotoWorkspaceSettings.write(identity,value)){notice('Keep this page open to retain these settings. Browser storage is full.');return;}browserPreferences=value;window.RocotoWorkspaceSettings.apply(value);renderWorkspaceSettings();}
  function teamMembers(){
    if(!session)return[];const id=session.environment?.id,members=new Map(),own=session.environments?.find(row=>row.id===id);
    if(session.principal?.id&&own)members.set(session.principal.id,{id:session.principal.id,is_self:true,display_name:session.principal.display_name,role:own.role||session.role});
    if(session.role==='owner'&&!observing())for(const person of observers){const membership=person.environments?.find(row=>row.id===id);if(membership)members.set(person.id,{id:person.id,is_self:person.id===session.principal?.id,display_name:person.display_name,role:membership.role});}
    return [...members.values()];
  }
  function renderWorkspaceSettings(){
    if(currentView==='messaging'){renderMessagingSettings();return;}if(!['appearance','notifications','team','billing'].includes(currentView))return;const identity=preferencesIdentity();
    $(currentView+'-view').replaceChildren(window.RocotoWorkspaceSettings.render(currentView,{preferences:browserPreferences,members:teamMembers(),loading:teamLoading,error:teamError,billing:usageData?.billing},{node,icon,onChange:value=>{if(identity===preferencesIdentity())savePreferences(value);},onPermission:async()=>{const result=await window.RocotoWorkspaceSettings.requestPermission();if(identity!==preferencesIdentity())return;if(result==='granted')savePreferences({...browserPreferences,desktopNotifications:true});else renderWorkspaceSettings();},onUsage:()=>openUsage(),onMember:member=>openMemberProfile(member)}));
  }
  function showWorkspaceSettings(view){persistDraft();setProfileMenu(false);setUsagePopover(false);currentView=view;history.replaceState({},'',`/?settings=${view}`);render();setSidebar(false);if(['team','messaging'].includes(view)&&session?.role==='owner'&&!observing())loadObservers();if(view==='messaging')loadMessaging();}
  function messagingScope(){return currentCollection()||'company';}
  function messagingViewData(data=messagingData){return {...(data||{}),principal_id:session?.principal?.id,environment_id:session?.environment?.id,members:data?.members?.map(member=>({...member,display_name:member.display_name||member.name}))||teamMembers(),loading:messagingLoading,error:messagingError};}
  function renderMessagingSettings(){
    const root=$('messaging-view');root.replaceChildren(window.RocotoMessaging.render(messagingViewData(),{node,icon,onMember:(member,page,channel)=>openMemberProfile(member,page,channel),onSetup:memberSetupPurpose()?()=>openOrganizationChat(memberSetupPurpose()):undefined}));
    if(messagingError)root.append(node('p','workspace-settings-note',messagingError));
  }
  async function loadMessaging(memberId=session?.principal?.id){
    const generation=sessionGeneration,epoch=++messagingEpoch;messagingLoading=true;messagingError='';if(currentView==='messaging')renderMessagingSettings();
    try{const response=await fetch(`/api/messaging?collection=${encodeURIComponent(messagingScope())}&member_id=${encodeURIComponent(memberId||'')}`,{cache:'no-store'});if(!response.ok)throw Error('Messaging is reconnecting. Try again.');const data=await response.json();if(generation!==sessionGeneration||epoch!==messagingEpoch)return null;if(memberId===session?.principal?.id)messagingData=data;return data;}
    catch(error){if(generation===sessionGeneration&&epoch===messagingEpoch)messagingError=error.message;return null;}
    finally{if(generation===sessionGeneration&&epoch===messagingEpoch){messagingLoading=false;if(currentView==='messaging')renderMessagingSettings();}}
  }
  async function openMemberProfile(member,page='profile',channel=''){
    if(!member?.id)return;memberDialog?.close();const generation=sessionGeneration,collection=messagingScope();const current=()=>generation===sessionGeneration&&collection===messagingScope();
    const mutate=async(path,payload)=>{if(!current())throw Error('Open your current workspace to continue.');const response=await request(path,{...payload,collection});if(!current())throw Error('Open your current workspace to continue.');return response.json();};
    const controller=window.RocotoMessaging.openMember({member,data:messagingViewData(member.is_self?messagingData:{}),page,channel,node,icon,onClose:()=>{if(memberDialog===controller)memberDialog=null;},onRequestCode:payload=>mutate('/api/messaging/verification',payload),onVerifyCode:payload=>mutate('/api/messaging/verification/confirm',payload),onSetup:async(person)=>{if(!current())return;const data=await loadMessaging(person.id);if(!current())return;if(data?.setup_chat_id)await openChatId(data.setup_chat_id);else await openOrganizationChat(memberSetupPurpose()||'organization',collection);if(person.is_self){try{await mutate('/api/messaging/hello',{member_id:person.id,request_id:uuid()});await loadServerChats();if(data?.setup_chat_id)pollChild(data.setup_chat_id);}catch(error){notice(error.message);}}}});
    memberDialog=controller;const data=await loadMessaging(member.id);if(current()&&memberDialog===controller&&data)controller.update(messagingViewData(data));
  }
  async function openOrganizationChat(purpose,collection=currentCollection()||'company'){
    if(observing()){await loadServerChats();const chat=visibleChats().find(item=>item.kind==='organization_chat'&&item.organization_purpose===purpose&&item.dataset===collection);if(chat)chooseChat(chat);return;}
    if(!session?.can_query)return;const generation=sessionGeneration;
    try{const response=await request('/api/organization/chats',{purpose,collection}),data=await response.json();if(generation!==sessionGeneration)return;const chat=mergeRemoteChat(data.chat||data);if(!chat)throw Error('Open this conversation from its company workspace.');chooseChat(chat);save();return chat;}catch(error){if(generation===sessionGeneration)notice(error.message);}
  }
  function renderProfileSettings(){
    const scope='person';$('profile-view').replaceChildren(window.RocotoProfileSettings.render(profileData,{node,icon,scope,mode:profileMode,loading:profileLoading,error:profileError,onMode:value=>{profileMode=value;},onAgent:id=>showAgents(id)}));
  }
  async function loadProfileSettings(){
    const view=currentView,scope='person',epoch=++profileEpoch,generation=sessionGeneration;profileData=null;profileLoading=true;profileError='';renderProfileSettings();
    try{const response=await fetch(`/api/profile/activity?scope=${scope}&days=364`,{cache:'no-store'});if(!response.ok)throw new Error(response.status===403?'Open your owner workspace to view organization activity.':'Profile activity is reconnecting. Try again.');const data=await response.json();if(epoch!==profileEpoch||generation!==sessionGeneration||currentView!==view)return;if(data.scope!==scope)throw new Error('Refresh this profile to load its activity.');profileData=data;organisationAllowed=data.organisation_allowed===true;}
    catch(error){if(epoch===profileEpoch&&generation===sessionGeneration)profileError=error.message;}
    finally{if(epoch===profileEpoch&&generation===sessionGeneration&&currentView===view){profileLoading=false;render();}}
  }
  function showProfileSettings(){persistDraft();setProfileMenu(false);setUsagePopover(false);currentView='profile';history.replaceState({},'',`/?settings=${currentView}`);render();setSidebar(false);loadProfileSettings();}
  function renderPluginSettings(){
    const plugins=[...new Set([...nativePlugins.map(item=>item.id),...nativePluginCleanup])].map(id=>({...connectionCatalogue.find(item=>item.id===id),...nativePlugins.find(item=>item.id===id),id,cleanup_pending:nativePluginCleanup.has(id)}));
    const mcps=brokerConnections.map(item=>({...item,id:item.provider||item.id})),marketplaces=[{id:'openai-curated-remote',name:'OpenAI plugins',description:'Original plugin directory'}];
    $('plugin-settings-view').replaceChildren(window.RocotoPluginSettings.render({plugins,apps:[],mcps,marketplaces,loading:catalogueLoading,error:nativePluginError||catalogueError,can_manage:pluginWritable()},{node,icon,logo:connectionLogo,onBrowse:()=>showMarketplace('connections'),onAdd:()=>showMarketplace('connections'),onOpen:(id,kind)=>{if(kind==='marketplaces'){showMarketplace('connections');return;}const item=kind==='plugins'?connectionCatalogue.find(item=>item.id===id):connectionCatalogue.find(item=>item.broker_id===id||!item.native_direct&&item.id===id);if(item)openConnectionDetail(item);}}));
  }
  function showPluginSettings(){persistDraft();setProfileMenu(false);currentView='plugins';history.replaceState({},'','/?settings=plugins');render();setSidebar(false);loadConnectionCatalogue();}
  function usageNumber(value){return value===null || value===undefined || value===''?NaN:Number(value);}
  function fundingNotice(){
    if(!usageData || observing() || !session?.can_query || homeMode==='search' || activeChat()?.parent_id)return '';
    const company=usageData.company||{},available=usageNumber(company.available_usd),monthly=usageNumber(company.monthly_available_usd),api=usageData.codex?.route==='OpenAI API';
    if(!api)return '';
    if(Number.isFinite(available)&&available<=0)return 'Your available credit has been used. Your draft is saved.';
    if(Number.isFinite(monthly)&&monthly<=0)return 'Your monthly limit has been reached. Your draft is saved.';
    return '';
  }
  function renderFundingNotice(){
    const banner=$('funding-notice'),text=fundingNotice();banner.hidden=!text;if(!text)return;
    if(banner.dataset.message===text)return;banner.dataset.message=text;const copy=node('span','',text),button=agentButton('View usage',openUsage,false,'chart');banner.replaceChildren(icon('chart'),copy,button);
  }
  async function refreshUsage() { const epoch=++usageEpoch;usageLoading = true; usageError = ''; renderUsagePopover(); const generation = sessionGeneration, chatId = activeId, saved=activeChat()?.server_saved; try { const response = await fetch(`/api/usage${chatId && saved ? `?chat_id=${encodeURIComponent(chatId)}` : ''}`); if (!response.ok) throw new Error('Usage is reconnecting. Try again.'); const data = await response.json(); if (generation !== sessionGeneration || epoch!==usageEpoch) return; usageData = data;usageCheckedAt=Date.now(); const chat = chats.find((item) => item.id === chatId); if (data.context && chat) { chat.context = data.context; renderContextRing(); } } catch (error) { if (generation === sessionGeneration && epoch===usageEpoch) usageError = error.message; } finally { if (generation === sessionGeneration && epoch===usageEpoch) { usageLoading = false; renderUsagePopover();renderFundingNotice(); if (currentView === 'usage') renderUsage(); } } }
  function setUsagePopover(open, restoreFocus = false) { clearTimeout(contextHoverTimer); $('usage-popover').hidden = !open; $('context-ring').setAttribute('aria-expanded', String(open)); $('context-tooltip').hidden = true; if (open) { renderUsagePopover(); refreshUsage(); $('usage-breakdown').focus(); } else if (restoreFocus) $('context-ring').focus(); }
  function usageBar(label, used, maximum, caption, suffix = '') { const section = node('section', 'compact-usage-section'), heading = node('div', 'compact-usage-heading'); heading.append(node('strong', '', label), node('span', '', suffix)); section.append(heading); if (Number.isFinite(used) && Number.isFinite(maximum) && maximum > 0) { const bar = node('progress'); bar.max = maximum; bar.value = Math.max(0, Math.min(maximum, used)); bar.setAttribute('aria-label', label); section.append(bar); } else { const track = node('div', 'usage-empty-track'); track.setAttribute('aria-label', 'Usage measurement pending'); section.append(track); } if (caption) section.append(node('small', '', caption)); return section; }
  function renderUsagePopover() { const container = $('usage-popover-content'); container.replaceChildren(); const context = activeChat()?.context, company = usageData?.company || {}, knownContext = Number.isFinite(context?.used_tokens) && Number.isFinite(context?.window_tokens) && context.window_tokens > 0;
    container.append(usageBar('Context window', context?.used_tokens, context?.window_tokens, knownContext ? `${new Intl.NumberFormat().format(context.used_tokens)} of ${new Intl.NumberFormat().format(context.window_tokens)} tokens · Latest message` : contextText(), knownContext ? `${new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(context.used_tokens)} / ${new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(context.window_tokens)} (${Math.round(context.used_tokens / context.window_tokens * 100)}%)` : ''));
    const used = company.monthly_used_usd == null ? NaN : Number(company.monthly_used_usd), limit = company.monthly_limit_usd == null ? NaN : Number(company.monthly_limit_usd), caption = Number.isFinite(used) && Number.isFinite(limit) ? `${money(used)} of ${money(limit)} used${Number(company.reserved_usd) ? ` · ${money(Number(company.reserved_usd))} reserved` : ''}` : usageError || (usageLoading ? 'Loading usage…' : 'Monthly usage appears when available'); container.append(usageBar('Monthly usage', used, limit, caption, Number.isFinite(limit) && limit > 0 ? `${Math.round(used / limit * 100)}%` : ''));
    for (const window of session?.operator?.mode === 'founder_demo' ? usageData?.codex?.limits || [] : []) { if (!Number.isFinite(window.used_percent)) continue; const duration = Number(window.window_minutes), name = duration === 10080 ? 'Weekly ChatGPT usage' : duration === 300 ? 'Session ChatGPT usage' : duration > 0 ? `ChatGPT · ${cadenceLabel(duration * 60)}` : window.name || 'ChatGPT usage'; container.append(usageBar(name, window.used_percent, 100, window.resets_at ? `Resets ${agentDate(window.resets_at)}` : '', `${window.used_percent}%`)); }
    const funded = Number(company.prepaid_funded_usd || 0) + Number(company.provider_credit_funded_usd || 0), available = company.available_usd == null ? NaN : Number(company.available_usd); container.append(usageBar('Credits remaining', available, funded, Number(company.provider_credit_funded_usd) ? `Includes ${money(Number(company.provider_credit_funded_usd))} provider promotional credit` : '', Number.isFinite(available) ? `${money(available)}${funded > 0 ? ` of ${money(funded)}` : ''}` : 'Awaiting report'));
  }
  async function openUsage() { setProfileMenu(false); setUsagePopover(false); if (observing()) await endObservation(); currentView = 'usage'; history.replaceState({}, '', '/?settings=usage'); render(); setSidebar(false); await refreshUsage(); }
  function renderUsage() { const data = usageData, content = $('usage-content'); content.replaceChildren(node('h1','usage-title','Usage'));  if (!data) { content.append(node('p', 'workspace-help', usageError || 'Loading usage…')); return; }     const company = data.company || {}, monthly=usageNumber(company.monthly_limit_usd),available=usageNumber(company.available_usd),funded=usageNumber(company.prepaid_funded_usd)+usageNumber(company.provider_credit_funded_usd||0),remaining=Number.isFinite(usageNumber(company.monthly_available_usd))?usageNumber(company.monthly_available_usd):Math.max(0,monthly-usageNumber(company.monthly_used_usd)-usageNumber(company.reserved_usd||0));content.append(usageBar('Monthly limit remaining',remaining,monthly,`${money(usageNumber(company.monthly_used_usd)||0)} used${company.monthly_reset_at?' · Resets '+agentDate(company.monthly_reset_at):''}`,Number.isFinite(monthly)&&monthly>0?`${Math.round(remaining/monthly*100)}% left`:'Awaiting allowance'),usageBar('Extra credit remaining',available,funded,Number(company.reserved_usd)?`${money(Number(company.reserved_usd))} reserved for current work`:'Available for new work',Number.isFinite(available)?money(available):'Awaiting report'));const stats = node('div', 'usage-stats'); for (const [key, label] of [['available_usd', 'Available credit'], ['reserved_usd', 'Reserved'], ['provider_actual_cost_usd', 'Observed provider cost'], ['retail_charged_usd', 'Customer charges']]) { const value = company[key], card = node('div'); card.append(node('span', '', label), node('strong', value===undefined||value===null?'usage-stat-unknown':'', value === undefined || value === null ? 'Awaiting report' : money(Number(value)))); stats.append(card); } content.append(node('h3', '', 'Company usage'), stats);
    const funding = node('dl', 'workspace-metadata'); for (const [key, label] of [['prepaid_funded_usd', 'Prepaid funding'], ['provider_credit_funded_usd', 'Provider promotional credit'], ['monthly_used_usd', 'Used this month'], ['monthly_available_usd', 'Monthly allowance remaining']]) if (company[key] !== undefined && company[key] !== null) funding.append(node('dt', '', label), node('dd', 'agent-price', money(Number(company[key])))); if (company.monthly_reset_at) funding.append(node('dt', '', 'Monthly reset'), node('dd', '', agentDate(company.monthly_reset_at))); content.append(funding);
    const form = node('form', 'usage-limit-form'), label = node('label', 'agent-field'), limit = node('input'); limit.type = 'number'; limit.min = '0'; limit.step = '0.01'; limit.required = true; limit.name = 'monthly_limit_usd'; limit.value = company.monthly_limit_usd == null ? '' : String(Number(company.monthly_limit_usd)); limit.placeholder = 'Set a monthly limit'; label.append(node('span', '', 'Company monthly limit ($)'), limit); const save = node('button', 'agent-secondary', 'Save limit'), error = node('p', 'agent-form-error'); save.type = 'submit'; save.prepend(icon('check')); error.hidden = true; form.append(label, error, save); form.addEventListener('submit', async (event) => { event.preventDefault(); if (!session?.can_query) { showSession('Enter your access code to update the company limit.'); return; } save.disabled = true; try { await request('/api/usage/settings', { expected_revision: company.revision, monthly_limit_usd: limit.value }); const response = await fetch(`/api/usage${activeId ? `?chat_id=${encodeURIComponent(activeId)}` : ''}`); if (!response.ok) throw new Error('Refresh usage to confirm the saved limit.'); usageData = await response.json(); if (usageData.context && activeChat()) { activeChat().context = usageData.context; renderContextRing(); } renderUsage(); } catch (failure) { error.textContent = failure.message; error.hidden = false; save.disabled = false; } }); if (observing()) form.querySelectorAll('input, button').forEach((control) => { control.disabled = true; }); content.append(form);
    content.append(node('h3', '', 'Usage routes')); const routes = node('dl', 'workspace-metadata'); workspaceExtension.renderUsageRoutes?.(routes,data); if (data.codex?.route) routes.append(node('dt', '', 'Agent harness'), node('dd', '', data.codex.route)); content.append(routes); if (session?.operator?.mode === 'founder_demo' && data.codex?.limits?.length) { const limits = node('details', 'usage-plan-details'); limits.append(node('summary', '', 'Reported subscription limits')); data.codex.limits.forEach((limit) => { const item = node('dl', 'workspace-metadata'); for (const [key, value] of Object.entries(limit)) if (value !== null && value !== undefined) item.append(node('dt', '', humanize(key)), node('dd', '', typeof value === 'object' ? JSON.stringify(value) : String(value))); limits.append(item); }); content.append(limits); } else if (session?.operator?.mode === 'founder_demo') content.append(node('p', 'workspace-help', 'Subscription limits appear when reported by Codex.'));
    workspaceExtension.renderUsageStorage?.(content,data); const billing = node('div', 'usage-billing'); billing.append(node('span', '', 'Payments and auto-reload'), node('span', 'development-status', 'Setup in progress')); const billingActions = node('div', 'usage-billing-actions'); for (const [label, symbol] of [['Add credits', 'plus'], ['Set up auto-reload', 'refresh']]) { const button = agentButton(label, () => {}, false, symbol); button.disabled = true; button.title = 'Payment setup is being configured'; billingActions.append(button); } content.append(billing, billingActions); if (company.retail_multiplier === null) content.append(node('p', 'workspace-help', 'Customer pricing: configuration required.'));
  }
  // MCP keys live only in the current private dialog; persisted state is metadata.
  let mcpData = null, mcpLoading = false, mcpError = '', mcpEpoch = 0, mcpMutation = false, mcpModal = null;
  const mcpWritable = () => Boolean(session?.can_query && !observing() && !session?.read_only);
  const mcpIdentity = () => `${sessionGeneration}:${storageIdentity}:${principalId()}`;
  function closeMcpDialog() {
    const dialog = $('mcp-dialog');
    dialog.querySelectorAll('input').forEach(input => { input.value = ''; });
    if (mcpModal) mcpModal.token = '';
    mcpModal = null; if (dialog.open) dialog.close(); dialog.replaceChildren();
  }
  function resetMcp() { ++mcpEpoch; closeMcpDialog(); mcpData = null; mcpLoading = mcpMutation = false; mcpError = ''; $('mcp-content').replaceChildren(); }
  function mcpRecord(item) { return { id: String(item.id || ''), label: String(item.label || ''), dataset: String(item.dataset || ''), expires_at: Number(item.expires_at), revoked_at: item.revoked_at == null ? null : Number(item.revoked_at) }; }
  function mcpEndpoint(value) { try { const url = new URL(value); return url.protocol === 'https:' && url.pathname === '/mcp' && !url.username && !url.password && !url.search && !url.hash ? url.href : ''; } catch { return ''; } }
  async function loadMcp() {
    if (!session) return; if (currentView === 'mcp') $('settings-mcp').scrollIntoView({block:'nearest',inline:'nearest'}); const identity = mcpIdentity(), epoch = ++mcpEpoch; mcpLoading = true; mcpError = ''; renderMcp();
    try {
      const response = await fetch('/api/mcp/connections', { cache:'no-store', credentials:'same-origin' });
      if (identity !== mcpIdentity() || epoch !== mcpEpoch) return;
      if ([401,403].includes(response.status)) { resetMcp(); if (response.status === 401) showSession('Open your workspace to manage MCP access.'); else { mcpError = 'Open your own workspace to manage MCP access.'; renderMcp(); } return; }
      if (!response.ok) throw new Error(); const data = await response.json();
      if (identity !== mcpIdentity() || epoch !== mcpEpoch) return;
      mcpData = { available:data.available === true, url:mcpEndpoint(data.url), connections:(data.connections || []).map(mcpRecord) };
    } catch { if (identity === mcpIdentity() && epoch === mcpEpoch) mcpError = 'MCP access is reconnecting. Refresh to continue.'; }
    finally { if (identity === mcpIdentity() && epoch === mcpEpoch) { mcpLoading = false; renderMcp(); } }
  }
  function showMcp() { setProfileMenu(false); currentView = 'mcp'; history.replaceState({}, '', '/?settings=mcp'); render(); setSidebar(false); loadMcp(); }
  async function copyMcp(value, button, identity = mcpIdentity()) { if (identity !== mcpIdentity()) return; try { await navigator.clipboard.writeText(value); if (identity === mcpIdentity() && button.isConnected) button.replaceChildren(icon('check'), document.createTextNode('Copied')); } catch { if (identity === mcpIdentity()) notice('Select the value and copy it to your client.'); } }
  function renderMcp() {
    if (currentView !== 'mcp') return; const content = $('mcp-content'); content.replaceChildren();
    content.append(node('p','mcp-intro',workspaceExtension.mcpIntroduction || 'Connect an MCP-compatible app to the authorized tools in your workspace. Each access key reads its selected collection.'));
    if (mcpError) { const error = node('p','agent-form-error',mcpError); error.setAttribute('role','alert'); content.append(error,agentButton('Refresh',loadMcp,false,'refresh')); }
    if (mcpLoading && !mcpData) { content.append(node('p','workspace-help','Loading access keys…')); return; }
    if (!mcpData) return;
    const endpoint = node('section','mcp-endpoint'); endpoint.append(node('h2','','Server address'));
    const line = node('div','mcp-value-row'), address = node('input'); address.type = 'text'; address.readOnly = true; address.value = mcpData.url; address.setAttribute('aria-label','MCP server address');
    const copy = agentButton('Copy address',() => copyMcp(address.value,copy),false,'copy'); copy.disabled = !mcpData.url; line.append(address,copy); endpoint.append(line,node('p','workspace-help','Choose Streamable HTTP in your client and use the access key as its bearer token.')); content.append(endpoint);
    const heading = node('div','mcp-section-heading'); heading.append(node('h2','','Access keys')); const add = agentButton('Create access key',openMcpForm,true,'plus'); add.disabled = !mcpWritable() || !mcpData.available || mcpMutation; heading.append(add); content.append(heading);
    if (!mcpData.available) content.append(node('p','workspace-help','MCP access is being prepared for this workspace.'));
    if (observing() || session?.read_only) content.append(node('p','workspace-help','Viewing this workspace’s access key metadata.'));
    const list = node('div','mcp-connections');
    for (const item of mcpData.connections) {
      const expired = item.expires_at * 1000 <= Date.now(), state = item.revoked_at != null ? 'Revoked' : expired ? 'Expired' : 'Active';
      const row = node('article','mcp-connection'), copy = node('div','mcp-connection-copy'); copy.append(node('strong','',item.label || 'MCP connection'),node('p','workspace-help',`${scopeLabel(item.dataset)} · ${state}`),node('small','',`Expires ${new Date(item.expires_at*1000).toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}`));
      row.append(icon('link'),copy); if (state === 'Active') { const revoke = agentButton('Revoke',() => openMcpRevoke(item),false,'close'); revoke.disabled = !mcpWritable() || mcpMutation; revoke.setAttribute('aria-label',`Revoke ${item.label || 'connection'}`); row.append(revoke); } list.append(row);
    }
    if (!mcpData.connections.length) list.append(node('p','workspace-help',workspaceExtension.mcpEmptyHint || 'Create an access key for your app.')); content.append(list);
  }
  function mcpDialogShell(title) {
    closeMcpDialog(); const dialog = $('mcp-dialog'), heading = node('div','mcp-section-heading'), name = node('h2','',title); name.id = 'mcp-dialog-title';
    const close = agentButton('Close',closeMcpDialog,false,'close'); close.setAttribute('aria-label','Close MCP setup'); heading.append(name,close); dialog.append(heading); mcpModal = { identity:mcpIdentity(), token:'' }; return dialog;
  }
  function openMcpForm() {
    if (!mcpWritable() || !mcpData?.available || mcpMutation) return;
    const dialog = mcpDialogShell('Create access key'), state = mcpModal, form = node('form'), name = node('input'), collection = node('select'); name.id = 'mcp-connection-name'; name.name = 'label'; name.required = true; name.maxLength = 80; name.placeholder = 'e.g. Codex desktop';
    collection.id = 'mcp-collection'; collection.name = 'dataset'; collection.required = true;
    for (const id of scopes().map(row=>row.id)) { const option = node('option','',scopeLabel(id)); option.value = id; collection.append(option); }
    for (const [text,input] of [['Key name',name],['Collection',collection]]) { const label = node('label','agent-field'); label.htmlFor = input.id; input.setAttribute('aria-label',text); label.append(node('span','',text),input); form.append(label); }
    form.append(node('p','workspace-help','Read access · Expires in 7 days')); const error = node('p','agent-form-error'); error.setAttribute('role','alert'); error.hidden = true; const submit = node('button','agent-primary'); submit.type = 'submit'; submit.append(icon('plus'),document.createTextNode('Create access key')); submit.disabled = !collection.options.length; form.append(error,submit); dialog.append(form);
    form.addEventListener('submit',async event => { event.preventDefault(); if (!mcpWritable() || mcpMutation || mcpModal !== state) return; ++mcpEpoch; mcpLoading = false; mcpMutation = true; submit.disabled = true; name.readOnly = true; collection.disabled = true;
      try {
        const response = await fetch('/api/mcp/connections',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json','X-Rocoto-Page':'1'},body:JSON.stringify({label:name.value.trim(),dataset:collection.value,ttl_seconds:604800})});
        if (state.identity !== mcpIdentity()) return;
        if ([401,403].includes(response.status)) { resetMcp(); if (response.status === 401) showSession('Open your workspace to manage MCP access.'); else { mcpError = 'Open your own workspace to manage MCP access.'; renderMcp(); } return; }
        if (!response.ok) throw new Error(); const data = await response.json();
        if (state.identity !== mcpIdentity() || mcpModal !== state || !mcpWritable()) { data.token = ''; return; }
        if (typeof data.token !== 'string' || !/^rmcp_[A-Za-z0-9_-]{64}$/.test(data.token) || !data.connection?.id) { data.token = ''; throw new Error(); }
        ++mcpEpoch; mcpLoading = false; const connection = mcpRecord(data.connection); mcpData.connections = [...mcpData.connections.filter(item => item.id !== connection.id),connection]; openMcpKey(data.token,connection); data.token = ''; renderMcp();
      } catch { if (state.identity === mcpIdentity() && mcpModal === state) { error.textContent = 'Check the access key list before creating another key, then try again.'; error.hidden = false; name.readOnly = false; collection.disabled = false; submit.disabled = false; } }
      finally { if (state.identity === mcpIdentity()) { mcpMutation = false; renderMcp(); if (mcpModal !== state && !mcpModal) loadMcp(); } }
    }); dialog.showModal(); name.focus();
  }
  function openMcpKey(token, connection) {
    const dialog = mcpDialogShell('Access key'), state = mcpModal; state.token = token;
    dialog.append(node('p','workspace-help',`Save this key in your client now. It is shown once and expires in 7 days. ${connection.label} can read ${scopeLabel(connection.dataset)}.`));
    const field = node('label','agent-field'), input = node('input'); input.type = 'password'; input.readOnly = true; input.autocomplete = 'off'; input.spellcheck = false; input.value = token; input.id = 'mcp-secret'; field.append(node('span','','Access key'),input); dialog.append(field);
    const actions = node('div','mcp-key-actions'), reveal = agentButton('Show key',() => { if (mcpModal !== state) return; const show = input.type === 'password'; input.type = show ? 'text' : 'password'; reveal.replaceChildren(icon(show ? 'eye-off' : 'eye'),document.createTextNode(show ? 'Hide key' : 'Show key')); },false,'eye'), copy = agentButton('Copy key',() => { if (mcpModal === state && mcpWritable()) copyMcp(state.token,copy,state.identity); },true,'copy'); actions.append(reveal,copy,agentButton('Done',closeMcpDialog,false,'check')); dialog.append(actions); dialog.showModal(); copy.focus();
  }
  function openMcpRevoke(item) {
    if (!mcpWritable() || mcpMutation) return; const dialog = mcpDialogShell('Revoke access key'), state = mcpModal;
    dialog.append(node('p','workspace-help',`Revoking ${item.label} ends this client’s access to ${scopeLabel(item.dataset)}.`)); const error = node('p','agent-form-error'); error.setAttribute('role','alert'); const revoke = agentButton('Revoke access key',async () => {
      if (!mcpWritable() || mcpModal !== state || mcpMutation) return; ++mcpEpoch; mcpLoading = false; mcpMutation = true; revoke.disabled = true;
      try { const response = await fetch(`/api/mcp/connections/${encodeURIComponent(item.id)}`,{method:'DELETE',credentials:'same-origin',cache:'no-store',headers:{'X-Rocoto-Page':'1'}});
        if (state.identity !== mcpIdentity()) return;
        if ([401,403].includes(response.status)) { resetMcp(); if (response.status === 401) showSession('Open your workspace to manage MCP access.'); else { mcpError = 'Open your own workspace to manage MCP access.'; renderMcp(); } return; }
        if (!response.ok) throw new Error(); const data = await response.json(); if (state.identity !== mcpIdentity()) return; if (data.connection?.id !== item.id || data.connection.revoked_at == null) throw new Error();
        ++mcpEpoch; mcpLoading = false; mcpData.connections = mcpData.connections.map(row => row.id === item.id ? mcpRecord(data.connection) : row); if (mcpModal === state) closeMcpDialog();
      } catch { if (state.identity === mcpIdentity() && mcpModal === state) { error.textContent = 'Refresh the access key list and retry revocation.'; revoke.disabled = false; } }
      finally { if (state.identity === mcpIdentity()) { mcpMutation = false; renderMcp(); } }
    },true,'close'); dialog.append(error,revoke); dialog.showModal(); revoke.focus();
  }

  function renderConnection() {
    const connection = status?.connection, now = Date.now(), reachable = !connectionFailed && statusReceivedAt > 0 && now - statusReceivedAt < 25000;
    const checked = Date.parse(connection?.checked_at || ''), authFresh = Number.isFinite(checked) && now - checked < ((connection?.check_max_age_seconds || 15) + 5) * 1000;
    const verified = reachable && authFresh && connection?.managed_authenticated && connection?.check_status === 'authenticated', host = connection?.host || 'Server';
    const summary = $('connection-status'), dot = node('span', `connection-dot${verified ? ' online' : ''}`); summary.replaceChildren(dot, node('span', '', statusReceivedAt ? host : 'Checking server')); summary.setAttribute('aria-label', `${host}: ${verified ? 'connected, OpenAI Agents API verified' : reachable ? 'connected, checking OpenAI Agents API' : 'offline'}`);
    const detail = $('connection-details'); detail.replaceChildren(node('strong', '', host), node('p', '', reachable ? 'Server connected' : statusReceivedAt ? 'Server offline' : 'Checking server connection'), node('p', '', verified ? 'OpenAI Agents API verified' : reachable && authFresh ? connection.message || 'OpenAI Agents API connection needs attention' : 'OpenAI Agents API connection needs a fresh check'));
    detail.append(node('p', 'connection-help', 'Core and agent environment hosted by OpenAI'));
    if (Number.isFinite(checked)) detail.append(node('small', '', `Access checked ${new Date(checked).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`));
    if (!reachable) detail.append(node('p', 'connection-help', 'Open your server connection, then retry here.'));
    const retry = node('button', 'text-button', 'Reconnect server'); retry.type = 'button'; retry.addEventListener('click', () => { retry.disabled = true; loadStatus(true); }); detail.append(retry);
  }
  async function openInitialSession() { if (initialAccessCode !== null) { const code = initialAccessCode; initialAccessCode = null; try { await request('/api/session', { code }); } catch (error) { showSession(error.message); return; } } await loadSession(); }
  async function loadSession() {
    try { const response = await fetch('/api/session'); if (!response.ok) { showSession(); return; } const data = await response.json(); $('explore-demo').hidden = data.demo_browsing !== true; $('session-intro').textContent = data.demo_browsing === true ? workspaceExtension.sessionIntroduction || 'Explore the demo, or open your company’s workspace.' : 'Open your workspace with an access code.'; if (!data.authenticated) { showSession(); return; } persistDraft(); selectSessionStorage(data); session = data; browserPreferences=window.RocotoWorkspaceSettings.read(preferencesIdentity());window.RocotoWorkspaceSettings.apply(browserPreferences); document.body.classList.remove('session-pending'); $('session-screen').hidden = true; $('session-code').value = ''; notice(''); chats.forEach((chat) => { chat.environment_id ||= data.environment.id; chat.owner_principal_id ||= principalId(); }); if (!visibleChats().some((chat) => chat.id === activeId)) { activeId = preferredChatId(visibleChats()); elements.message.value = textDrafts.get(activeId) || ''; } renderOperator(); renderObserver(); save(); loadHarness(); await loadServerChats(); await loadStatus();if(demoEnvironment()&&activeChat()&&!activeChat().server_saved&&!activeChat().messages.length&&!activeChat().dataset){activeChat().dataset=currentCollection();save();render();} await loadWorkspaceCapabilities(activeChat()?.dataset || 'company'); if (data.role === 'owner') loadObservers(); if (currentView === 'agents') await loadAgents(); await workspaceExtension.resumeWorkspace?.(); if (currentView === 'business') { businessCollection ||= currentCollection();await loadBusiness(); }if(currentView==='stats')await loadStatsActivity(); await refreshUsage();if(currentView==='profile')await loadProfileSettings();if(currentView==='messaging')await loadMessaging(); if (currentView === 'mcp') await loadMcp(); if (currentView === 'marketplace' && marketplaceTab === 'agents') await loadAgentTemplates(); await loadSkills(); await loadConnectionCatalogue(); if (currentView === 'marketplace') await loadConnectionDrafts(); for (const chat of visibleChats()) if (chat.remote_running || (chat.parent_id || chat.kind === 'agent_run' || chat.agent_id) && queued.some((item) => item.chat_id === chat.id)) pollChild(chat.id); }
    catch { showSession('The workspace is reconnecting. Try opening it again.'); }
  }
  async function login(event, demo = false) { event?.preventDefault(); const button = $(demo ? 'explore-demo' : 'session-submit'); button.disabled = true; $('session-message').textContent = 'Opening workspace…'; try { await request(demo ? '/api/session/demo' : '/api/session', demo ? {} : { code: $('session-code').value.trim() }); await loadSession(); } catch (error) { $('session-message').textContent = error.message; } finally { button.disabled = false; } }
  async function loadStatus(refresh = false) {
    try {
      const generation = sessionGeneration, response = await fetch('/api/status'); if (generation !== sessionGeneration) return; if (response.status === 401) { showSession('Open your workspace to continue.'); return; } if (!response.ok) throw new Error(); const latest = await response.json();if(generation!==sessionGeneration)return;status=latest; statusReceivedAt = Date.now(); connectionFailed = false; renderConnection(); renderOperator();
      if (!status.agent?.authenticated && !status.connection?.inference_enabled) notice(status.connection?.message || 'Connect the agent runtime to continue.');
      const active = status.agent?.active_chats || {}; visibleChats().forEach((chat) => { if (!running.has(chat.id) && !chat.parent_id && chat.kind !== 'agent_run' && !chat.agent_id) { chat.remote_running = Boolean(active[chat.id]); verifiedRunning.add(chat.id); } });
      workspaceExtension.updateStatus?.(status);
      elements.dataset.replaceChildren(...scopes().map(scope=>{const option=node('option','',scope.label);option.value=scope.id;return option;}));
      if (refresh) { if(Date.now()-usageCheckedAt>30000)refreshUsage();renderSidebar(); renderQueue(); updateMode(); loadServerChats(); if (activeChat()?.agent_setup) loadWorkspaceCapabilities(activeChat()?.dataset || 'company'); } else render();
    } catch { connectionFailed = true; renderConnection(); }
    finally { clearTimeout(statusTimer); if (session) statusTimer = setTimeout(() => loadStatus(true), 7000); }
  }
  // An interrupted page reload preserves the partial conversation without a stuck activity indicator.
  chats.forEach((chat) => chat.messages.forEach((message) => { if (message.pending && !chat.parent_id) { message.pending = false; message.error = true; message.text = [message.text, 'This run was interrupted. Continue with a follow-up or try again.'].filter(Boolean).join('\n\n'); } }));
  for (const [id, symbol] of Object.entries({ 'settings-mcp': 'link', 'settings-usage': 'chart', 'settings-archives': 'archive', 'open-settings': 'settings', 'open-usage': 'chart', 'profile-logout': 'logout', 'customize-agents': 'clock', 'customize-skills': 'skill', 'customize-connections': 'at', 'agents-skills-link': 'grid', 'add-new-skill': 'plus', 'browse-skills': 'grid', 'home-quick-search': 'search', 'home-chat': 'chat' })) if ($(id) && !$(id).querySelector('svg')) $(id).prepend(icon(symbol));

  function openQuickSearch() { showHome(); }
  $('quick-search-nav')?.addEventListener('click', openQuickSearch);
  const shortcuts = { '1': ['new-chat', 'Home'], '2': ['agents-nav', 'Agents'], '4': ['marketplace-nav', 'Connections'] }; if (legacyQuickSearch && $('quick-search-nav')) shortcuts.k = ['quick-search-nav', 'Quick Search'];
  const modifier = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl'; for (const [key, [id, title]] of Object.entries(shortcuts)) { $(id).title = `${title} (${modifier}+${key.toUpperCase()})`; $(id).setAttribute('aria-keyshortcuts', `Control+${key} Meta+${key}`); }
  document.addEventListener('keydown', (event) => { if ((!event.metaKey && !event.ctrlKey) || event.altKey || event.shiftKey || document.querySelector('dialog[open]') || event.repeat) return; const binding = shortcuts[event.key.toLowerCase()]; if (binding) { event.preventDefault(); $(binding[0]).click(); } });

  $('new-chat').addEventListener('click', () => { showHome(); setSidebar(false); });
  $('add-chat').addEventListener('click', newChat);
  $('home-quick-search').addEventListener('click', showHome);
  $('home-chat').addEventListener('click', () => { homeMode = 'chat'; render(); elements.message.focus(); });
  setInterval(renderConnection, 5000);
  $('agents-nav').addEventListener('click', () => showAgents());
  $('rail-chats').addEventListener('click', () => setRailChats(!document.body.classList.contains('chats-open')));
  $('observer-select').addEventListener('change', changeObserver); $('end-observation').addEventListener('click', endObservation);
  $('account-button').addEventListener('click', () => setProfileMenu($('profile-menu').hidden));
  $('open-settings').addEventListener('click', async () => { setProfileMenu(false); if (observing()) await endObservation(); showWorkspaceSettings('appearance'); });
  $('open-usage').addEventListener('click', openUsage); $('usage-breakdown').addEventListener('click', openUsage);
  $('profile-logout').addEventListener('click', () => { setProfileMenu(false); $('session-logout').click(); });
  for(const view of ['appearance','notifications','team','messaging','billing'])$('settings-'+view).addEventListener('click',()=>showWorkspaceSettings(view));
  $('open-profile').addEventListener('click',()=>showProfileSettings());$('settings-profile').addEventListener('click',()=>showProfileSettings());$('settings-plugins').addEventListener('click',showPluginSettings);
  $('settings-mcp').addEventListener('click', showMcp);
  $('mcp-dialog').addEventListener('close', () => { if (!$('mcp-dialog').open) closeMcpDialog(); });
  $('mcp-dialog').addEventListener('cancel', event => { event.preventDefault(); closeMcpDialog(); });
  window.addEventListener('pagehide', resetMcp);
  window.addEventListener('pagehide', clearConnectionCredential);
   $('settings-usage').addEventListener('click', openUsage); $('settings-archives').addEventListener('click', showArchives);
  $('founder-nav').addEventListener('click',openFounder);$('founder-demo-close').addEventListener('click',()=>$('founder-demo-popover').close());$('founder-demo-search').addEventListener('input',renderFounderOptions);$('founder-demo-popover').addEventListener('close',()=>{if(founderAllowed())$('founder-nav').focus();});
  $('collection-picker-close').addEventListener('click',()=>$('collection-picker-dialog').close());$('collection-picker-search').addEventListener('input',renderCollectionOptions);$('collection-picker-dialog').addEventListener('close',()=>{const opener=collectionPicker?.opener;collectionPicker=null;if(opener?.isConnected)opener.focus();});
  $('settings-business').addEventListener('click', () => showBusiness());$('settings-stats').addEventListener('click',showStats);

  $('close-business-chat').addEventListener('click', closeBusinessChat); $('onboarding-full-chat').addEventListener('click', () => { closeBusinessChat(); if (onboardingChatId) openChatId(onboardingChatId); });
  $('marketplace-nav').addEventListener('click', () => { showMarketplace(); if (matchMedia('(max-width: 760px)').matches) document.body.classList.add('context-nav-open'); }); $('agents-skills-link').addEventListener('click', () => showMarketplace('agents'));
  for (const [id, tab] of [['market-agents', 'agents'], ['market-skills', 'skills'], ['market-connections', 'connections'], ['customize-agents', 'agents'], ['customize-skills', 'skills'], ['customize-connections', 'connections']]) $(id).addEventListener('click', () => showMarketplace(tab));
  $('agents-search').addEventListener('input',renderAgents);
  $('marketplace-search').addEventListener('input', event => { connectionSearch=event.target.value; renderMarketplace(); });
  $('close-native-question').addEventListener('click', () => submitNativeQuestion('skip').catch(()=>{}));
  $('native-question-dialog').addEventListener('cancel', event => { event.preventDefault(); submitNativeQuestion('skip').catch(()=>{}); });
  $('native-question-dialog').addEventListener('close', () => { if (!$('native-question-dialog').open) finishQuestionClose(); });
  document.addEventListener('close', scheduleQuestionSync, true);
  document.addEventListener('close', scheduleCredentialSync, true);
  $('close-credential-request').addEventListener('click', () => submitCredentialRequest('skip').catch(()=>{}));
  $('credential-request-dialog').addEventListener('cancel', event => { event.preventDefault(); submitCredentialRequest('skip').catch(()=>{}); });
  $('credential-request-dialog').addEventListener('close', () => { if (!$('credential-request-dialog').open) finishCredentialClose(); });
  document.addEventListener('visibilitychange', scheduleCredentialSync);
  document.addEventListener('visibilitychange', scheduleQuestionSync);
  $('add-custom-connector').addEventListener('click', () => openCustomConnectorDialog($('add-custom-connector')));
  $('custom-connector-form').addEventListener('submit', createCustomConnector);
  for (const id of ['close-custom-connector', 'cancel-custom-connector']) $(id).addEventListener('click', () => $('custom-connector-dialog').close());
  $('custom-connector-dialog').addEventListener('close', () => { customConnectorScope=null; $('custom-connector-form').reset(); if (customConnectorOpener?.isConnected && !customConnectorOpener.hidden) customConnectorOpener.focus(); });
  $('close-connection-setup').addEventListener('click', () => { clearConnectionCredential(); $('connection-setup-dialog').close(); });
  $('connection-setup-dialog').addEventListener('cancel', clearConnectionCredential);
  $('connection-setup-dialog').addEventListener('close', () => { if($('connection-setup-dialog').open)return;clearConnectionCredential();clearTimeout(connectionFlowTimer); connectionFlow=null; nativePluginDialog=null; });
  window.addEventListener('message', event => { if (event.origin===location.origin && event.data?.type==='rocoto:connection-update') loadConnectionCatalogue(); });
  $('close-skill').addEventListener('click', () => $('skill-dialog').close());
  $('context-ring').addEventListener('click', () => setUsagePopover($('usage-popover').hidden)); for (const type of ['mouseenter', 'focus']) $('context-ring').addEventListener(type, () => { clearTimeout(contextHoverTimer); contextHoverTimer = setTimeout(() => { renderContextRing(); $('context-tooltip').hidden = !$('usage-popover').hidden; }, 600); }); for (const type of ['mouseleave', 'blur']) $('context-ring').addEventListener(type, () => { clearTimeout(contextHoverTimer); $('context-tooltip').hidden = true; });
  document.addEventListener('click', (event) => { if (!eventWithin(event, '#usage-popover, #context-ring')) setUsagePopover(false); if (!eventWithin(event, '#command-menu, #message')) closeCommandMenu(); if (!eventWithin(event, '#customize-context, #marketplace-nav, [data-workspace-context]')) document.body.classList.remove('context-nav-open'); if (!eventWithin(event, '#agent-context, #agents-nav, .agent-back')) document.body.classList.remove('agents-list-open'); if (!eventWithin(event, '#profile-menu, #account-button')) setProfileMenu(false); if (!eventWithin(event, '#chat-list, .sidebar-caption, #rail-chats, #new-chat')) setRailChats(false); });

  $('file-selector').addEventListener('change',event=>{const file=filesData.find(item=>item.id===event.target.value);if(file)openGeneratedFile(file,$('file-selector'));});

  $('file-question-form').addEventListener('submit',event=>{event.preventDefault();const file=filesData.find(item=>item.id===selectedFileId);if(file&&$('file-question').value.trim()){const question=$('file-question').value;$('file-question').value='';chatAboutFile(file,question);}});
  $('rename-file-form').addEventListener('submit',renameFile);$('rename-file-cancel').addEventListener('click',()=>$('rename-file-dialog').close());$('rename-file-dialog').addEventListener('close',()=>{const state=fileRename;fileRename=null;$('rename-file-input').value='';$('rename-file-save').disabled=false;state?.opener?.isConnected&&state.opener.focus();});

  $('chat-context-toggle').addEventListener('click', () => setChatContext($('chat-context-popup').hidden));
  document.addEventListener('click', event => { if(!eventWithin(event,'.chat-context-anchor'))setChatContext(false); });
  setInterval(()=>{for(const el of document.querySelectorAll('.work-timing[data-started-at]'))el.textContent=workDuration({pending:true,started_at:el.dataset.startedAt});},1000);
  $('chat-files-toggle').addEventListener('click', () => setFilesPane(!filesPaneOpen));
  $('close-chat-files').addEventListener('click', () => setFilesPane(false));
  $('refresh-chat-files').addEventListener('click', loadFiles);
  setInterval(() => { if (!document.hidden && (filesPaneOpen || Boolean(workspaceExtension.filesSurface?.()?.active))) loadFiles(); }, 5000);

  $('attachment-toggle').addEventListener('click', (event) => { attachmentMention = null; const open = $('attachment-menu').hidden; toggleAttachmentMenu(open); if (open && event.detail === 0) $('attach-images').focus(); });
  for (const event of ['mouseenter', 'focus', 'click']) $('attach-skills').addEventListener(event, () => openSkillsSubmenu());
  $('add-new-skill').addEventListener('click', createSkillChat); $('browse-skills').addEventListener('click', () => { toggleAttachmentMenu(false); showMarketplace('skills'); });
  $('attach-images').addEventListener('click', () => { toggleAttachmentMenu(false); $('image-files').click(); });
  $('image-files').addEventListener('change', async (event) => { const files = [...event.target.files]; event.target.value = ''; await uploadImages(files); });

  document.addEventListener('click', (event) => { if (!eventWithin(event, '.attachment-menu-anchor')) toggleAttachmentMenu(false); });
  $('create-agent').addEventListener('click', () => startAgentChat()); $('context-new-agent').addEventListener('click', () => startAgentChat());
  $('agent-form').addEventListener('submit', saveAgent);
  for (const id of ['close-agent-editor', 'cancel-agent-editor']) $(id).addEventListener('click', () => $('agent-editor').close());
  $('agent-editor').addEventListener('close', () => { if (agentEditorOpener?.isConnected) agentEditorOpener.focus(); });
  for (const input of document.querySelectorAll('[name="agent-mode"]')) input.addEventListener('change', () => { $('agent-interval').value = input.value === 'ongoing' ? 10 : 60; updateAgentMode(); });
  $('agent-max-runs').addEventListener('input',()=>workspaceExtension.updateAgentEditor?.());
  document.addEventListener('visibilitychange', () => { if (document.hidden) clearTimeout(agentsTimer); else if (currentView === 'agents') loadAgents(); });
  $('session-form').addEventListener('submit', (event) => login(event));
  $('explore-demo').addEventListener('click', (event) => login(event, true));
  $('session-logout').addEventListener('click', async () => { try { const response = await fetch('/api/session', { method: 'DELETE', headers: { 'X-Rocoto-Page': '1' } }); if (!response.ok) throw new Error(); setPicker('environment', false); showSession(); } catch { notice('Sign out is reconnecting. Try again.'); } });
  for (const name of ['workspace', 'environment']) { $(`${name}-toggle`).addEventListener('click', () => setPicker(name, $(`${name}-popover`).hidden)); $(`${name}-search`).addEventListener('input', name === 'workspace' ? renderScopeOptions : renderEnvironments); }
  document.addEventListener('click', (event) => { if (!eventWithin(event, '.workspace-picker')) { setPicker('workspace', false); setPicker('environment', false); } });
  $('close-business-map').addEventListener('click',()=>$('business-map-dialog').close());$('business-map-dialog').addEventListener('close',()=>{for(const root of $('business-map-expanded').querySelectorAll('.business-map'))window.RocotoBusinessMap.dispose(root);$('business-map-expanded').replaceChildren();if(mapDialogOpener?.isConnected)mapDialogOpener.focus();mapDialogOpener=null;});

  window.addEventListener('resize', () => { workspaceExtension.resize?.(); resizeComposer(); });

  $('sidebar-toggle').addEventListener('click', () => setSidebar(!document.body.classList.contains('sidebar-open')));
  $('sidebar-scrim').addEventListener('click', () => setSidebar(false));
  document.addEventListener('keydown', (event) => { if (event.target.closest('#attachment-menu, #attachment-toggle') && handleComposerMenu(event)) return; if (event.key === 'Escape') { workspaceExtension.cancelPending?.();if (document.fullscreenElement) { event.preventDefault(); document.exitFullscreen?.().catch(() => {}); return; } workspaceExtension.closeOverlay?.(); if(!$('chat-context-popup').hidden){setChatContext(false);$('chat-context-toggle').focus();} if (filesPaneOpen) setFilesPane(false); setSidebar(false); document.body.classList.remove('agents-list-open', 'context-nav-open'); setProfileMenu(false); const usageOpen = !$('usage-popover').hidden; setUsagePopover(false, usageOpen); closeCommandMenu(); toggleAttachmentMenu(false); for (const name of ['workspace', 'environment']) if (!$(`${name}-popover`).hidden) { setPicker(name, false); $(`${name}-toggle`).focus(); } } });
  $('composer').addEventListener('submit', submit);
  window.addEventListener('storage', (event) => { if (event.key !== STORAGE_KEY || !event.newValue) return; try { const incoming = JSON.parse(event.newValue); if (!Array.isArray(incoming)) return; for (const chat of incoming) { if (!chat?.id || !Array.isArray(chat.messages) || running.has(chat.id)) continue; const existing = chats.find((item) => item.id === chat.id); if (!existing) chats.push(chat); else if ((chat.updated || 0) >= (existing.updated || 0)) Object.assign(existing, chat); } renderSidebar(); } catch { /* Keep this window's current conversation when shared browser data is incomplete. */ } });
  window.addEventListener('pagehide',()=>persistDraft());
  document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='hidden')persistDraft();});
  elements.message.addEventListener('input', () => { if(!activeChat() && session && !observing()){const value=elements.message.value;newChat();elements.message.value=value;}resizeComposer(); updateMode(); updateMentionMenu(); });
  elements.message.addEventListener('paste', pasteImages);
  elements.message.addEventListener('keydown', (event) => { if (handleCommandMenu(event) || handleComposerMenu(event)) return; if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); $('composer').requestSubmit(); } });
  elements.mode.addEventListener('change', updateMode);
  if (new URL(location.href).searchParams.get('drawer') === '1') document.body.classList.add('drawer-mode');
  for (const [id, label] of [['new-chat', 'Home'], ['agents-nav', 'Agents'], ['marketplace-nav', 'Connections']]) { $(id).setAttribute('aria-label', label); $(id).title = label; }
  let tooltipFrame = null; const refreshTooltips = () => { tooltipFrame = null; for (const button of document.querySelectorAll('button')) { const label = button.getAttribute('aria-label') || button.textContent.trim().replace(/\s+/g, ' '); if (label && (!button.title || button.dataset.autoTitle)) { button.title = label.slice(0, 180); button.dataset.autoTitle = '1'; } } }; new MutationObserver(() => { if (tooltipFrame === null) tooltipFrame = requestAnimationFrame(refreshTooltips); }).observe(document.body, { childList: true, subtree: true }); refreshTooltips();
  render(); updateMode(); openInitialSession().then(() => { if (!session) return; const params = new URL(location.href).searchParams, initialChat = params.get('chat'); if (initialChat && currentView === 'chat' && homeMode === 'chat') openChatId(initialChat); else if (session.host_read_only && params.size === 0) { const example = visibleChats().find(chat => chat.kind === 'organization_chat' && chat.organization_purpose === 'organization'); if (example) chooseChat(example); } workspaceExtension.openInitialRoute?.(params); });
})();
