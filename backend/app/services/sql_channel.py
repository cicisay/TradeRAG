"""SQL 通道（对应 PRD 7.4 五条铁律 + FR-204 混合检索的 SQL 路）。

五铁律在代码里的落点（对照看更清楚）：
1) 只读且权限受限 → 用 `mode=ro` 打开业务库 + `PRAGMA query_only` + 只允许查 4 个视图；
2) 限流访问 → 用户/会话/IP/全局四级令牌桶，超限返回 E3002 并降级文档检索；
3) 禁止 LLM 生成 SQL → LLM 最多给 template_id + params（JSON Schema 严格校验），SQL 由服务端渲染；
4) 必须来自注册模板 → sql_template 表 + 代码内固定模板，未注册的 template_id 一律拒绝；
5) 敏感表拒绝访问 → 视图本身不含敏感列 + AST 白名单 + SQLite 授权回调三层。

执行链路：槽位抽取 → 模板路由 → 参数校验（Pydantic 严格模式）→ 服务端渲染
→ AST 守卫（单条 SELECT / 白名单视图 / 禁 UNION / 禁系统表）→ 限流 → 只读执行
→ 列级脱敏 → 审计落库。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import sqlglot
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session
from sqlglot import exp

from app.config import BASE_DIR, get_settings
from app.db.trade_seed import trade_db_path
from app.models.sql_channel import SqlExecAudit, SqlTemplate
from app.models.user import User

logger = logging.getLogger(__name__)

# 错误码（PRD 5.8.2 E3xxx：模板未匹配 / 限流 / 汇率源不可用，统一话术"已转为文档检索"）
E_TEMPLATE_NOT_MATCHED = "E3001"
E_RATE_LIMITED = "E3002"
E_SOURCE_UNAVAILABLE = "E3003"
E_PARAM_INVALID = "E3004"
E_GUARD_REJECTED = "E3005"

ALLOWED_VIEWS = {"v_fx_rate", "v_quotation", "v_incoterms_cost", "v_tariff_rate"}

# 视图展开时 SQLite 会去读基表（授权回调拿到的是基表名），因此基表也要放行；
# 直接查基表由 AST 守卫拦住（不在 ALLOWED_VIEWS 白名单）。这是"分层防御"不是漏洞。
VIEW_BASE_TABLES = {"fx_rate", "quotation", "incoterms_cost", "tariff_rate"}

SENSITIVE_COLUMNS = {"customer_name", "contact", "phone", "email", "address", "id_card", "passport"}

NOTICES = {
    "T-FX": "汇率按中国外汇交易中心中间价换算，仅供参考；实际结算以银行成交价为准。",
    "T-QUO": "报价已按业务组与有效期过滤，仅展示你所在业务组可见价格。",
    "T-INC": "费用为模板估算值，实际以订舱时承运人报价为准。",
    "T-TAR": "关税税率以目的国海关最新公布为准，本数据仅供参考。",
}


# --------------------------------------------------------------------------- #
# 限流：令牌桶（纯内存实现；生产多实例应换 Redis，接口保持一致）
# --------------------------------------------------------------------------- #
class TokenBucket:
    """容量 capacity，每秒补充 rate_per_sec 个令牌；取不到令牌即拒绝。"""

    def __init__(self, capacity: int, rate_per_sec: float) -> None:
        self.capacity = float(capacity)
        self.rate = float(rate_per_sec)
        self.tokens = float(capacity)
        self.updated = time.monotonic()

    def allow(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class RateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str, capacity: int, rate_per_min: float) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(capacity, rate_per_min / 60.0)
            self._buckets[key] = bucket
        return bucket

    def hit(self, user_id: int, session_id: int | None, ip: str | None, is_admin: bool = False) -> str | None:
        """返回命中的限制名（None = 放行）。管理员仍受全局 QPS 约束。"""
        s = get_settings()
        with self._lock:
            # 全局 50 QPS：容量 50、每秒补 50
            g = self._buckets.get("__global__")
            if g is None:
                g = TokenBucket(s.sql_global_qps, s.sql_global_qps)
                self._buckets["__global__"] = g
            if not g.allow():
                return "global_qps"

            if not is_admin:
                if not self._bucket(f"user:{user_id}", s.sql_user_rate_per_min, s.sql_user_rate_per_min).allow():
                    return "user"
                if session_id and not self._bucket(
                    f"session:{session_id}", s.sql_session_rate_per_min, s.sql_session_rate_per_min
                ).allow():
                    return "session"
                if ip and not self._bucket(f"ip:{ip}", s.sql_ip_rate_per_min, s.sql_ip_rate_per_min).allow():
                    return "ip"
        return None

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


LIMITER = RateLimiter()


# --------------------------------------------------------------------------- #
# 参数模型（Pydantic 严格模式：多一个字段、类型不对、格式不符都直接拒）
# --------------------------------------------------------------------------- #
class FxParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_currency: str = Field(pattern=r"^[A-Z]{3}$")
    quote_currency: str = Field(pattern=r"^[A-Z]{3}$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class QuoteParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product_code: str = Field(pattern=r"^[A-Za-z0-9_-]{2,32}$")
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class IncotermsParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    incoterm: Literal["EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DDP"]
    route_from: str = Field(min_length=2, max_length=32)
    route_to: str = Field(min_length=2, max_length=32)


class TariffParams(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    dest_country: str = Field(pattern=r"^[A-Z]{2}$")
    hs_code: str = Field(pattern=r"^\d{6,10}$")


# --------------------------------------------------------------------------- #
# 模板定义（代码内固定，数据库只登记元信息；改模板必须走版本 + 双人复核）
# --------------------------------------------------------------------------- #
TEMPLATES: dict[str, dict[str, Any]] = {
    "T-FX": {
        "name": "汇率查询",
        "description": "币种对 + 日期，查最近一个有效交易日的中间价",
        "view": "v_fx_rate",
        "model": FxParams,
        "columns": ["base_currency", "quote_currency", "rate", "rate_date", "source"],
        "visible_roles": ["*"],
        "params_schema": {
            "type": "object",
            "required": ["base_currency", "quote_currency", "date"],
            "properties": {
                "base_currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "quote_currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
                "date": {"type": "string", "format": "date"},
            },
            "additionalProperties": False,
        },
    },
    "T-QUO": {
        "name": "报价单查询",
        "description": "产品编码 + 日期，按用户业务组行级过滤",
        "view": "v_quotation",
        "model": QuoteParams,
        "columns": ["product_code", "product_name", "biz_group", "currency", "unit_price", "valid_from", "valid_to"],
        "visible_roles": ["*"],
        "params_schema": {
            "type": "object",
            "required": ["product_code", "date"],
            "properties": {
                "product_code": {"type": "string"},
                "date": {"type": "string", "format": "date"},
            },
            "additionalProperties": False,
        },
    },
    "T-INC": {
        "name": "Incoterms 费用拆分",
        "description": "术语 + 起运地 + 目的地",
        "view": "v_incoterms_cost",
        "model": IncotermsParams,
        "columns": ["incoterm", "route_from", "route_to", "freight", "insurance", "handling", "currency"],
        "visible_roles": ["*"],
        "params_schema": {
            "type": "object",
            "required": ["incoterm", "route_from", "route_to"],
            "properties": {
                "incoterm": {"type": "string"},
                "route_from": {"type": "string"},
                "route_to": {"type": "string"},
            },
            "additionalProperties": False,
        },
    },
    "T-TAR": {
        "name": "目的国关税查询",
        "description": "目的国 + HS 编码",
        "view": "v_tariff_rate",
        "model": TariffParams,
        "columns": ["dest_country", "hs_code", "tariff_rate", "note", "effective_date", "source"],
        "visible_roles": ["*"],
        "params_schema": {
            "type": "object",
            "required": ["dest_country", "hs_code"],
            "properties": {
                "dest_country": {"type": "string", "pattern": "^[A-Z]{2}$"},
                "hs_code": {"type": "string", "pattern": "^\\d{6,10}$"},
            },
            "additionalProperties": False,
        },
    },
}


def sync_templates(db: Session) -> None:
    """把代码内模板登记到 sql_template 表（幂等）。审批人留空表示内置模板。"""
    changed = False
    for tid, meta in TEMPLATES.items():
        row = db.query(SqlTemplate).filter(SqlTemplate.template_id == tid).first()
        if row is None:
            db.add(SqlTemplate(
                template_id=tid, name=meta["name"], description=meta["description"],
                view_name=meta["view"], allowed_columns_json=json.dumps(meta["columns"]),
                params_schema_json=json.dumps(meta["params_schema"], ensure_ascii=False),
                max_rows=get_settings().sql_max_rows,
                visible_roles_json=json.dumps(meta["visible_roles"]),
                created_by="system", approved_by="system",
            ))
            changed = True
    if changed:
        db.commit()


# --------------------------------------------------------------------------- #
# 服务端渲染（参数全部走占位符绑定，绝不字符串拼接用户输入）
# --------------------------------------------------------------------------- #
def render_sql(template_id: str, params: dict, biz_group: str) -> tuple[str, list[Any]]:
    limit = get_settings().sql_max_rows
    if template_id == "T-FX":
        sql = ("SELECT base_currency, quote_currency, rate, rate_date, source FROM v_fx_rate "
               "WHERE base_currency = ? AND quote_currency = ? AND rate_date <= ? "
               "ORDER BY rate_date DESC LIMIT 1")
        return sql, [params["base_currency"], params["quote_currency"], params["date"]]
    if template_id == "T-QUO":
        sql = ("SELECT product_code, product_name, biz_group, currency, unit_price, valid_from, valid_to "
               "FROM v_quotation WHERE product_code = ? AND biz_group = ? AND valid_from <= ? AND valid_to >= ? "
               "ORDER BY valid_from DESC LIMIT ?")
        # biz_group 来自登录用户，不接受前端传入 —— 防止"指定别组拿报价"
        return sql, [params["product_code"], biz_group, params["date"], params["date"], limit]
    if template_id == "T-INC":
        sql = ("SELECT incoterm, route_from, route_to, freight, insurance, handling, currency "
               "FROM v_incoterms_cost WHERE incoterm = ? AND route_from = ? AND route_to = ? "
               "ORDER BY valid_from DESC LIMIT ?")
        return sql, [params["incoterm"], params["route_from"], params["route_to"], limit]
    if template_id == "T-TAR":
        sql = ("SELECT dest_country, hs_code, tariff_rate, note, effective_date, source FROM v_tariff_rate "
               "WHERE dest_country = ? AND hs_code = ? ORDER BY effective_date DESC LIMIT 1")
        return sql, [params["dest_country"], params["hs_code"]]
    raise ValueError(f"未注册模板 {template_id}")


# --------------------------------------------------------------------------- #
# AST 守卫（sqlglot 把 SQL 解析成语法树，逐条规则检查）
# --------------------------------------------------------------------------- #
FORBIDDEN_FUNCTIONS = {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer"}


def guard_sql(sql: str, allowed_views: set[str] | None = None) -> tuple[bool, str]:
    """返回 (是否通过, 拒绝原因)。这是铁律 5 的"应用层 AST 校验"。"""
    allowed = allowed_views or ALLOWED_VIEWS
    try:
        statements = [s for s in sqlglot.parse(sql, read="sqlite") if s is not None]
    except Exception as exc:  # 解析失败本身就是可疑输入
        return False, f"SQL 解析失败：{exc}"
    if len(statements) != 1:
        return False, "只允许单条语句"
    stmt = statements[0]
    if isinstance(stmt, exp.SetOperation) or stmt.find(exp.Union):
        return False, "禁止 UNION / 集合运算"
    if not isinstance(stmt, exp.Select):
        return False, "只允许 SELECT"
    if stmt.find(exp.Into) or stmt.find(exp.Insert) or stmt.find(exp.Update) or stmt.find(exp.Delete):
        return False, "禁止写操作"
    for table in stmt.find_all(exp.Table):
        if table.db or table.catalog:
            return False, "禁止库名/模式限定（防跨库访问）"
        if table.name.lower() not in allowed:
            return False, f"表/视图 {table.name} 不在白名单"
    # sqlglot 对未知函数会解析成 Anonymous（sql_name 不是真实函数名），两种都要查；
    # 再叠一层正则兜底，防止解析器版本差异导致漏检。
    func_names: set[str] = set()
    for func in stmt.find_all(exp.Func):
        func_names.add((getattr(func, "name", "") or func.sql_name() or "").lower())
    for fn in re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", sql.lower()):
        func_names.add(fn)
    for fname in func_names:
        if fname in FORBIDDEN_FUNCTIONS or fname.startswith("sqlite_"):
            return False, f"禁止调用函数 {fname}"
    return True, ""


# --------------------------------------------------------------------------- #
# 只读执行
# --------------------------------------------------------------------------- #
def _authorizer(action: int, arg1: str | None, arg2: str | None, dbname: str | None, trigger: str | None) -> int:
    """SQLite 级授权回调：第 4 层防线（即使 AST 守卫被绕过也写不进、读不到别的表）。"""
    if action == sqlite3.SQLITE_SELECT:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ:
        table = (arg1 or "").lower()
        # sqlite_master 只用于展开视图定义；基表是视图展开的必经路径（直接访问由 AST 守卫拦住）
        if table in ALLOWED_VIEWS or table in VIEW_BASE_TABLES or table == "sqlite_master":
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        if (arg2 or "").lower() in {"count", "sum", "min", "max", "avg", "round", "coalesce", "ifnull", "lower", "upper", "length"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    # 其余动作（PRAGMA/ATTACH/写事务/DDL/事务控制）一律拒绝
    return sqlite3.SQLITE_DENY


def execute_readonly(sql: str, binds: list[Any]) -> tuple[list[str], list[tuple]]:
    """用只读连接执行，带 3 秒超时。"""
    settings = get_settings()
    path = trade_db_path()
    if not Path(path).exists():
        raise FileNotFoundError(str(path))
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + settings.sql_statement_timeout_ms / 1000.0
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 5000)
        cur = conn.execute(sql, binds)
        columns = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(settings.sql_max_rows + 1)
        conn.set_progress_handler(None, 0)
        return columns, rows
    finally:
        conn.close()


def mask_rows(columns: list[str], rows: list[tuple]) -> list[dict]:
    """列级脱敏：敏感列一律打码（正常模板不会选到，纯防御）。"""
    out: list[dict] = []
    for row in rows:
        item = {}
        for col, val in zip(columns, row):
            if col.lower() in SENSITIVE_COLUMNS or col.lower().endswith("_name") and col.lower() != "product_name":
                item[col] = "***"
            else:
                item[col] = val
        out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 自然语言 → 模板槽位（确定性规则；LLM 只应输出同样的 template_id + params）
# --------------------------------------------------------------------------- #
CURRENCY_NAMES = {
    "美元": "USD", "美金": "USD", "人民币": "CNY", "欧元": "EUR", "日元": "JPY",
    "英镑": "GBP", "港币": "HKD", "港元": "HKD", "沙特里亚尔": "SAR",
}
COUNTRY_NAMES = {
    "美国": "US", "德国": "DE", "沙特": "SA", "沙特阿拉伯": "SA", "阿联酋": "AE",
    "日本": "JP", "英国": "GB", "法国": "FR", "巴西": "BR", "荷兰": "NL",
}
CITY_NAMES = {
    "上海": "Shanghai", "宁波": "Ningbo", "深圳": "Shenzhen", "广州": "Guangzhou",
    "汉堡": "Hamburg", "鹿特丹": "Rotterdam", "吉达": "Jeddah", "杰贝阿里": "Jebel Ali",
}
INCOTERMS = ("EXW", "FCA", "FOB", "CFR", "CIF", "CPT", "CIP", "DAP", "DDP")


def _parse_date(query: str) -> str:
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", query)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return date.today().isoformat()


def route_question(query: str) -> tuple[str, dict] | None:
    """把问题映射到 (template_id, params)。槽位不全时返回 None → 走文档检索。

    为什么不用 LLM 抽槽位？可以，但**只能**输出这个结构，且必须有确定性兜底：
    模型抖动时不能把"查报价"变成"随便查点东西"。
    """
    q = query.strip()
    squashed = q.replace(" ", "")
    today = _parse_date(q)

    # ---- T-TAR：HS 编码 + 目的国 ----
    hs = re.search(r"\b(\d{6,10})\b", q)
    country = next((code for name, code in COUNTRY_NAMES.items() if name in q), None)
    if hs and country and ("关税" in q or "税率" in q or "tariff" in q.lower()):
        return "T-TAR", {"dest_country": country, "hs_code": hs.group(1)}

    # ---- T-FX：币种对 + 汇率/换算 ----
    if "汇率" in q or "换算" in q or "rate" in q.lower():
        codes = re.findall(r"\b([A-Z]{3})\b", q)
        codes = [c for c in codes if c in {"USD", "CNY", "EUR", "JPY", "GBP", "HKD", "SAR"}]
        codes += [code for name, code in CURRENCY_NAMES.items() if name in q]
        codes = list(dict.fromkeys(codes))
        if len(codes) >= 2:
            return "T-FX", {"base_currency": codes[0], "quote_currency": codes[1], "date": today}
        if len(codes) == 1:
            base = codes[0]
            quote = "CNY" if base != "CNY" else "USD"
            return "T-FX", {"base_currency": base, "quote_currency": quote, "date": today}

    # ---- T-INC：术语 + 起运地 + 目的地 ----
    upper = q.upper()
    term = next((t for t in INCOTERMS if re.search(rf"\b{t}\b", upper)), None)
    cities = [en for cn, en in CITY_NAMES.items() if cn in q]
    if term and len(cities) >= 2:
        return "T-INC", {"incoterm": term, "route_from": cities[0], "route_to": cities[1]}

    # ---- T-QUO：产品编码 + 报价/单价 ----
    sku = re.search(r"(SKU[-_ ]?\d{3,6}|[A-Z]{2,4}-\d{3,6})", q, re.I)
    if sku and ("报价" in q or "单价" in q or "价格" in q):
        code = re.sub(r"[\s_]", "-", sku.group(1).upper())
        return "T-QUO", {"product_code": code, "date": today}

    _ = squashed
    return None


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def _fmt_money(v: float) -> str:
    return f"{v:,.2f}"


def build_answer(template_id: str, rows: list[dict], params: dict) -> str:
    """把结构化结果转成"人话答案"。数字来自数据库，不让 LLM 复述（避免改数）。"""
    if not rows:
        return ""
    if template_id == "T-FX":
        r = rows[0]
        return (f"按 {r['rate_date']} {r['base_currency']}/{r['quote_currency']} 中间价 "
                f"1 {r['base_currency']} = {r['rate']:.4f} {r['quote_currency']}（来源：{r['source']}）。\n"
                f"如需换算金额，请直接给出金额与币种，我可按此汇率计算（仅供参考）。")
    if template_id == "T-TAR":
        r = rows[0]
        note = f"；备注：{r['note']}" if r.get("note") else ""
        return (f"{r['dest_country']} 目的国 HS {r['hs_code']} 的进口关税税率为 "
                f"{r['tariff_rate']}%（生效日期 {r['effective_date']}，来源：{r['source']}{note}）。")
    if template_id == "T-QUO":
        lines = ["按你所在业务组的有效报价："]
        for r in rows:
            lines.append(f"· {r['product_code']} {r['product_name']}：{_fmt_money(r['unit_price'])} "
                         f"{r['currency']}（有效期 {r['valid_from']} ~ {r['valid_to']}）")
        return "\n".join(lines)
    if template_id == "T-INC":
        lines = [f"{params.get('incoterm')} {params.get('route_from')} → {params.get('route_to')} 费用拆分："]
        for r in rows:
            total = (r["freight"] or 0) + (r["insurance"] or 0) + (r["handling"] or 0)
            lines.append(f"· {r['incoterm']}：海运费 {_fmt_money(r['freight'])} + 保险 {_fmt_money(r['insurance'])} "
                         f"+ 操作费 {_fmt_money(r['handling'])} = 合计 {_fmt_money(total)} {r['currency']}")
        return "\n".join(lines)
    return json.dumps(rows, ensure_ascii=False)


def _audit(db: Session, *, request_id, user, template_id, params, sql, rows, status, error_code, elapsed_ms) -> None:
    db.add(SqlExecAudit(
        request_id=request_id,
        user_id=user.id if user else None,
        template_id=template_id,
        params_json=json.dumps(params, ensure_ascii=False) if params else None,
        rendered_sql=sql,
        row_count=len(rows) if rows else 0,
        status=status,
        error_code=error_code,
        elapsed_ms=elapsed_ms,
    ))
    db.commit()


def run_template(
    db: Session,
    user: User,
    template_id: str,
    params: dict,
    *,
    request_id: str | None = None,
    session_id: int | None = None,
    ip: str | None = None,
) -> dict:
    """执行一个注册模板，返回统一结构（ok / denied / empty / error）。"""
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    limit_hit = LIMITER.hit(user.id, session_id, ip, is_admin=(user.role.code == "admin" if user.role else False))
    if limit_hit:
        _audit(db, request_id=request_id, user=user, template_id=template_id, params=params,
               sql=None, rows=[], status="denied", error_code=E_RATE_LIMITED, elapsed_ms=elapsed())
        return {"status": "denied", "error_code": E_RATE_LIMITED, "degraded": True,
                "message": "该数据查询暂不支持（已触发访问频率限制），已转为文档检索", "limit": limit_hit}

    meta = TEMPLATES.get(template_id)
    if meta is None:
        _audit(db, request_id=request_id, user=user, template_id=template_id, params=params,
               sql=None, rows=[], status="denied", error_code=E_TEMPLATE_NOT_MATCHED, elapsed_ms=elapsed())
        return {"status": "denied", "error_code": E_TEMPLATE_NOT_MATCHED, "degraded": True,
                "message": "该数据查询暂不支持，已转为文档检索"}

    role_code = user.role.code if user.role else ""
    if "*" not in meta["visible_roles"] and role_code not in meta["visible_roles"]:
        return {"status": "denied", "error_code": E_TEMPLATE_NOT_MATCHED, "degraded": True,
                "message": "该数据查询暂不支持，已转为文档检索"}

    try:
        validated = meta["model"](**params)
    except ValidationError as exc:
        _audit(db, request_id=request_id, user=user, template_id=template_id, params=params,
               sql=None, rows=[], status="denied", error_code=E_PARAM_INVALID, elapsed_ms=elapsed())
        return {"status": "denied", "error_code": E_PARAM_INVALID, "degraded": False,
                "message": "查询参数不合法", "detail": exc.errors(include_url=False)[:3]}

    clean_params = validated.model_dump()
    sql, binds = render_sql(template_id, clean_params, user.biz_group)

    ok, reason = guard_sql(sql)
    if not ok:
        _audit(db, request_id=request_id, user=user, template_id=template_id, params=clean_params,
               sql=sql, rows=[], status="denied", error_code=E_GUARD_REJECTED, elapsed_ms=elapsed())
        logger.error("SQL 守卫拒绝：%s | %s", reason, sql)
        return {"status": "denied", "error_code": E_GUARD_REJECTED, "degraded": True,
                "message": "该数据查询暂不支持，已转为文档检索"}

    try:
        columns, raw_rows = execute_readonly(sql, binds)
    except Exception:
        logger.exception("SQL 执行失败 template=%s", template_id)
        _audit(db, request_id=request_id, user=user, template_id=template_id, params=clean_params,
               sql=sql, rows=[], status="error", error_code=E_SOURCE_UNAVAILABLE, elapsed_ms=elapsed())
        return {"status": "error", "error_code": E_SOURCE_UNAVAILABLE, "degraded": True,
                "message": "数据源暂时不可用，已转为文档检索"}

    if len(raw_rows) > get_settings().sql_max_rows:
        raw_rows = raw_rows[: get_settings().sql_max_rows]
    rows = mask_rows(columns, raw_rows)

    # 汇率过期 > 7 天禁止硬算（PRD 7.4 行业特有校验）
    if template_id == "T-FX" and rows:
        try:
            rate_date = datetime.strptime(rows[0]["rate_date"], "%Y-%m-%d").date()
            asked = datetime.strptime(clean_params["date"], "%Y-%m-%d").date()
            if (asked - rate_date).days > 7:
                _audit(db, request_id=request_id, user=user, template_id=template_id, params=clean_params,
                       sql=sql, rows=rows, status="error", error_code=E_SOURCE_UNAVAILABLE, elapsed_ms=elapsed())
                return {"status": "error", "error_code": E_SOURCE_UNAVAILABLE, "degraded": True,
                        "message": f"最近汇率为 {rows[0]['rate_date']}，已超过 7 天有效期，按规则不做换算，"
                                   f"请查询最新汇率或改用文档检索", "rows": rows}
        except (ValueError, KeyError):
            logger.warning("汇率日期解析失败，跳过过期校验")

    answer = build_answer(template_id, rows, clean_params)
    _audit(db, request_id=request_id, user=user, template_id=template_id, params=clean_params,
           sql=sql, rows=rows, status="success", error_code=None, elapsed_ms=elapsed())

    return {
        "status": "ok" if rows else "empty",
        "template_id": template_id,
        "template_name": meta["name"],
        "params": clean_params,
        "columns": columns,
        "rows": rows,
        "row_count": len(rows),
        "rendered_sql": sql,  # 调试台可展示，但接口层只读、不可编辑执行（FR-703）
        "answer": answer,
        "notice": NOTICES.get(template_id),
        "degraded": False,
        "llm_sql_generated": False,  # 铁律3：永远由服务端渲染
        "elapsed_ms": elapsed(),
    }


def list_templates(db: Session) -> list[dict]:
    rows = db.query(SqlTemplate).filter(SqlTemplate.status == "active").all()
    return [
        {
            "template_id": r.template_id, "name": r.name, "description": r.description,
            "view_name": r.view_name,
            "allowed_columns": json.loads(r.allowed_columns_json or "[]"),
            "params_schema": json.loads(r.params_schema_json or "{}"),
            "max_rows": r.max_rows, "version": r.version,
        }
        for r in rows
    ]
