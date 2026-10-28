"""Retail profile defaults, local camera setup drafts and their HTTP routes."""
from typing import Literal
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from business_context import BusinessStore, _public_text, _validate


class BusinessSettings(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    initial_history_hours: int=Field(default=48,ge=0,le=48)
    analysis_window_seconds: int=Field(default=8,ge=8,le=8)


class ConnectionDraft(BaseModel):
    model_config=ConfigDict(extra="forbid",strict=True)
    expected_revision: int=Field(ge=0)
    provider: str=Field(min_length=1,max_length=160)
    type: Literal["camera_stream","recorder_export","cloud_storage","manual_upload"]
    source_description: str=Field(min_length=1,max_length=2000)
    history_hours: int|None=Field(default=None,ge=0,le=48)

    @model_validator(mode="after")
    def safe_description(self):
        for value in [self.provider,self.source_description]:
            if not value.strip():
                raise ValueError("Describe the source and provider.")
            _public_text(value)
        return self


class RetailBusinessStore(BusinessStore):
    def __init__(self, state, index=None, *, collections=None):
        self.index = index
        super().__init__(state, collections=collections)

    def _collection_exists(self, collection):
        if self.collections is not None:
            return super()._collection_exists(collection)
        return self.index is not None and any(row['dataset'] == collection for row in self.index.clips)

    def _initial(self, collection):
        return {**super()._initial(collection),
            'settings': BusinessSettings().model_dump(), 'connections': [],
            'settings_scope': 'future_authorized_imports',
            'footage_relationship': {'status': 'unestablished',
                'basis': 'Imported footage retains its own source provenance; this profile supplies reference business context.'}}

    def _present(self, record):
        return record

    def update_settings(self,collection,expected_revision,settings):
        if not isinstance(settings,dict) or not settings:
            raise ValueError("Provide the requested initial-history setting.")
        # Validate the patch without dropping fields from the current settings.
        _validate(BusinessSettings,settings)
        def change(current,revision,timestamp):
            current["settings"]=_validate(BusinessSettings,{**current["settings"],**settings})
        return self._mutate(collection,expected_revision,"settings","operator",change)

    def save_connection(self,collection,body):
        draft=_validate(ConnectionDraft,body)
        def change(current,revision,timestamp):
            if len(current["connections"])>=16:
                raise ValueError("Review the existing source configuration drafts.")
            current["connections"].append({"id":"connection-"+uuid.uuid4().hex,
                "collection":collection,"provider":draft["provider"],"type":draft["type"],
                "source_description":draft["source_description"],
                "history_hours":current["settings"]["initial_history_hours"] if draft["history_hours"] is None else draft["history_hours"],
                "status":"configuration_required","ingestion_started":False,"created_at":timestamp,
                "provenance":{"origin":"operator_configuration_draft","revision":revision}})
        return self._mutate(collection,draft["expected_revision"],"connection_draft","operator",change)


def install_business(selected):
    selected['business'] = RetailBusinessStore(selected['state'], selected.get('index'),
        collections=selected.get('collections'))
    selected['agent'].business_store = selected['business']


class BusinessSettingsRequest(BaseModel):
    collection: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    expected_revision: int = Field(ge=0)
    settings: dict


class BusinessConnectionRequest(BaseModel):
    collection: str = Field(pattern=r'^[a-z0-9][a-z0-9_-]{0,63}$')
    connection: dict


def register_routes(app):
    from retail_urls import retail_router
    routes = retail_router(app)

    @routes.post('/api/retail/business/settings')
    async def business_settings(body: BusinessSettingsRequest, request: Request):
        try:
            return request.state.workspace['business'].update_settings(
                body.collection, body.expected_revision, body.settings)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @routes.post('/api/retail/business/connections')
    async def business_connection(body: BusinessConnectionRequest, request: Request):
        try:
            return request.state.workspace['business'].save_connection(body.collection, body.connection)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
