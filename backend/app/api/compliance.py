"""合规接口（PRD 第 9 章 API + FR-308 事件查询 + FR-309/604 双人复核）。

权限设计：
- 词库/规则维护：compliance:manage（合规管理员）
- 合规事件查询：audit:read（安全审计员、合规管理员）
- 预检演练：debug:retrieval（管理员/审计员，相当于"合规测试台"）
"""
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.compliance import (
    SEVERITY_BLOCK,
    STATUS_ACTIVE,
    STATUS_DRAFT,
    ComplianceEvent,
    ComplianceRule,
    ComplianceTerm,
)
from app.models.user import User
from app.schemas.compliance import (
    ComplianceCheckIn,
    ComplianceCheckOut,
    EventOut,
    RuleIn,
    RuleOut,
    TermIn,
    TermOut,
)
from app.services.compliance import invalidate_compliance_cache, normalize, precheck

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])


def _term_out(t: ComplianceTerm) -> TermOut:
    return TermOut(
        id=t.id, term=t.term, term_type=t.term_type,
        variants=json.loads(t.variants_json or "[]"),
        severity=t.severity, notice=t.notice, status=t.status,
        version=t.version, created_by=t.created_by, approved_by=t.approved_by,
    )


def _rule_out(r: ComplianceRule) -> RuleOut:
    return RuleOut(
        id=r.id, rule_name=r.rule_name,
        countries=json.loads(r.countries_json or '["*"]'),
        items=json.loads(r.items_json or '["*"]'),
        intents=json.loads(r.intents_json or '["*"]'),
        action=r.action, priority=r.priority, status=r.status,
        version=r.version, approved_by=r.approved_by,
    )


@router.post("/check", response_model=ComplianceCheckOut)
def check_query(
    payload: ComplianceCheckIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("debug:retrieval")),
) -> ComplianceCheckOut:
    """合规预检演练：只判定、不生成回答（合规评测集/测试台用）。"""
    decision = precheck(db, user, payload.query, request_id=uuid.uuid4().hex[:16])
    return ComplianceCheckOut(**decision.as_dict())


@router.get("/events", response_model=list[EventOut])
def list_events(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    action: str | None = None,
    layer: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
) -> list[EventOut]:
    """合规事件（摘要脱敏，仅审计员/合规负责人可见，FR-308/506）。"""
    query = db.query(ComplianceEvent)
    if action:
        query = query.filter(ComplianceEvent.action == action)
    if layer:
        query = query.filter(ComplianceEvent.layer == layer)
    rows = query.order_by(ComplianceEvent.id.desc()).offset(offset).limit(limit).all()
    return [
        EventOut(
            id=r.id, request_id=r.request_id, user_id=r.user_id, layer=r.layer,
            hit_terms=json.loads(r.hit_terms or "[]"),
            region_or_item=r.region_or_item, action=r.action, detail=r.detail,
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.get("/terms", response_model=list[TermOut])
def list_terms(
    status: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("compliance:manage")),
) -> list[TermOut]:
    query = db.query(ComplianceTerm)
    if status:
        query = query.filter(ComplianceTerm.status == status)
    return [_term_out(t) for t in query.order_by(ComplianceTerm.id.desc()).limit(500).all()]


@router.post("/terms", response_model=TermOut, status_code=201)
def create_term(
    payload: TermIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("compliance:manage")),
) -> TermOut:
    """新增词条：默认进入 draft，等另一位合规管理员复核后才生效（FR-604）。"""
    term = ComplianceTerm(
        term=payload.term.strip(),
        term_key=normalize(payload.term),
        term_type=payload.term_type,
        variants_json=json.dumps(payload.variants, ensure_ascii=False),
        severity=payload.severity,
        notice=payload.notice,
        source=payload.source,
        status=STATUS_DRAFT,
        created_by=user.username,
    )
    db.add(term)
    db.commit()
    db.refresh(term)
    return _term_out(term)


@router.post("/terms/{term_id}/approve", response_model=TermOut)
def approve_term(
    term_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("compliance:manage")),
) -> TermOut:
    """复核生效：提交人不能自己批自己（双人复核），否则 403。"""
    term = db.get(ComplianceTerm, term_id)
    if term is None:
        raise HTTPException(404, detail={"error_code": "E4004", "message": "词条不存在"})
    if term.created_by == user.username:
        raise HTTPException(403, detail={"error_code": "E6003", "message": "双人复核要求：不能复核自己提交的变更"})
    term.status = STATUS_ACTIVE
    term.approved_by = user.username
    term.version += 1
    db.commit()
    db.refresh(term)
    invalidate_compliance_cache()
    return _term_out(term)


@router.get("/rules", response_model=list[RuleOut])
def list_rules(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("compliance:manage")),
) -> list[RuleOut]:
    rows = db.query(ComplianceRule).order_by(ComplianceRule.priority.desc()).all()
    return [_rule_out(r) for r in rows]


@router.post("/rules", response_model=RuleOut, status_code=201)
def create_rule(
    payload: RuleIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("compliance:manage")),
) -> RuleOut:
    rule = ComplianceRule(
        rule_name=payload.rule_name,
        countries_json=json.dumps(payload.countries, ensure_ascii=False),
        items_json=json.dumps(payload.items, ensure_ascii=False),
        intents_json=json.dumps(payload.intents, ensure_ascii=False),
        action=payload.action,
        notice=payload.notice,
        priority=payload.priority,
        status=STATUS_DRAFT,
        created_by=user.username,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return _rule_out(rule)


@router.post("/rules/{rule_id}/approve", response_model=RuleOut)
def approve_rule(
    rule_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("compliance:manage")),
) -> RuleOut:
    rule = db.get(ComplianceRule, rule_id)
    if rule is None:
        raise HTTPException(404, detail={"error_code": "E4004", "message": "规则不存在"})
    if rule.created_by == user.username:
        raise HTTPException(403, detail={"error_code": "E6003", "message": "双人复核要求：不能复核自己提交的变更"})
    rule.status = STATUS_ACTIVE
    rule.approved_by = user.username
    rule.version += 1
    db.commit()
    db.refresh(rule)
    invalidate_compliance_cache()
    return _rule_out(rule)


@router.get("/summary")
def compliance_summary(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("audit:read")),
) -> dict:
    """合规观测小面板：拦截数/提示数/待复核数（对应 PRD 5.7 指标）。"""
    total = db.query(ComplianceEvent).count()
    blocks = db.query(ComplianceEvent).filter(ComplianceEvent.action == "block").count()
    annotates = db.query(ComplianceEvent).filter(ComplianceEvent.action == "annotate").count()
    pending_terms = db.query(ComplianceTerm).filter(ComplianceTerm.status == STATUS_DRAFT).count()
    block_terms = db.query(ComplianceTerm).filter(
        ComplianceTerm.status == STATUS_ACTIVE, ComplianceTerm.severity == SEVERITY_BLOCK
    ).count()
    return {
        "events_total": total, "blocks": blocks, "annotates": annotates,
        "pending_terms": pending_terms, "active_block_terms": block_terms,
        "retention_days": 180,
    }
