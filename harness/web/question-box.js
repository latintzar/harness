/* Host-owned question metadata. Callbacks choose fixed private routes; this module has no transport or storage. */
(() => {
  'use strict';
  function element(tag, className = '', text = '') { const item = document.createElement(tag); item.className = className; item.textContent = text; return item; }
  function mount(container, request, helpers = {}) {
    const secure = request.secure === true, questions = Array.isArray(request.questions) ? request.questions.slice(0, 3) : [];
    const box = element('section', `question-box${secure ? ' question-box-secure' : ''}`);
    box.setAttribute('role', 'group'); box.setAttribute('aria-label', secure ? 'Secure entry' : 'Question');
    let destroyed = false, busy = false, expired = false, page = 0, timer = null;
    const answers = Object.create(null), selected = new Set();
    const glyph = name => { const mark=helpers.icon ? helpers.icon(name) : element('span', '', ({ close:'×', arrow:'→', lock:'⌑', chat:'?', eye:'◉', 'eye-off':'◉' })[name] || ''); mark.setAttribute('aria-hidden','true'); return mark; };
    const button = (label, className, action, symbol) => { const item = element('button', className, label); item.type = 'button'; if (symbol) item.append(glyph(symbol)); if (action) item.addEventListener('click', action); return item; };
    const header = element('header', 'question-box-heading'), name = element('span', 'question-box-kind', secure ? 'Secure entry' : 'Question');
    if (!secure) name.prepend(glyph('chat'));
    const status = element('span', 'question-box-deadline'), close = button('', 'question-box-close', () => send('skip'), 'close'); close.setAttribute('aria-label', secure ? 'Skip secure entry' : 'Skip question'); close.title = close.getAttribute('aria-label');
    header.append(name); if (!secure) header.append(status); if (secure) { const lock = element('span', 'question-box-lock'); lock.append(glyph('lock')); lock.title = 'Saved privately for this connection. The agent receives confirmation only.'; header.append(lock); } header.append(close);
    const form = element('form', 'question-box-form'), content = element('div', 'question-box-content'), error = element('p', 'question-box-error'); error.setAttribute('role', 'status');
    const footer = element('footer', 'question-box-footer'), skip = button('Skip', 'question-box-skip', () => send('skip')), submit = button('Send', 'question-box-send', null, 'arrow'); submit.type = 'submit'; footer.append(skip, submit); form.append(content, error, footer); box.append(header, form); container.append(box);
    const current = () => !destroyed && (!helpers.canAnswer || helpers.canAnswer());
    const clearPrivate = () => { for (const input of box.querySelectorAll('[data-credential-name]')) { input.value = ''; input.type = 'password'; } };
    const value = () => { const question = questions[page], text = content.querySelector('.question-box-answer')?.value.trim(); if (!question) return null; const values = [...selected]; if (text) { if (!question.multiple) values.length = 0; if (!values.includes(text)) values.push(text); } return values.length && values.length <= 10 ? values : null; };
    function update() {
      for (const control of box.querySelectorAll('button,input,textarea')) control.disabled = busy || expired || !current();
      submit.disabled ||= secure ? !form.checkValidity() : !value();
      for (const row of box.querySelectorAll('.question-box-option')) row.setAttribute('aria-checked', String(selected.has(row.dataset.label)));
      box.setAttribute('aria-busy', String(busy));
    }
    function render() {
      selected.clear(); content.replaceChildren(); error.textContent = '';
      if (secure) {
        const title = element('h2', 'question-box-title', request.title || 'Connection credentials'); title.id = 'credential-request-title'; content.append(title);
        if (request.reason) { const reason = element('p', 'question-box-purpose', request.reason); reason.id = 'credential-request-purpose'; content.append(reason); }
        for (const field of (request.fields || []).slice(0, 16)) {
          if (!/^[A-Z][A-Z0-9_]{0,63}$/.test(field.name)) continue;
          const label = element('label', 'question-box-secret-label', field.label || field.name), row = element('div', 'question-box-secret-row'), input = element('input');
          input.id = `credential-value-${field.name}`; label.htmlFor = input.id; input.dataset.credentialName = field.name; input.type = 'password'; input.autocomplete = 'new-password'; input.spellcheck = false; input.maxLength = 16384; input.required = !field.configured; input.placeholder = field.configured ? 'Saved credential · enter to replace' : 'Enter credential';
          const reveal = button('', 'question-box-reveal', () => { const show = input.type === 'password'; input.type = show ? 'text' : 'password'; reveal.setAttribute('aria-label', `${show ? 'Hide' : 'Show'} ${field.label || field.name}`); reveal.setAttribute('aria-pressed', String(show)); reveal.replaceChildren(glyph(show ? 'eye-off' : 'eye')); }, 'eye'); reveal.setAttribute('aria-label', `Show ${field.label || field.name}`); reveal.setAttribute('aria-pressed', 'false');
          row.append(input, reveal); content.append(label, row); if (field.description) content.append(element('p', 'question-box-field-description', field.description));
        }
        content.append(element('p', 'question-box-private-note', 'Saved privately for this connection. The agent receives confirmation only.'));
      } else {
        const question = questions[page]; if (!question) return;
        const title = element('h2', 'question-box-title', question.question); title.id = 'native-question-title'; content.append(title);
        if (questions.length > 1) content.append(element('p', 'question-box-progress', `${page + 1} of ${questions.length}`));
        const options = element('div', 'question-box-options'); options.setAttribute('role', question.multiple ? 'group' : 'radiogroup'); options.setAttribute('aria-label', question.header || question.question);
        for (const [index, option] of (question.options || []).slice(0, 20).entries()) {
          const row = button('', 'question-box-option', () => {
            if (!current() || busy || expired) return;
            if (question.multiple) { if (selected.has(option.label)) selected.delete(option.label); else if (selected.size < 10) selected.add(option.label); update(); }
            else { selected.clear(); selected.add(option.label); const input = content.querySelector('.question-box-answer'); if (input) input.value = ''; proceed(); }
          }); row.dataset.label = option.label; row.setAttribute('role', question.multiple ? 'checkbox' : 'radio'); row.setAttribute('aria-checked', 'false');
          const number = element('span', 'question-box-number', String(index + 1)), copy = element('span', 'question-box-option-copy'); number.setAttribute('aria-hidden', 'true'); copy.append(element('strong', '', option.label)); if (option.description) copy.append(element('small', '', option.description)); const arrow = element('span', 'question-box-option-arrow'); arrow.append(glyph('arrow')); arrow.setAttribute('aria-hidden', 'true'); row.append(number, copy, arrow); options.append(row);
        }
        content.append(options);
        if (!question.options?.length || question.is_other) { const input = element('input', 'question-box-answer'); input.type = 'text'; input.maxLength = 4000; input.autocomplete = 'off'; input.placeholder = question.options?.length ? 'Or write your own answer' : 'Type your reply here'; input.setAttribute('aria-label', `Your answer · ${question.header || `Question ${page + 1}`}`); content.append(input); }
      }
      update();
    }
    async function proceed() {
      const values = value(); if (!values || !current() || busy || expired) return;
      answers[questions[page].id] = values;
      if (page < questions.length - 1) { page++; render(); content.querySelector('input,button')?.focus(); }
      else await send('submit');
    }
    async function send(action) {
      if (!current() || busy || (expired && action !== 'timeout')) return;
      let values = null; if (secure && action === 'submit') { if (!form.reportValidity()) return; values = Object.create(null); for (const input of content.querySelectorAll('[data-credential-name]')) if (input.value) values[input.dataset.credentialName] = input.value; }
      clearPrivate(); busy = true; error.textContent = ''; update();
      try {
        if (action === 'submit') { if (secure) await helpers.onSecureSubmit?.(values); else await helpers.onSubmit?.({...answers}); }
        else if (action === 'timeout') await helpers.onTimeout?.();
        else await helpers.onSkip?.();
      } catch { if (current()) error.textContent = secure ? 'Enter the credential again to retry, or choose Skip.' : 'Your answer is ready to send. Please try again.'; }
      finally { if (values) for (const key of Object.keys(values)) delete values[key]; clearPrivate(); busy = false; if (!destroyed) update(); }
    }
    const deadline = Date.parse(request.expires_at);
    function tick() {
      if (destroyed) return;
      const remaining = Number.isFinite(deadline) ? Math.max(0, Math.ceil((deadline - Date.now()) / 1000)) : null;
      status.textContent = busy ? 'Sending…' : remaining === null ? 'Waiting for your answer' : remaining ? `${remaining}s` : 'Time elapsed';
      if (remaining === 0 && !expired && !busy && current()) { expired = true; clearPrivate(); send('timeout'); }
    }
    form.addEventListener('input', update); form.addEventListener('submit', event => { event.preventDefault(); if (secure) send('submit'); else proceed(); });
    box.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); send('skip'); } });
    render(); timer = setInterval(tick, 250); queueMicrotask(tick);
    return { element:box, focus:() => content.querySelector('input,button')?.focus(), destroy() { destroyed = true; clearInterval(timer); clearPrivate(); for (const key of Object.keys(answers)) delete answers[key]; selected.clear(); box.remove(); } };
  }
  window.RocotoQuestionBox = Object.freeze({mount});
})();
