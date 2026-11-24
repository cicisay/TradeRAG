"""合规词库/规则的内置种子数据（演示用，真实上线由合规管理员维护）。

说明：这些条目是**演示用样例**，覆盖 PRD 验收用例涉及的类型（受制裁国别、管制物项、
受限名单、规避话术）。真实词库应来自官方发布源/律师订阅源，导入走双人复核（FR-309），
这里的 seed 只是让系统开箱可跑、可评测。
"""
import json
import logging

from sqlalchemy.orm import Session

from app.models.compliance import (
    SEVERITY_ANNOTATE,
    SEVERITY_BLOCK,
    STATUS_ACTIVE,
    TERM_COUNTRY,
    TERM_EVASION,
    TERM_ITEM,
    TERM_ORG,
    ComplianceRule,
    ComplianceTerm,
)
from app.services.compliance import normalize

logger = logging.getLogger(__name__)

# (主词, 类型, 变体, 级别, 提示语)
TERMS: list[tuple[str, str, list[str], str, str | None]] = [
    # ---- 受制裁/敏感国别 ----
    ("伊朗", TERM_COUNTRY, ["Iran", "Irán", "إيران"], SEVERITY_ANNOTATE,
     "涉及伊朗：受美国及联合国相关制裁措施约束，交易前须完成制裁筛查并确认是否需特殊许可。"),
    ("朝鲜", TERM_COUNTRY, ["North Korea", "DPRK", "Corea del Norte", "كوريا الشمالية"], SEVERITY_ANNOTATE,
     "涉及朝鲜：属全面制裁对象，绝大多数贸易与金融往来受限。"),
    ("叙利亚", TERM_COUNTRY, ["Syria", "Siria", "سوريا"], SEVERITY_ANNOTATE,
     "涉及叙利亚：受制裁措施约束，须逐笔评估许可要求。"),
    ("古巴", TERM_COUNTRY, ["Cuba", "كوبا"], SEVERITY_ANNOTATE,
     "涉及古巴：受制裁措施约束，须完成合规评估。"),
    ("克里米亚", TERM_COUNTRY, ["Crimea", "Crimea region"], SEVERITY_ANNOTATE,
     "涉及克里米亚地区：存在领土制裁限制，相关贸易须专项评估。"),
    ("俄罗斯", TERM_COUNTRY, ["Russia", "Rusia", "روسيا"], SEVERITY_ANNOTATE,
     "涉及俄罗斯：受多轮出口管制与制裁措施约束，须核查物项清单与最终用户。"),
    ("委内瑞拉", TERM_COUNTRY, ["Venezuela"], SEVERITY_ANNOTATE,
     "涉及委内瑞拉：受制裁措施约束，须完成合规评估。"),

    # ---- 管制/敏感物项 ----
    ("无人机", TERM_ITEM, ["无人机", "drone", "drones", "UAV", "dron", "طائرة بدون طيار"], SEVERITY_ANNOTATE,
     "涉及无人机：可能属于两用物项，出口须确认管制清单归类与许可要求。"),
    ("碳纤维", TERM_ITEM, ["carbon fiber", "carbon fibre", "fibra de carbono"], SEVERITY_ANNOTATE,
     "涉及碳纤维：高性能碳纤维属常见两用物项，注意规格阈值与最终用途。"),
    ("数控机床", TERM_ITEM, ["CNC machine", "machine tool", "máquina CNC"], SEVERITY_ANNOTATE,
     "涉及数控机床：高精度机床受出口管制，须核对精度指标与最终用户。"),
    ("夜视仪", TERM_ITEM, ["night vision", "visión nocturna"], SEVERITY_ANNOTATE,
     "涉及夜视设备：属军警/两用敏感物项，出口须取得许可。"),
    ("加密设备", TERM_ITEM, ["encryption device", "cryptographic equipment"], SEVERITY_ANNOTATE,
     "涉及加密设备：受密码类物项出口管制约束。"),
    ("光刻机", TERM_ITEM, ["lithography machine", "semiconductor equipment"], SEVERITY_ANNOTATE,
     "涉及半导体制造设备：属高技术管制物项，须核查许可与最终用户。"),
    ("导弹", TERM_ITEM, ["missile", "misil"], SEVERITY_BLOCK,
     "涉及导弹相关物项：属严格管制/军品范畴，不得提供任何贸易操作建议。"),
    ("浓缩铀", TERM_ITEM, ["enriched uranium", "uranium enrichment"], SEVERITY_BLOCK,
     "涉及核材料：属核两用品/核材料管制范畴，禁止提供操作建议。"),

    # ---- 受限机构/名单主题 ----
    ("实体清单", TERM_ORG, ["Entity List", "entity list"], SEVERITY_ANNOTATE,
     "涉及实体清单：交易前须核查交易对手是否在清单内，并评估适用限制。"),
    ("SDN名单", TERM_ORG, ["SDN List", "Specially Designated Nationals"], SEVERITY_ANNOTATE,
     "涉及 SDN 名单：须完成制裁名单筛查，命中即停止交易并上报合规。"),
    ("未经核实清单", TERM_ORG, ["Unverified List", "UVL"], SEVERITY_ANNOTATE,
     "涉及未经核实清单：须补充最终用户尽职调查。"),

    # ---- 规避类话术（命中即硬拒绝，除非是合规知识咨询）----
    ("低报货值", TERM_EVASION, ["under-invoice", "understate value", "低报价格"], SEVERITY_BLOCK,
     "低报货值属于走私/逃税行为，不提供任何操作方法。"),
    ("拆单避税", TERM_EVASION, ["split shipment", "拆单", "分单报关"], SEVERITY_BLOCK,
     "以拆单方式规避关税/监管属于违规行为，不提供操作方法。"),
    ("伪报品名", TERM_EVASION, ["misdeclare", "false declaration"], SEVERITY_BLOCK,
     "伪报品名或 HS 编码属于申报不实，可能构成走私，不提供操作方法。"),
    ("第三国转口", TERM_EVASION, ["transshipment", "transship"], SEVERITY_BLOCK,
     "以第三国转口规避制裁/反倾销属于违规行为，不提供操作方法。"),
]

# (规则名, 国别条件, 物项条件, 意图条件, 动作, 优先级, 提示语)
RULES: list[tuple[str, list[str], list[str], list[str], str, int, str | None]] = [
    ("规避监管意图-硬拒绝",
     ["*"], ["*"],
     ["understate_value", "evade_duty", "split_shipment", "misdeclare_hs", "transshipment", "evade_license", "evade_inspection"],
     SEVERITY_BLOCK, 300,
     "· 风险：问题包含规避海关监管或制裁的意图，相关行为可能构成走私、逃税或违反出口管制。\n"
     "· 建议：不接受此类操作咨询；如有真实业务需求，请通过关务与合规部门评估合法路径。"),
    ("制裁国别×管制物项-高危",
     ["伊朗", "朝鲜", "叙利亚", "古巴", "克里米亚", "俄罗斯", "委内瑞拉"],
     ["无人机", "碳纤维", "数控机床", "夜视仪", "加密设备", "光刻机", "导弹", "浓缩铀"],
     ["*"],
     SEVERITY_BLOCK, 200,
     "· 风险：该组合（受制裁/敏感国别 + 管制物项）属高危交易，可能同时触发出口管制与经济制裁限制。\n"
     "· 建议：停止推进交易，提交合规部门进行最终用户/最终用途审查与许可评估。"),
    ("制裁国别-强制提示",
     ["伊朗", "朝鲜", "叙利亚", "古巴", "克里米亚", "俄罗斯", "委内瑞拉"],
     ["*"], ["*"],
     SEVERITY_ANNOTATE, 150,
     "· 风险：问题涉及受制裁/敏感国别，相关交易可能触发经济制裁与出口管制限制。\n"
     "· 建议：交易前完成制裁名单与最终用户筛查，必要时申请许可；具体安排以合规负责人确认为准。"),
    ("管制物项-强制提示",
     ["*"],
     ["无人机", "碳纤维", "数控机床", "夜视仪", "加密设备", "光刻机", "导弹", "浓缩铀"],
     ["*"],
     SEVERITY_ANNOTATE, 120,
     "· 风险：问题涉及管制/两用物项，出口可能需许可证或最终用户声明。\n"
     "· 建议：核对出口管制清单归类与目的国许可要求后再安排出货。"),
]


def seed_compliance_data(db: Session) -> None:
    """写入内置词库与规则（幂等：已有数据则跳过）。"""
    if db.query(ComplianceTerm).count() == 0:
        for term, term_type, variants, severity, notice in TERMS:
            db.add(ComplianceTerm(
                term=term,
                term_key=normalize(term),
                term_type=term_type,
                variants_json=json.dumps(variants, ensure_ascii=False),
                severity=severity,
                notice=notice,
                source="内置演示词库",
                status=STATUS_ACTIVE,
                created_by="system",
                approved_by="system",
            ))
        logger.info("合规词库初始化：%d 条", len(TERMS))

    if db.query(ComplianceRule).count() == 0:
        for name, countries, items, intents, action, priority, notice in RULES:
            db.add(ComplianceRule(
                rule_name=name,
                countries_json=json.dumps(countries, ensure_ascii=False),
                items_json=json.dumps(items, ensure_ascii=False),
                intents_json=json.dumps(intents, ensure_ascii=False),
                action=action,
                notice=notice,
                priority=priority,
                status=STATUS_ACTIVE,
                created_by="system",
                approved_by="system",
            ))
        logger.info("合规规则初始化：%d 条", len(RULES))

    db.commit()
