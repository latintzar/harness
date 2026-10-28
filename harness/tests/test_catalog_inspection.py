"""Pixel provenance, isolation and restart semantics of automatic item inspection."""
import hashlib
from pathlib import Path
import sys
import pytest
from PIL import Image
sys.path.insert(0, str(Path(__file__).parents[1]))
from catalog_inspection import CatalogInspection, link_catalog


def service(tmp_path, **overrides):
    movie = tmp_path / 'recording.mp4'; movie.write_bytes(b'registered source fixture')
    digest = hashlib.sha256(movie.read_bytes()).hexdigest()
    source = {'path': movie, 'sha256': digest, 'end': 10}
    event = {'id': 'event-1', 'description': 'Lifts the green carton.'}
    payload = {'source_id': 'camera-a', 'source_sha256': digest, 'source_time_s': 3.25,
        'object_bbox': [.25, .25, .5, .75]}
    saved = []
    args = dict(authorized=lambda owner,event: owner == 'peter',
        source_resolver=lambda *args: source, catalog=lambda *args: [],
        publish_match=lambda *args: saved.append(args),
        frame_reader=lambda *args: Image.new('RGB', (400, 200), 'green'), ocr=lambda image: '')
    args.update(overrides)
    return CatalogInspection(tmp_path, **args), event, payload, saved, source, args


def test_crop_preserves_source_pixels_and_retry_repairs_projection(tmp_path):
    store, event, payload, saved, source, args = service(tmp_path)
    result = store.process('peter', event, payload, 'job-1')
    assert result['status'] == 'unresolved'
    assert result['evidence']['source_time_s'] == 3.25
    assert (result['evidence']['width'], result['evidence']['height']) == (100, 100)
    args['frame_reader'] = lambda *args: pytest.fail('A completed retry decodes no pixels')
    restarted = CatalogInspection(tmp_path, **args)
    assert restarted.process('peter', event, payload, 'job-1') == result
    assert len(saved) == 2  # Re-applying the idempotent projection is intentional.
    image = restarted.image('peter', result['inspection_id'], lambda *args: event)
    assert Image.open(image).size == (100, 100)
    with pytest.raises(ValueError): restarted.image('another', result['inspection_id'], lambda *args: event)


def test_revocation_stops_even_a_previously_saved_crop(tmp_path):
    store, event, payload, _, _, _ = service(tmp_path)
    result = store.process('peter', event, payload, 'job-1')
    store.authorized = lambda *args: False
    with pytest.raises(ValueError): store.process('peter', event, payload, 'job-1')
    with pytest.raises(ValueError): store.image('peter', result['inspection_id'], lambda *args: event)


def test_full_frame_ocr_does_not_confirm_an_unrelated_shelf_identifier(tmp_path):
    catalog = [{'id': 'sku-a', 'sku': '987654321', 'status': 'confirmed'}]
    assert link_catalog('987654321', catalog)['status'] == 'unresolved'
    assert link_catalog('987654321', catalog, object_grounded=True)['product_id'] == 'sku-a'
    duplicate = catalog + [{'id': 'sku-b', 'sku': '987654321', 'status': 'confirmed'}]
    assert link_catalog('987654321', duplicate, object_grounded=True)['status'] == 'unresolved'
    assert link_catalog('', catalog, proposed_id='foreign-sku')['product_id'] is None


def test_model_geometry_is_validated_and_uncertain_call_is_not_repeated(tmp_path):
    calls = []
    def inspect(**kwargs):
        calls.append(kwargs['request_id'])
        return {'object_bbox': [0, 0, 5, 5]}
    store, event, payload, *_ = service(tmp_path, inspect_image=inspect)
    payload['object_bbox'] = None
    with pytest.raises(ValueError): store.process('peter', event, payload, 'job-1')
    with pytest.raises(ValueError): store.process('peter', event, payload, 'job-1')
    assert len(calls) == 1


def test_changed_source_pixels_are_rejected_before_inspection(tmp_path):
    store, event, payload, _, source, _ = service(tmp_path,
        frame_reader=lambda *args: pytest.fail('Changed source must not be decoded'))
    source['path'].write_bytes(b'replaced bytes')
    with pytest.raises(ValueError): store.process('peter', event, payload, 'job-1')


def test_model_full_frame_text_cannot_confirm_another_product(tmp_path):
    catalog = [{'id': 'sku-a', 'sku': '987654321', 'status': 'confirmed'}]
    calls = []
    def ocr(image):
        calls.append(image.size)
        return '987654321' if image.size == (400, 200) else ''
    def inspect(**kwargs):
        return {'object_bbox': [.25,.25,.5,.75], 'visible_text': '987654321', 'product_id': 'sku-a'}
    store,event,payload,*_ = service(tmp_path, catalog=lambda *args:catalog, ocr=ocr, inspect_image=inspect)
    payload['object_bbox'] = None
    result = store.process('peter', event, payload, 'job-1')
    assert result['status'] == 'candidate'
    assert calls == [(400,200),(100,100)]
    assert result['evidence']['ocr_text'] == ''


@pytest.mark.parametrize('box', [None, [0,0,1,1]])
def test_unverified_rectangles_keep_shelf_identifiers_unconfirmed(tmp_path,box):
    catalog=[{'id':'shelf-sku','sku':'987654321','status':'confirmed'}]
    store,event,payload,*_=service(tmp_path,catalog=lambda *args:catalog,
        ocr=lambda image:'987654321',inspect_image=lambda **kwargs:{'object_bbox':[0,0,1,1],'product_id':None})
    payload['object_bbox']=box
    assert store.process('peter',event,payload,'job-1')['status']=='unresolved'


def test_host_verified_crop_can_confirm_unique_identifier(tmp_path):
    catalog=[{'id':'sku-a','sku':'987654321','status':'confirmed'}]
    store,event,payload,*_=service(tmp_path,catalog=lambda *args:catalog,
        ocr=lambda image:'987654321',object_verified=lambda *args:True)
    result=store.process('peter',event,payload,'job-1')
    assert result['status']=='confirmed' and result['product_id']=='sku-a'
