/* One member profile, channel card and Tracking Agent phone block, shared by Team, Messaging and agents. */
(() => {
  'use strict';
  const make = (tag, className = '', text) => { const el = document.createElement(tag); el.className = className; if (text !== undefined) el.textContent = text; return el; };
  const identity = member => String(member?.id || member?.principal_id || member?.member_id || '');
  const name = member => member?.display_name || member?.name || 'Team member';
  const initials = member => name(member).trim().split(/\s+/).slice(0, 2).map(word => [...word][0]).join('').toLocaleUpperCase();
  const actor = data => String(data?.principal_id || data?.actor?.id || data?.actor?.principal_id || '');
  const self = (member, data) => actor(data) ? Boolean(identity(member)) && identity(member) === actor(data) : member?.is_self === true;
  const role = member => ({ owner: 'Organization admin', operator: 'Member', observer: 'Viewer' }[member?.role] || 'Member');
  const asList = value => Array.isArray(value) ? value : [];
  const scope = data => JSON.stringify([actor(data), data?.environment_id || data?.environment?.id || '', data?.collection || '']);
  const contactFor = (member, data) => self(member, data) && Object.hasOwn(data || {}, 'contact') ? data.contact : member?.contact || asList(data?.contacts).find(item => String(item.member_id || item.principal_id || '') === identity(member)) || (self(member, data) ? data?.contact : null);
  const readiness = data => data?.readiness || {};

  /* Channels, in channel order. Each one is named by its own name everywhere. */
  const CHANNELS = Object.freeze(['whatsapp', 'viber', 'email', 'voice']);
  const LABELS = Object.freeze({ whatsapp: 'WhatsApp', viber: 'Viber', email: 'Email', voice: 'Voice', sms: 'SMS' });
  const VIA = Object.freeze({ whatsapp: 'on WhatsApp', viber: 'on Viber', email: 'by email', voice: 'by voice call', sms: 'by SMS' });
  const channelLabel = channel => LABELS[channel] || '';
  const builtChannel = data => LABELS[data?.provider] ? data.provider : 'whatsapp';
  const contactChannel = (contact, data) => LABELS[contact?.channel] ? contact.channel : builtChannel(data);
  function channelList(data) {
    const listed = asList(data?.channels), built = builtChannel(data), base = readiness(data), known = Object.keys(base).length > 0;
    const names = Array.isArray(data?.channels) ? listed.map(item => item?.channel).filter(name => LABELS[name]) : CHANNELS;
    return names.map(channel => {
      const row = listed.find(item => item?.channel === channel) || null, own = channel === built && known;
      const ready = own ? base.otp_ready === true || base.ready === true : row?.ready === true;
      return { channel, label: LABELS[channel], ready, reason: (own ? base.reason : row?.reason) || (ready ? null : 'provider_setup_required'), sender: row?.sender || (own ? base.sender_phone : null) || null };
    });
  }
  function contactsFor(member, data) {
    const found = [], add = contact => { if (contact?.phone && !found.some(item => contactChannel(item, data) === contactChannel(contact, data))) found.push(contact); };
    add(contactFor(member, data));
    for (const item of asList(data?.contacts)) if (String(item?.member_id || item?.principal_id || '') === identity(member)) add(item);
    return found;
  }
  const contactOn = (member, data, channel) => contactsFor(member, data).find(item => contactChannel(item, data) === channel) || null;
  const noteFor = (reason, label) => ({ auth_template_required: 'Security code delivery is being prepared. Continue in the messaging chat for setup updates.', hello_template_required: `Your number is verified. The ${label} welcome message is being prepared.`, webhook_setup_required: `${label} replies are being connected to your chats.` }[reason] || `${label} is being connected.`);
  const timeLabel = value => { const date = new Date(typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : value); return value && Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date) : ''; };
  const sentence = names => names.length < 2 ? names.join('') : `${names.slice(0, -1).join(', ')} and ${names.at(-1)}`;

  /* Delivery state labels: CHANNELS.md, "Labels for delivery state". */
  function deliveryLabel(delivery) {
    const status = delivery?.status, reason = delivery?.held_reason;
    if (status === 'held') return reason === 'spend_cap' ? 'Today’s alert limit reached' : reason === 'stopped' ? 'Alerts paused' : reason === 'number_required' ? 'Add a number' : 'Alert kept in Rocoto';
    return { queued: 'Sending', dispatching: 'Sending', sent: 'Sent', delivered: 'Delivered', read: 'Read', uncertain: 'Confirming delivery', failed: 'Delivery needs attention', cancelled: 'Alerts paused', expired: 'Alert kept in Rocoto' }[status] || '';
  }
  function deliveryText(delivery) { const state = deliveryLabel(delivery), channel = channelLabel(delivery?.channel); return state && channel && !['held', 'cancelled', 'expired'].includes(delivery.status) ? `${channel} · ${state}` : state; }
  /* The chip under an agent's own answer: the same states as before, naming the channel it travelled on. */
  function chipLabel(delivery) {
    const channel = channelLabel(delivery?.channel) || LABELS.whatsapp;
    if (delivery?.waiting_for_reply) return `Waiting for your reply in ${channel}`;
    return { queued: `Sending to ${channel}`, dispatching: `Sending to ${channel}`, sent: `Sent to ${channel}`, delivered: `Delivered in ${channel}`, read: `Read in ${channel}`, uncertain: `Confirming ${channel} delivery`, failed: `${channel} delivery needs attention` }[delivery?.status] || '';
  }
  /* A send that needs a person's attention reads in ink. */
  const needsAttention = delivery => delivery?.status === 'failed';
  /* A message that arrived from a phone names its channel. */
  function senderLabel(sender, ownId) {
    const channel = channelLabel(sender?.channel); if (!channel) return '';
    const mine = !sender.principal_id || String(sender.principal_id) === String(ownId || '');
    return mine || !sender.display_name ? `Sent from ${channel}` : `${sender.display_name} · sent from ${channel}`;
  }

  async function api(path, body) {
    const response = await fetch(path, body === undefined ? { credentials: 'same-origin', cache: 'no-store' } : { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json', 'X-Rocoto-Page': '1' }, body: JSON.stringify(body) });
    const text = await response.text(); let value = null; try { value = JSON.parse(text); } catch {}
    if (!response.ok) { const detail = typeof value?.detail === 'string' ? value.detail : value === null && text.length <= 200 && !text.includes('<') ? text.trim() : ''; const error = new Error(detail.slice(0, 400) || 'Phone alerts are reconnecting. Try again.'); error.status = response.status; throw error; }
    return value;
  }

  function button(node, label, fn, className = 'messaging-button') { const el = node('button', className, label); el.type = 'button'; el.addEventListener('click', fn); return el; }
  function avatar(node, member, large = false) { const el = node('span', `messaging-avatar${large ? ' messaging-avatar-large' : ''}`, initials(member)); el.setAttribute('aria-hidden', 'true'); return el; }
  function labelValue(node, label, value) { const row = node('div', 'messaging-profile-row'); row.append(node('span', 'messaging-muted', label), node('span', '', value)); return row; }
  function render(data = {}, { node = make, icon, onMember, onSetup } = {}) {
    const root = node('section', 'messaging-settings'), head = node('header', 'messaging-settings-heading');
    head.append(node('h1', '', 'Messaging')); if (onSetup) head.append(button(node, 'Messaging setup chat', () => onSetup(), 'messaging-button messaging-button-quiet')); root.append(head, node('p', 'messaging-muted', 'Verified numbers of people in your organization.'));
    if (data.loading || data.error) { const status = node('p', 'messaging-status', data.error || 'Loading numbers…'); status.setAttribute('role', 'status'); root.append(status); return root; }
    const members = asList(data.members), own = members.find(member => self(member, data)), list = node('div', 'messaging-number-list');
    for (const member of members) for (const contact of contactsFor(member, data)) {
      const channel = contactChannel(contact, data), row = button(node, '', () => onMember?.(member, 'channel', channel), 'messaging-number-row'), copy = node('span', 'messaging-number-copy');
      copy.append(node('strong', '', name(member)), node('span', 'messaging-muted', `${LABELS[channel]} · ${contact.phone}`)); row.append(avatar(node, member), copy, node('span', 'messaging-number-status', contact.verified === false ? 'Verification in progress' : 'Verified'));
      if (icon) row.append(icon('chevron')); row.setAttribute('aria-label', `${name(member)}, ${LABELS[channel]}, ${contact.phone}`); list.append(row);
    }
    if (list.childNodes.length) root.append(list);
    else { const empty = node('div', 'messaging-empty'); if (icon) empty.append(icon('chat')); empty.append(node('h2', '', 'Your messaging channels'), node('p', 'messaging-muted', 'Verify your contact details. Agent messages arrive there, and replies continue the same chat.')); root.append(empty); }
    if (own && !contactsFor(own, data).length) root.append(button(node, 'Add a number', () => onMember?.(own, 'channel')));
    for (const item of channelList(data)) if (item.sender) root.append(node('p', 'messaging-provider-note', `${item.label} messages arrive from Rocoto · ${item.sender}`));
    return root;
  }

  function openMember({ member, data = {}, page = 'profile', channel: wanted = '', purpose = '', onClose, onRequestCode, onVerifyCode, onSetup, node = make, icon } = {}) {
    const dialog = node('dialog', 'messaging-profile-dialog'), previous = document.activeElement, pageOf = value => ['channel', 'whatsapp'].includes(value) ? 'channel' : 'profile';
    let state = data, current = pageOf(page), alive = true, epoch = 0, pending = false, error = '', country = '', phone = '', code = '', expiryTimer, picked = LABELS[wanted] ? wanted : '';
    const unique = `messaging-member-${Math.random().toString(36).slice(2)}`;
    dialog.setAttribute('aria-labelledby', `${unique}-title`);
    /* The channel in use on this card: the member's pick, their verified channel, the first ready channel, then the built transport. */
    function selected() {
      const list = channelList(state);
      if (picked && (list.find(item => item.channel === picked)?.ready || contactOn(member, state, picked))) return picked;
      const verified = contactFor(member, state); if (verified?.phone) return contactChannel(verified, state);
      if (state.challenge?.id && LABELS[state.challenge.channel]) return state.challenge.channel;
      return list.find(item => item.ready)?.channel || builtChannel(state);
    }
    function dispose() { if (!alive) return; alive = false; ++epoch; clearTimeout(expiryTimer); code = ''; dialog.querySelectorAll('input').forEach(field => { field.value = ''; }); dialog.remove(); if (previous?.isConnected) previous.focus({ preventScroll: true }); onClose?.(); }
    function close() { if (!alive) return; if (dialog.open) dialog.close(); dispose(); }
    function setup() { const contact = contactFor(member, state); close(); onSetup?.(member, contact); }
    function show(next) { if (!alive) return; ++epoch; pending = false; error = ''; code = ''; current = pageOf(next); draw(current === 'channel' ? 'entry' : 'channel'); }
    function focus(key) { const version = epoch; queueMicrotask(() => { if (!alive || version !== epoch || !dialog.open) return; const entry = () => ['code', 'country', 'setup', 'channel-option', 'back', 'close'].map(item => dialog.querySelector(`[data-focus="${item}"]:not(:disabled)`)).find(Boolean); const target = key === 'entry' ? entry() : dialog.querySelector(`[data-focus="${key}"]:not(:disabled)`) || entry(); target?.focus({ preventScroll: true }); }); }
    function input(label, value, key, { placeholder = '', autocomplete = '', maxLength = 30 } = {}) { const wrap = node('label', 'messaging-field'), el = node('input', 'messaging-input'); wrap.append(node('span', '', label)); el.type = 'text'; el.inputMode = 'tel'; el.autocomplete = autocomplete; el.placeholder = placeholder; el.maxLength = maxLength; el.value = value; el.dataset.focus = key; wrap.append(el); return { wrap, el }; }
    async function run(action, apply) {
      if (pending || !alive || !self(member, state)) return;
      const version = ++epoch; pending = true; error = ''; draw();
      try { const value = await action(); if (!alive || version !== epoch) return; apply(value); }
      catch (cause) { if (!alive || version !== epoch) return; error = typeof cause?.message === 'string' ? cause.message.slice(0, 400) : 'Try this step again.'; }
      finally { if (alive && version === epoch) { pending = false; draw(state.challenge && !contactOn(member, state, selected())?.verified ? 'code' : 'setup'); } }
    }
    function channelPicker(active) {
      const group = node('fieldset', 'messaging-channels'); group.append(node('legend', '', 'Channel'));
      for (const item of channelList(state)) {
        const verified = contactOn(member, state, item.channel)?.verified === true, open = item.ready || verified || item.channel === active, option = node('label', 'messaging-channel'), radio = node('input');
        radio.type = 'radio'; radio.name = `${unique}-channel`; radio.value = item.channel; radio.checked = item.channel === active; radio.disabled = !open || pending; if (radio.checked) radio.dataset.focus = 'channel-option';
        radio.addEventListener('change', () => { if (!radio.checked || item.channel === selected()) return; ++epoch; pending = false; error = ''; code = ''; picked = item.channel; draw('channel-option'); });
        option.append(radio, node('span', 'messaging-channel-name', item.label), node('span', 'messaging-channel-state', verified ? 'Verified' : item.ready ? 'Ready' : 'Being connected')); group.append(option);
      }
      return group;
    }
    function draw(preferred) {
      if (!alive) return;
      clearTimeout(expiryTimer);
      const heldFocus = preferred || (dialog.contains(document.activeElement) && document.activeElement !== dialog ? document.activeElement.dataset.focus || 'entry' : '');
      dialog.replaceChildren(); dialog.dataset.page = current; let shown = null;
      const cover = node('div', 'messaging-profile-cover'); cover.setAttribute('aria-hidden', 'true');
      const currentMember = asList(state.members).find(item => identity(item) === identity(member));
      const artworkData = currentMember ? currentMember.cover : member?.cover;
      if (/^\/api\/profile-covers\/cover-[a-f0-9]{32}$/.test(artworkData?.url || '') && artworkData?.target === 'member' && artworkData.target_id === identity(member)) {
        const artwork = node('img', 'messaging-profile-cover-image'); artwork.alt = ''; artwork.src = artworkData.url;
        artwork.addEventListener('error', () => artwork.remove(), { once: true }); cover.append(artwork);
      }
      const closeButton = button(node, '', close, 'messaging-icon-button messaging-profile-close'); closeButton.setAttribute('aria-label', purpose ? 'Close contact card' : 'Close profile'); closeButton.dataset.focus = 'close'; if (icon) closeButton.append(icon('close')); else closeButton.textContent = '×';
      dialog.append(cover, closeButton);
      const body = node('div', 'messaging-profile-body'), person = node('header', 'messaging-profile-person'), title = node('h2', '', name(member)); title.id = `${unique}-title`;
      person.append(avatar(node, member, true), title, node('p', 'messaging-muted', purpose || role(member))); body.append(person);
      const isSelf = self(member, state);
      if (current === 'profile') {
        const saved = contactsFor(member, state)[0], details = node('div', 'messaging-profile-details'); if (member?.email) details.append(labelValue(node, 'Email', member.email)); details.append(labelValue(node, 'Access', role(member))); body.append(details);
        const control = button(node, '', () => show('channel'), 'messaging-profile-whatsapp'); control.dataset.focus = 'channel'; const copy = node('span', 'messaging-number-copy'); copy.append(node('strong', '', 'Channel and contact'), node('span', 'messaging-muted', saved?.phone ? `${LABELS[contactChannel(saved, state)]} · ${saved.phone}` : isSelf ? 'Verify your contact details to receive agent messages' : 'This member’s verified contact details')); if (icon) control.append(icon('chat')); control.append(copy, node('span', 'messaging-profile-action', saved?.phone ? 'View' : isSelf ? 'Add' : 'View')); control.setAttribute('aria-label', isSelf && !saved?.phone ? 'Add a number' : 'Channel and contact'); body.append(control);
      } else {
        const active = selected(), label = LABELS[active], via = VIA[active], addressKind = active === 'email' ? 'email address' : 'number', contact = contactOn(member, state, active), ready = channelList(state).find(item => item.channel === active)?.ready === true;
        if (!purpose) { const back = button(node, 'Back to profile', () => show('profile'), 'messaging-back'); back.dataset.focus = 'back'; if (icon) back.prepend(icon('chevron')); body.append(back); }
        const panel = node('section', `messaging-enrollment${purpose ? ' messaging-contact-card' : ''}`); panel.append(node('h3', '', 'Channel and contact'), channelPicker(active));
        const problem = error ? node('p', 'messaging-status messaging-error', error) : null; let placed = false; if (problem) problem.setAttribute('role', 'alert');
        if (!isSelf) {
          if (contact?.phone) panel.append(node('p', 'messaging-verified-number', contact.phone), node('p', 'messaging-muted', `Verified ${label} contact`));
          panel.append(node('p', 'messaging-muted', `${name(member)} verifies their own contact details from their profile.`));
        } else if (contact?.verified === true) {
          panel.append(node('p', 'messaging-verified-number', contact.phone), node('p', 'messaging-muted', `Your ${label} contact is verified.`));
          if (purpose) { panel.append(node('p', 'messaging-muted', `${purpose} arrive here. Your replies continue the same chat.`)); const done = button(node, 'Done', close); done.dataset.focus = 'setup'; panel.append(done); }
          else {
            const outbox = asList(state.outbox).filter(item => item.kind !== 'otp'), latest = outbox[0];
            if (latest) { const receipt = node('div', 'messaging-receipt'); const text = latest.text || latest.body || latest.message; if (typeof text === 'string' && text) receipt.append(node('p', 'messaging-message-preview', text)); const status = ({ uncertain: 'Confirming message delivery', sending: 'Sending your message…', queued: 'Preparing your message…', sent: 'Message sent', delivered: 'Message delivered', read: 'Message read', failed: 'Message delivery needs attention' }[latest.status] || 'Checking message delivery'); receipt.append(node('p', 'messaging-muted', status)); panel.append(receipt); }
            if (state.waiting && !['uncertain', 'failed', 'sending', 'queued'].includes(latest?.status)) { const waiting = node('p', 'messaging-waiting', 'Waiting for your reply'); waiting.setAttribute('role', 'status'); panel.append(waiting, node('p', 'messaging-muted', `Reply in ${label} to see your message in the messaging setup chat.`)); }
            else if (active === builtChannel(state) && !readiness(state).hello_ready && readiness(state).hello_reason) panel.append(node('p', 'messaging-status', noteFor(readiness(state).hello_reason, label)));
            else if (!latest) panel.append(node('p', 'messaging-muted', 'Continue in the messaging setup chat to send your first hello.'));
            const next = button(node, 'Continue in messaging chat', setup); next.dataset.focus = 'setup'; panel.append(next);
          }
        } else if (state.challenge?.id) {
          const challenge = state.challenge, delivered = ['sent', 'delivered', 'read'].includes(challenge.status), destination = challenge.phone || challenge.masked_phone || 'your number';
          const expired = ['revoked', 'superseded', 'failed', 'verified'].includes(challenge.status) || (Number.isFinite(challenge.expires_at) && challenge.expires_at * 1000 <= Date.now()) || challenge.attempts_remaining === 0;
          const confirmable = !expired && ['sent', 'delivered', 'read', 'uncertain'].includes(challenge.status);
          if (!expired && Number.isFinite(challenge.expires_at)) expiryTimer = setTimeout(() => draw(), Math.min(2147483647, Math.max(1, challenge.expires_at * 1000 - Date.now() + 10)));
          panel.append(node('p', 'messaging-muted', delivered ? `Enter the security code sent to ${destination} ${via}.` : `Check the security code for ${destination} ${via}.`));
          if (expired) panel.append(node('p', 'messaging-status', 'Request a fresh security code to continue.'));
          if (challenge.status === 'uncertain') panel.append(node('p', 'messaging-status', 'Confirming security code delivery.'));
          const form = node('form', 'messaging-form'), field = input('Security code', code, 'code', { autocomplete: 'one-time-code', placeholder: '000000', maxLength: 6 }); field.el.inputMode = 'numeric'; field.el.pattern = '[0-9]{6}'; field.el.required = true; field.el.disabled = pending;
          const verify = button(node, pending ? 'Verifying…' : active === 'email' ? 'Verify email' : 'Verify number', () => {}); verify.type = 'submit'; verify.disabled = pending || !confirmable || !/^\d{6}$/.test(code) || typeof onVerifyCode !== 'function'; field.el.addEventListener('input', () => { code = field.el.value; verify.disabled = !confirmable || !/^\d{6}$/.test(code) || pending || typeof onVerifyCode !== 'function'; });
          form.append(field.wrap, ...(problem ? [problem] : []), verify); placed = Boolean(problem); form.addEventListener('submit', event => { event.preventDefault(); if (verify.disabled) return; run(() => onVerifyCode({ challenge_id: challenge.id, code, member_id: identity(member) }), result => { if (result?.contact) state = { ...state, ...result, challenge: null }; else if (result?.verified === true) state = { ...state, contact: { channel: active, ...result }, challenge: null }; else throw new Error(`Enter the code from ${label} to verify your number.`); code = ''; }); }); panel.append(form);
          const change = button(node, expired ? 'Request a new code' : `Change ${addressKind}`, () => { ++epoch; pending = false; error = ''; code = ''; state = { ...state, challenge: null }; draw('country'); }, 'messaging-button messaging-button-quiet'); panel.append(change);
        } else {
          panel.append(node('p', 'messaging-muted', active === 'email' ? 'Add your email address. Rocoto sends a security code there.' : `Add your number with its country code. Rocoto sends a security code ${via}.`));
          const form = node('form', 'messaging-form'), row = node('div', 'messaging-phone-fields'), prefix = input('Country code', country, 'country', { placeholder: '+34', autocomplete: 'tel-country-code', maxLength: 4 }), number = input('Phone number', phone, 'phone', { placeholder: '612 345 678', autocomplete: 'tel-national', maxLength: 24 });
          prefix.el.required = active !== 'email'; number.el.required = true; prefix.el.disabled = number.el.disabled = pending;
          if (active === 'email') { number.wrap.querySelector('span')?.replaceChildren(document.createTextNode('Email address')); number.el.type = 'email'; number.el.inputMode = 'email'; number.el.placeholder = 'you@company.com'; number.el.autocomplete = 'email'; number.el.maxLength = 254; }
          const normalized = () => active === 'email' ? phone.trim() : '+' + country.trim().replace(/^\+/, '') + phone.replace(/[\s().-]/g, '');
          const send = button(node, pending ? 'Sending security code…' : 'Send security code', () => {}); send.type = 'submit';
          function valid() { return active === 'email' ? /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(normalized()) : /^\+?[1-9]\d{0,2}$/.test(country.trim()) && /^\+[1-9]\d{7,14}$/.test(normalized()); }
          function sync() { send.disabled = pending || !valid() || !ready || typeof onRequestCode !== 'function'; }
          prefix.el.addEventListener('input', () => { country = prefix.el.value; sync(); }); number.el.addEventListener('input', () => { phone = number.el.value; sync(); }); sync();
          /* Without a channel the verification route selects WhatsApp, the built transport. */
          if (active !== 'email') row.append(prefix.wrap); row.append(number.wrap); form.append(row, ...(problem ? [problem] : []), send); placed = Boolean(problem); form.addEventListener('submit', event => { event.preventDefault(); if (send.disabled) return; run(() => onRequestCode({ phone: normalized(), member_id: identity(member), ...(active === 'whatsapp' ? {} : { channel: active }) }), result => { const challenge = result?.challenge || result; if (!challenge?.id) throw new Error('Try sending your security code again.'); state = { ...state, ...(result?.readiness ? result : {}), challenge: { channel: active, ...challenge } }; }); }); panel.append(form);
          if (!ready) { panel.append(node('p', 'messaging-status', noteFor(active === builtChannel(state) ? readiness(state).reason : '', label))); if (onSetup) panel.append(button(node, 'Continue in messaging chat', setup, 'messaging-button messaging-button-quiet')); }
        }
        if (problem && !placed) panel.append(problem);
        body.append(panel); shown = problem;
      }
      dialog.append(body); if (heldFocus) focus(heldFocus); if (shown && dialog.open) shown.scrollIntoView({ block: 'nearest' });
    }
    dialog.addEventListener('close', dispose);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
    document.body.append(dialog); draw(); dialog.showModal(); focus(current === 'channel' ? 'entry' : 'channel');
    return { element: dialog, close, show, update(nextData = {}) { if (!alive) return; const changed = scope(nextData) !== scope(state); if (changed) { ++epoch; pending = false; member = { ...member, contact: null, cover: null }; country = phone = code = ''; error = ''; picked = ''; } state = nextData; draw(); } };
  }

  /* The contact card of a Tracking Agent: the participant's own channel card, on the built verification routes. */
  function openContactCard({ block, participant, agentTitle = '', node = make, icon, onClose } = {}) {
    const collection = block?.collection || 'company', memberId = String(participant?.member_id || ''), query = `/api/messaging?member_id=${encodeURIComponent(memberId)}&collection=${encodeURIComponent(collection)}`;
    const shape = data => ({ ...(data || {}), collection, channels: asList(data?.channels).length ? data.channels : asList(block?.channels) });
    let open = true, touched = false;
    const card = openMember({ member: { id: memberId, display_name: participant?.name, is_self: participant?.is_self === true }, data: shape({}), page: 'channel', purpose: agentTitle ? `Alerts from ${agentTitle}` : 'Alerts from this agent', node, icon,
      onClose: () => { open = false; onClose?.(); },
      onRequestCode: payload => { touched = true; return api('/api/messaging/verification', { ...payload, collection }); },
      onVerifyCode: payload => { touched = true; return api('/api/messaging/verification/confirm', { ...payload, collection }); } });
    api(query).then(data => { if (open && !touched && data && typeof data === 'object') card.update(shape(data)); }).catch(() => {});
    return card;
  }

  /* "Alerts on your phone": one Tracking Agent's participants, channels, delivery state and switches. */
  function mountPhone({ agentId, agentTitle = '', node = make, icon, readOnly = false, onChange } = {}) {
    const root = node('section', 'phone-block'), unique = `phone-${Math.random().toString(36).slice(2)}`, base = `/api/agents/${encodeURIComponent(agentId)}/phone`;
    let block = null, title = agentTitle, alive = true, epoch = 0, loading = false, failure = '', error = '', pending = false, card = null, leaving = false, timer;
    root.setAttribute('aria-labelledby', `${unique}-title`); root.dataset.agentId = agentId;
    const valid = value => Boolean(value) && typeof value === 'object' && value.agent_id === agentId && Array.isArray(value.participants) && Array.isArray(value.channels);
    const locked = () => readOnly || block?.read_only === true, announce = () => { const current = block; queueMicrotask(() => { if (alive) onChange?.(current); }); };
    function schedule() { clearTimeout(timer); if (alive) timer = setTimeout(() => { if (root.getClientRects().length && !document.hidden && !pending && !card && !root.contains(document.activeElement)) load(true); else schedule(); }, 30000); }
    async function load(quiet = false) {
      if (!alive) return; const version = ++epoch; if (!quiet) { loading = true; failure = ''; draw(); }
      try { const value = await api(base); if (!alive || version !== epoch) return; if (!valid(value)) throw new Error('Phone alerts are reconnecting. Try again.'); const changed = JSON.stringify(value) !== JSON.stringify(block); block = value; failure = ''; if (!quiet || changed) { loading = false; draw(); announce(); } }
      catch (cause) { if (!alive || version !== epoch) return; if (!quiet || !block) failure = cause.message; }
      finally { if (alive && version === epoch) { if (loading) { loading = false; draw(); } schedule(); } }
    }
    async function change(path, body, focusKey) {
      if (!alive || pending || locked() || !block) return;
      const version = ++epoch; pending = true; error = ''; leaving = false; draw(focusKey);
      try { const value = await api(base + path, body); if (!alive || version !== epoch) return; if (!valid(value)) throw new Error('Refresh the phone alerts and repeat your change.'); block = value; announce(); }
      catch (cause) { if (!alive || version !== epoch) return; error = cause.message; if (cause.status === 409 || cause.status === 404) { try { const fresh = await api(base); if (alive && version === epoch && valid(fresh)) { block = fresh; announce(); } } catch {} } }
      finally { if (alive && version === epoch) { pending = false; draw(focusKey); schedule(); } }
    }
    const generation = () => ({ expected_generation: block.generation });
    function contactCard(participant) {
      if (card || !alive) return;
      card = openContactCard({ block, participant, agentTitle: title, node, icon, onClose: () => { card = null; if (alive) load(true); } });
    }
    async function addNumber(participant, key) {
      if (locked() || pending) return;
      if (!participant.contact_request && participant.verification === 'number_required') await change('/contact-request', { member_id: participant.member_id }, key);
      if (alive && !error && participant.is_self) { const fresh = block.participants.find(item => item.member_id === participant.member_id) || participant; if (fresh.verification !== 'verified') contactCard(fresh); }
    }
    function toggle(label, checked, key, disabled, apply) {
      const control = node('input', 'phone-switch'); control.type = 'checkbox'; control.setAttribute('role', 'switch'); control.setAttribute('aria-label', label); control.checked = checked; control.disabled = disabled; control.dataset.focus = key;
      control.addEventListener('change', () => { if (pending) { control.checked = !control.checked; return; } apply(control.checked); }); return control;
    }
    function spendLine(spend) {
      if (!spend || typeof spend !== 'object') return '';
      if (spend.state === 'cap_reached') { const resume = timeLabel(spend.resets_at); return `Today’s alert limit reached. Alerts stay in Rocoto${resume ? ` and phone delivery starts again on ${resume}` : ''}.`; }
      if (spend.state === 'stopped') return 'Phone alerts are paused for your company. Alerts arrive in Rocoto.';
      if (spend.state === 'unavailable') return 'Phone alerts wait for your company’s daily message limit. Alerts arrive in Rocoto.';
      const used = Number(spend.agent?.used), cap = Number(spend.agent?.cap);
      return Number.isFinite(used) && Number.isFinite(cap) && cap > 0 ? `${used} of ${cap} messages sent today` : '';
    }
    /* What holds this agent's phone alerts right now, ahead of any participant's own state. */
    function hold(value) {
      const state = value?.spend?.state;
      if (value?.delivery === 'paused') return { heading: 'Phone alerts paused', row: 'Alerts paused' };
      if (state === 'stopped') return { heading: 'Phone alerts paused for your company', row: 'Alerts paused' };
      if (state === 'unavailable') return { heading: 'Phone alerts waiting', row: 'Waiting for the daily message limit' };
      if (state === 'cap_reached') return { heading: 'Phone alerts in Rocoto today', row: 'Today’s alert limit reached' };
      if (value?.history === 'full') return { heading: 'Phone alerts in Rocoto', row: 'Alert kept in Rocoto' };
      return null;
    }
    function participantRow(person, manage) {
      const row = node('li', 'phone-participant'), member = { id: person.member_id, display_name: person.name }, copy = node('div', 'phone-participant-copy'), controls = node('div', 'phone-participant-controls'), who = name(member), key = person.member_id;
      row.dataset.memberId = person.member_id;
      const heading = node('p', 'phone-participant-name'); heading.append(node('strong', '', who)); const tags = [person.role === 'creator' ? 'Creator' : '', person.is_self ? 'You' : ''].filter(Boolean).join(' · '); if (tags) heading.append(node('span', 'messaging-muted', tags)); copy.append(heading);
      const request = person.contact_request?.status === 'open' ? person.contact_request : null;
      const address = person.verification === 'verified' && person.channel ? [channelLabel(person.channel), person.address_masked].filter(Boolean).join(' · ')
        : person.verification === 'verified' ? [sentence(asList(person.verified_channels).map(channelLabel).filter(Boolean)), 'verified'].filter(Boolean).join(' · ')
        : person.verification === 'in_progress' ? 'Verification in progress'
        : person.verification === 'opted_out' ? 'Alerts stopped from this phone'
        : person.is_self ? 'Add a number to get alerts on your phone.' : request ? `Number requested${timeLabel(request.created_at) ? ` · ${timeLabel(request.created_at)}` : ''}` : 'Waiting for a verified number';
      copy.append(node('p', 'phone-participant-channel', address));
      const last = person.last_delivery, held = hold(block), state = last ? [deliveryText(last), timeLabel(last.updated_at)].filter(Boolean).join(' · ') : person.verification === 'verified' && person.enabled ? held ? held.row : 'Ready for the first alert' : '';
      if (state) copy.append(node('p', `phone-participant-delivery${needsAttention(last) ? ' phone-attention' : ' messaging-muted'}`, state));
      if (person.is_self && asList(person.verified_channels).length) {
        const field = node('label', 'phone-field'), select = node('select', 'phone-select'), choices = [['automatic', 'Automatic'], ...asList(block.channels).filter(item => LABELS[item?.channel]).map(item => [item.channel, LABELS[item.channel], item])];
        field.append(node('span', 'messaging-muted', 'Channel')); select.setAttribute('aria-label', `Channel for ${who}`); select.dataset.focus = `channel:${key}`;
        for (const [value, label, item] of choices) { const option = node('option', '', item && !item.ready ? `${label} · being connected` : item && !person.verified_channels.includes(value) ? `${label} · add a number` : label); option.value = value; option.disabled = Boolean(item) && (!item.ready || !person.verified_channels.includes(value)); select.append(option); }
        const saved = person.channel_choice === 'picked' && person.channel ? person.channel : 'automatic'; select.value = saved; select.disabled = locked();
        select.addEventListener('change', () => pending ? (select.value = saved) : change(`/participants/${encodeURIComponent(person.member_id)}`, { channel: select.value, ...generation() }, `channel:${key}`)); field.append(select); controls.append(field);
      }
      const alerts = node('label', 'phone-field phone-field-switch'); alerts.append(node('span', 'messaging-muted', person.enabled ? 'Alerts on' : 'Alerts off'), toggle(`Alerts for ${who}`, person.enabled === true, `switch:${key}`, locked() || !(person.is_self || manage && person.enabled), enabled => change(`/participants/${encodeURIComponent(person.member_id)}`, { enabled, ...generation() }, `switch:${key}`))); controls.append(alerts);
      if (['number_required', 'in_progress'].includes(person.verification)) {
        if (person.is_self) { const add = button(node, person.verification === 'in_progress' ? 'Enter security code' : 'Add a number', () => addNumber(person, `number:${key}`), 'messaging-button phone-button'); add.dataset.focus = `number:${key}`; add.disabled = locked(); controls.append(add); }
        else if (manage && !request && person.verification === 'number_required') { const ask = button(node, 'Ask for a number', () => addNumber(person, `number:${key}`), 'messaging-button messaging-button-quiet phone-button'); ask.dataset.focus = `number:${key}`; ask.setAttribute('aria-label', `Ask ${who} for a number`); ask.disabled = locked(); controls.append(ask); }
      }
      if (person.role === 'added' && (manage || person.is_self)) {
        const confirm = person.is_self && leaving, remove = button(node, person.is_self ? confirm ? 'Confirm leaving' : 'Leave' : 'Remove', () => { if (person.is_self && !leaving) { leaving = true; draw(`remove:${key}`); return; } change(`/participants/${encodeURIComponent(person.member_id)}/remove`, generation(), 'entry'); }, 'messaging-button messaging-button-quiet phone-button');
        remove.dataset.focus = `remove:${key}`; remove.setAttribute('aria-label', person.is_self ? confirm ? 'Confirm leaving these phone alerts' : 'Leave these phone alerts' : `Remove ${who}`); remove.disabled = locked(); controls.append(remove);
      }
      row.append(avatar(node, member), copy, controls); return row;
    }
    function draw(preferred) {
      if (!alive) return;
      const active = root.contains(document.activeElement) ? document.activeElement : null, heldFocus = preferred || active?.dataset.focus || '';
      root.replaceChildren(); root.setAttribute('aria-busy', String(loading || pending));
      const head = node('header', 'phone-block-heading'), titles = node('div', 'phone-block-titles'), heading = node('h3', '', 'Alerts on your phone'); heading.id = `${unique}-title`;
      titles.append(heading, node('p', 'messaging-muted', 'Each participant gets this agent’s alerts on their own verified phone. A reply from the phone continues the same chat.')); head.append(titles); root.append(head);
      if (!block) {
        const status = node('p', 'messaging-status', failure || 'Loading phone alerts…'); status.setAttribute('role', 'status'); root.append(status);
        if (failure) { const retry = button(node, 'Try again', () => load(), 'messaging-button messaging-button-quiet phone-button'); retry.dataset.focus = 'entry'; root.append(retry); }
        if (heldFocus) restore(heldFocus); return;
      }
      const manage = block.can_manage === true && !locked(), on = block.delivery !== 'paused', delivery = node('label', 'phone-field phone-field-switch phone-delivery'), policyLabel = hold(block)?.heading || 'Phone alerts on';
      if (block.can_manage === true) delivery.append(node('span', '', policyLabel), toggle('Phone alerts for this agent', on, 'delivery', locked(), checked => change('/delivery', { state: checked ? 'on' : 'paused', ...generation() }, 'delivery')));
      else delivery.append(node('span', 'messaging-muted', policyLabel));
      head.append(delivery);
      const channels = asList(block.channels).filter(item => LABELS[item?.channel]), readyNames = channels.filter(item => item.ready).map(item => LABELS[item.channel]), connecting = channels.filter(item => !item.ready).map(item => LABELS[item.channel]);
      const facts = [readyNames.length ? `${sentence(readyNames)} ${readyNames.length > 1 ? 'are' : 'is'} ready.` : '', connecting.length ? `${sentence(connecting)} ${connecting.length > 1 ? 'are' : 'is'} being connected.` : ''].filter(Boolean).join(' ');
      if (facts) root.append(node('p', 'phone-channels messaging-muted', facts));
      const spend = spendLine(block.spend); if (spend) root.append(node('p', `phone-spend${block.spend.state === 'open' ? ' messaging-muted' : ' messaging-status'}`, spend));
      if (block.history === 'full') root.append(node('p', 'phone-history messaging-status', 'Phone delivery history is full. Alerts arrive in Rocoto.'));
      const list = node('ul', 'phone-participants'); list.setAttribute('aria-label', 'Participants'); for (const person of block.participants) list.append(participantRow(person, manage)); root.append(list);
      const available = asList(block.available_members).filter(item => item?.member_id);
      if (manage && block.participants.length >= 20) root.append(node('p', 'messaging-muted', 'This agent holds its full 20 participants.'));
      else if (manage && available.length) {
        const form = node('form', 'phone-add'), field = node('label', 'phone-field'), select = node('select', 'phone-select'), add = button(node, 'Add', () => {}, 'messaging-button phone-button');
        field.append(node('span', '', 'Add a participant')); select.dataset.focus = 'add-member'; for (const item of available) { const option = node('option', '', item.verified ? item.name || 'Team member' : `${item.name || 'Team member'} · needs a number`); option.value = item.member_id; select.append(option); }
        add.type = 'submit'; add.dataset.focus = 'add'; field.append(select); form.append(field, add);
        form.addEventListener('submit', event => { event.preventDefault(); if (select.value) change('/participants', { member_id: select.value, ...generation() }, 'add-member'); }); root.append(form);
      }
      if (error) { const message = node('p', 'messaging-status messaging-error', error); message.setAttribute('role', 'alert'); root.append(message); }
      if (heldFocus) restore(heldFocus);
    }
    function restore(key) {
      const controls = 'input:not(:disabled),select:not(:disabled),button:not(:disabled)', memberId = key.includes(':') ? key.slice(key.indexOf(':') + 1) : '';
      const row = memberId ? [...root.querySelectorAll('.phone-participant')].find(item => item.dataset.memberId === memberId) : null;
      (root.querySelector(`[data-focus="${CSS.escape(key)}"]:not(:disabled)`) || row?.querySelector(controls) || root.querySelector(controls))?.focus({ preventScroll: true });
    }
    draw(); load();
    return { element: root, agentId, refresh: () => load(true), block: () => block,
      setTitle(next) { title = typeof next === 'string' ? next : title; },
      setReadOnly(next) { if (readOnly !== Boolean(next)) { readOnly = Boolean(next); draw(); } },
      destroy() { if (!alive) return; alive = false; ++epoch; clearTimeout(timer); card?.close(); card = null; root.remove(); } };
  }

  window.RocotoMessaging = Object.freeze({ render, openMember, openContactCard, mountPhone, channelLabel, deliveryLabel, deliveryText, chipLabel, senderLabel });
})();
