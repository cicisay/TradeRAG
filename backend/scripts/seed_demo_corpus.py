"""生成演示语料库（M5 评测与前端演示用）。

为什么需要它：
- 评测集里的每道题都要有"标准答案文档"在库里，否则 Recall 恒为 0，评测毫无意义。
- 前端演示也需要一批结构真实的文档（产品规格 / 单证 / 合规 / 多语种）。

用法：
    python scripts/seed_demo_corpus.py          # 幂等：已存在的同名文档跳过
    python scripts/seed_demo_corpus.py --reindex # 对已存在文档重跑切分+嵌入

注意：这是**演示语料**，不是真实业务数据；生产环境不要执行。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import BASE_DIR
from app.core.security import hash_password
from app.db.seed import seed_default_data
from app.db.seed_compliance import seed_compliance_data
from app.db.session import SessionLocal, init_db
from app.models.document import PARSE_DONE, STATUS_PARSED, DocVersion, Document, DocumentMeta
from app.models.knowledge_base import KnowledgeBase
from app.models.user import Role, User
from app.services.pipeline import run_indexing
from app.services.sql_channel import sync_templates

KB_PRODUCT = "产品规格库"
KB_DOCS = "单证合规库"
KB_GROUP_B = "B组海外市场库"

USERS = [
    ("demo_sales", "demo123456", "user", "default"),
    ("demo_kb", "demo123456", "knowledge", "default"),
    ("demo_sales_b", "demo123456", "user", "biz_b"),
]

DOCS: list[dict] = [
    # ---------------- 产品规格（中文） ----------------
    {
        "name": "法兰规格书_zh.txt", "kb": KB_PRODUCT, "langs": ["zh"],
        "doc_type": "spec", "authority": 1,
        "text": """产品规格书：不锈钢法兰
品名：板式平焊法兰（PL）
材质：304 / 316L 不锈钢
公称通径：DN15 ~ DN300（常用 DN50）
公称压力：PN10 / PN16 / PN25
执行标准：HG/T 20592-2009
工作温度：-20℃ ~ 200℃
密封面形式：RF 突面
适用介质：水、蒸汽、弱腐蚀性液体
表面处理：酸洗钝化
包装：木箱，每箱 50 片
备注：316L 材质需在订单中单独注明；DN50 PN16 常规库存 2000 片。""",
    },
    {
        "name": "球阀规格书_zh.txt", "kb": KB_PRODUCT, "langs": ["zh"],
        "doc_type": "spec", "authority": 1,
        "text": """产品规格书：不锈钢球阀
型号：Q41F-16P
材质：304 不锈钢阀体，PTFE 密封
公称通径：DN25
公称压力：PN16（1.6MPa）
强度试验压力：2.4MPa
密封试验压力：1.76MPa
适用介质：水、气体、油品
工作温度：-10℃ ~ 150℃
连接方式：法兰连接
特点：全通径、低流阻、开关迅速""",
    },
    {
        "name": "搅拌机规格书_zh.txt", "kb": KB_PRODUCT, "langs": ["zh"],
        "doc_type": "spec", "authority": 1,
        "text": """产品规格书：工业搅拌机
型号：JB-750
额定电压：380V
频率：50Hz
功率：7.5kW
转速：1450rpm
搅拌容量：500L
防护等级：IP55
绝缘等级：F
噪音：≤75dB(A)
材质：304 不锈钢罐体
适用：化工液体混合、食品搅拌""",
    },
    # ---------------- 多语种规格 ----------------
    {
        "name": "flange_spec_en.txt", "kb": KB_PRODUCT, "langs": ["en"],
        "doc_type": "spec", "authority": 1,
        "text": """Product Specification — Stainless Steel Flange
Type: Plate flange (PL)
Material grade: 304 / 316L stainless steel
Nominal size: DN15 to DN300 (DN50 standard)
Pressure rating: PN16
Standard: EN 1092-1
Working temperature: -20°C to 200°C
Sealing face: raised face (RF)
Application: water, steam, mild corrosive liquids
Packing: wooden case, 50 pieces per case
Note: 316L must be specified separately in the purchase order.""",
    },
    {
        "name": "blender_spec_es.txt", "kb": KB_PRODUCT, "langs": ["es"],
        "doc_type": "spec", "authority": 1,
        "text": """Especificación de producto — Mezcladora industrial
Modelo: JB-750
Voltaje nominal: 380 V
Frecuencia: 50 Hz
Potencia: 7,5 kW
Velocidad: 1450 rpm
Capacidad: 500 litros
Grado de protección: IP55
Nivel de ruido: ≤75 dB(A)
Material del tanque: acero inoxidable 304
Aplicación: mezcla de líquidos químicos y alimentos""",
    },
    {
        "name": "flange_spec_ar.txt", "kb": KB_PRODUCT, "langs": ["ar"],
        "doc_type": "spec", "authority": 1,
        "text": """مواصفات المنتج — فلنجة من الفولاذ المقاوم للصدأ
النوع: فلنجة مسطحة (PL)
المادة: فولاذ مقاوم للصدأ 304 / 316L
المقاس الاسمي: DN50
ضغط العمل: PN16
درجة حرارة العمل: من -20 إلى 200 درجة مئوية
المعيار: EN 1092-1
الاستخدام: الماء والبخار والسوائل قليلة التآكل
ملاحظة: يجب تحديد مادة 316L بشكل منفصل في أمر الشراء.""",
    },
    # ---------------- 单证 / 合规 ----------------
    {
        "name": "商业发票要素_zh.txt", "kb": KB_DOCS, "langs": ["zh"],
        "doc_type": "customs", "authority": 1,
        "text": """单证规范：商业发票（Commercial Invoice）必备要素
1. 发票号码与开票日期
2. 买卖双方名称、地址、联系方式
3. 合同号与信用证号（如适用）
4. 货物描述：品名、规格、HS 编码
5. 数量、单价、总价（须标明币种，如 USD / EUR）
6. 贸易术语（如 FOB Shanghai、CIF Hamburg）及对应金额构成
7. 原产国
8. 唛头与包装件数
9. 付款条件
10. 出口商签章
注意：发票金额必须与报关单、装箱单一致；CIF 条款需单列运费与保险费。""",
    },
    {
        "name": "信用证交单要求_zh.txt", "kb": KB_DOCS, "langs": ["zh"],
        "doc_type": "customs", "authority": 1,
        "text": """单证规范：信用证（L/C）交单要求
最迟装运期：以信用证 44C 字段为准
交单期：提单日后 21 天内，且不得晚于信用证有效期
常见单据：商业发票、装箱单、提单、原产地证、保险单（CIF 时）
提单要求：清洁已装船提单，收货人按信用证规定
不符点处理：开证行可在 5 个工作日内提出不符点
注意：交单期与装运期是两个不同日期，逾期交单可能被拒付。""",
    },
    {
        "name": "Incoterms_DAP_DDP_zh.txt", "kb": KB_DOCS, "langs": ["zh"],
        "doc_type": "policy", "authority": 1,
        "text": """贸易术语说明：DAP 与 DDP 的区别
DAP（Delivered at Place，目的地交货）：卖方负责运输到指定目的地，货物在运输工具上待卸；进口关税、进口清关由买方承担。
DDP（Delivered Duty Paid，完税后交货）：卖方承担进口清关与关税、税费，负责送到目的地。
风险转移：两者均在目的地交货时转移给买方。
适用建议：买方有进口资质、希望自行清关时用 DAP；卖方在目的国有清关能力时用 DDP。
注意：DDP 下卖方需了解目的国进口税率与合规要求。""",
    },
    {
        "name": "沙特SASO认证要求_zh.txt", "kb": KB_DOCS, "langs": ["zh"],
        "doc_type": "compliance", "authority": 0,
        "text": """目的国合规：沙特阿拉伯
认证制度：SASO 认证 + SABER 平台注册
适用产品：电子电器、灯具、玩具、建材、汽车配件等
流程：产品测试 → 取得符合性证书（CoC）→ SABER 平台注册 → 装运前验货
关键文件：测试报告、产品照片、说明书（阿拉伯语或双语）、发票与装箱单
注意：灯串等电器产品需符合 SASO 2870 能效与安全要求；证书以最新法规为准。""",
    },
    {
        "name": "出口管制合规须知_zh.txt", "kb": KB_DOCS, "langs": ["zh"],
        "doc_type": "compliance", "authority": 0,
        "text": """合规制度：出口管制与制裁筛查
适用范围：两用物项、军品、敏感技术
管制清单：参照中国两用物项出口管制清单、美国 EAR、欧盟双用途清单
筛查步骤：
1. 物项归类：确认 HS 编码与管制编码
2. 最终用户与最终用途审查
3. 制裁名单筛查（SDN、实体清单、UVL）
4. 许可申请与备案
红线：不得向受制裁国别出口管制物项；不得协助规避许可要求。
记录保存：筛查记录与许可文件至少保存 5 年。""",
    },
    {
        "name": "欧盟PII处理规范_zh.txt", "kb": KB_DOCS, "langs": ["zh"],
        "doc_type": "policy", "authority": 0,
        "text": """合规制度：欧盟个人数据保护（GDPR）
适用范围：欧盟客户、员工、供应商联系人数据
合法基础：合同履行、法定义务、正当利益或明示同意
最小必要：仅收集履约必需字段（姓名、公司、地址、联系方式）
数据主体权利：访问、更正、删除、限制处理、可携带
跨境传输：向欧盟以外传输需 SCC 标准合同条款或充分性认定
保存期限：交易完成后按税务要求保存，超期删除
泄露通知：72 小时内向监管机构报告""",
    },
    # ---------------- B 组（隔离验证） ----------------
    {
        "name": "B组价格政策_biz_b.txt", "kb": KB_GROUP_B, "langs": ["zh"],
        "doc_type": "policy", "authority": 1,
        "text": """内部资料：B 组客户价格政策
适用于 biz_b 业务组。
价格条款：FOB Shanghai，USD 计价
付款方式：30% 预付 + 70% 见提单副本
佣金：经销商佣金 3%，需在合同中明确
折扣：年度采购超过 100 万美元可申请 2% 返点
注意：本文件仅限 B 组内部使用，不得向其他业务组或客户披露。""",
    },
]


def ensure_kb(db, name: str, biz_group: str) -> KnowledgeBase:
    kb = db.query(KnowledgeBase).filter(KnowledgeBase.name == name).first()
    if kb:
        return kb
    kb = KnowledgeBase(name=name, biz_group=biz_group, product_line="general", created_by="seed")
    db.add(kb)
    db.flush()
    return kb


def ensure_users(db) -> None:
    for username, password, role_code, biz_group in USERS:
        if db.query(User).filter(User.username == username).first():
            continue
        role = db.query(Role).filter(Role.code == role_code).first()
        if role is None:
            raise RuntimeError(f"角色未初始化：{role_code}")
        db.add(User(
            username=username, password_hash=hash_password(password),
            role_id=role.id, biz_group=biz_group,
        ))
    db.commit()


def ensure_doc(db, spec: dict, kb: KnowledgeBase, reindex: bool) -> str:
    existing = (
        db.query(Document)
        .filter(Document.doc_name == spec["name"], Document.kb_id == kb.id)
        .first()
    )
    if existing:
        if reindex:
            run_indexing(existing.id, db)
            return "reindexed"
        return "skipped"

    doc = Document(kb_id=kb.id, doc_name=spec["name"], status=STATUS_PARSED, created_by="seed")
    db.add(doc)
    db.flush()
    rel = Path("files") / str(kb.id) / str(doc.id) / f"v1_{spec['name']}"
    abs_path = BASE_DIR / rel
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(spec["text"], encoding="utf-8")

    version = DocVersion(
        doc_id=doc.id, version_no=1, file_path=str(rel),
        file_size=len(spec["text"].encode("utf-8")), checksum="seed",
        parse_status=PARSE_DONE, raw_text=spec["text"],
        page_count=1,
    )
    db.add(version)
    db.flush()
    doc.current_version_id = version.id
    db.add(DocumentMeta(
        doc_id=doc.id, languages=spec["langs"], biz_group=kb.biz_group,
        doc_type=spec["doc_type"], authority_level=spec["authority"],
    ))
    db.commit()
    run_indexing(doc.id, db)
    return "created"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reindex", action="store_true", help="已存在的文档重跑切分与嵌入")
    args = parser.parse_args()

    init_db()
    seed_default_data()
    db = SessionLocal()
    try:
        seed_compliance_data(db)
        sync_templates(db)
        ensure_users(db)
        kb_product = ensure_kb(db, KB_PRODUCT, "default")
        kb_docs = ensure_kb(db, KB_DOCS, "default")
        kb_b = ensure_kb(db, KB_GROUP_B, "biz_b")
        kb_map = {KB_PRODUCT: kb_product, KB_DOCS: kb_docs, KB_GROUP_B: kb_b}

        stats = {"created": 0, "skipped": 0, "reindexed": 0}
        for spec in DOCS:
            outcome = ensure_doc(db, spec, kb_map[spec["kb"]], args.reindex)
            stats[outcome] += 1
            print(f"[{outcome:9}] {spec['name']} -> {spec['kb']}")
        print(f"\n完成：{stats}")
        print("演示账号（密码均为 demo123456）：demo_sales / demo_kb / demo_sales_b")
    finally:
        db.close()


if __name__ == "__main__":
    main()
