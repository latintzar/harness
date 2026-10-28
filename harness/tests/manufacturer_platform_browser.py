#!/usr/bin/env python3
"""Exercise the real vertical tabs and native mount lifecycle without provider calls.

Set PLAYWRIGHT_BROWSERS_PATH=/home/peter/.cache/ms-playwright, then run
python3 harness/tests/manufacturer_platform_browser.py.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'verticals/manufacturer/web'


def run():
    original = {
        'settings': {'accent': '#444444', 'headline': 'Find your door hardware',
                     'description': 'Choose the parts for your project.',
                     'button_label': 'Start order', 'company_slug': 'metal-ls',
                     'embed_url': 'https://orders-demo.soyrocoto.com/metal-ls/rocoto/configurator.js',
                     'preview_url': '/api/manufacturer/preview/v1'},
        'document': {'configuration': {'questionnaire': {'id': 'doors', 'questions': [
            {'id': 'purpose', 'kind': 'single', 'title': {'en': 'Which door?', 'bg': 'Коя врата?'},
             'unsure': True,
             'options': [{'id': 'inside', 'label': {'en': 'Interior'}, 'match': {'door': ['inside']}},
                         {'id': 'outside', 'label': {'en': 'Exterior'}}]}]}},
            'catalogue': {'cats': [{'id': 'locks', 'items': [{'c': '001', 'p': 4.5}]}]}, 'attachments': []},
        'revision': 1, 'version_id': 'v1', 'state': 'draft', 'published': False,
        'actor': {'id': 'qa-person', 'can_edit_settings': True, 'can_manage_team': True},
    }
    current = copy.deepcopy(original)
    versions = {'v1': copy.deepcopy(original)}
    writes = []
    reads = []
    browser_errors = []
    order = {'id': 'qa-order', 'number': 'MLS-001', 'created_at': '2026-10-14T10:00:00Z',
             'status': 'requested',
             'customer': {'name': 'Alex', 'company': '<img src=x onerror=alert(1)>',
                          'email': 'alex@example.com',
                          'website': 'https://example.com', 'project': 'Hotel doors'},
             'items_count': 1, 'total': 44, 'currency': 'EUR', 'fulfilment': 'delivery',
             'enrichment': {'status': 'enriched', 'summary': 'A hotel renovation company.',
                            'sources': [{'title': 'Company website', 'url': 'https://example.com'},
                                        {'title': 'Unsafe link', 'url': 'javascript:alert(1)'}]},
             'lines': [{'name': 'Door lock', 'quantity': 2, 'total': 44}]}

    def metadata(version):
        return {'id': version['version_id'], 'revision': version['revision'],
                'created_at': '2026-10-14T10:00:00Z',
                'author': {'id': 'qa-person', 'name': 'Alex Morgan', 'type': 'user'}, 'name': '',
                'is_current': version['version_id'] == current['version_id'], 'document': version['document'],
                'settings': version['settings']}

    def respond(route):
        nonlocal current
        path = route.request.url.split('manufacturer.test', 1)[-1]
        if path == '/' or path.startswith('/?'):
            route.fulfill(content_type='text/html', body='''<!doctype html>
            <meta charset="utf-8"><title>Manufacturer UI proof</title>
            <style>:root{--ink:#222;--paper:#fff;--surface:#f6f6f6;--border:#ddd;--border-strong:#bbb;
            --muted:#666;--accent:#5751d6;--radius:6px;--radius-lg:10px;--radius-sm:4px;
            --font:system-ui;--font-code:monospace;--font-size-ui:14px}
            *{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui;color:var(--ink)}
            [hidden]{display:none!important}.icon{width:20px;height:20px;fill:none;stroke:currentColor}
            button,input,textarea,select{font:inherit}button{cursor:pointer;color:inherit}</style>
            <link rel="stylesheet" href="/manufacturer.css"><main id="root"></main>
            <script src="/workspace-tabs.js"></script><script src="/manufacturer-tabs.js"></script>
            <script>window.tabs=RocotoWorkspaceTabs.create({root:document.getElementById('root'),context:{},defaultHome:{mount(){}}});
            tabs.configure({vertical:{id:'manufacturer',revision:'test'},surfaces:[{id:'orders'},{id:'configurator'},{id:'company'}]},'company-qa');tabs.setVisible(true);tabs.open('orders');</script>''')
        elif path == '/manufacturer.css':
            route.fulfill(content_type='text/css', body=(WEB / 'manufacturer.css').read_text())
        elif path in {'/workspace-tabs.js', '/manufacturer-tabs.js'}:
            source = (ROOT / 'web/workspace-tabs.js' if path.startswith('/workspace')
                      else WEB / 'manufacturer-tabs.js')
            route.fulfill(content_type='application/javascript', body=source.read_text())
        elif path.startswith('/api/manufacturer/preview/'):
            route.fulfill(content_type='text/html', body=f'<h1>Saved snapshot {path.rsplit("/",1)[-1]}</h1>')
        elif path.startswith('/api/manufacturer/'):
            reads.append(path)
            if path == '/api/manufacturer/overview':
                result = {'company': {'name': 'Metal LS'}, 'orders': [order], 'revision': 1}
            elif path == '/api/manufacturer/orders/qa-order':
                result = {'order': order}
            elif path == '/api/manufacturer/orders/temporary':
                route.fulfill(status=503, json={'detail': 'The workspace is reconnecting. Try again.'})
                return
            elif path == '/api/manufacturer/configurator/versions':
                result = {'versions': [metadata(v) for v in reversed(list(versions.values()))],
                          'revision': current['revision']}
            elif '/configurator/versions/' in path:
                version_id = path.split('/versions/')[1].split('/')[0]
                if path.endswith('/restore'):
                    payload = route.request.post_data_json
                    assert payload['expected_revision'] == current['revision']
                    writes.append(('restore', payload))
                    revision = current['revision'] + 1
                    current = copy.deepcopy(versions[version_id])
                    current.update(revision=revision, version_id=f'v{revision}')
                    versions[current['version_id']] = copy.deepcopy(current)
                    result = current
                else:
                    result = {'version': metadata(versions[version_id]), 'revision': current['revision']}
            elif path == '/api/manufacturer/configurator':
                if route.request.method == 'POST':
                    payload = route.request.post_data_json
                    assert payload['expected_revision'] == current['revision']
                    writes.append(('save', copy.deepcopy(payload)))
                    current['settings'].update(payload['settings'])
                    current['document'] = copy.deepcopy(payload['document'])
                    current['revision'] += 1
                    current['version_id'] = f"v{current['revision']}"
                    versions[current['version_id']] = copy.deepcopy(current)
                result = current
            else:
                route.fulfill(status=404, json={'message': 'Unknown test route'})
                return
            route.fulfill(content_type='application/json', body=json.dumps(result))
        else:
            route.abort()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox','--disable-gpu'])
        page = browser.new_page(viewport={'width': 1600, 'height': 1000})
        page.on('pageerror', lambda error: browser_errors.append(str(error)))
        page.route('**/*', respond)
        page.goto('https://manufacturer.test/')
        page.get_by_role('link', name='Open order MLS-001', exact=False).wait_for()
        assert page.locator('.mf-order-customer img').count() == 0
        assert 'Updates live' not in page.locator('.mf-orders-page').inner_text()
        assert page.locator('select[aria-label="Order status"]').is_hidden()
        initial_history = page.evaluate('history.length')
        row = page.locator('[data-order-id="qa-order"]')
        assert 'order=qa-order' in row.get_attribute('href')
        row.evaluate("""el => {
            const event = new MouseEvent('click', {bubbles:true, cancelable:true, ctrlKey:true});
            window.modifiedClickPreserved = el.dispatchEvent(event);
        }""")
        assert page.evaluate('modifiedClickPreserved')
        assert 'order=' not in page.url
        row.click()
        page.get_by_role('heading', name='Customer research').wait_for()
        assert page.evaluate('history.length') == initial_history + 1
        page.go_back()
        page.get_by_role('heading', name='Orders', exact=True).wait_for()
        assert 'order=' not in page.url
        assert page.locator('[data-order-id="qa-order"]:focus').count() == 1
        page.go_forward()
        page.get_by_role('heading', name='Customer research').wait_for()
        assert 'order=qa-order' in page.url
        assert page.evaluate('history.length') == initial_history + 1
        assert page.get_by_role('link', name='Unsafe link').count() == 0
        assert page.get_by_role('link', name='Company website').get_attribute('rel') == 'noopener noreferrer'
        page.get_by_role('button', name='Orders', exact=True).click()
        page.get_by_role('heading', name='Orders', exact=True).wait_for()
        before = len([path for path in reads if path.endswith('/overview')])
        page.evaluate('tabs.setVisible(false)')
        page.wait_for_timeout(5200)
        assert len([path for path in reads if path.endswith('/overview')]) == before
        page.evaluate("tabs.setVisible(true);tabs.open('configurator')")
        page.get_by_label('Main text', exact=True).wait_for()
        page.get_by_label('Main text', exact=True).fill('Choose your hardware')
        page.wait_for_timeout(1000)
        assert current['settings']['headline'] == 'Choose your hardware'
        assert writes[-1][1]['document']['configuration']['questionnaire']['questions'][0]['unsure'] is True
        page.get_by_label('Question 1, option 1', exact=True).fill('Interior doors')
        page.wait_for_timeout(1000)
        question = current['document']['configuration']['questionnaire']['questions'][0]
        assert question['options'][0]['match'] == {'door': ['inside']}
        page.get_by_label('Main text', exact=True).fill('Keep this unfinished edit')
        page.get_by_role('button', name='Version history', exact=True).click()
        page.locator('[data-version-id="v1"]').click()
        page.wait_for_function("document.querySelector('.mf-version-frame').src.endsWith('/preview/v1')")
        page.get_by_role('button', name='Restore this version', exact=True).click()
        page.get_by_label('Main text', exact=True).wait_for()
        assert current['settings']['headline'] == original['settings']['headline']
        assert writes[-2][0] == 'save'
        assert writes[-2][1]['settings']['headline'] == 'Keep this unfinished edit'
        assert writes[-1][0] == 'restore'
        page.get_by_label('Main text', exact=True).fill('Keep across navigation')
        page.evaluate("tabs.open('orders')")
        page.get_by_role('heading', name='Orders', exact=True).wait_for()
        page.evaluate("tabs.open('configurator')")
        page.get_by_label('Main text', exact=True).wait_for()
        assert page.get_by_label('Main text', exact=True).input_value() == 'Keep across navigation'
        page.wait_for_timeout(1000)
        assert current['settings']['headline'] == 'Keep across navigation'
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Phone layout overflows'
        page.goto('https://manufacturer.test/?order=removed')
        page.get_by_role('link', name='Open order MLS-001', exact=False).wait_for()
        assert 'order=' not in page.url
        assert page.locator('.mo-skeleton-product').count() == 0
        page.goto('https://manufacturer.test/?order=temporary')
        page.get_by_role('button', name='Try again', exact=True).wait_for()
        assert page.locator('.mo-skeleton-product').count() == 0
        held = []
        page.route('**/api/manufacturer/orders/temporary', lambda route: held.append(route))
        page.wait_for_timeout(5500)
        assert len(held) == 1, 'Expected periodic detail refresh while the retry page is open'
        page.get_by_role('button', name='Try again', exact=True).click()
        page.wait_for_timeout(100)
        assert len(held) == 2, 'Retry must cancel the older request and start a fresh one'
        held[-1].fulfill(json={'order': order})
        page.get_by_role('heading', name='Products', exact=True).wait_for()
        held[0].abort()
        page.unroute('**/api/manufacturer/orders/temporary')
        page.get_by_role('button', name='Orders', exact=True).click()
        order['enrichment'] = {'status': 'queued'}
        order['pricing_state'] = 'to_quote'
        order['total'] = None
        page.locator('[data-order-id="qa-order"]').click()
        page.get_by_text('Quote requested', exact=True).wait_for()
        assert page.get_by_role('heading', name='Customer research', exact=True).count() == 0
        assert not browser_errors, browser_errors
        browser.close()
    print('Manufacturer UI: order navigation, retry recovery, safe content, paused polling, '
          'autosave, questionnaire preservation, exact restore and draft recovery passed.')


if __name__ == '__main__':
    run()
