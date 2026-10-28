"""Retail camera and evidence HTTP routes installed by its host extension."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import time
import layout

def web_asset(relative):
    return layout.vertical_root()/"web"/relative
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field, ConfigDict
from camera_context import ContextDraft
from imported_media import VerifiedFile, imported_record, byte_response
from retired_tools import RETIREMENT_MESSAGE
from chat_permissions import workspace_writable

class CameraConfirmation(BaseModel):
    expected_revision: int = Field(ge=1)
    draft_id: str = Field(pattern=r"^draft-[a-f0-9]{32}$")


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: str = Field(min_length=1, max_length=100)
    document: dict
    chat_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,100}$")


class CameraReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    collection: str = Field(min_length=1, max_length=100)
    camera_ids: list[str] = Field(min_length=1, max_length=4)


def register_routes(app, host, http):
    operator=host.options["operator"]
    export_principal=http.file_owner
    require_parent_control=http.require_parent_control
    from monitoring_routes import register_monitoring_routes
    from control_room_routes import register_control_room_routes
    from location_intelligence_routes import register_location_intelligence_routes
    from realtime_read_routes import register_realtime_read_routes
    from catalog_setup_routes import register_catalog_setup_routes
    from profile_cover_host import register_routes as register_cover_routes
    from storage_routes import register_storage_routes
    from source_routes import register_source_routes
    from link_routes import register_link_routes
    register_monitoring_routes(app)
    register_control_room_routes(app)
    register_realtime_read_routes(app)
    register_location_intelligence_routes(app)
    register_catalog_setup_routes(app,host.workspace,host.access)
    register_cover_routes(app)
    register_storage_routes(app,http.connection_identity,lambda selected:selected['vertical'].storage_runtime(),
        owner_for_connector=http.custom_identity)
    register_source_routes(app,store_for_workspace=lambda selected:selected['vertical'].source_store(),
        policy_for_workspace=lambda selected:selected['vertical'].source_host().policy(),
        admission_for_workspace=lambda selected:selected['vertical'].source_host(),
        projection_for_workspace=lambda selected:selected['intake_projection'])
    register_link_routes(app,service_for_workspace=lambda selected:selected['vertical'].link_intake())
    from retail_urls import retail_router
    app = retail_router(app)

    @app.post('/api/retail/search')
    async def search(request: Request):
        # Authentication, company scope, and query permission are admitted by
        # local_operator_only before this route. The archive search workflow is
        # retired; keeping the route explicit prevents an embeddings/index call.
        raise HTTPException(410, RETIREMENT_MESSAGE)


    @app.post('/api/retail/cameras/review')
    async def review_cameras(body: CameraReviewRequest, request: Request):
        selected = request.state.workspace
        if not workspace_writable(request.state.session):
            raise HTTPException(403, "Return to your workspace to review cameras.")
        try:
            cameras = [await asyncio.to_thread(selected["cameras"].get, key, body.collection)
                       for key in dict.fromkeys(body.camera_ids)]
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, "Choose cameras in this business collection.") from exc
        import hashlib
        principal = selected["chats"].principal(request.state.session)
        references = [{"camera_id": camera["id"], "name": camera["name"],
                       "revision": camera["revision"], "clip": camera["reference_clip"],
                       "source_metadata": camera["source_metadata"]} for camera in cameras]
        identity = "camera-review-" + hashlib.sha256(json.dumps(
            [principal, body.collection, sorted((c["id"], c["revision"]) for c in cameras)]).encode()).hexdigest()[:24]
        agent = selected["agent"]
        if identity not in agent.children:
            prompt = ("Review these camera views for onboarding. Read their camera context and inspect the actual "
                "reference frames for each camera. Describe visible zones, angle, coverage, occlusion and useful "
                "search terms. Compare possible overlapping views using visible landmarks; distinguish a candidate "
                "spatial relationship from a verified shared room or synchronized recording. Use the supplied camera "
                "names. Propose evidence-anchored camera context drafts for operator review, citing source frame "
                "times. Keep model observations separate from operator-confirmed context. This orientation uses "
                "source frames and existing evidence; fresh GPU inspection is reserved for later investigations. "
                "Finish with a short camera-by-camera summary and any questions needed to confirm the layout.\n\n"
                "Host-selected camera references (reference data):\n" + json.dumps(references))
            agent.children[identity] = {"id": identity, "title": "Camera orientation", "kind": "camera_review",
                "parent_id": None, "owner_principal_id": principal, "dataset": body.collection,
                "camera_ids": [c["id"] for c in cameras], "running": True, "messages": [], "created_at": time.time()}
            agent._save_child(agent.children[identity])
            async def run_review():
                from execution_policy import FRESH_INSPECTION_ADMISSION
                def source_frames_only(maximum):
                    raise ValueError("Use original source frames for camera orientation.")
                token = FRESH_INSPECTION_ADMISSION.set(source_frames_only)
                try:
                    async for _ in agent.turn(prompt, identity, body.collection):
                        pass
                finally:
                    FRESH_INSPECTION_ADMISSION.reset(token)
                    agent.children[identity]["running"] = False
                    agent._save_child(agent.children[identity])
                    agent.child_tasks.pop(identity, None)
            agent.child_tasks[identity] = asyncio.create_task(run_review())
        return {**selected["chats"].get(agent.children[identity], request.state.session), "url": f"/?chat={identity}"}


    @app.post('/api/retail/exports')
    async def prepare_export(body: ExportRequest, request: Request):
        principal = export_principal(request, write=True)
        selected = request.state.workspace
        if body.chat_id:
            record = selected["agent"].children.get(body.chat_id)
            if not selected["chats"].can_write(record, request.state.session):
                raise HTTPException(404, "Choose a chat from your workspace.")
            require_parent_control(record)
            if record.get("dataset") != body.dataset:
                raise HTTPException(400, "Choose the business collection used by this chat.")
        try:
            result = await selected["exports"].prepare(body.document, dataset=body.dataset,
                                                       owner_principal_id=principal)
            if body.chat_id:
                await asyncio.to_thread(selected["files"].link_export, result["id"],
                    owner_principal_id=principal, chat_id=body.chat_id, dataset=body.dataset)
            return result
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc


    def export_scope_allowed(request,payload_id,principal):
        selected=request.state.workspace
        try:
            with selected['exports'].connect() as db:
                row=db.execute('SELECT body FROM exports WHERE id=? AND owner=?',(payload_id,principal)).fetchone()
            if row is None:return False
            document=json.loads(row[0]);dataset=document['dataset']
            view=selected['index'].snapshot(principal)
            available={row['id'] for row in view.clips if row['dataset']==dataset}
            return bool(document['selection']['items']) and all(item['clip_id'] in available for item in document['selection']['items'])
        except (ValueError,KeyError,TypeError,OSError,sqlite3.Error):return False


    def require_export_scope(request,payload_id,principal):
        guard=lambda:export_scope_allowed(request,payload_id,principal)
        if not guard():raise HTTPException(404,'Choose an editing package within your current source scope.')
        request.state.scope_guards.append(guard)


    @app.get('/api/retail/exports')
    async def list_exports(request: Request, dataset: str | None = None):
        principal = export_principal(request)
        rows=await asyncio.to_thread(request.state.workspace["exports"].list,owner_principal_id=principal,dataset=dataset)
        rows=[row for row in rows if export_scope_allowed(request,row["id"],principal)]
        request.state.scope_guards.append(lambda:all(export_scope_allowed(request,row["id"],principal) for row in rows))
        return {"exports":rows}


    @app.get('/api/retail/inspection-previews/{receipt_id}/{index}.jpg')
    async def inspection_preview(receipt_id: str, index: str, request: Request):
        from inspection_previews import InspectionPreviews, PreviewError
        selected = request.state.workspace
        permissions = selected['chats']
        principal = permissions.principal(request.state.session)
        headers = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff'}

        def unavailable():
            return HTTPException(404, 'Choose an inspection preview from this workspace.', headers=headers)

        if (principal is None or not re.fullmatch(r'[a-f0-9]{32}', receipt_id)
                or not re.fullmatch(r'[0-3]', index)):
            raise unavailable()
        try:
            row = await asyncio.to_thread(InspectionPreviews(selected['state']).get,
                owner_principal_id=principal, receipt_id=receipt_id, index=int(index))
            if row is None:
                row = await asyncio.to_thread(InspectionPreviews(selected['state']).get,
                    owner_principal_id=selected['organization_chats'].owner, receipt_id=receipt_id, index=int(index))
            if row is None:
                raise unavailable()
            chat = selected['agent'].children.get(row['chat_id'])
            if (not permissions.can_read(chat, request.state.session)
                    or chat.get('dataset') not in (None, row['dataset'])):
                raise unavailable()
            # The request middleware binds a freshly verified owner/source view.
            # A saved receipt never grants continued access to withdrawn footage.
            clip = await visible_clip(request, row['clip_id'])
            if clip is None or clip.get('dataset') != row['dataset']:
                raise unavailable()
            return Response(row['image_bytes'], media_type='image/jpeg', headers=headers)
        except (PreviewError, OSError, ValueError, KeyError, TypeError):
            raise unavailable() from None


    @app.get('/api/retail/exports/{payload_id}')
    async def get_export(payload_id: str, request: Request):
        principal = export_principal(request)
        require_export_scope(request,payload_id,principal)
        try:
            return await asyncio.to_thread(request.state.workspace["exports"].get, payload_id,
                                           owner_principal_id=principal)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.get('/api/retail/exports/{payload_id}/download')
    async def download_export(payload_id: str, request: Request):
        principal = export_principal(request)
        require_export_scope(request,payload_id,principal)
        try:
            path = await asyncio.to_thread(request.state.workspace["exports"].download_path, payload_id,
                                           owner_principal_id=principal)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, media_type="application/zip", filename=f"{payload_id}.zip",
                            headers={"Cache-Control": "private, no-store"})


    @app.get('/api/retail/cameras')
    async def cameras(request: Request, collection: str):
        try:
            return await asyncio.to_thread(request.state.workspace["cameras"].list, collection)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc


    @app.get('/api/retail/cameras/{camera_id}')
    async def camera_details(camera_id: str, request: Request, collection: str | None = None):
        try:
            return await asyncio.to_thread(request.state.workspace["cameras"].get, camera_id, collection)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc


    @app.post('/api/retail/cameras/{camera_id}/context')
    async def camera_context_draft(camera_id: str, body: ContextDraft, request: Request, collection: str | None = None):
        try:
            return await asyncio.to_thread(request.state.workspace["cameras"].propose, camera_id, body.model_dump(),
                                           origin="operator_note", collection=collection)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc) if isinstance(exc,ValueError) else "Reconnect the camera source to save context.") from exc


    @app.post('/api/retail/cameras/{camera_id}/confirm')
    async def confirm_camera_context(camera_id: str, body: CameraConfirmation, request: Request, collection: str | None = None):
        try:
            return await asyncio.to_thread(request.state.workspace["cameras"].confirm, camera_id, body.draft_id,
                                           expected_revision=body.expected_revision, operator=operator["name"], collection=collection)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(409, str(exc) if isinstance(exc,ValueError) else "Reconnect the camera source to confirm context.") from exc


    @app.get('/api/retail/library')
    async def source_library(request: Request, collection: str, folder: str = ""):
        try:
            return await asyncio.to_thread(request.state.workspace["library"].listing, collection, folder)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc


    @app.get('/api/retail/assets/{asset_id}')
    async def source_asset(asset_id: str, request: Request, collection: str | None = None):
        library = request.state.workspace["library"]
        try:
            asset = await asyncio.to_thread(library.resolve, asset_id, collection)
            return await asyncio.to_thread(library.public_asset, asset, include_clips=True)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, exc.args[0]) from exc


    async def visible_clip(request, clip_id):
        selected = request.state.workspace
        record = selected['index'].get_clip(clip_id)
        if record is not None: return record
        organization = selected['organization_chats']
        if not any(organization.can_read(chat, request.state.session) for chat in selected['agent'].children.values()):
            return None
        from scope_host import filter_view
        view = await asyncio.to_thread(selected['index'].snapshot, organization.owner, refresh=True,apply_scope=False)
        view = await asyncio.to_thread(filter_view,selected,view,selected['chats'].principal(request.state.session))
        request.state.scope_views.append(view)
        # ReadyIndex verifies the immutable source's exact owner and receipts.
        return view.get_clip(clip_id)


    @app.get("/clips/{clip_id}.mp4")
    async def evidence_clip(clip_id: str, request: Request):
        index, media_cache = request.state.workspace["index"], request.state.workspace["media"]
        record = await visible_clip(request, clip_id)
        if not record:
            raise HTTPException(404, "Choose an indexed interval.")
        try:
            path = await media_cache.clip(record)
            if isinstance(path, VerifiedFile):
                return await asyncio.to_thread(byte_response, path, range_header=request.headers.get("range"))
        except (ValueError, OSError, TimeoutError) as exc:
            raise HTTPException(503, str(exc) or "The clip is being prepared.") from exc
        return FileResponse(path, media_type="video/mp4", content_disposition_type="inline")


    @app.get("/thumbnails/{clip_id}.jpg")
    async def thumbnail(clip_id: str, request: Request):
        index = request.state.workspace["index"]
        record = await visible_clip(request, clip_id)
        if not record:
            raise HTTPException(404, "Choose an indexed interval.")
        if imported_record(record):
            try:
                resolved = await asyncio.to_thread(request.state.workspace["imported_media"].resolve, record)
                return await asyncio.to_thread(byte_response, resolved.thumbnail,
                    range_header=request.headers.get("range"))
            except (ValueError, OSError) as exc:
                raise HTTPException(404, "Choose an available source preview.") from exc
        return FileResponse(record["thumbnail_path"], media_type="image/jpeg")


    @app.get("/downloads/{clip_id}.mp4")
    async def download_clip(clip_id: str, request: Request):
        selected = request.state.workspace
        record = selected["index"].get_clip(clip_id)
        if not record:
            raise HTTPException(404, "Choose an indexed interval.")
        try:
            options = ({'owner_principal_id': selected['chats'].principal(request.state.session)}
                       if imported_record(record) else {})
            path = await selected["media"].clip(record, source_resolution=True, **options)
            if isinstance(path, VerifiedFile):
                return await asyncio.to_thread(byte_response, path,
                    range_header=request.headers.get('range'), attachment=True)
        except (ValueError, OSError, TimeoutError) as exc:
            raise HTTPException(503, str(exc) or "The clip is being prepared.") from exc
        return FileResponse(path, media_type="video/mp4", filename=clip_id + ".mp4",
                            content_disposition_type="attachment")


    def clip_manifest(record, resolved=None, *, source_cut_available=False):
        manifest = {"schema_version": 1, "clip_id": record["id"], "asset_id": record["video_id"],
                "source_filename": Path(record["path"]).name, "source_reference": record.get("source_reference"),
                "dataset": record["dataset"], "camera": record["camera"],
                "source_interval_seconds": {"start": record["start"], "end": record["end"]},
                "clock": "source-relative", "playback_interval_seconds": {"start": 0, "end": record["end"] - record["start"]},
                "download_url": f"/downloads/{record['id']}.mp4",
                "export": {"format": "mp4", "video": "H.264 CRF18 source resolution, even dimensions",
                           "audio": "AAC when present", "kind": "bounded source cut", "speed": 1},
                "provenance": record.get("provenance"),
                "embedding": {"model": record.get("model"), "revision": record.get("model_revision"),
                              "prepared_clip_sha256": record.get("embedded_clip_sha256")}}
        if resolved is not None:
            manifest.update(source_filename=resolved.original.filename,
                original_download_url=f"/originals/{record['video_id']}",
                download_url=f"/clips/{record['id']}.mp4",
                export={"format": "mp4", "kind": "prepared preview", "video": "H.264",
                        "width": record["prepared_width"], "height": record["prepared_height"],
                        "fps": 24, "audio": "silent", "speed": 1,
                        "sha256": resolved.prepared.sha256})
            if source_cut_available:
                manifest.update(download_url=f"/downloads/{record['id']}.mp4",
                    export={"format": "mp4", "kind": "bounded source cut",
                        "video": "H.264 CRF18 source resolution, even dimensions",
                        "audio": "AAC when present", "speed": 1,
                        "derivation_profile": "source-media-derive-v1"})
        return manifest


    def clip_focus(state, clip_id, focus_id=None):
        if focus_id is not None and not re.fullmatch(r"[a-f0-9]{32}", focus_id):
            raise HTTPException(400, "Choose a tracked subject from the answer.")
        path = state / "focus" / ((focus_id or clip_id) + ".json")
        focus = json.loads(path.read_text()) if path.exists() else None
        if not focus or focus.get("clip_id") != clip_id:
            return None
        focus.pop("sidecar_path", None)
        return focus


    @app.get('/api/retail/clip/{clip_id}/manifest')
    async def download_manifest(clip_id: str, request: Request, focus_id: str | None = None):
        selected = request.state.workspace
        record = selected["index"].get_clip(clip_id)
        if not record:
            raise HTTPException(404, "Choose an indexed interval.")
        try:
            resolved = (await asyncio.to_thread(selected["imported_media"].resolve, record)
                        if imported_record(record) else None)
        except (ValueError, OSError) as exc:
            raise HTTPException(404, "Choose an available source interval.") from exc
        manifest = clip_manifest(record, resolved,
            source_cut_available=bool(getattr(selected['media'], 'derivatives', None)))
        if focus_id is not None:
            focus = clip_focus(selected["state"], clip_id, focus_id)
            if focus is None:
                raise HTTPException(404, "Choose a tracked subject from this clip.")
            manifest["presentation"] = {"focus_id": focus_id, "focus_track": focus,
                                        "rendering": "Metadata overlay; MP4 contains the source scene"}
        return Response(json.dumps(manifest, indent=2), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{clip_id}.json"'})


    @app.get('/api/retail/clip/{clip_id}')
    async def clip_details(clip_id: str, request: Request, focus_id: str | None = None):
        index, state = request.state.workspace["index"], request.state.workspace["state"]
        record = await visible_clip(request, clip_id)
        if not record:
            raise HTTPException(404, "Choose an indexed interval.")
        result = {key: record[key] for key in ("id", "video_id", "dataset", "camera", "start", "end", "sample_time")}
        library = request.state.workspace["library"]
        result["descriptions"] = await asyncio.to_thread(library.descriptions, record)
        result["focus_json"] = clip_focus(state, clip_id, focus_id)
        relation_path = state / "relations.json"
        known={row["id"] for view in request.state.scope_views for row in view.clips}
        result["relations"] = [r for r in json.loads(relation_path.read_text())
            if clip_id in (r.get("from_clip_id"),r.get("to_clip_id"))
            and r.get("from_clip_id") in known and r.get("to_clip_id") in known] if relation_path.exists() else []
        result["source_url"] = f"/?clip={clip_id}"
        result["video_url"] = f"/clips/{clip_id}.mp4"
        result.update(asset_id=record["video_id"], source_filename=Path(record["path"]).name,
                      source_reference=record.get("source_reference"), provenance=record.get("provenance"),
                      download_url=f"/downloads/{clip_id}.mp4", manifest_url=f"/api/clip/{clip_id}/manifest",
                      entities={"status": "in_development", "occurrences": []},
                      room={"status": "unassigned", "id": None})
        context = await asyncio.to_thread(request.state.workspace["cameras"].get_context_for_clip, record)
        result["camera_context"] = context
        if context:
            result["room"] = {"status": "confirmed", "id": None,
                "name": context["context"].get("room", ""), "camera_id": context["camera_id"],
                "view_revision": context["view_revision"], "provenance": context["provenance"]}
        source = await asyncio.to_thread(library.source_details, record)
        result.update(collection=record["dataset"], source_filename=source["filename"], library_folder=source["folder"],
                      library_url=source["library_url"], source_metadata={k:source[k] for k in
                          ("width", "height", "fps", "frame_count", "duration", "bytes", "metadata_basis")},
                      original_download_url=source["original_download_url"],
                      camera_semantics=record.get("camera_semantics"))
        return result


    def file_authorization(request, file_id, principal):
        if file_id.startswith('export-'):
            require_export_scope(request,file_id,principal)
    host.options['file_authorization']=file_authorization

    @app.get("/originals/{asset_id}")
    async def original_source(asset_id: str, request: Request, collection: str | None = None):
        try:
            path, mime = await asyncio.to_thread(request.state.workspace["library"].original, asset_id, collection)
            if isinstance(path, VerifiedFile):
                return await asyncio.to_thread(byte_response, path,
                    range_header=request.headers.get("range"), attachment=True)
        except (KeyError, ValueError) as exc:
            raise HTTPException(404, exc.args[0]) from exc
        return FileResponse(path, media_type=mime, filename=path.name, content_disposition_type="attachment")



    @app.get('/static/retail/assets/demo-companies/sij-acroni.png')
    async def sij_acroni_logo():
        return FileResponse(web_asset("assets/demo-companies/sij-acroni.png"),media_type="image/png")


    @app.get('/static/retail/control-room.js')
    async def control_room_script():
        return FileResponse(web_asset("control-room.js"), media_type="application/javascript")


    @app.get('/static/retail/assets/camera-providers/{filename}')
    async def camera_provider_asset(filename: str):
        if not re.fullmatch(r'[a-z][a-z0-9-]*\.(png|svg|ico)', filename):
            raise HTTPException(404, 'Choose a camera provider image.')
        path = web_asset("assets/camera-providers/" + filename)
        if not path.is_file() or path.is_symlink():
            raise HTTPException(404, 'Choose a camera provider image.')
        return FileResponse(path)


    @app.get('/static/retail/retail-scope.js')
    @app.get('/static/retail/retail-scope.css')
    async def retail_scope_component(request: Request):
        suffix='css' if request.url.path.endswith('.css') else 'js'
        return FileResponse(web_asset('retail-scope.'+suffix))

    @app.get('/static/retail/retail-ui.js')
    @app.get('/static/retail/retail-ui.css')
    @app.get('/static/retail/retail-shell.js')
    @app.get('/static/retail/tracker-canvas.js')
    @app.get('/static/retail/tracker-canvas.css')
    @app.get('/static/retail/camera-streams.js')
    @app.get('/static/retail/camera-streams.css')
    async def retail_component(request: Request):
        return FileResponse(web_asset(request.url.path.removeprefix('/static/retail').removeprefix('/')))
