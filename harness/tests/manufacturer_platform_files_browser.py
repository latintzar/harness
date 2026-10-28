#!/usr/bin/env python3
"""Exercise company-file extension admission, queueing and scoped draft recovery.

Run with Playwright Chromium installed; all requests and chat admission are local fixtures.
"""
from pathlib import Path

from playwright.sync_api import sync_playwright

WEB = Path(__file__).resolve().parents[1] / 'verticals/manufacturer/web'
HTML = '''<!doctype html><meta charset="utf-8"><title>Company file attachment proof</title><body>
<template data-workspace-slot="source-attachments"></template>
<form id="composer"><textarea id="message"></textarea><button id="send">Send</button></form>
<button id="attach-images">Images</button><template data-workspace-slot="source-attachment-action"></template>
<p id="composer-note"></p><p id="notice"></p><main id="messages"></main>
<script>
window.chat = {id:'organization-qa',kind:'organization_chat',messages:[]};
window.accepted=0;
const node=(tag,cls='',text='')=>{const value=document.createElement(tag);
  value.className=cls;value.textContent=text;return value;};
window.host={node,icon:()=>node('span'),$:id=>document.getElementById(id),activeChat:()=>chat,
activeId:chat.id,session:{can_query:true},sessionGeneration:1,elements:{message:document.getElementById('message'),send:document.getElementById('send')},
fileSize:value=>value+' B',persistDraft(){},toggleAttachmentMenu(){},
notice(value){document.getElementById('notice').textContent=value;},
updateMode(){if(!extension.updateAlternateComposer())this.elements.send.disabled=false;}};
</script>'''


def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-gpu'])
        page = browser.new_page()
        errors, uploads = [], []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.route('https://files.test/', lambda route: route.fulfill(content_type='text/html', body=HTML))
        page.route('**/api/manufacturer/files', lambda route: uploads.append(route))
        page.goto('https://files.test/')
        page.add_script_tag(path=str(WEB / 'manufacturer-files.js'))
        page.evaluate('''() => {
          window.extension=RocotoManufacturerFiles(host);
          extension.loadDrafts(':qa-owner',key=>JSON.parse(localStorage.getItem(key)||'[]'));
          extension.renderComposerAttachments();
          document.getElementById('composer').addEventListener('submit',event=>{event.preventDefault();accepted++;});
        }''')
        page.locator('#manufacturer-file-input').set_input_files({
            'name': 'Prices <script>.csv', 'mimeType': 'text/csv', 'buffer': b'SKU,price\n123,12.50'})
        page.locator('#manufacturer-file-drafts small').wait_for(state='attached')
        assert page.locator('#send').is_disabled()
        page.locator('#composer').evaluate('(form)=>form.requestSubmit()')
        assert page.evaluate('accepted') == 0
        original, reading = 'file-' + 'a' * 32, 'file-' + 'b' * 32
        assert uploads[0].request.headers['x-rocoto-page'] == '1'
        uploads.pop(0).fulfill(json={
            'file': {'id': original, 'name': 'Prices <script>.csv', 'size_bytes': 18},
            'read_file': {'id': reading}, 'chat_id': 'organization-qa', 'extraction': {'status': 'ready'}})
        page.locator('#manufacturer-file-drafts a').wait_for()
        assert page.locator('#manufacturer-file-drafts script').count() == 0
        assert page.locator('#send').is_enabled()
        page.locator('#message').fill('Update the matching prices.')
        envelope = page.evaluate('''() => {
          const draft=extension.composerData(host.activeId), request=extension.requestData(draft);
          const normalized=extension.messageData({role:'user',content:request.message});
          extension.renderUserAttachments(document.getElementById('messages'),normalized);
          return {request,normalized,
            queued:extension.requestData({...draft,query:'Use these prices from November.'})};
        }''')
        assert original in envelope['request']['message'] and reading in envelope['request']['message']
        assert envelope['normalized']['text'] == 'Update the matching prices.'
        assert envelope['queued']['message'].startswith('Use these prices from November.')
        assert page.locator('#messages').inner_text().startswith('Prices <script>.csv')
        assert '<manufacturer-files>' not in page.locator('#messages').inner_text()
        assert page.evaluate("extension.messageData({role:'assistant',content:"
                             "'Example\\n\\n<manufacturer-files>\\n{}\\n</manufacturer-files>'})") == {}
        page.evaluate('''() => {
          extension.resetWorkspace();host.sessionGeneration++;
          extension.loadDrafts(':another-company',key=>JSON.parse(localStorage.getItem(key)||'[]'));
          extension.renderComposerAttachments();
        }''')
        assert page.locator('#manufacturer-file-drafts').is_hidden()
        page.evaluate('''() => {
          extension.loadDrafts(':qa-owner',key=>JSON.parse(localStorage.getItem(key)||'[]'));
          extension.renderComposerAttachments();
        }''')
        page.locator('#manufacturer-file-drafts a').wait_for()
        page.evaluate('extension.setComposerReadOnly(true)')
        assert page.locator('#manufacturer-attach-files').is_disabled()
        assert page.get_by_role('button', name='Remove attachment Prices <script>.csv').is_disabled()
        page.evaluate('extension.setComposerReadOnly(false)')
        page.get_by_role('button', name='Remove attachment Prices <script>.csv').click()
        assert page.locator('#manufacturer-file-drafts').is_hidden()
        page.locator('#manufacturer-file-input').set_input_files({
            'name': 'Corrupt.pdf', 'mimeType': 'application/pdf', 'buffer': b'%PDF corrupt'})
        page.locator('#manufacturer-file-drafts small').wait_for(state='attached')
        uploads.pop(0).fulfill(status=400, json={'detail': 'Upload an exported PDF.'})
        page.get_by_text('Upload an exported PDF.', exact=True).wait_for()
        assert page.locator('#send').is_disabled()
        page.get_by_role('button', name='Remove attachment Corrupt.pdf').click()
        assert page.locator('#send').is_enabled()
        page.evaluate('extension.clearDraft(host.activeId)')
        assert page.evaluate("JSON.parse(localStorage.getItem("
                             "'rocoto-manufacturer-file-drafts-v1:qa-owner')).length") == 0
        assert not errors, errors
        browser.close()
    print('Company files: pending/error admission, original/reading refs, safe chips, queue edits, '
          'scoped reload recovery and read-only controls passed.')


if __name__ == '__main__':
    run()
