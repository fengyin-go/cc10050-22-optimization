"""货币换算器领域服务：管理按日期汇率快照的多币种价格口径统一，负责快照生效与过期治理。

数据链路位置：口径统一层，消费站点目录声明的生效货币，为比价计算提供
汇率换算依据，产出的统一币种价格供基线与比价使用。

汇率替换失败恢复：
- 把“一次只启用一条快照”扩成“一笔可续办的替换申请”，运营一次提交多个币对；
- 每个币对只认自己币对下可用的快照，同日其他币对的快照不能顶替；
- 逐币对返回变更回执：已确认 / 待补材料，失败项保留旧快照与旧价供继续查询；
- 同一申请内已确认的币对再次提交时幂等，不生成第二份换算变更记录；
- 换算变更记录进入变价历史流水消费，按币对关联价格事件并重算实际换算金额，
  每次变更只重算一次，同时留存前后换算依据与后续复核范围。
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

APPLICATION_TABLE = "currency_replacement"
CHANGE_TABLE = "currency_change"

ITEM_CONFIRMED = "已确认"
ITEM_PENDING = "待补材料"

APP_CONFIRMED = "已全部确认"
APP_PARTIAL = "部分确认"
APP_PENDING = "待补材料"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


def _new_id(rows: list[dict[str, Any]]) -> int:
    return max((int(row.get("id", 0) or 0) for row in rows), default=0) + 1


def _pair_label(source: str, target: str) -> str:
    return f"{source}/{target}"


def _as_float(value: Any) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number


def _fmt_rate(value: float | None) -> str:
    if value is None:
        return ""
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    return text if "." in text else f"{text}.0"


class CurrencyConvService(BaseModuleService):
    MODULE = "currency_conv"
    ENTITY = "汇率快照"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["快照日期", "源货币", "目标货币", "汇率值", "取数来源"]
    REQUIRED_FIELDS = ["快照日期", "源货币", "目标货币"]
    STATUS_ORDER = ["待生效", "已生效", "已过期"]
    ACTION_RULES = {"生效快照": "已生效", "过期快照": "已过期"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：汇率快照与换算口径 ----

    def activate_snapshot(self, entry_id: int) -> dict[str, Any]:
        """生效汇率快照，同币对旧快照自动过期。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        rate = str(entry.get("汇率值", "") or "")
        try:
            value = float(rate)
        except ValueError:
            return {"ok": False, "message": "汇率值无效，不能生效"}
        if value <= 0:
            return {"ok": False, "message": "汇率值必须为正数"}
        expired = 0
        for row in store.rows(self.MODULE):
            if row.get("id") != entry.get("id") and row.get("status") == "已生效":
                if (row.get("源货币"), row.get("目标货币")) == (
                    entry.get("源货币"), entry.get("目标货币")
                ):
                    row["status"] = "已过期"
                    row["pending"] = False
                    expired += 1
        entry["status"] = "已生效"
        entry["pending"] = False
        return {"ok": True, "message": "快照已生效", "expired_old": expired}

    def convert_price(self, entry_id: int, amount: float) -> dict[str, Any]:
        """按生效快照把金额从源货币换算到目标货币。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"ok": False, "message": "快照不存在或未生效"}
        rate = float(entry.get("汇率值", "0") or 0)
        if rate <= 0:
            return {"ok": False, "message": "汇率值无效"}
        converted = round(amount * rate, 2)
        return {
            "ok": True,
            "message": "换算完成",
            "from": entry.get("源货币", ""),
            "to": entry.get("目标货币", ""),
            "amount": amount,
            "converted": converted,
            "snapshot_date": entry.get("快照日期", ""),
        }

    def effective_rate(self, source: str, target: str) -> float | None:
        """查询指定币对当前生效汇率。"""
        for row in store.rows(self.MODULE):
            if (row.get("status") == "已生效"
                    and row.get("源货币") == source and row.get("目标货币") == target):
                try:
                    return float(row.get("汇率值", "0") or 0)
                except ValueError:
                    return None
        return None

    def expire_by_date(self, before_date: str) -> dict[str, Any]:
        """按日期批量过期旧快照，并向变价历史同步口径变更。"""
        expired = 0
        for row in store.rows(self.MODULE):
            if row.get("status") == "已生效" and str(row.get("快照日期", "")) < before_date:
                row["status"] = "已过期"
                row["pending"] = False
                expired += 1
        if expired:
            hrows = store.rows("change_history")
            hid = _new_id(hrows)
            hrows.append({
                "id": hid,
                "商品标识": "CURRENCY-ALL",
                "旧价格": "旧汇率快照",
                "新价格": f"已过期 {expired} 条",
                "变化幅度": "口径切换",
                "发现时间": _now(),
                "status": "待核实",
                "pending": True,
                "abnormal": False,
            })
        return {"ok": True, "message": f"已过期 {expired} 条快照", "expired": expired}

    def ensure_site_currency(self, entry_id: int) -> dict[str, Any]:
        """检查站点生效货币是否具备可用快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "快照不存在"}
        currency = entry.get("源货币", "")
        covered = any(
            r.get("status") == "已生效"
            and (r.get("源货币") == currency or r.get("目标货币") == currency)
            for r in store.rows(self.MODULE)
        )
        if not covered:
            entry["pending"] = True
        return {
            "ok": True,
            "message": "货币覆盖检查完成",
            "currency": currency,
            "covered": covered,
        }

    def snapshot_for_export(self, entry_id: int) -> dict[str, Any]:
        """向比价导出通道提供换算口径说明。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已生效":
            return {"ok": False, "message": "快照不存在或未生效"}
        return {
            "ok": True,
            "message": "换算口径已提供",
            "rate": entry.get("汇率值", ""),
            "pair": f"{entry.get('源货币', '')}->{entry.get('目标货币', '')}",
            "source": entry.get("取数来源", ""),
            "date": entry.get("快照日期", ""),
        }

    # ---- 替换申请：多币对批次提交、失败恢复与续办 ----

    def submit_replacement(
        self,
        items: list[dict[str, Any]] | None = None,
        *,
        application_id: int | None = None,
        remark: str = "",
    ) -> dict[str, Any]:
        """提交或续办一笔汇率替换申请。

        首次提交（application_id 为空）必须是“核对过的替换快照”，缺快照或快照
        没有有效汇率时返回逐项待补材料回执；续办（带 application_id）只允许按
        原申请里的币对补传，可以直接补交汇率值并回填到该币对自己的快照。

        同一申请内每个币对最多生成一条换算变更记录，重复提交已确认币对直接
        返回原回执，不会生成第二份记录，也不会触发第二次重算。
        """
        normalized = self._normalize_items(items or [])
        if not normalized:
            return {"ok": False, "message": "替换申请缺少币对明细", "receipts": []}

        app: dict[str, Any] | None = None
        if application_id is not None:
            app = self._find_application(application_id)
            if app is None:
                return {
                    "ok": False,
                    "message": f"替换申请 {application_id} 不存在",
                    "receipts": [],
                }
            original_pairs = {item.get("币对") for item in app.get("items", [])}
            unknown = [item["币对"] for item in normalized if item.get("币对") not in original_pairs]
            absent_pair = [item for item in normalized if not item.get("币对")]
            if unknown or absent_pair:
                names = "、".join(unknown + ["无法识别币对"] * len(absent_pair))
                return {
                    "ok": False,
                    "message": f"续办只能从原申请按原币对补传，以下币对不在原单：{names}",
                    "code": "PAIR_NOT_IN_APPLICATION",
                    "application": self.application_view(app),
                    "receipts": self._receipts(app),
                }
            app["resubmit_count"] = int(app.get("resubmit_count", 0)) + 1
        else:
            app = self._create_application(normalized, remark)

        if remark:
            app["remark"] = remark
            app["备注"] = remark

        submitted_pairs: list[str] = []
        duplicate_pairs: list[str] = []
        for raw in normalized:
            pair = raw["币对"]
            submitted_pairs.append(pair)
            item = self._application_item(app, pair)
            if item is None:
                item = self._new_item(raw)
                app["items"].append(item)
            if item.get("状态") == ITEM_CONFIRMED:
                duplicate_pairs.append(pair)
                continue
            self._process_item(app, item, raw, allow_supplement=application_id is not None)

        self._refresh_application(app)
        receipts = self._receipts(app, duplicate_pairs)
        changes = self._application_changes(app)
        comparisons = self._comparisons(app)
        payload: dict[str, Any] = {
            "ok": True,
            "message": self._application_message(app),
            "code": "PARTIAL_RECEIPTS" if app.get("pending_count") else "ALL_CONFIRMED",
            "application": self.application_view(app),
            "receipts": receipts,
            "逐项回执": receipts,
            "change_records": changes,
            "换算变更记录": changes,
            "comparison": comparisons,
            "submitted_pairs": submitted_pairs,
            "duplicate_pairs": duplicate_pairs,
            "recompute_count": sum(int(c.get("recompute_count", 0)) for c in changes),
        }
        if app.get("pending_count"):
            payload["pending_pairs"] = [
                item.get("币对") for item in app.get("items", [])
                if item.get("状态") != ITEM_CONFIRMED
            ]
            payload["retryable"] = True
        return payload

    def resubmit_replacement(
        self,
        application_id: int,
        items: list[dict[str, Any]],
        remark: str = "",
    ) -> dict[str, Any]:
        """从原申请续办：按原币对补传材料并返回变价流水与前后口径对比。"""
        result = self.submit_replacement(
            items, application_id=application_id, remark=remark)
        if not result.get("ok"):
            return result
        app = self._find_application(application_id)
        if app is None:
            return {"ok": False, "message": f"替换申请 {application_id} 不存在"}
        pairs = [item.get("币对") for item in app.get("items", [])]
        history = self.change_history_for_pairs(pairs)
        result["change_history"] = history
        result["变价流水"] = history
        result["comparison"] = self._comparisons(app)
        return result

    def list_applications(self, status: str | None = None) -> dict[str, Any]:
        """列出全部替换申请，供运营查看续办状态。"""
        rows = list(store.rows(APPLICATION_TABLE))
        if status:
            rows = [row for row in rows if row.get("status") == status]
        rows.sort(key=lambda row: str(row.get("updated_at", "")), reverse=True)
        views = [self.application_view(row) for row in rows]
        return {
            "ok": True,
            "message": "替换申请列表读取完成",
            "items": views,
            "total": len(views),
        }

    def get_application(self, application_id: int) -> dict[str, Any]:
        """读取单笔替换申请及逐项回执。"""
        app = self._find_application(application_id)
        if app is None:
            return {"ok": False, "message": f"替换申请 {application_id} 不存在"}
        view = self.application_view(app)
        view["receipts"] = self._receipts(app)
        view["change_records"] = self._application_changes(app)
        view["comparison"] = self._comparisons(app)
        return {"ok": True, "message": "替换申请读取完成", "application": view,
                "receipts": view["receipts"]}

    def consume_application(self, application_id: int, mode: str = "per_pair") -> dict[str, Any]:
        """消费替换申请释出的换算变更。

        mode=per_pair：已经确认的币对立即释出依据，待补材料的币对继续等待；
        mode=all/group：整组未齐时只返回等候状态，确认齐了才一次释出；
        任一模式对同一条变更都只重算一次，重复消费拿到同一个重算凭据。
        """
        app = self._find_application(application_id)
        if app is None:
            return {"ok": False, "message": f"替换申请 {application_id} 不存在"}
        mode = (mode or "per_pair").lower()
        group_mode = mode in {"all", "group", "整组", "whole"}
        changes = self._application_changes(app)
        pending = [
            item.get("币对") for item in app.get("items", [])
            if item.get("状态") != ITEM_CONFIRMED
        ]
        if group_mode and pending:
            return {
                "ok": True,
                "message": "整组尚未确认，消费者继续等候",
                "waiting_for_group": True,
                "mode": "group",
                "released": [],
                "released_pairs": [],
                "pending_pairs": pending,
                "recompute_count": 0,
            }
        released = []
        for change in changes:
            consumption = self._recompute_pair_events(change)
            released.append({
                "币对": change.get("币对"),
                "pair": change.get("币对"),
                "汇率值": change.get("新汇率"),
                "new_rate": change.get("新汇率"),
                "旧汇率": change.get("旧汇率"),
                "old_rate": change.get("旧汇率"),
                "快照ID": change.get("新快照ID"),
                "snapshot_id": change.get("新快照ID"),
                "快照日期": change.get("新快照日期"),
                "申请编号": change.get("申请编号"),
                "变更记录ID": change.get("id"),
                "change_record_id": change.get("id"),
                "recompute_count": consumption["recompute_count"],
                "recompute_token": consumption["recompute_token"],
                "重算凭据": consumption["recompute_token"],
                "recomputed_at": consumption.get("recomputed_at", ""),
                "events": consumption["events"],
                "复核范围": consumption["review_scope"],
                "retain_basis": consumption["basis"],
            })
        return {
            "ok": True,
            "message": "逐币对释出完成" if not group_mode else "整组释出完成",
            "waiting_for_group": False,
            "mode": "group" if group_mode else "per_pair",
            "released": released,
            "released_pairs": [item["pair"] for item in released],
            "pending_pairs": pending,
            "recompute_count": sum(item["recompute_count"] for item in released),
            "复核范围": sorted({
                scope
                for item in released
                for scope in item.get("复核范围", [])
            }),
        }

    def change_history_for_pairs(self, pairs: list[str]) -> list[dict[str, Any]]:
        """按币对返回变价流水里已关联换算依据的价格事件。"""
        rows = [
            row for row in store.rows("change_history")
            if row.get("币对") in pairs
        ]
        return [self._history_view(row) for row in rows]

    def conversion_changes(self, pair: str | None = None) -> dict[str, Any]:
        """列出换算变更记录及各自重算状态。"""
        rows = list(store.rows(CHANGE_TABLE))
        if pair:
            rows = [row for row in rows if row.get("币对") == pair]
        return {
            "ok": True,
            "message": "换算变更记录读取完成",
            "items": [dict(row) for row in rows],
            "total": len(rows),
        }

    def conversion_metrics(self) -> dict[str, Any]:
        """指标接口：消费各币对的换算变更并重算实际换算金额。

        每个变更记录只在首次消费时重算一次，后续调用复用同一重算凭据，
        返回值保留换算前后依据与后续复核范围。
        """
        rows = list(store.rows(CHANGE_TABLE))
        by_pair: dict[str, dict[str, Any]] = {}
        for change in rows:
            consumption = self._recompute_pair_events(change)
            pair = str(change.get("币对", ""))
            bucket = by_pair.setdefault(pair, {
                "币对": pair,
                "pair": pair,
                "变更记录数": 0,
                "汇率版本": [],
                "实际换算金额合计": 0.0,
                "价格事件": [],
                "复核范围": [],
                "recompute_count": 0,
                "_events": {},
            })
            bucket["变更记录数"] += 1
            bucket["汇率版本"].append(change.get("汇率版本", ""))
            bucket["复核范围"] = sorted(set(bucket["复核范围"]) | set(consumption["review_scope"]))
            bucket["recompute_count"] += consumption["recompute_count"]
            # 同一价格事件可能出现在多条变更记录里：只保留最近一次重算结果，
            # 避免按币对加总时把同一事件重复计入。
            for event in consumption["events"]:
                bucket["_events"][event.get("id")] = event
        pairs = []
        for bucket in by_pair.values():
            events = list(bucket.pop("_events").values())
            bucket["价格事件"] = events
            bucket["实际换算金额合计"] = round(
                sum(float(event.get("换算后金额") or 0) for event in events), 2)
            pairs.append(bucket)
        total_actual = round(sum(float(p["实际换算金额合计"]) for p in pairs), 2)
        return {
            "ok": True,
            "message": "换算指标重算完成",
            "generated_at": _now(),
            "changes": len(rows),
            "pairs": pairs,
            "total_actual_amount": total_actual,
            "实际换算金额合计": total_actual,
            "review_scope": sorted({scope for p in pairs for scope in p["复核范围"]}),
        }

    def pair_effective_snapshot(self, pair: str) -> dict[str, Any]:
        """查询币对当前生效快照，缺替换资料时旧价仍可查。"""
        source, target = self._split_pair(pair)
        if not source or not target:
            return {"ok": False, "message": "币对格式应为 源货币/目标货币"}
        row = self._effective_snapshot(source, target)
        if row is None:
            return {"ok": False, "message": f"{pair} 没有生效快照", "pair": pair}
        return {
            "ok": True,
            "message": "生效快照读取完成",
            "pair": pair,
            "snapshot_id": row.get("id"),
            "rate": _as_float(row.get("汇率值")),
            "snapshot_date": row.get("快照日期", ""),
            "source": row.get("取数来源", ""),
        }

    # ---- 内部实现 ----

    @staticmethod
    def _split_pair(pair: Any) -> tuple[str, str]:
        if not pair:
            return "", ""
        text = str(pair).strip().replace("-", "/").replace("->", "/")
        if "/" not in text:
            return "", ""
        source, target = (part.strip().upper() for part in text.split("/", 1))
        return source, target

    def _normalize_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for raw in items:
            if not isinstance(raw, dict):
                continue
            source, target = self._split_pair(
                raw.get("币对") or raw.get("pair") or raw.get("currency_pair"))
            source = source or str(raw.get("源货币") or raw.get("source") or "").strip().upper()
            target = target or str(raw.get("目标货币") or raw.get("target") or "").strip().upper()
            snapshot_raw = raw.get("快照ID", raw.get("snapshot_id", raw.get("snapshotId")))
            snapshot_id: int | None = None
            if snapshot_raw not in (None, ""):
                try:
                    snapshot_id = int(str(snapshot_raw))
                except ValueError:
                    snapshot_id = None
            rate = _as_float(raw.get("汇率值", raw.get("rate")))
            normalized.append({
                "币对": _pair_label(source, target) if source and target else "",
                "源货币": source,
                "目标货币": target,
                "汇率值": rate,
                "快照日期": str(raw.get("快照日期") or raw.get("snapshot_date") or ""),
                "快照ID": snapshot_id,
                "取数来源": str(raw.get("取数来源") or raw.get("source_name") or ""),
            })
        return normalized

    def _create_application(self, items: list[dict[str, Any]], remark: str) -> dict[str, Any]:
        rows = store.rows(APPLICATION_TABLE)
        app_id = _new_id(rows)
        app_no = f"RATE-{datetime.now(timezone.utc):%Y%m%d}-{app_id:04d}"
        app = {
            "id": app_id,
            "申请编号": app_no,
            "code": app_no,
            "status": APP_PENDING,
            "状态": APP_PENDING,
            "items": [],
            "created_at": _now(),
            "updated_at": _now(),
            "resubmit_count": 0,
            "remark": remark or "",
            "备注": remark or "",
            "confirmed_count": 0,
            "pending_count": len(items),
        }
        rows.append(app)
        return app

    def _find_application(self, application_id: int) -> dict[str, Any] | None:
        for row in store.rows(APPLICATION_TABLE):
            if int(row.get("id", 0) or 0) == int(application_id):
                return row
        return None

    def _application_item(self, app: dict[str, Any], pair: str) -> dict[str, Any] | None:
        for item in app.get("items", []):
            if item.get("币对") == pair:
                return item
        return None

    def _new_item(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {
            "币对": raw.get("币对", ""),
            "pair": raw.get("币对", ""),
            "源货币": raw.get("源货币", ""),
            "目标货币": raw.get("目标货币", ""),
            "汇率值": None,
            "快照ID": None,
            "快照日期": "",
            "旧汇率": None,
            "旧快照ID": None,
            "旧快照日期": "",
            "状态": ITEM_PENDING,
            "state": "pending_material",
            "可重试": True,
            "retryable": True,
            "变更记录ID": None,
            "change_record_id": None,
            "回执": "",
            "receipt": "",
        }

    def _snapshot_candidates(self, source: str, target: str) -> list[dict[str, Any]]:
        """只返回该币对自己的快照；日期相同的其他币对不参与匹配。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("源货币") == source and row.get("目标货币") == target
        ]

    def _effective_snapshot(self, source: str, target: str,
                            exclude_id: int | None = None) -> dict[str, Any] | None:
        candidates = [
            row for row in self._snapshot_candidates(source, target)
            if row.get("status") == "已生效"
            and (exclude_id is None or int(row.get("id", 0)) != exclude_id)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: str(row.get("快照日期", "")))

    def _resolve_snapshot(self, item: dict[str, Any], raw: dict[str, Any],
                          allow_supplement: bool) -> tuple[dict[str, Any] | None, str]:
        """解析该币对可用的替换快照，返回 (快照, 失败原因)。

        规则：
        1. 指定快照 ID 时必须是该币对自己快照，不属于该币对直接失败；
        2. 未指定 ID 时只在该币对下按日期/最新待生效快照匹配，绝不跨币对；
        3. 首次提交缺快照或快照汇率无效 → 待补材料；续办时允许把补传汇率
           回填到该币对自己缺失/无效的快照，或为原币对新建补传快照。
        """
        source, target = item["源货币"], item["目标货币"]
        snapshot_id = raw.get("快照ID")
        pair = item["币对"]

        if snapshot_id is not None:
            row = store.find(self.MODULE, int(snapshot_id))
            if row is None:
                return None, f"{pair} 指定的替换快照 {snapshot_id} 不存在，待补材料"
            if (row.get("源货币"), row.get("目标货币")) != (source, target):
                return None, (
                    f"快照 {snapshot_id} 属于 {row.get('源货币')}/"
                    f"{row.get('目标货币')}，不能用于 {pair}；"
                    "相同日期的其他币对快照不能替代"
                )
            candidate = row
        else:
            same_pair = self._snapshot_candidates(source, target)
            date_hint = str(raw.get("快照日期") or "")
            if date_hint:
                dated = [row for row in same_pair if str(row.get("快照日期", "")) == date_hint]
                if dated:
                    candidate = max(dated, key=lambda row: 1 if row.get("status") != "已过期" else 0)
                elif allow_supplement:
                    candidate = None
                else:
                    return None, (
                        f"{pair} 在 {date_hint} 没有自己的替换快照，"
                        "不能借用同日其他币对快照"
                    )
            else:
                usable = [row for row in same_pair if row.get("status") in ("待生效", "已生效")]
                if usable:
                    candidate = max(usable, key=lambda row: str(row.get("快照日期", "")))
                elif allow_supplement:
                    candidate = None
                else:
                    return None, f"{pair} 缺替换快照"
        if candidate is None:
            if not allow_supplement:
                return None, f"{pair} 缺替换快照"
            rate = raw.get("汇率值")
            if rate is None or rate <= 0:
                return None, f"{pair} 缺有效汇率，补传后才能重试"
            candidate = self._create_supplement_snapshot(item, raw)
            return candidate, ""

        rate = _as_float(candidate.get("汇率值"))
        if rate is None or rate <= 0:
            provided = raw.get("汇率值")
            if allow_supplement and provided is not None and provided > 0:
                candidate["汇率值"] = _fmt_rate(provided)
                candidate["取数来源"] = candidate.get("取数来源") or "原单补传"
                candidate["补传"] = True
                return candidate, ""
            return None, (
                f"{pair} 的替换快照 {candidate.get('id')} 没有有效汇率，"
                "待补材料后可重试"
            )

        provided = raw.get("汇率值")
        if provided is not None and abs(provided - rate) > 1e-12:
            return None, (
                f"{pair} 提交汇率 {_fmt_rate(provided)} 与快照 "
                f"{candidate.get('id')} 的 {_fmt_rate(rate)} 不一致"
            )
        if raw.get("快照日期") and str(raw.get("快照日期")) != str(candidate.get("快照日期", "")):
            return None, (
                f"{pair} 提交日期 {raw.get('快照日期')} 与快照 "
                f"{candidate.get('id')} 的 {candidate.get('快照日期')} 不一致"
            )
        return candidate, ""

    def _create_supplement_snapshot(self, item: dict[str, Any],
                                    raw: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(self.MODULE)
        row = {
            "id": _new_id(rows),
            "快照日期": raw.get("快照日期") or _today(),
            "源货币": item["源货币"],
            "目标货币": item["目标货币"],
            "汇率值": _fmt_rate(raw.get("汇率值")),
            "取数来源": item.get("取数来源") or "原单补传",
            "status": "待生效",
            "pending": True,
            "abnormal": False,
            "补传": True,
        }
        rows.append(row)
        return row

    def _process_item(self, app: dict[str, Any], item: dict[str, Any],
                      raw: dict[str, Any], *, allow_supplement: bool) -> None:
        pair = item["币对"]
        if raw.get("快照ID") is not None:
            item["提交快照ID"] = raw.get("快照ID")
        snapshot, reason = self._resolve_snapshot(item, raw, allow_supplement)
        old_snapshot = self._effective_snapshot(item["源货币"], item["目标货币"])
        if old_snapshot is not None:
            item["旧汇率"] = _as_float(old_snapshot.get("汇率值"))
            item["旧快照ID"] = old_snapshot.get("id")
            item["旧快照日期"] = old_snapshot.get("快照日期", "")
            item["old_rate"] = item["旧汇率"]
            item["old_snapshot_id"] = item["旧快照ID"]
            item["旧价可查"] = True
            item["old_price_queryable"] = True
        if snapshot is None:
            self._mark_pending(item, pair, reason)
            return

        rate = _as_float(snapshot.get("汇率值"))
        if rate is None or rate <= 0:
            self._mark_pending(item, pair, f"{pair} 替换快照没有有效汇率，待补材料")
            return

        if (old_snapshot is not None
                and int(old_snapshot.get("id", 0)) == int(snapshot.get("id", 0))):
            # 提交的就是当前生效快照：汇率没有变化，不生成换算变更记录。
            item["状态"] = ITEM_CONFIRMED
            item["state"] = "confirmed"
            item["可重试"] = False
            item["retryable"] = False
            item["汇率值"] = rate
            item["rate"] = rate
            item["快照ID"] = snapshot.get("id")
            item["snapshot_id"] = snapshot.get("id")
            item["快照日期"] = snapshot.get("快照日期", "")
            item["变更记录ID"] = None
            item["change_record_id"] = None
            item["冲突"] = False
            item["conflict_with_unverified"] = False
            item["冲突说明"] = ""
            item["回执"] = (
                f"{pair} 已是当前生效口径（{_fmt_rate(rate)}，快照 {snapshot.get('id')}），"
                "汇率未变化，不生成换算变更记录"
            )
            item["receipt"] = item["回执"]
            return

        # 只过期该币对的旧生效快照，其他币对（即使同日）不受影响。
        self._activate_for_pair(snapshot, item["源货币"], item["目标货币"])

        existing_change = self._find_change(app, pair)
        created = False
        if existing_change is None:
            existing_change = self._create_change(app, item, old_snapshot, snapshot)
            created = True
        self._apply_change_to_item(item, snapshot, existing_change)
        if created:
            item["回执"] = (
                f"{pair} 已确认：{_fmt_rate(item.get('旧汇率'))} -> {_fmt_rate(rate)}，"
                f"快照 {snapshot.get('id')}（{snapshot.get('快照日期')}）已生效"
            )
        else:
            item["回执"] = f"{pair} 已确认，换算变更记录 {existing_change.get('id')} 已存在，未重复生成"
        item["receipt"] = item["回执"]
        self._recompute_pair_events(existing_change)

    def _mark_pending(self, item: dict[str, Any], pair: str, reason: str) -> None:
        item["状态"] = ITEM_PENDING
        item["state"] = "pending_material"
        item["可重试"] = True
        item["retryable"] = True
        # 同日存在该币对但未核验（待生效 / 汇率无效）的替换快照时，标记冲突：
        # 未核验数据不能顶替，交付继续沿用原生效快照，直到补传确认。
        unverified = [
            row for row in self._snapshot_candidates(item["源货币"], item["目标货币"])
            if row.get("status") == "待生效"
            or (_as_float(row.get("汇率值")) or 0) <= 0
        ]
        conflict = bool(unverified)
        item["冲突"] = conflict
        item["conflict_with_unverified"] = conflict
        if conflict:
            item["冲突说明"] = (
                f"{pair} 存在未核验的替换数据，本次不参与交付；原价继续可查")
        item["回执"] = (
            f"{reason}；可重试，未核验数据不参与交付。旧价 "
            f"{_fmt_rate(item.get('旧汇率')) or '未记录'}（快照 "
            f"{item.get('旧快照ID') or '—'}）仍可查询"
        )
        item["receipt"] = item["回执"]
        item["change_record_id"] = None
        item["变更记录ID"] = None

    def _activate_for_pair(self, snapshot: dict[str, Any], source: str, target: str) -> None:
        for row in self._snapshot_candidates(source, target):
            if int(row.get("id", 0)) != int(snapshot.get("id", 0)) and row.get("status") == "已生效":
                row["status"] = "已过期"
                row["pending"] = False
        snapshot["status"] = "已生效"
        snapshot["pending"] = False
        snapshot["abnormal"] = False
        snapshot["生效时间"] = _now()

    def _apply_change_to_item(self, item: dict[str, Any], snapshot: dict[str, Any],
                              change: dict[str, Any]) -> None:
        item["状态"] = ITEM_CONFIRMED
        item["state"] = "confirmed"
        item["可重试"] = False
        item["retryable"] = False
        item["汇率值"] = change.get("新汇率")
        item["rate"] = change.get("新汇率")
        item["快照ID"] = snapshot.get("id")
        item["snapshot_id"] = snapshot.get("id")
        item["快照日期"] = snapshot.get("快照日期", "")
        item["snapshot_date"] = snapshot.get("快照日期", "")
        item["变更记录ID"] = change.get("id")
        item["change_record_id"] = change.get("id")
        item["冲突"] = False
        item["conflict_with_unverified"] = False
        item["冲突说明"] = ""

    def _find_change(self, app: dict[str, Any], pair: str) -> dict[str, Any] | None:
        for row in store.rows(CHANGE_TABLE):
            if row.get("申请ID") == app.get("id") and row.get("币对") == pair:
                return row
        return None

    def _create_change(self, app: dict[str, Any], item: dict[str, Any],
                       old_snapshot: dict[str, Any] | None,
                       snapshot: dict[str, Any]) -> dict[str, Any]:
        rows = store.rows(CHANGE_TABLE)
        change_id = _new_id(rows)
        old_rate = _as_float(old_snapshot.get("汇率值")) if old_snapshot else None
        new_rate = _as_float(snapshot.get("汇率值"))
        if old_rate is None:
            # 没有旧生效快照时，用同币对最近一条变更记录的新汇率作为前值，避免伪造旧值，
            # 旧快照 ID / 日期取该变更记录里的新快照，而不是变更记录自己的 id。
            previous = [
                row for row in rows
                if row.get("币对") == item["币对"] and row.get("新汇率") is not None
            ]
            if previous:
                latest = previous[-1]
                old_rate = latest.get("新汇率")
                old_snapshot = {
                    "id": latest.get("新快照ID"),
                    "快照日期": latest.get("新快照日期", ""),
                }
            else:
                old_rate = new_rate
                old_snapshot = None
        change = {
            "id": change_id,
            "申请ID": app.get("id"),
            "application_id": app.get("id"),
            "申请编号": app.get("申请编号"),
            "币对": item["币对"],
            "pair": item["币对"],
            "源货币": item["源货币"],
            "目标货币": item["目标货币"],
            "旧汇率": old_rate,
            "新汇率": new_rate,
            "old_rate": old_rate,
            "new_rate": new_rate,
            "旧快照ID": old_snapshot.get("id") if old_snapshot else None,
            "新快照ID": snapshot.get("id"),
            "old_snapshot_id": old_snapshot.get("id") if old_snapshot else None,
            "new_snapshot_id": snapshot.get("id"),
            "旧快照日期": old_snapshot.get("快照日期", "") if old_snapshot else "",
            "新快照日期": snapshot.get("快照日期", ""),
            "汇率版本": f"{_fmt_rate(old_rate)} -> {_fmt_rate(new_rate)}",
            "status": "已确认",
            "状态": "已确认",
            "created_at": _now(),
            "applied_at": _now(),
            "recompute_count": 0,
            "recomputed_at": "",
            "events": [],
            "review_scope": [f"{item['币对']} 价格事件"],
            "复算凭据": "",
        }
        rows.append(change)
        return change

    def _recompute_pair_events(self, change: dict[str, Any]) -> dict[str, Any]:
        """按币对把换算快照关联到价格事件，只重算一次并缓存凭据。

        重算口径：以原币金额为准，旧价格=旧汇率×旧原币金额，新价格=新汇率×
        新原币金额，同时留存换算前金额、换算后金额与前后汇率版本。
        """
        if int(change.get("recompute_count", 0) or 0) > 0 and change.get("events") is not None:
            cached = change.get("consumption") or {}
            return cached
        pair = str(change.get("币对", ""))
        old_rate = float(change.get("旧汇率") or change.get("新汇率") or 0)
        new_rate = float(change.get("新汇率") or 0)
        events: list[dict[str, Any]] = []
        actual_total = 0.0
        timestamp = _now()
        for row in store.rows("change_history"):
            if row.get("币对") != pair:
                continue
            old_amount = _as_float(row.get("旧原币金额", row.get("原币金额")))
            new_amount = _as_float(row.get("新原币金额", row.get("原币金额")))
            if old_amount is None or new_amount is None:
                continue
            if old_rate <= 0 or new_rate <= 0:
                continue
            before_amount = round(old_amount * old_rate, 2)
            after_amount = round(new_amount * new_rate, 2)
            ratio = (after_amount - before_amount) / before_amount if before_amount else 0.0
            row["旧价格"] = before_amount
            row["新价格"] = after_amount
            row["变化幅度"] = f"{ratio:.2%}"
            row["换算前金额"] = before_amount
            row["换算后金额"] = after_amount
            row["换算金额"] = after_amount
            row["换算前汇率"] = old_rate
            row["换算后汇率"] = new_rate
            row["旧汇率版本"] = _fmt_rate(old_rate)
            row["新汇率版本"] = _fmt_rate(new_rate)
            row["汇率版本"] = f"{_fmt_rate(old_rate)} -> {_fmt_rate(new_rate)}"
            row["汇率值"] = _fmt_rate(new_rate)
            row["换算快照"] = change.get("新快照ID")
            row["换算快照日期"] = change.get("新快照日期", "")
            row["换算变更记录"] = change.get("id")
            row["换算申请"] = change.get("申请编号")
            row["换算依据"] = {
                "before": {
                    "rate": old_rate,
                    "snapshot_id": change.get("旧快照ID"),
                    "snapshot_date": change.get("旧快照日期", ""),
                },
                "after": {
                    "rate": new_rate,
                    "snapshot_id": change.get("新快照ID"),
                    "snapshot_date": change.get("新快照日期", ""),
                },
            }
            row["复核范围"] = f"{pair} 价格事件"
            row["重算时间"] = timestamp
            row["旧价仍可查"] = True
            events.append({
                "id": row.get("id"),
                "商品标识": row.get("商品标识", ""),
                "币对": pair,
                "pair": pair,
                "旧原币金额": old_amount,
                "新原币金额": new_amount,
                "换算前金额": before_amount,
                "换算后金额": after_amount,
                "旧价格": before_amount,
                "新价格": after_amount,
                "变化幅度": f"{ratio:.2%}",
                "旧汇率": old_rate,
                "新汇率": new_rate,
                "旧快照ID": change.get("旧快照ID"),
                "快照ID": change.get("新快照ID"),
                "变更记录ID": change.get("id"),
                "复核范围": f"{pair} 价格事件",
            })
            actual_total = round(actual_total + after_amount, 2)
        change["recompute_count"] = int(change.get("recompute_count", 0) or 0) + 1
        change["recomputed_at"] = timestamp
        change["events"] = events
        change["event_count"] = len(events)
        change["actual_total"] = actual_total
        change["recompute_token"] = f"RC-{change.get('id')}-{change.get('recompute_count')}"
        consumption = {
            "ok": True,
            "message": "换算变更已重算",
            "pair": pair,
            "change_record_id": change.get("id"),
            "events": events,
            "event_count": len(events),
            "actual_total": actual_total,
            "recompute_count": change.get("recompute_count"),
            "recompute_token": change.get("recompute_token"),
            "recomputed_at": timestamp,
            "review_scope": change.get("review_scope", []),
            "basis": change.get("汇率版本", ""),
        }
        change["consumption"] = consumption
        return consumption

    def _history_view(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row.get("id"),
            "商品标识": row.get("商品标识", ""),
            "币对": row.get("币对", ""),
            "pair": row.get("币对", ""),
            "旧价格": row.get("旧价格"),
            "新价格": row.get("新价格"),
            "变化幅度": row.get("变化幅度", ""),
            "换算前金额": row.get("换算前金额"),
            "换算后金额": row.get("换算后金额"),
            "换算前汇率": row.get("换算前汇率"),
            "换算后汇率": row.get("换算后汇率"),
            "旧汇率版本": row.get("旧汇率版本", ""),
            "新汇率版本": row.get("新汇率版本", ""),
            "汇率版本": row.get("汇率版本", ""),
            "换算快照": row.get("换算快照"),
            "换算快照日期": row.get("换算快照日期", ""),
            "换算变更记录": row.get("换算变更记录"),
            "换算申请": row.get("换算申请", ""),
            "换算依据": row.get("换算依据", {}),
            "复核范围": row.get("复核范围", ""),
            "发现时间": row.get("发现时间", ""),
            "重算时间": row.get("重算时间", ""),
            "status": row.get("status", ""),
            "旧价仍可查": row.get("旧价仍可查", True),
        }

    def _refresh_application(self, app: dict[str, Any]) -> None:
        items = app.get("items", [])
        confirmed = sum(1 for item in items if item.get("状态") == ITEM_CONFIRMED)
        pending = len(items) - confirmed
        app["confirmed_count"] = confirmed
        app["pending_count"] = pending
        if pending == 0:
            app["status"] = APP_CONFIRMED
        elif confirmed > 0:
            app["status"] = APP_PARTIAL
        else:
            app["status"] = APP_PENDING
        app["状态"] = app["status"]
        app["updated_at"] = _now()

    def _application_message(self, app: dict[str, Any]) -> str:
        if app.get("status") == APP_CONFIRMED:
            return (f"替换申请 {app.get('申请编号')} 已全部确认，"
                    f"{app.get('confirmed_count')} 个币对均生成一条换算变更记录")
        return (f"替换申请 {app.get('申请编号')} 部分确认："
                f"已确认 {app.get('confirmed_count')} 个币对，"
                f"待补材料 {app.get('pending_count')} 个币对，可按原币对续办")

    def _application_changes(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            dict(row) for row in store.rows(CHANGE_TABLE)
            if row.get("申请ID") == app.get("id")
        ]

    def _receipts(self, app: dict[str, Any],
                  duplicates: list[str] | None = None) -> list[dict[str, Any]]:
        duplicate_set = set(duplicates or [])
        receipts = []
        for item in app.get("items", []):
            pair = item.get("币对")
            note = item.get("回执", "")
            if pair in duplicate_set:
                change_id = item.get("变更记录ID")
                if change_id is not None:
                    note = (f"{pair} 已确认，本次复查沿用变更记录 {change_id}，"
                            "不再生成第二份换算变更记录")
                else:
                    note = f"{pair} 已确认且汇率未变化，本次复查不生成换算变更记录"
            receipts.append({
                "币对": pair,
                "pair": pair,
                "状态": item.get("状态"),
                "state": item.get("state"),
                "confirmed": item.get("状态") == ITEM_CONFIRMED,
                "复查": pair in duplicate_set,
                "reviewed": pair in duplicate_set,
                "可重试": bool(item.get("可重试")),
                "retryable": bool(item.get("可重试")),
                "回执": note,
                "receipt": note,
                "message": note,
                "汇率值": item.get("汇率值"),
                "rate": item.get("汇率值"),
                "快照ID": item.get("快照ID"),
                "snapshot_id": item.get("快照ID"),
                "提交快照ID": item.get("提交快照ID"),
                "submitted_snapshot_id": item.get("提交快照ID"),
                "快照日期": item.get("快照日期", ""),
                "snapshot_date": item.get("快照日期", ""),
                "旧汇率": item.get("旧汇率"),
                "old_rate": item.get("旧汇率"),
                "旧快照ID": item.get("旧快照ID"),
                "old_snapshot_id": item.get("旧快照ID"),
                "旧快照日期": item.get("旧快照日期", ""),
                "旧价可查": bool(item.get("旧价可查")),
                "old_price_queryable": bool(item.get("旧价可查")),
                "冲突": bool(item.get("冲突")),
                "conflict_with_unverified": bool(item.get("冲突")),
                "冲突说明": item.get("冲突说明", ""),
                "变更记录ID": item.get("变更记录ID"),
                "change_record_id": item.get("变更记录ID"),
            })
        return receipts

    def _comparisons(self, app: dict[str, Any]) -> list[dict[str, Any]]:
        """对比实际重算金额与对应汇率版本，供变价流水复核。"""
        comparisons = []
        for change in self._application_changes(app):
            consumption = self._recompute_pair_events(change)
            comparisons.append({
                "币对": change.get("币对"),
                "pair": change.get("币对"),
                "变更记录ID": change.get("id"),
                "申请编号": change.get("申请编号"),
                "旧汇率": change.get("旧汇率"),
                "新汇率": change.get("新汇率"),
                "旧汇率版本": _fmt_rate(change.get("旧汇率")),
                "新汇率版本": _fmt_rate(change.get("新汇率")),
                "旧快照ID": change.get("旧快照ID"),
                "新快照ID": change.get("新快照ID"),
                "实际换算金额": consumption["actual_total"],
                "价格事件数": consumption["event_count"],
                "recompute_count": consumption["recompute_count"],
                "recompute_token": consumption["recompute_token"],
                "复核范围": change.get("review_scope", []),
                "价差": round(
                    consumption["actual_total"]
                    - sum(
                        float(event.get("换算前金额") or 0)
                        for event in consumption["events"]
                    ), 2),
            })
        return comparisons

    def application_view(self, app: dict[str, Any]) -> dict[str, Any]:
        view = dict(app)
        view["items"] = [self._receipts({"items": [item]})[0] for item in app.get("items", [])]
        view["receipts"] = self._receipts(app)
        view["pending_pairs"] = [
            item.get("币对") for item in app.get("items", [])
            if item.get("状态") != ITEM_CONFIRMED
        ]
        view["confirmed_pairs"] = [
            item.get("币对") for item in app.get("items", [])
            if item.get("状态") == ITEM_CONFIRMED
        ]
        return view
