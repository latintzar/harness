"""Tenant-local navigation of indexed sources and saved observation provenance.

Public paths are logical paths within a collection. Filesystem resolution starts
only from the trusted imported index, never from a browser-supplied path.
"""
from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import subprocess
from urllib.parse import quote, urlencode

from imported_media import ImportedMedia, ImportedMediaError, imported_path, imported_record


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


class SourceLibrary:
    def __init__(self, state, index):
        self.state, self.index = Path(state), index
        self.imported_media = ImportedMedia(state)
        self.metadata_cache = {}
        self.hash_cache = {}
        self.result_cache = {}

    def catalog(self):
        assets = {}
        for clip in self.index.clips:
            if imported_record(clip):
                try:
                    self.imported_media.resolve(clip)
                except ImportedMediaError:
                    continue
            asset_id = clip["video_id"]
            key = (clip["dataset"], str(Path(clip["path"]).absolute()))
            if asset_id in assets and assets[asset_id]["key"] != key:
                # An ambiguous import cannot authorize either underlying file.
                raise ValueError("Refresh the source catalog to resolve duplicate asset identifiers.")
            if asset_id not in assets:
                assets[asset_id] = {"key": key, "record": clip, "clips": []}
            assets[asset_id]["clips"].append(clip)
        roots = {}
        for collection in {value["key"][0] for value in assets.values()}:
            legacy = [str(Path(value["key"][1]).parent) for value in assets.values()
                      if value["key"][0] == collection and not imported_record(value["record"])]
            if legacy:
                roots[collection] = Path(os.path.commonpath(legacy))
        for value in assets.values():
            if imported_record(value["record"]):
                value["relative_path"] = self.imported_media.resolve(value["record"]).original.filename
                value["folder"] = ""
                continue
            path = Path(value["key"][1])
            value["relative_path"] = path.relative_to(roots[value["key"][0]]).as_posix()
            value["folder"] = str(PurePosixPath(value["relative_path"]).parent)
            if value["folder"] == ".":
                value["folder"] = ""
        return assets

    @staticmethod
    def folder(value):
        if value == "":
            return ""
        if (len(value) > 1000 or "\\" in value or "\x00" in value or value.startswith("/")
                or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("Choose a folder from the Gallery.")
        return value

    def metadata(self, path, *, record=None):
        if record is not None and imported_record(record):
            return self.imported_media.resolve(record).metadata.copy()
        if imported_path(path):
            raise ImportedMediaError("imported_metadata_requires_verified_record")
        path = Path(path)
        fingerprint = None
        empty = dict(width=None, height=None, fps=None, frame_count=None, duration=None, bytes=None)
        try:
            stat = path.stat()
            fingerprint = (str(path), stat.st_mtime_ns, stat.st_size)
            if fingerprint in self.metadata_cache:
                return self.metadata_cache[fingerprint].copy()
            data = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
                "-of", "json", str(path)], stderr=subprocess.DEVNULL, timeout=10))
            stream = data.get("streams", [{}])[0]
            rate = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
            try:
                fps = float(Fraction(rate)) if rate else None
            except (ValueError, ZeroDivisionError):
                fps = None
            duration = stream.get("duration") or data.get("format", {}).get("duration")
            count = stream.get("nb_frames")
            result = dict(width=stream.get("width"), height=stream.get("height"), fps=fps,
                          frame_count=int(count) if count and count != "N/A" else None,
                          duration=float(duration) if duration and duration != "N/A" else None,
                          bytes=stat.st_size)
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            result = empty
            try:
                result["bytes"] = path.stat().st_size
            except OSError:
                return result
        if fingerprint is not None:
            self.metadata_cache[fingerprint] = result
        return result.copy()

    @staticmethod
    def public_clip(clip):
        key = quote(clip["id"], safe="")
        return {**{k: clip.get(k) for k in ("id", "video_id", "dataset", "camera", "start", "end")},
                "asset_id": clip["video_id"], "collection": clip["dataset"],
                "thumbnail_url": f"/thumbnails/{key}.jpg", "source_url": f"/?clip={key}",
                "video_url": f"/clips/{key}.mp4", "playback_start": 0}

    def public_asset(self, asset, *, include_clips=False):
        record = asset["record"]
        key = quote(record["video_id"], safe="")
        imported = self.imported_media.resolve(record) if imported_record(record) else None
        result = {"asset_id": record["video_id"], "filename": imported.original.filename if imported else Path(record["path"]).name,
                  "relative_path": asset["relative_path"], "folder": asset["folder"],
                  "library_folder": asset["folder"], "collection": record["dataset"],
                  "dataset": record["dataset"], "camera": record["camera"],
                  "source_reference": record.get("source_reference"), "provenance": record.get("provenance"),
                  "camera_semantics": record.get("camera_semantics"),
                  "thumbnail_url": f"/thumbnails/{quote(record['id'], safe='')}.jpg",
                  "original_download_url": f"/originals/{key}",
                  "library_url": "/?" + urlencode({"library":1, "collection":record["dataset"],
                                                   "folder":asset["folder"], "asset":record["video_id"]}),
                  "available": Path(record["path"]).is_file(), **self.metadata(record["path"], record=record)}
        result["indexed_until"] = max(c["end"] for c in asset["clips"])
        result["metadata_basis"] = "sandbox_source_probe" if imported else "available_source_file"
        if imported:
            result["playback_basis"] = "prepared_640px_24fps"
        if include_clips:
            result["clips"] = [self.public_clip(c) for c in sorted(asset["clips"], key=lambda c: c["start"])]
        return result

    def listing(self, collection, folder=""):
        folder = self.folder(folder)
        selected = [a for a in self.catalog().values() if a["key"][0] == collection]
        if not selected:
            raise KeyError("Choose an indexed collection.")
        prefix = folder + "/" if folder else ""
        descendants = [a for a in selected if a["relative_path"].startswith(prefix)]
        if folder and not descendants:
            raise KeyError("Choose an indexed folder.")
        folders, files = {}, []
        for asset in descendants:
            relative = asset["relative_path"][len(prefix):]
            if "/" in relative:
                name = relative.split("/", 1)[0]
                folders.setdefault(name, {"name":name, "path":prefix+name, "asset_count":0})["asset_count"] += 1
            else:
                files.append(self.public_asset(asset))
        breadcrumbs = [{"name":collection, "path":""}]
        parts = folder.split("/") if folder else []
        breadcrumbs += [{"name":part, "path":"/".join(parts[:i+1])} for i, part in enumerate(parts)]
        return {"collection":collection, "folder":folder, "breadcrumbs":breadcrumbs,
                "folders":sorted(folders.values(), key=lambda f:f["name"].casefold()),
                "assets":sorted(files, key=lambda a:a["filename"].casefold())}

    def resolve(self, asset_id, collection=None):
        asset = self.catalog().get(asset_id)
        if not asset or (collection is not None and asset["key"][0] != collection):
            raise KeyError("Choose an indexed source file.")
        return asset

    def original(self, asset_id, collection=None):
        asset = self.resolve(asset_id, collection)
        if imported_record(asset["record"]):
            verified = self.imported_media.resolve(asset["record"]).original
            return verified, verified.media_type
        path = Path(asset["record"]["path"])
        if not path.is_file():
            raise KeyError("Reconnect this source file to download it.")
        return path, mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def source_details(self, record):
        return self.public_asset(self.resolve(record["video_id"]))

    def _results(self):
        # Only model output locations are eligible; account/compute records never enter this API.
        paths = list((self.state/"reasoner-cache").glob("*.json"))
        paths += list((self.state/"reasoner-runs").glob("*/result.json"))
        for directory in [self.state/"reasoner-pilot", *self.state.glob("super-*")]:
            if directory.is_dir():
                paths += list(directory.glob("*result*.json"))
                paths += list(directory.glob("recovered-observations.json"))
        for path in sorted(set(paths)):
            try:
                stat = path.stat()
            except OSError:
                continue
            fingerprint = (stat.st_mtime_ns, stat.st_size)
            cached = self.result_cache.get(path)
            if not cached or cached[0] != fingerprint:
                cached = (fingerprint, read_json(path, {}))
                self.result_cache[path] = cached
            result = cached[1]
            if isinstance(result, dict) and isinstance(result.get("requests"), list):
                yield path, result

    def _matching_result(self, description, clip_id):
        source_hash = (description.get("source") or {}).get("sha256")
        if not source_hash:
            return None
        for path, result in self._results():
            if (result.get("model"), result.get("model_revision")) != (description.get("model"), description.get("model_revision")):
                continue
            if any(key in description and description[key] != result.get(key, default)
                   for key, default in (("quality_profile", "standard"), ("prompt_layout", "legacy-interleaved-v1"))):
                continue
            for request in result["requests"]:
                if request.get("question") != description.get("prompt"):
                    continue
                source = next((s for s in request.get("sources", []) if s.get("id") == clip_id and s.get("sha256") == source_hash), None)
                observations = (request.get("result") or {}).get("observations", [])
                same_text = any(o.get("clip_id") == clip_id and o.get("summary") == description.get("text") for o in observations)
                if source and (same_text or request.get("final_text") == description.get("text")):
                    yield path, result, request, source

    def _input_metadata(self, result_path, clip_id, sha256):
        # A manifest path is trusted local import metadata; verify the exact submitted bytes.
        for manifest in result_path.parent.glob("*manifest*.json"):
            data = read_json(manifest, {})
            if not isinstance(data, dict):
                continue
            for entry in data.get("clips", []):
                if entry.get("id") != clip_id or not entry.get("path"):
                    continue
                path = Path(entry["path"])
                if imported_path(path):
                    continue
                try:
                    stat = path.stat()
                    key = (str(path), stat.st_size, stat.st_mtime_ns)
                    if key not in self.hash_cache:
                        with path.open("rb") as stream:
                            self.hash_cache[key] = hashlib.file_digest(stream, "sha256").hexdigest()
                    if self.hash_cache[key] == sha256:
                        return self.metadata(path)
                except OSError:
                    continue
        return None

    def descriptions(self, record):
        entries = read_json(self.state/"descriptions"/(record["id"]+".json"), [])
        output = []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            # Preserve the exact original model answer; enrich with evidence-backed fields only.
            description = {k:entry[k] for k in ("model", "model_revision", "created_at", "run_id", "verification",
                "clip_id", "text", "objects", "activities", "prompt", "source_url", "source",
                "model_structure_valid", "source_mapping", "review", "quality_profile", "prompt_layout",
                "benchmark_only") if k in entry}
            if isinstance(description.get("source"), dict):
                description["source"] = {k:v for k,v in description["source"].items() if k in
                    {"id", "sha256", "duration_seconds", "camera", "dataset", "source_video_id",
                     "start", "end", "width", "height", "fps", "frame_count"}}
            description["question"] = entry.get("prompt")
            settings = dict(width=None, height=None, fps=None, num_frames=None, sampling_fps=None,
                            max_new_tokens=None, reasoning_requested=None, quality_profile=None,
                            effective_width=None, effective_height=None, video_grid_thw=None,
                            sampled_frame_indices=None, sampled_frame_count=None, padded_frame_count=None,
                            source_fps=None, source_width=None, source_height=None,
                            rendered_temporal_positions=None)
            for path, result, request, source in self._matching_result(entry, record["id"]):
                settings.update(sampling_fps=result.get("fps"), max_new_tokens=result.get("max_new_tokens"),
                                reasoning_requested=request.get("reasoning_requested", result.get("reasoning_requested")))
                # Worker measurements are bound to the matched source bytes. Preserve
                # them even after temporary manifests/prepared files have been removed.
                for target, field in (("width", "submitted_width"), ("height", "submitted_height"),
                                      ("num_frames", "submitted_num_frames")):
                    try:
                        value = int(source.get(field))
                        if value > 0:
                            settings[target] = value
                    except (TypeError, ValueError):
                        pass
                try:
                    rate = float(Fraction(source.get("submitted_avg_frame_rate")))
                    if rate > 0:
                        settings["fps"] = rate
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
                actual = self._input_metadata(path, record["id"], source["sha256"])
                if actual:
                    settings.update(width=actual["width"], height=actual["height"], fps=actual["fps"], num_frames=actual["frame_count"])
                # New runs can carry measured preprocessing records directly. Keep the allowlist.
                if isinstance(request.get("input_settings"), dict):
                    settings.update({k:v for k,v in request["input_settings"].items() if k in settings})
                measured = next((p for p in request.get("preprocessing", []) if p.get("clip_id") == record["id"]), None)
                if measured:
                    settings.update({k:v for k,v in measured.items() if k in settings})
                for key in ("input_tokens", "output_tokens", "reasoning_completed", "applied_instruction", "applied_question_prompt"):
                    if key in request:
                        description[key] = request[key]
                description["settings_basis"] = "matched_model_revision_question_source_hash_and_answer"
                if actual:
                    break
            description["input_settings"] = settings
            description["source_metadata"] = self.metadata(record["path"], record=record)
            output.append(description)
        return output
