"""变价历史流水领域服务：记录商品价格的历次变化供追溯，负责变价确认、误报标记与口径切换沉淀。

数据链路位置：追溯沉淀层，消费比价计算器的价差结果与货币换算器的口径变更，
确认后的变价记录供告警规则与导出通道引用。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ChangeHistoryService(BaseModuleService):
    MODULE = "change_history"
    ENTITY = "变价记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["商品标识", "旧价格", "新价格", "变化幅度", "发现时间"]
    REQUIRED_FIELDS = ["商品标识", "旧价格", "新价格"]
    STATUS_ORDER = ["已确认", "待核实", "误报"]
    ACTION_RULES = {"确认变价": "已确认", "标记误报": "误报"}
    NEGATIVE_ACTIONS = []

    # ---- 业务扩展：变价确认与追溯 ----

    def confirm_change(self, entry_id: int) -> dict[str, Any]:
        """确认一次变价并校验价格口径有效。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        try:
            old_price = float(entry.get("旧价格", "0") or 0)
            new_price = float(entry.get("新价格", "0") or 0)
        except ValueError:
            return {"ok": False, "message": "旧价格/新价格数值无效"}
        if old_price <= 0:
            return {"ok": False, "message": "旧价格必须为正数"}
        ratio = (new_price - old_price) / old_price
        entry["变化幅度"] = f"{ratio:.2%}"
        entry["status"] = "已确认"
        entry["pending"] = False
        return {
            "ok": True,
            "message": "变价已确认",
            "ratio": f"{ratio:.2%}",
            "goods": entry.get("商品标识", ""),
        }

    def mark_false_positive(self, entry_id: int, reason: str) -> dict[str, Any]:
        """标记误报并回写告警规则联动关闭。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "变价记录不存在"}
        entry["status"] = "误报"
        entry["pending"] = False
        goods_id = entry.get("商品标识", "")
        closed = 0
        for rule in store.rows("price_alert"):
            if rule.get("监控商品") == goods_id and rule.get("status") == "已生效":
                rule["status"] = "已关闭"
                rule["pending"] = False
                closed += 1
        return {
            "ok": True,
            "message": f"误报已标记: {reason}",
            "closed_rules": closed,
        }

    def query_by_goods(self, goods_id: str, status: str | None = None) -> dict[str, Any]:
        """按商品查询变价历史并支持状态过滤。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("商品标识") == goods_id]
        if status:
            rows = [r for r in rows if r.get("status") == status]
        rows.sort(key=lambda r: str(r.get("发现时间", "")), reverse=True)
        return {
            "ok": True,
            "message": "变价历史查询完成",
            "goods": goods_id,
            "count": len(rows),
            "records": [
                {
                    "old": r.get("旧价格", ""),
                    "new": r.get("新价格", ""),
                    "ratio": r.get("变化幅度", ""),
                    "found_at": r.get("发现时间", ""),
                }
                for r in rows[:10]
            ],
        }

    def sync_currency_switch(self, goods_id: str, old_price: float, new_price: float) -> dict[str, Any]:
        """货币口径切换时重新核算变价幅度。"""
        if old_price <= 0:
            return {"ok": False, "message": "旧价格无效"}
        ratio = (new_price - old_price) / old_price
        affected = 0
        for row in store.rows(self.MODULE):
            if row.get("商品标识") == goods_id and row.get("status") == "已确认":
                row["变化幅度"] = f"{ratio:.2%}"
                row["发现时间"] = datetime.now(timezone.utc).isoformat()
                affected += 1
        return {
            "ok": True,
            "message": "口径切换已同步变价幅度",
            "affected": affected,
            "ratio": f"{ratio:.2%}",
        }

    # ---- 业务扩展：消费货币换算器的换算变更 ----

    def consume_currency_changes(self, pair: str | None = None) -> dict[str, Any]:
        """消费各币对的换算变更，把快照关联到相关价格事件并重算实际换算金额。

        换算变更记录由货币换算器持有，这里只做流水侧的关联与复核范围登记；
        每条变更只重算一次，重复消费拿到同一个重算凭据。
        """
        from app.services import service_for

        currency = service_for("currency_conv")
        if currency is None:
            return {"ok": False, "message": "货币换算器服务不可用"}
        changes = currency.conversion_changes(pair).get("items", [])
        if not changes:
            return {"ok": False, "message": "没有可消费的换算变更记录", "events": []}
        linked: list[dict[str, Any]] = []
        scopes: set[str] = set()
        for change in changes:
            consumption = currency._recompute_pair_events(change)
            scopes |= set(consumption["review_scope"])
            linked.extend(consumption["events"])
        return {
            "ok": True,
            "message": "换算变更已消费，变价流水完成重算",
            "pair": pair or "全部币对",
            "changes": len(changes),
            "events": linked,
            "event_count": len(linked),
            "actual_total": round(
                sum(float(event.get("换算后金额") or 0) for event in linked), 2),
            "review_scope": sorted(scopes),
        }

    def currency_price_events(self, pair: str | None = None) -> dict[str, Any]:
        """按币对列出已关联换算快照的价格事件，旧价与前后依据都可查。"""
        rows = [
            row for row in store.rows(self.MODULE)
            if row.get("币对") and (pair is None or row.get("币对") == pair)
        ]
        rows.sort(key=lambda row: str(row.get("发现时间", "")), reverse=True)
        events = [
            {
                "id": row.get("id"),
                "商品标识": row.get("商品标识", ""),
                "币对": row.get("币对", ""),
                "旧价格": row.get("旧价格"),
                "新价格": row.get("新价格"),
                "变化幅度": row.get("变化幅度", ""),
                "原币金额": row.get("原币金额"),
                "换算前金额": row.get("换算前金额"),
                "换算后金额": row.get("换算后金额"),
                "换算前汇率": row.get("换算前汇率"),
                "换算后汇率": row.get("换算后汇率"),
                "汇率版本": row.get("汇率版本", ""),
                "换算快照": row.get("换算快照"),
                "换算快照日期": row.get("换算快照日期", ""),
                "换算变更记录": row.get("换算变更记录"),
                "换算申请": row.get("换算申请", ""),
                "换算依据": row.get("换算依据", {}),
                "复核范围": row.get("复核范围", ""),
                "旧价仍可查": True,
                "status": row.get("status", ""),
            }
            for row in rows
        ]
        return {
            "ok": True,
            "message": "价格事件读取完成",
            "pair": pair or "全部币对",
            "count": len(events),
            "events": events,
            "events_by_pair": {
                p: [event for event in events if event["币对"] == p]
                for p in sorted({str(row.get("币对")) for row in rows})
            },
        }

    def currency_review_queue(self, application_id: int | None = None) -> dict[str, Any]:
        """输出换算变更后的复核范围，供人工复核队列承接。"""
        from app.services import service_for

        currency = service_for("currency_conv")
        if currency is None:
            return {"ok": False, "message": "货币换算器服务不可用"}
        applications = currency.list_applications().get("items", [])
        if application_id is not None:
            applications = [a for a in applications if int(a.get("id", 0)) == int(application_id)]
        pending_pairs: list[dict[str, Any]] = []
        scopes: list[str] = []
        for app in applications:
            scopes.append(f"{app.get('申请编号')} 复核")
            pending_pairs.append({
                "申请编号": app.get("申请编号"),
                "待补材料": app.get("pending_pairs", []),
                "已确认": app.get("confirmed_pairs", []),
            })
        return {
            "ok": True,
            "message": "复核范围已登记",
            "applications": len(applications),
            "review_scope": scopes,
            "pairs": pending_pairs,
        }

    def pending_for_alert(self, goods_id: str) -> dict[str, Any]:
        """把待核实记录交给告警规则消费。"""
        rows = [
            r for r in store.rows(self.MODULE)
            if r.get("商品标识") == goods_id and r.get("status") == "待核实"
        ]
        if not rows:
            return {"ok": False, "message": "没有待核实的变价记录", "count": 0}
        return {
            "ok": True,
            "message": "待核实记录已移交告警规则",
            "count": len(rows),
            "goods": goods_id,
        }

    def history_stats(self, days: int = 7) -> dict[str, Any]:
        """输出变价历史统计快照。"""
        rows = store.rows(self.MODULE)
        confirmed = sum(1 for r in rows if r.get("status") == "已确认")
        false_pos = sum(1 for r in rows if r.get("status") == "误报")
        return {
            "ok": True,
            "message": "变价统计完成",
            "total": len(rows),
            "confirmed": confirmed,
            "false_positive": false_pos,
            "pending": len(rows) - confirmed - false_pos,
        }
