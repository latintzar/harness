'use strict';
(async () => {
  const params = new URLSearchParams(location.search);
  const body = {state: params.get('state'), code: params.get('code'), error: params.get('error')};
  history.replaceState(null, '', location.pathname);
  const title = document.getElementById('connection-title');
  const message = document.getElementById('connection-message');
  try {
    if (!body.state) throw new Error('authorization');
    const response = await fetch(location.pathname, {method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Rocoto-Page': '1'}, body: JSON.stringify(body)});
    if (!response.ok) throw new Error('authorization');
    const result = await response.json();
    if (['failed', 'expired', 'interrupted', 'disconnected', 'declined'].includes(result.status)) throw new Error('authorization');
    const connected = result.status === 'connected';
    title.textContent = connected ? 'Account connected' : 'Authorization received';
    message.textContent = connected ? 'Your read tools are ready in Rocoto.' : 'Return to Connections to follow the account check.';
    if (window.opener) window.opener.postMessage({type: 'rocoto:connection-update'}, location.origin);
  } catch {
    title.textContent = 'Connection needs review';
    message.textContent = 'Return to Connections in the session that started this request and connect again.';
  }
})();
