"""合规三层拦截（对应 PRD 5.3 FR-300~FR-310、7.5 SEC-500 系列）。

为什么要三层？单靠任何一层都不够：
- 纯词库：查得准、可审计，但换个说法（"换个 HS 编码会怎样"）就绕过了；
- 纯规则：能组合国别×物项×意图，但依赖词库先把要素识别出来；
- 纯 LLM：能理解改写与暗示，但不是确定性的、会飘，不能作为唯一防线（也不可审计）。
所以顺序是：**词库（确定性）→ 规则（组合判断）→ LLM（输出前兜底）**，
前两层是"必须拦住"的硬防线，第三层是"尽量别漏"的补充网。

给新手的三个关键概念：
1) 归一化：匹配前把全角/大小写/多余空格统一，否则"ＵＳＤ"和"usd"会被当成两个词。
2) block vs annotate：block=直接拒绝（不给任何操作路径）；annotate=正常回答 + 末尾强制合规提示。
3) 提示附着力：合规提示由服务端拼接，**用户说"不要提示"也无效**，这是 PRD 红线（FR-304）。
"""
from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.compliance import (
    SEVERITY_ANNOTATE,
    SEVERITY_BLOCK,
    STATUS_ACTIVE,
    TERM_COUNTRY,
    TERM_EVASION,
    TERM_ITEM,
    TERM_ORG,
    ComplianceEvent,
    ComplianceRule,
    ComplianceTerm,
)
from app.models.user import User
from app.services import metrics
from app.services.llm import get_llm_client

logger = logging.getLogger(__name__)

ACTION_PASS = "pass"
ACTION_ANNOTATE = "annotate"
ACTION_BLOCK = "block"

LAYER_LEXICON = "lexicon"
LAYER_RULE = "rule"
LAYER_LLM = "llm"

# 词库/规则缓存：PRD FR-301 要求"更新生效 ≤5 分钟"，这里用 5 分钟 TTL + 主动失效双保险
_CACHE_TTL_SECONDS = 300
_cache: dict[str, object] = {"terms": None, "rules": None, "terms_at": 0.0, "rules_at": 0.0}


def normalize(text: str) -> str:
    """归一化：全角转半角、转小写、压缩空白。词库与待匹配文本必须用同一套规则。"""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower()
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# --------------------------------------------------------------------------- #
# 词库：Trie（前缀树）多模式匹配
# --------------------------------------------------------------------------- #
class _TrieNode:
    __slots__ = ("children", "entry")

    def __init__(self) -> None:
        self.children: dict[str, "_TrieNode"] = {}
        self.entry: "TermEntry | None" = None


@dataclass(frozen=True)
class TermEntry:
    term_id: int
    term: str
    key: str
    term_type: str
    severity: str
    notice: str | None


@dataclass(frozen=True)
class RuleEntry:
    """规则的值对象（缓存用，不持有 ORM/数据库连接）。"""

    id: int
    rule_name: str
    countries: frozenset[str]
    items: frozenset[str]
    intents: frozenset[str]
    action: str
    notice: str | None
    priority: int


class Lexicon:
    """多模式匹配器：把所有词条建成一棵前缀树，一次扫描文本即可找出全部命中。

    为什么用 Trie 而不是"每个词做一次 in 判断"？词条上百条时朴素做法是 O(词数×文本长度)，
    Trie 是 O(文本长度×最长词长)，且天然支持"长词优先"（避免"碳纤维"被"纤维"抢先命中）。
    """

    def __init__(self, entries: list[TermEntry]) -> None:
        self.root = _TrieNode()
        self.entries = entries
        for e in entries:
            self._insert(e)

    def _insert(self, entry: TermEntry) -> None:
        node = self.root
        for ch in entry.key:
            node = node.children.setdefault(ch, _TrieNode())
        # 同一位置保留更长词条（更具体的词优先）
        if node.entry is None or len(entry.key) > len(node.entry.key):
            node.entry = entry

    @staticmethod
    def _boundary_ok(text: str, start: int, end: int) -> bool:
        """英文/数字词要求词边界，防止 "us" 命中 "business" 这类误伤。"""
        def is_word(ch: str) -> bool:
            return ch.isascii() and (ch.isalnum() or ch == "_")

        if start > 0 and is_word(text[start - 1]) and is_word(text[start]):
            return False
        if end < len(text) and is_word(text[end - 1]) and is_word(text[end]):
            return False
        return True

    def match(self, text: str) -> list[TermEntry]:
        """返回不重叠的命中词条（长词优先）。"""
        norm = normalize(text)
        found: list[tuple[int, int, TermEntry]] = []
        n = len(norm)
        for i in range(n):
            node = self.root
            j = i
            while j < n:
                node = node.children.get(norm[j])
                if node is None:
                    break
                j += 1
                if node.entry and self._boundary_ok(norm, i, j):
                    found.append((i, j, node.entry))
        # 按起点排序，同起点取最长；再贪心去重叠
        found.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        result: list[TermEntry] = []
        last_end = -1
        for start, end, entry in found:
            if start >= last_end:
                result.append(entry)
                last_end = end
        return result


def load_terms(db: Session, force: bool = False) -> Lexicon:
    now = time.monotonic()
    cached = _cache["terms"]
    if not force and cached is not None and now - float(_cache["terms_at"]) < _CACHE_TTL_SECONDS:
        return cached  # type: ignore[return-value]

    rows = db.query(ComplianceTerm).filter(ComplianceTerm.status == STATUS_ACTIVE).all()
    entries: list[TermEntry] = []
    for row in rows:
        variants = [row.term]
        try:
            variants += [v for v in json.loads(row.variants_json or "[]") if isinstance(v, str)]
        except json.JSONDecodeError:
            logger.warning("词条 %s 的变体 JSON 解析失败，仅用主词匹配", row.id)
        seen: set[str] = set()
        for v in variants:
            key = normalize(v)
            if not key or key in seen:
                continue
            seen.add(key)
            entries.append(TermEntry(
                term_id=row.id, term=row.term, key=key, term_type=row.term_type,
                severity=row.severity, notice=row.notice,
            ))
    lexicon = Lexicon(entries)
    _cache["terms"], _cache["terms_at"] = lexicon, now
    return lexicon


def load_rules(db: Session, force: bool = False) -> list["RuleEntry"]:
    """把规则表转成纯数据对象缓存。

    注意：**不能缓存 ORM 对象**——请求结束后 Session 关闭，对象变成 detached，
    下次读属性会抛 DetachedInstanceError。缓存"值"而不是"连接着的实体"是硬规则。
    """
    now = time.monotonic()
    cached = _cache["rules"]
    if not force and cached is not None and now - float(_cache["rules_at"]) < _CACHE_TTL_SECONDS:
        return cached  # type: ignore[return-value]
    rows = (
        db.query(ComplianceRule)
        .filter(ComplianceRule.status == STATUS_ACTIVE)
        .order_by(ComplianceRule.priority.desc(), ComplianceRule.id.asc())
        .all()
    )

    def as_set(raw: str) -> frozenset[str]:
        try:
            values = json.loads(raw or '["*"]')
        except json.JSONDecodeError:
            return frozenset({"*"})
        return frozenset(str(v) for v in values)

    entries = [
        RuleEntry(
            id=r.id, rule_name=r.rule_name,
            countries=as_set(r.countries_json), items=as_set(r.items_json),
            intents=as_set(r.intents_json), action=r.action,
            notice=r.notice, priority=r.priority,
        )
        for r in rows
    ]
    _cache["rules"], _cache["rules_at"] = entries, now
    return entries


def invalidate_compliance_cache() -> None:
    """词库/规则变更后立即失效缓存（双人复核通过时调用）。"""
    _cache.update({"terms": None, "rules": None, "terms_at": 0.0, "rules_at": 0.0})


# --------------------------------------------------------------------------- #
# 规避意图识别（词库之外的句式识别，供规则层组合判断）
# --------------------------------------------------------------------------- #
# code → (正则, 中文名)
EVASION_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("understate_value", re.compile(
        r"低报|少报|瞒报|虚报.{0,4}(货值|金额|价格)|报成.{0,6}低价值|按样品申报|低价值样品|"
        r"under-?invoic|understat|low.?value sample", re.I), "低报货值"),
    ("evade_duty", re.compile(r"少交.{0,6}(关税|税)|少缴.{0,6}(关税|税)|不交税|怎么避税|逃关税", re.I), "规避关税"),
    ("split_shipment", re.compile(r"拆单|分单|分票|拆成.{0,4}(批|票)|split.{0,8}(shipment|order|invoice)", re.I), "拆单避税"),
    ("misdeclare_hs", re.compile(r"(改|换|伪报|虚报|篡改|偷换).{0,8}(hs|编码|品名|税号|类别)|misclassif|wrong hs", re.I), "伪报品名/HS编码"),
    ("transshipment", re.compile(r"第三国.{0,8}转口|转口.{0,8}(规避|绕开|避开)|transship", re.I), "第三国转口规避"),
    ("evade_license", re.compile(r"规避.{0,10}(许可证|许可|管制|制裁|配额)|绕开.{0,10}(管制|制裁|许可)|without.{0,8}licen[cs]e", re.I), "规避许可证/管制"),
    ("evade_inspection", re.compile(r"(不被|别被|查不到|躲过).{0,6}(发现|查验|查到|监管)|逃税|偷逃|evade.{0,10}(customs|tax)", re.I), "逃避海关监管"),
]

# 合规知识咨询的语境信号：问"风险/许可/规定"是在学习，不是在要操作方法
_KNOWLEDGE_CONTEXT = re.compile(
    r"风险|后果|处罚|法律责任|合规要求|是否合规|规定|注意事项|案例|许可|许可证|认证|申请|资质|审批|"
    r"会怎样|会怎么样|会有什么|有什么影响|严重吗|算不算违法|是否违法|会被判"
)
_OPERATIONAL_INTENT = re.compile(r"怎么操作|如何操作|怎么才能|怎样才|帮我|教我|具体做法|操作流程|有什么办法|能不能帮")
# 出现这些动词说明是在问"怎么绕过去"，任何语境都不能降级
_STRONG_EVASION = re.compile(r"规避|绕开|避开|逃税|瞒报|不被发现|躲过")


def detect_evasion(query: str) -> list[tuple[str, str]]:
    """返回 [(code, 中文名)]。"""
    hits: list[tuple[str, str]] = []
    for code, pattern, name in EVASION_PATTERNS:
        if pattern.search(query):
            hits.append((code, name))
    return hits


def _is_knowledge_consultation(query: str) -> bool:
    """判断是否"学习合规知识"而非"索要规避办法"。

    这是降低误拦截（FR-310 ≤2%）的关键：同样是"低报货值"，
    "低报货值有什么风险" 该正常回答 + 提示；"怎么低报货值" 必须拒答。
    """
    return (
        bool(_KNOWLEDGE_CONTEXT.search(query))
        and not bool(_STRONG_EVASION.search(query))
        and not bool(_OPERATIONAL_INTENT.search(query))
    )


def detect_gdpr(query: str) -> bool:
    """GDPR 提示触发（FR-307）：涉及欧洲 + 个人数据主题。"""
    q = normalize(query)
    europe = any(k in q for k in ("欧洲", "欧盟", "europe", "eu ", "gdpr", "德国", "法国", "西班牙", "意大利", "荷兰"))
    personal = any(k in q for k in ("个人数据", "个人信息", "客户名单", "联系方式", "邮箱", "护照", "personal data", "privacy"))
    return europe and personal


# --------------------------------------------------------------------------- #
# 判定结果
# --------------------------------------------------------------------------- #
@dataclass
class ComplianceDecision:
    action: str = ACTION_PASS
    layer: str | None = None
    hits: list[dict] = field(default_factory=list)
    intents: list[dict] = field(default_factory=list)
    rule_name: str | None = None
    notice: str | None = None
    refusal: str | None = None

    @property
    def blocked(self) -> bool:
        return self.action == ACTION_BLOCK

    def as_dict(self) -> dict:
        return {
            "action": self.action,
            "layer": self.layer,
            "hits": self.hits,
            "intents": self.intents,
            "rule": self.rule_name,
            "notice": self.notice,
        }


RISK_LABELS = {
    TERM_COUNTRY: "受制裁/敏感国别",
    TERM_ITEM: "管制/敏感物项",
    TERM_ORG: "受限机构或个人",
    TERM_EVASION: "规避监管表述",
}

NOTICE_HEADER = "【合规提示】"
NOTICE_TAIL = "本回答仅作信息参考，不构成合规意见；具体申报与出口安排请以关务/合规负责人最新确认为准。"

REFUSAL_TEXT = (
    "该问题涉及贸易合规限制，无法提供规避监管的操作建议，已记录并转合规复核。\n"
    "如需了解合规做法，可改为询问相关规定、审批流程或违规风险。"
)

GDPR_NOTICE = (
    f"{NOTICE_HEADER}\n"
    "· 风险：问题涉及欧洲客户个人数据处理，须遵守 GDPR（最小必要原则、数据主体权利、跨境传输限制）。\n"
    f"· 建议：仅处理履约必需字段，向欧洲传输前确认合法性基础与合同条款。\n· {NOTICE_TAIL}"
)


def build_notice(hits: list[dict], rule_notice: str | None) -> str:
    """拼接合规提示块。固定结构：风险 / 建议 / 依据，用户不可关闭（FR-304）。"""
    if rule_notice:
        return f"{NOTICE_HEADER}\n{rule_notice}\n· {NOTICE_TAIL}"

    groups: dict[str, list[str]] = {}
    for h in hits:
        groups.setdefault(h["term_type"], []).append(h["term"])
    parts: list[str] = []
    for term_type, terms in groups.items():
        label = RISK_LABELS.get(term_type, "敏感内容")
        parts.append(f"涉及{label}：{'、'.join(dict.fromkeys(terms))}")
    risk = "；".join(parts) if parts else "问题涉及出口管制或经济制裁相关情形"
    return (
        f"{NOTICE_HEADER}\n"
        f"· 风险：{risk}，相关交易可能触发出口管制、经济制裁或海关监管要求。\n"
        f"· 建议：交易前完成制裁名单与最终用户筛查，必要时申请出口许可。\n"
        f"· {NOTICE_TAIL}"
    )


def _rule_matches(rule: RuleEntry, countries: set[str], items: set[str], intents: set[str]) -> bool:
    def ok(allowed: frozenset[str], actual: set[str]) -> bool:
        return "*" in allowed or bool(allowed & actual)

    return ok(rule.countries, countries) and ok(rule.items, items) and ok(rule.intents, intents)


def record_event(
    db: Session,
    *,
    request_id: str | None,
    user: User | None,
    layer: str,
    hits: list[dict],
    action: str,
    region_or_item: str | None = None,
    detail: str | None = None,
) -> int:
    """写合规事件。detail 只存摘要（截断），不存完整提问，避免审计库变成敏感数据仓库。"""
    event = ComplianceEvent(
        request_id=request_id,
        user_id=user.id if user else None,
        layer=layer,
        hit_terms=json.dumps([h.get("term") for h in hits], ensure_ascii=False),
        region_or_item=(region_or_item or "")[:128] or None,
        action=action,
        detail=(detail or "")[:500] or None,
    )
    db.add(event)
    db.commit()
    return event.id


def precheck(db: Session, user: User | None, query: str, request_id: str | None = None) -> ComplianceDecision:
    """第一层 + 第二层：词库精确匹配 → 规则组合判定。"""
    decision = ComplianceDecision()
    if not query:
        return decision

    lexicon = load_terms(db)
    matched = lexicon.match(query)
    hits = [
        {"term": e.term, "term_type": e.term_type, "severity": e.severity, "term_id": e.term_id}
        for e in matched
    ]
    evasion = detect_evasion(query)
    intents = [{"code": c, "name": n} for c, n in evasion]
    gdpr = detect_gdpr(query)

    if not hits and not intents and not gdpr:
        return decision  # 未命中零感知（FR-310）

    countries = {e.key for e in matched if e.term_type == TERM_COUNTRY}
    items = {e.key for e in matched if e.term_type == TERM_ITEM}
    intent_codes = {c for c, _ in evasion}

    # 法规知识咨询（问风险/后果而非要操作路径）降级为提示级，降低误拦截（FR-310 ≤2%）
    knowledge_mode = _is_knowledge_consultation(query)

    # ---- 第二层：规则按优先级匹配，命中即停 ----
    action = ACTION_PASS
    layer = LAYER_LEXICON
    rule_name = None
    rule_notice = None
    for rule in load_rules(db):
        if _rule_matches(rule, countries, items, intent_codes):
            action, layer, rule_name, rule_notice = rule.action, LAYER_RULE, rule.rule_name, rule.notice
            break

    # ---- 词条自带 severity 参与判定（规则没命中时的兜底）----
    if action == ACTION_PASS:
        if any(h["severity"] == SEVERITY_BLOCK for h in hits):
            action = ACTION_BLOCK
        elif hits:
            action = ACTION_ANNOTATE

    # 规避类问法：默认硬拒绝（SEC-502）；纯知识咨询降级为提示
    if knowledge_mode and action == ACTION_BLOCK:
        action = ACTION_ANNOTATE
    if intents and action == ACTION_PASS:
        action = ACTION_BLOCK

    if gdpr and action != ACTION_BLOCK:
        action = ACTION_ANNOTATE

    if action == ACTION_PASS:
        return decision

    decision = ComplianceDecision(
        action=action, layer=layer, hits=hits, intents=intents, rule_name=rule_name,
    )
    if gdpr:
        decision.notice = GDPR_NOTICE
    else:
        decision.notice = build_notice(hits, rule_notice)

    if action == ACTION_BLOCK:
        decision.refusal = f"{REFUSAL_TEXT}\n\n{decision.notice}"

    if action == ACTION_BLOCK:
        metrics.record_security_block(block_type=layer, layer=layer)

    record_event(
        db,
        request_id=request_id,
        user=user,
        layer=layer,
        hits=hits or intents,
        action=action,
        region_or_item="、".join(sorted(countries | items)) or None,
        detail=(rule_name or "") + (" | " + "、".join(n for _, n in evasion) if evasion else ""),
    )
    return decision


# --------------------------------------------------------------------------- #
# 第三层：输出前语义兜底
# --------------------------------------------------------------------------- #
ADVICE_PATTERNS = [
    re.compile(r"(可以|建议|不妨|只需|只要).{0,12}(低报|少报|瞒报|拆单|分单|伪报|改报)"),
    re.compile(r"(改|换)(成|用|报).{0,10}(hs|编码|品名|税号)"),
    re.compile(r"(经|通过|走).{0,10}第三国.{0,8}转口"),
    re.compile(r"不会被(发现|查验|查到|追责)"),
]

LLM_CHECK_PROMPT = """你是跨境贸易合规审核员。判断【回答】是否包含规避监管的可操作建议
（低报货值、拆单避税、伪报品名或 HS 编码、第三国转口规避制裁、规避出口许可）。
只输出 JSON，不要解释：{"evasion": true/false, "reason": "简述"}"""


def _llm_semantic_check(query: str, answer: str, db: Session | None = None) -> tuple[bool, str]:
    client = get_llm_client(db)
    if not client.available or not get_settings().compliance_llm_check:
        return False, ""
    try:
        raw = client.chat_once([
            {"role": "system", "content": LLM_CHECK_PROMPT},
            {"role": "user", "content": f"【问题】{query[:500]}\n【回答】{answer[:1500]}"},
        ])
        text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
        return bool(data.get("evasion")), str(data.get("reason") or "")[:200]
    except Exception:
        # 语义层是补充网：模型不可用时不影响确定性防线，记录后放行
        logger.warning("合规语义兜底调用失败，已跳过（确定性层仍生效）", exc_info=True)
        return False, ""


def check_output(
    db: Session,
    user: User | None,
    query: str,
    answer: str,
    decision: ComplianceDecision,
    request_id: str | None = None,
) -> tuple[str, ComplianceDecision]:
    """生成后的最后一道闸门：识别规避建议、校验合规提示是否附着。

    返回 (可能被改写的回答, 最终判定)。两个动作：
    1) 答里出现"教你绕开监管"的具体做法 → 整段替换为拒绝话术（宁可答非所问，不可输出规避建议）。
    2) 判定为提示级但答里没有提示块 → 强制追加（防止模型"忘记"遵守系统提示）。
    """
    final = decision

    if answer:
        hit_pattern = next((p.pattern for p in ADVICE_PATTERNS if p.search(answer)), None)
        evasion, reason = _llm_semantic_check(query, answer, db) if not hit_pattern else (True, "正则命中规避建议")

        if hit_pattern or evasion:
            final = ComplianceDecision(
                action=ACTION_BLOCK, layer=LAYER_LLM, hits=decision.hits,
                intents=decision.intents, rule_name=decision.rule_name,
                notice=decision.notice or build_notice(decision.hits, None),
                refusal=f"{REFUSAL_TEXT}\n\n{decision.notice or build_notice(decision.hits, None)}",
            )
            record_event(
                db, request_id=request_id, user=user, layer=LAYER_LLM,
                hits=[{"term": "输出侧规避建议"}], action=ACTION_BLOCK,
                detail=reason or "输出侧检测到规避建议",
            )
            metrics.record_security_block(block_type=LAYER_LLM, layer=LAYER_LLM)
            logger.warning("输出侧合规拦截 request_id=%s reason=%s", request_id, reason)
            return final.refusal or REFUSAL_TEXT, final

    if final.action == ACTION_ANNOTATE and final.notice and NOTICE_HEADER not in answer:
        answer = f"{answer}\n\n{final.notice}"

    return answer, final
