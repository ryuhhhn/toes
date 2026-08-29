"""Ingestion API: analyze, inspect, approve.

The merchant approval screen (Frontend's work) is driven entirely by the JSON these
endpoints return. That screen is where derived domain claims become authorised ones, so
these responses carry not just the profile but what we could not parse and what we guessed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field

from app.clients.merchant import MerchantUnavailable, get_merchant_client
from app.ingestion.loader import UnreadableFile
from app.ingestion.merge import merge_profiles
from app.ingestion.pipeline import IngestResult, analyze_file, analyze_rows, enrich_with_llm
from app.ingestion.profile_store import get_profile_store
from app.models.profile import AgentProfile
from app.retrieval.registry import get_registry

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ingest"])


class AnalyzeRequest(BaseModel):
    merchant_id: str
    use_llm: bool = True


class ApproveRequest(BaseModel):
    profile: AgentProfile
    approved_by: str = "merchant"
    edited_fields: list[str] = Field(default_factory=list)
    reindex: bool = True


def _report(result: IngestResult) -> dict:
    """What we read, what we could not parse, and what we guessed."""
    coercions = []
    for column in result.column_profiles:
        report = column.coercion
        coercions.append(
            {
                "column": column.name,
                "read_as": report.applied or "text",
                "detail": report.detail,
                "unit": report.unit,
                "currency": report.currency,
                "unparseable_cells": report.failed_cells,
                "empty_rate": round(column.null_rate, 3),
                "kind": column.kind.value,
            }
        )

    roles = result.profile.roles
    return {
        "source": result.profile.source.model_dump(),
        "notes": result.profile.notes,
        "columns": coercions,
        "roles": {
            role: {
                "column": confidence.column,
                "confidence": confidence.confidence,
                "reason": confidence.reason,
            }
            for role, confidence in roles.confidence.items()
        },
        "questions": [
            f"We are not confident which column is the {role}. Please confirm."
            for role in result.low_confidence
        ],
        "blocking": [
            f"No {role} column was found. Checkout stays disabled until this is set."
            for role in roles.missing_required()
        ],
    }


async def _run(result: IngestResult, *, use_llm: bool, merchant_id: str) -> dict:
    if use_llm:
        result = await enrich_with_llm(result)

    store = get_profile_store()
    approved = store.load_approved(merchant_id)

    if approved is not None:
        merged, merge_report = merge_profiles(approved, result.profile)
        merged.version = store.next_version(merchant_id)
        result.profile = merged
        merge_summary = {
            "added": merge_report.added,
            "removed": merge_report.removed,
            "preserved_edits": merge_report.preserved,
            "requires_reapproval": merge_report.requires_reapproval,
            "reasons": merge_report.reasons,
        }
    else:
        result.profile.version = store.next_version(merchant_id)
        merge_summary = None

    store.save(result.profile)

    return {
        "profile": result.profile.model_dump(by_alias=True),
        "report": _report(result),
        "merge": merge_summary,
    }


@router.post("/analyze")
async def analyze(request: AnalyzeRequest) -> dict:
    """Pull rows from the Merchant Backend and derive a draft profile.

    Called by the merchant backend after an upload; also safe to call directly.
    """
    try:
        rows = await get_merchant_client().fetch_catalog(request.merchant_id)
    except MerchantUnavailable as exc:
        raise HTTPException(503, f"could not reach the merchant backend: {exc}") from exc

    if not rows:
        raise HTTPException(422, "the merchant backend returned an empty catalog")

    result = analyze_rows(rows, merchant_id=request.merchant_id)
    return await _run(result, use_llm=request.use_llm, merchant_id=request.merchant_id)


@router.post("/analyze/upload")
async def analyze_upload(
    merchant_id: str = Form(...),
    use_llm: bool = Form(True),
    sheet: str | None = Form(None),
    file: UploadFile = File(...),
) -> dict:
    """Direct file upload. The dev path, and the live "onboard an unrelated catalog" demo.

    Accepts every format the loader accepts: .csv, .tsv, .txt, .xlsx, .xls.
    """
    data = await file.read()
    try:
        result = analyze_file(
            data,
            merchant_id=merchant_id,
            filename=file.filename or "upload",
            sheet=sheet,
        )
    except UnreadableFile as exc:
        raise HTTPException(422, str(exc)) from exc

    return await _run(result, use_llm=use_llm, merchant_id=merchant_id)


@router.get("/profile/{merchant_id}")
async def get_profile(merchant_id: str, version: int | None = Query(None)) -> dict:
    store = get_profile_store()
    profile = (
        store.load_version(merchant_id, version) if version is not None else store.load(merchant_id)
    )
    if profile is None:
        raise HTTPException(404, f"no profile for merchant {merchant_id!r}")

    return {
        "profile": profile.model_dump(by_alias=True),
        "versions": store.list_versions(merchant_id),
        "approved_version": (
            store.load_approved(merchant_id).version
            if store.load_approved(merchant_id)
            else None
        ),
    }


@router.put("/profile/{merchant_id}")
async def approve_profile(merchant_id: str, request: ApproveRequest) -> dict:
    """The merchant's edited profile becomes the approved one, and triggers a reindex.

    Only here can required_before_purchase be set, and only here does a cross-field rule
    become something the agent is allowed to say.
    """
    profile = request.profile
    if profile.merchant_id != merchant_id:
        raise HTTPException(400, "merchant_id in the body does not match the URL")

    store = get_profile_store()
    if profile.version not in store.list_versions(merchant_id):
        profile.version = store.next_version(merchant_id)

    approved = store.approve(
        profile, approved_by=request.approved_by, edited_fields=request.edited_fields
    )

    reindexed = None
    if request.reindex:
        try:
            reindexed = await get_registry().rebuild(merchant_id, approved)
        except Exception as exc:  # noqa: BLE001 - approval must survive an index failure
            log.error("reindex after approval failed for %s: %s", merchant_id, exc)
            reindexed = {"ok": False, "error": str(exc)}

    return {"profile": approved.model_dump(by_alias=True), "reindex": reindexed}


@router.get("/report/{merchant_id}")
async def get_report(merchant_id: str) -> dict:
    """Human-readable ingestion summary, for the approval screen and for trust."""
    profile = get_profile_store().load(merchant_id)
    if profile is None:
        raise HTTPException(404, f"no profile for merchant {merchant_id!r}")

    return {
        "merchant_id": merchant_id,
        "version": profile.version,
        "status": profile.status,
        "category": profile.category,
        "category_confidence": profile.category_confidence,
        "derived_by": profile.derived_by,
        "source": profile.source.model_dump(),
        "notes": profile.notes,
        "roles": profile.roles.model_dump(),
        "fields": [
            {
                "column": spec.column,
                "read_as": spec.coercion.applied or "text",
                "kind": spec.kind.value,
                "tier": spec.tier,
                "layman_name": spec.layman_name,
                "why_it_matters": spec.why_it_matters,
                "values": spec.canonical_values[:12],
                "aliases_collapsed": len(spec.aliases),
                "unit": spec.unit,
                "currency": spec.currency,
                "empty_rate": spec.null_rate,
                "unparseable_cells": spec.coercion.failed_cells,
                "suggested_required": spec.suggested_required_before_purchase,
                "required_before_purchase": spec.required_before_purchase,
                "stale": spec.stale,
            }
            for spec in profile.fields
        ],
        "proposed_rules": [
            {
                "if": rule.if_,
                "then": rule.then,
                "message": rule.message,
                "approved": rule.approved_by_merchant,
                "columns": rule.columns,
            }
            for rule in profile.cross_field_rules
        ],
    }


@router.get("/merchants")
async def list_merchants() -> dict:
    store = get_profile_store()
    return {
        "merchants": [
            {
                "merchant_id": merchant_id,
                "versions": store.list_versions(merchant_id),
                "approved": bool(store.load_approved(merchant_id)),
            }
            for merchant_id in store.list_merchants()
        ]
    }
