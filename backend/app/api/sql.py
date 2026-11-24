"""SQL 通道接口（FR-204 / FR-703 / PRD 7.4 五铁律）。

接口设计要点：
- `/sql/query` 只接受 **template_id + params**，没有"自由 SQL"入口（铁律3）。
- `/sql/route` 是调试台用：展示自然语言如何映射到模板、渲染出的只读 SQL（不可编辑执行）。
- 出错不抛 500：统一返回 status=denied/error + degraded=true，让上层降级到文档检索
  （PRD E3xxx 话术："该数据查询暂不支持，已转为文档检索"）。
"""
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.deps import require_permission
from app.db.session import get_db
from app.models.user import User
from app.services.sql_channel import list_templates, route_question, run_template

router = APIRouter(prefix="/api/v1/sql", tags=["sql"])


class SqlQueryIn(BaseModel):
    template_id: str = Field(pattern=r"^T-[A-Z]{2,4}$")
    params: dict = Field(default_factory=dict)
    session_id: int | None = None


class SqlRouteIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)


@router.get("/templates")
def get_templates(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("chat:use")),
) -> list[dict]:
    """已注册模板清单（前端可用它渲染"可查询的数据范围"提示）。"""
    return list_templates(db)


@router.post("/query")
def sql_query(
    payload: SqlQueryIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("chat:use")),
) -> dict:
    """按注册模板查询。参数走严格校验，业务组由服务端注入。"""
    result = run_template(
        db, user, payload.template_id, payload.params,
        request_id=uuid.uuid4().hex[:16],
        session_id=payload.session_id,
        ip=request.client.host if request.client else None,
    )
    result.pop("rendered_sql", None)  # 正常查询不回显 SQL；调试台走 /sql/route
    return result


@router.post("/route")
def sql_route(
    payload: SqlRouteIn,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("debug:retrieval")),
) -> dict:
    """调试台：自然语言 → 模板 → 渲染 SQL → 只读执行，全链路可见（FR-703/704）。"""
    routed = route_question(payload.query)
    if routed is None:
        return {"routed": False, "message": "未匹配到数据查询模板，将走文档检索", "template_id": None}
    template_id, params = routed
    result = run_template(
        db, user, template_id, params,
        request_id=uuid.uuid4().hex[:16],
        ip=request.client.host if request.client else None,
    )
    return {"routed": True, "template_id": template_id, "params": params, **result}
