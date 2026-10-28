"""Immutable model-authored investigation plans and local candidate retrieval.

The agent supplies semantic categories and queries. This module validates their
structure/scope, runs the registered retrieval adapter, and records candidate
provenance. Similarity never becomes an event verdict or alert here.
"""
from __future__ import annotations

from contextlib import nullcontext
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal
import uuid

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing_extensions import Annotated

from camera_context import CameraStore
from retail_business import RetailBusinessStore as BusinessStore
from library import read_json


ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class SearchCategory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=160)
    observable_criteria: list[ShortText] = Field(min_length=1, max_length=12)
    queries: list[ShortText] = Field(min_length=1, max_length=4)
    verification_question: str = Field(min_length=1, max_length=2000)
    counterexamples: list[ShortText] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def meaningful_strings(self):
        texts = [self.label,self.verification_question,*self.observable_criteria,*self.queries,*self.counterexamples]
        if any(not text.strip() for text in texts):
            raise ValueError("Provide nonempty category criteria, queries and verification questions.")
        self.queries = [query.strip() for query in self.queries]
        return self


class PlanDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=8000)
    business_context: str = Field(max_length=6000)
    assumptions: list[ShortText] = Field(default_factory=list, max_length=16)
    categories: list[SearchCategory] = Field(min_length=1, max_length=8)
    coverage_goal: Literal["examples","overview","all_occurrences"]
    direct_clip_ids: list[str] = Field(default_factory=list, max_length=24)

    @model_validator(mode="after")
    def limits(self):
        if not self.question.strip() or any(not value.strip() for value in self.assumptions):
            raise ValueError("Provide an investigation question and explicit assumptions.")
        if len({category.id for category in self.categories}) != len(self.categories):
            raise ValueError("Use distinct category identifiers.")
        if sum(len(category.queries) for category in self.categories) > 24:
            raise ValueError("An investigation plan supports up to 24 visual queries.")
        if len(set(self.direct_clip_ids)) != len(self.direct_clip_ids):
            raise ValueError("Select distinct source clips for direct inspection.")
        return self


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()


class InvestigationPlans:
    def __init__(self, state, index):
        self.state, self.index = Path(state), index
        self.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        self.path = self.state/"investigation-plans.sqlite"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, dataset TEXT NOT NULL,
                    chat_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS retrieval_receipts (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    dataset TEXT NOT NULL, chat_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS inspection_receipts (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
                    dataset TEXT NOT NULL, chat_id TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS query_vectors (key TEXT PRIMARY KEY, dimensions INTEGER NOT NULL,
                    vector BLOB NOT NULL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db=sqlite3.connect(self.path,timeout=15)
        db.row_factory=sqlite3.Row
        return db

    @staticmethod
    def _snapshot(index):
        with getattr(index,"_lock",nullcontext()):
            view=copy.copy(index)
            view.clips=copy.deepcopy(index.clips)
            view.report=copy.deepcopy(getattr(index,"report",{}))
            if hasattr(index,"embeddings"):
                view.embeddings=index.embeddings
            # Keep one shared CPU text tower, including for incremental retrieval views.
            if hasattr(index,"_encode_text"):
                view._encode_text=index._encode_text
            return view

    @staticmethod
    def _model(index):
        report=getattr(index,"report",{}) or {}
        first=index.clips[0] if index.clips else {}
        vectors=getattr(index,"embeddings",None)
        dimensions=int(vectors.shape[1]) if vectors is not None and vectors.ndim==2 else report.get("embedding_dimensions")
        return {"name":report.get("model",first.get("model")),
                "revision":report.get("model_revision",first.get("model_revision")),"dimensions":dimensions}

    def _index_snapshot(self,index,dataset):
        records=[c for c in index.clips if c["dataset"]==dataset]
        identity=[{k:c.get(k) for k in ("id","video_id","start","end","embedded_clip_sha256","model_revision")} for c in records]
        model=self._model(index)
        result={"scope_snapshot_sha256":_digest({"model":model,"records":identity}),
                "indexed_window_count":len(records),"retrieval_model":model,"index_generation_id":None,
                "identity_basis":"loaded_source_metadata_and_model_revision"}
        marker_path=Path(getattr(index,"state",self.state/"cosmos"))/"index-generation.json"
        try:
            stat=marker_path.stat()
            identity=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
            if getattr(index,"_generation_identity",None)==identity:
                marker=read_json(marker_path,{})
                result["index_generation_id"]=marker.get("generation_id")
        except OSError:
            pass
        return result

    def context(self,dataset):
        index=self._snapshot(self.index)
        records=[c for c in index.clips if c["dataset"]==dataset]
        if not records:
            raise ValueError("Choose an indexed business collection.")
        snapshot=self._index_snapshot(index,dataset)
        sources={}
        for clip in records:
            source=sources.setdefault(clip["video_id"],{"asset_id":clip["video_id"],"camera":clip["camera"],
                "source_reference":clip.get("source_reference"),"provenance":clip.get("provenance"),
                "camera_semantics":clip.get("camera_semantics"),"indexed_windows":0,"indexed_until_seconds":0})
            source["indexed_windows"]+=1
            source["indexed_until_seconds"]=max(source["indexed_until_seconds"],clip["end"])
        cameras=CameraStore(self.state,index).list(dataset)["cameras"]
        business=BusinessStore(self.state,index).get(dataset)
        confirmed=[c["confirmed_context"] for c in cameras if c["confirmed_context"]]
        contexts=[]
        for view in confirmed[:24]:
            data=view["context"]
            contexts.append({"camera_id":view["camera_id"],"view_revision":view["view_revision"],
                "source_asset_id":view["provenance"]["source_asset_id"],
                "valid_from_seconds":view["provenance"]["valid_from_seconds"],
                "context_sha256":_digest(view),"room":data["room"],
                "layout_notes":data["layout_notes"][:800],
                "zones":[{"name":z["name"],"description":z["description"][:240],"bbox":z.get("bbox")} for z in data["zones"][:8]],
                "summary_truncated":len(data["layout_notes"])>800 or len(data["zones"])>8 or any(len(z["description"])>240 for z in data["zones"][:8]),
                "provenance":"operator_confirmed_reference_data"})
        return {"dataset":dataset,"captured_at":_now(),**snapshot,"source_count":len(sources),
            "sources":list(sources.values())[:64],"sources_omitted":max(0,len(sources)-64),
            "camera_contexts":contexts,"confirmed_camera_context_count":len(confirmed),
            "camera_contexts_omitted":max(0,len(confirmed)-24),
            "camera_context_summary_scope":"Latest confirmed view per source; earlier footage may require its historical view context.",
            "business_profile":business["profile"],"business_profile_revision":business["profile_revision"],
            "business_profile_provenance":business["provenance"],
            "business_profile_status":business["provenance"]["epistemic_status"],
            "business_footage_relationship":business["footage_relationship"],
            "business_settings":business["settings"],"business_revision":business["revision"],
            "interpretation_boundary":"Source metadata and confirmed reference notes; scene findings require actual observations."}

    def save(self,dataset,chat_id,document):
        if not isinstance(chat_id,str) or not chat_id.strip() or len(chat_id)>100:
            raise ValueError("Choose the current investigation chat.")
        body=PlanDocument.model_validate(document).model_dump()
        snapshot=self.context(dataset)
        for clip_id in body["direct_clip_ids"]:
            clip=self.index.get_clip(clip_id)
            if not clip or clip["dataset"]!=dataset:
                raise ValueError("Select direct source clips from this business collection.")
        plan={**body,"id":"plan-"+uuid.uuid4().hex,"revision":1,"dataset":dataset,"chat_id":chat_id,
              "created_at":_now(),"mode":"direct" if body["direct_clip_ids"] else "archive",
              "context_snapshot":snapshot,"authorship":{"categories":"model_authored",
                  "business_context":"model_interpretation","assumptions":"model_supplied",
                  "context_snapshot":"host_source_metadata"},"status":"planned"}
        with self.connect() as db:
            db.execute("INSERT INTO plans VALUES (?,?,?,?)",(plan["id"],dataset,chat_id,json.dumps(plan,allow_nan=False)))
        return plan

    def get(self,plan_id,dataset,chat_id):
        with self.connect() as db:
            row=db.execute("SELECT body FROM plans WHERE id=? AND dataset=? AND chat_id=?",(plan_id,dataset,chat_id)).fetchone()
        if row is None:
            raise ValueError("Choose a plan from this chat and business collection.")
        return json.loads(row["body"])

    def get_receipt(self,receipt_id,dataset,chat_id):
        with self.connect() as db:
            row=db.execute("SELECT body FROM retrieval_receipts WHERE id=? AND dataset=? AND chat_id=?",(receipt_id,dataset,chat_id)).fetchone()
        if row is None:
            raise ValueError("Choose a retrieval receipt from this chat and business collection.")
        return json.loads(row["body"])

    def get_inspection(self,receipt_id,dataset,chat_id):
        with self.connect() as db:
            row=db.execute("SELECT body FROM inspection_receipts WHERE id=? AND dataset=? AND chat_id=?",(receipt_id,dataset,chat_id)).fetchone()
        if row is None:
            raise ValueError("Choose an inspection receipt from this chat and business collection.")
        return json.loads(row["body"])

    def record_inspection(self,plan_id,dataset,chat_id,category_ids,question,clip_ids,result):
        """Record a complete host inspection; model observations remain interpretations."""
        from cosmos_inspect import inspection_complete
        plan=self.get(plan_id,dataset,chat_id)
        categories={c["id"] for c in plan["categories"]}
        if (not isinstance(category_ids,list) or not 1<=len(category_ids)<=8 or
                any(not isinstance(key,str) or key not in categories for key in category_ids) or len(set(category_ids))!=len(category_ids)):
            raise ValueError("Link this inspection to distinct categories from its plan.")
        if not isinstance(clip_ids,list) or not 1<=len(clip_ids)<=6 or any(not isinstance(key,str) for key in clip_ids) or len(set(clip_ids))!=len(clip_ids):
            raise ValueError("Record one to six distinct inspected source clips.")
        if not isinstance(question,str) or not question.strip() or len(question)>2000:
            raise ValueError("Record the exact question sent to the video reasoner.")
        records={key:self.index.get_clip(key) for key in clip_ids}
        if any(record is None or record["dataset"]!=dataset for record in records.values()):
            raise ValueError("Inspect source clips from this business collection.")
        if plan["direct_clip_ids"] and not set(clip_ids).issubset(plan["direct_clip_ids"]):
            raise ValueError("Inspect the source clips selected by this direct plan.")
        if not isinstance(result,dict) or not result.get("model") or not result.get("model_revision"):
            raise ValueError("Record an actual model result with its pinned revision.")
        if not inspection_complete(result):
            raise ValueError("Record a complete, parsed inspection answer for every request.")
        requests=result.get("requests")
        if not isinstance(requests,list) or not 1<=len(requests)<=6:
            raise ValueError("Record the model's actual request outputs.")
        observed=set()
        for request in requests:
            ids=request.get("clip_ids",[])
            if request.get("question")!=question or not ids or not set(ids).issubset(records):
                raise ValueError("Inspection outputs must match the actual question and supplied clips.")
            observed.update(ids)
            for source in request.get("sources",[]):
                record=records.get(source.get("id"))
                if record is None or source.get("source_video_id",record["video_id"])!=record["video_id"]:
                    raise ValueError("Model output provenance must match the selected source.")
                if any(key in source and source[key]!=record[key] for key in ("start","end")):
                    raise ValueError("Model output timing must match the inspected interval.")
        if observed!=set(clip_ids):
            raise ValueError("Every inspected clip needs its actual model request output.")

        # Provider accounting and filesystem details are deliberately absent from public receipts.
        public={key:result[key] for key in ("model","model_revision","run_id","cached","quality_profile","prompt_layout") if key in result}
        request_fields={"clip_ids","question","parsed","model_structure_valid","final_text","result","verification",
            "elapsed_seconds","decode_and_preprocess_seconds","inference_seconds","output_tokens","input_tokens",
            "reasoning_requested","reasoning_completed","prompt_layout","user_content_order","preprocessing",
            "applied_instruction","applied_question_prompt"}
        source_fields={"id","sha256","duration_seconds","camera","dataset","source_video_id","start","end",
            "submitted_width","submitted_height","submitted_avg_frame_rate","submitted_num_frames","confirmed_camera_context"}
        public["requests"]=[]
        for request in requests:
            saved={key:value for key,value in request.items() if key in request_fields}
            saved["sources"]=[{key:value for key,value in source.items() if key in source_fields} for source in request.get("sources",[])]
            public["requests"].append(saved)
        source_clips=[{**{key:record[key] for key in ("id","video_id","dataset","camera","start","end")},
                       "source_url":f"/?clip={record['id']}"} for record in records.values()]
        receipt={"receipt_id":"inspection-"+uuid.uuid4().hex,"plan_id":plan_id,"dataset":dataset,"chat_id":chat_id,
            "created_at":_now(),"category_ids":category_ids,"question":question,"clip_ids":clip_ids,
            "source_clips":source_clips,"result":public,"evidence_type":"model_observations",
            "verification":"Model-generated observations require interpretation against source evidence."}
        with self.connect() as db:
            db.execute("INSERT INTO inspection_receipts VALUES (?,?,?,?,?)",(receipt["receipt_id"],plan_id,dataset,chat_id,json.dumps(receipt,allow_nan=False)))
        return receipt["receipt_id"]

    def _cached_encoder(self,index,model,stats):
        def encode(query):
            key=_digest({"model":model,"query":query})
            with self.connect() as db:
                row=db.execute("SELECT dimensions,vector FROM query_vectors WHERE key=?",(key,)).fetchone()
            if row is not None:
                vector=np.frombuffer(row["vector"],dtype="<f4").copy()
                if row["dimensions"]!=model["dimensions"] or vector.shape!=(model["dimensions"],) or not np.isfinite(vector).all() or np.linalg.norm(vector)<1e-8:
                    raise ValueError("Rebuild the matching local query-vector cache.")
                stats["hits"]+=1
                return vector
            vector=np.asarray(index._encode_text(query),dtype="<f4")
            if vector.shape!=(model["dimensions"],) or not np.isfinite(vector).all() or np.linalg.norm(vector)<1e-8:
                raise ValueError("The matching query encoder must return a finite nonzero vector.")
            with self.connect() as db:
                db.execute("INSERT OR IGNORE INTO query_vectors VALUES (?,?,?)",(key,len(vector),vector.tobytes()))
            stats["misses"]+=1
            return vector
        return encode

    @staticmethod
    def _intervals(candidates):
        intervals=[]
        for clip in sorted(candidates,key=lambda c:(c["video_id"],c["start"],c["end"],c["id"])):
            if intervals and intervals[-1]["video_id"]==clip["video_id"] and clip["start"]<=intervals[-1]["end"]+1e-6:
                entry=intervals[-1]
                entry["end"]=max(entry["end"],clip["end"])
                entry["clip_ids"].append(clip["id"])
                entry["matched_categories"]=sorted(set(entry["matched_categories"])|set(clip["matched_categories"]))
            else:
                intervals.append({"video_id":clip["video_id"],"camera":clip["camera"],"dataset":clip["dataset"],
                    "start":clip["start"],"end":clip["end"],"clip_ids":[clip["id"]],
                    "matched_categories":clip["matched_categories"],"verification":"candidate"})
        for interval in intervals:
            interval["id"]="candidate-interval-"+_digest(interval["clip_ids"])[:20]
            interval["candidate_window_count"]=len(interval["clip_ids"])
        return intervals

    def retrieve(self,plan_id,dataset,chat_id,index=None,limit_per_query=6):
        if type(limit_per_query) is not int or not 1<=limit_per_query<=12:
            raise ValueError("Choose one to twelve candidates per visual query.")
        started=time.monotonic()
        plan=self.get(plan_id,dataset,chat_id)
        base=self._snapshot(self.index)
        query_index=self._snapshot(index if index is not None else self.index)
        model=self._model(query_index)
        if model!=plan["context_snapshot"]["retrieval_model"]:
            raise ValueError("Create a new plan for the current retrieval model revision.")
        known={c["id"]:c for c in base.clips if c["dataset"]==dataset}
        if any(key not in known for key in plan["direct_clip_ids"]):
            raise ValueError("Select currently indexed sources for this direct inspection plan.")
        positions=[]
        for i,clip in enumerate(query_index.clips):
            if clip["dataset"]!=dataset:
                continue
            original=known.get(clip["id"])
            if original is None or any(original.get(k)!=clip.get(k) for k in ("video_id","start","end","embedded_clip_sha256","path")):
                raise ValueError("Retrieve only the host's selected source intervals.")
            if not plan["direct_clip_ids"] or clip["id"] in plan["direct_clip_ids"]:
                positions.append(i)
        if not hasattr(query_index,"embeddings") and plan["direct_clip_ids"]:
            raise ValueError("Direct source selection requires the indexed vector view.")
        if hasattr(query_index,"embeddings"):
            if len(query_index.embeddings)!=len(query_index.clips):
                raise ValueError("Source metadata and vectors must have matching rows.")
            query_index.embeddings=query_index.embeddings[positions]
            query_index.clips=[query_index.clips[i] for i in positions]
        active={c["id"]:c for c in query_index.clips if c["dataset"]==dataset}
        source_index=query_index
        query_index=copy.copy(source_index)
        stats={"hits":0,"misses":0}
        cache_supported=callable(getattr(source_index,"_encode_text",None)) and all(model.values())
        if cache_supported:
            query_index._encode_text=self._cached_encoder(source_index,model,stats)
        candidates,completed_queries={},{}
        runs=[]
        for category in plan["categories"]:
            for number,query in enumerate(category["queries"],1):
                query_id=f"{category['id']}:q{number}"
                repeated=query in completed_queries
                if not repeated:
                    completed_queries[query]=(query_id,query_index.search(query,limit_per_query,dataset))
                first_query,hits=completed_queries[query]
                ids=[]
                for rank,hit in enumerate(hits,1):
                    record=active.get(hit.get("id"))
                    if record is None:
                        raise ValueError("The retrieval adapter returned a source outside the selected scope.")
                    score=float(hit["score"])
                    if not np.isfinite(score):
                        raise ValueError("Candidate scores must come from finite retrieval results.")
                    ids.append(record["id"])
                    key=record["id"]
                    if key not in candidates:
                        candidates[key]={**{k:record[k] for k in ("id","video_id","dataset","camera","start","end")},
                            "source_url":f"/?clip={key}","video_url":f"/clips/{key}.mp4","thumbnail_url":f"/thumbnails/{key}.jpg",
                            "verification":"candidate","score":score,"score_basis":"highest_observed_query_similarity",
                            "matches":[],"matched_categories":[],"matched_queries":[]}
                    candidate=candidates[key]
                    candidate["score"]=max(candidate["score"],score)
                    candidate["matches"].append({"category_id":category["id"],"query_id":query_id,"query":query,"score":score,"rank":rank})
                    candidate["matched_categories"]=sorted(set(candidate["matched_categories"])|{category["id"]})
                    candidate["matched_queries"].append(query_id)
                runs.append({"id":query_id,"category_id":category["id"],"query":query,"candidate_window_count":len(set(ids)),
                    "reused_query_id":first_query if repeated else None,"candidate_ids":list(dict.fromkeys(ids))})
        ordered=sorted(candidates.values(),key=lambda c:(min(m["rank"] for m in c["matches"]),-c["score"],c["id"]))
        returned=ordered[:96];returned_ids={c["id"] for c in returned}
        for run in runs:
            run["returned_candidate_ids"]=[key for key in run.pop("candidate_ids") if key in returned_ids]
        per_category=[]
        for category in plan["categories"]:
            all_matches=[c for c in ordered if category["id"] in c["matched_categories"]]
            per_category.append({"id":category["id"],"label":category["label"],
                "verification_question":category["verification_question"],"candidate_window_count":len(all_matches),
                "candidate_ids":[c["id"] for c in all_matches if c["id"] in returned_ids]})
        intervals=self._intervals(returned)
        receipt={"receipt_id":"retrieval-"+uuid.uuid4().hex,"plan_id":plan_id,"dataset":dataset,"chat_id":chat_id,
            "created_at":_now(),"retrieval_model":model,"index_snapshot":self._index_snapshot(source_index,dataset),
            "results":returned,"per_category":per_category,"query_runs":runs,"intervals":intervals,
            "coverage":{"goal":plan["coverage_goal"],"collection_window_count":len(known),"scored_window_count":len(active),
                "planned_query_count":len(runs),"unique_query_count":len(completed_queries),"limit_per_query":limit_per_query,
                "candidate_window_count":len(ordered),"returned_candidate_window_count":len(returned),
                "candidate_interval_count":len(self._intervals(ordered)),"returned_candidate_interval_count":len(intervals),
                "omitted_candidate_window_count":max(0,len(ordered)-len(returned)),"verified_window_count":0,
                "complete_recall_established":False,"scoring_coverage":"all_windows_in_host_scoped_index",
                "interpretation":"Candidate windows and contiguous source intervals; occurrences and identities require verification."},
            "query_vector_cache":{"enabled":cache_supported,**stats},"elapsed_ms":round((time.monotonic()-started)*1000,2),
            "verification_status":"awaiting_question_specific_inspection"}
        with self.connect() as db:
            db.execute("INSERT INTO retrieval_receipts VALUES (?,?,?,?,?)",(receipt["receipt_id"],plan_id,dataset,chat_id,json.dumps(receipt,allow_nan=False)))
        return receipt
