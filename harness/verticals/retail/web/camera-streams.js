/* Camera stream setup is separate from the imported recording registry. */
(() => {
  'use strict';
  function render({ connections = [], query = '', loading = false, error = '', canManage = false, onSetup, onRetry }, { node, icon }) {
    const section = node('section', 'camera-streams');
    section.setAttribute('aria-label', 'Camera streams');
    const heading = node('div', 'camera-streams-heading');
    heading.append(node('h2', '', 'Streams'), node('p', '', 'Camera streams and connection setup.'));
    section.append(heading);
    if (loading) { const status = node('p', 'camera-streams-status', 'Loading camera connections…'); status.setAttribute('role', 'status'); section.append(status); return section; }
    if (error) {
      const status = node('div', 'camera-streams-status'); status.setAttribute('role', 'status'); status.append(node('p', '', error));
      if (onRetry) { const retry = node('button', 'agent-secondary', 'Retry'); retry.type = 'button'; retry.addEventListener('click', onRetry); status.append(retry); }
      section.append(status); return section;
    }
    // These are operator setup records, not verified device observations. A future
    // health adapter must provide a separate measured stream contract before a
    // record can gain a Live badge, a player or any uptime/quality percentage.
    const setups = connections.filter(item => item.type === 'camera_stream');
    const search = query.trim().toLocaleLowerCase();
    const visible = setups.filter(item => `${item.provider || ''} ${item.source_description || ''}`.toLocaleLowerCase().includes(search));
    if (!visible.length) {
      const empty = node('div', 'camera-streams-empty');
      empty.append(icon('video'), node('h3', '', search ? 'Try another camera name' : 'Connect your cameras'));
      if (!search) {
        empty.append(node('p', '', 'Connect a camera to see live video, resolution and seven-day uptime.'));
        if (canManage && onSetup) { const add = node('button', 'agent-primary', 'Add camera'); add.type = 'button'; add.prepend(icon('plus')); add.addEventListener('click', () => onSetup(null, add)); empty.append(add); }
      }
      section.append(empty); return section;
    }
    const rows = node('div', 'camera-streams-list');
    for (const connection of visible) {
      const row = node('article', 'camera-stream-row'), mark = node('span', 'camera-stream-mark'); mark.append(icon('video'));
      const copy = node('div', 'camera-stream-copy'); copy.append(node('h3', '', connection.provider || 'Camera connection'));
      if (connection.source_description) copy.append(node('p', '', connection.source_description));
      copy.append(node('span', 'camera-stream-state', 'Setup in progress'));
      row.append(mark, copy);
      if (canManage && onSetup) { const manage = node('button', 'agent-secondary', 'Continue setup'); manage.type = 'button'; manage.addEventListener('click', () => onSetup(connection, manage)); row.append(manage); }
      rows.append(row);
    }
    section.append(rows, node('p', 'camera-streams-footnote', 'Live previews and connection measurements appear after the stream is verified.'));
    return section;
  }
  window.RocotoCameraStreams = { render };
})();
