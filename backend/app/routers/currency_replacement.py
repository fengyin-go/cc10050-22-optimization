"""汇率替换批次的专用接口：多币对提交、续办、换算变更消费与指标重算。

通用资源路由只覆盖单条快照的登记与动作；跨币对的替换申请需要按申请编号
续办并返回逐项回执，因此单独挂一条业务路由，业务判断仍留在 app/services 里。
固定路径（metrics / history）必须写在 /{application_id} 之前，避免被动态段抢先匹配。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionPayload, EntryPayload
from app.services import service_for

router = APIRouter(prefix="/api/currency-replacement", tags=["汇率替换"])


def currency_service():
    service = service_for("currency_conv")
    if service is None:
        raise HTTPException(status_code=404, detail="货币换算器服务未注册")
    return service


def history_service():
    service = service_for("change_history")
    if service is None:
        raise HTTPException(status_code=404, detail="变价历史流水服务未注册")
    return service


def _items(payload: EntryPayload) -> list[dict[str, Any]]:
    items = payload.values.get("items") or payload.values.get("币对") or []
    if isinstance(items, dict):
        items = [items]
    return items


@router.get("/metrics/converted", response_model=dict)
def conversion_metrics() -> dict[str, Any]:
    """指标接口：按币对重算实际换算金额并留存前后依据。"""
    return currency_service().conversion_metrics()


@router.post("/metrics/recompute", response_model=dict)
def recompute_metrics() -> dict[str, Any]:
    """指标接口：把重算结果写回采集量指标，便于在指标页查看。"""
    service = service_for("collect_metrics")
    if service is None:
        raise HTTPException(status_code=404, detail="采集量指标服务未注册")
    return service.recompute_converted_amounts()


@router.get("/history/events", response_model=dict)
def history_events(pair: str | None = Query(default=None)) -> dict[str, Any]:
    """按币对查看变价流水里已关联换算依据的价格事件（旧价仍可查）。"""
    return history_service().currency_price_events(pair)


@router.post("/history/consume", response_model=dict)
def consume_history(payload: EntryPayload) -> dict[str, Any]:
    """变价历史流水消费换算变更，按币对关联价格事件并重算一次。"""
    pair = payload.values.get("币对") or payload.values.get("pair") or None
    return history_service().consume_currency_changes(str(pair) if pair else None)


@router.get("/history/review-queue", response_model=dict)
def history_review_queue(application_id: int | None = Query(default=None)) -> dict[str, Any]:
    """换算变更后的复核范围。"""
    return history_service().currency_review_queue(application_id)


@router.post("", response_model=dict)
def submit_application(payload: EntryPayload) -> dict[str, Any]:
    """提交一笔可续办的多币对替换申请，返回逐项变更回执。"""
    remark = str(payload.values.get("remark") or payload.remark or "")
    return currency_service().submit_replacement(_items(payload), remark=remark)


@router.get("", response_model=dict)
def list_applications(status: str | None = Query(default=None)) -> dict[str, Any]:
    return currency_service().list_applications(status=status)


@router.get("/{application_id}", response_model=dict)
def get_application(application_id: int) -> dict[str, Any]:
    result = currency_service().get_application(application_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "申请不存在"))
    return result


@router.post("/{application_id}/resubmit", response_model=dict)
def resubmit_application(application_id: int, payload: EntryPayload) -> dict[str, Any]:
    """从原申请续办：只按原币对补传材料，返回变价流水与前后口径对比。"""
    result = currency_service().resubmit_replacement(
        application_id, _items(payload), remark=str(payload.remark or ""))
    return result


@router.post("/{application_id}/consume", response_model=dict)
def consume_application(application_id: int, payload: ActionPayload) -> dict[str, Any]:
    """消费释出的换算变更：per_pair 逐币对释出，group/all 等候整组。"""
    action = (payload.action or "").strip().lower()
    if action in {"per_pair", "group", "all", "逐币对", "整组"}:
        mode = action
    else:
        mode = (payload.remark or "").strip() or "per_pair"
    return currency_service().consume_application(application_id, mode=mode)


@router.get("/{application_id}/changes", response_model=dict)
def application_changes(application_id: int) -> dict[str, Any]:
    result = currency_service().get_application(application_id)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "申请不存在"))
    return {
        "ok": True,
        "message": "换算变更记录读取完成",
        "application": result.get("application"),
        "items": result.get("change_records", []),
        "total": len(result.get("change_records", [])),
    }
