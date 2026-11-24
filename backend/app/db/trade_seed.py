"""业务库初始化（SQL 通道的"数据源"）。

给新手的说明：
- 这是**另一个数据库文件**（data/trade.db），和 RAG 主库（app.db）分开。
  生产上对应"只读账号 rag_ro 只能看 4 个视图"：汇率、报价、Incoterms 费用、关税。
- 基表（fx_rate/quotation/...）只在初始化时建，应用运行期**只读视图**，
  视图里刻意不放客户名称、联系人等敏感列 —— 从数据源层面就"查不到"（铁律5）。
- 演示数据是虚构的，仅用于跑通链路；真实接入时换成企业 ERP/财务库的同步任务即可。
"""
import logging
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from app.config import BASE_DIR, get_settings

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS fx_rate (
  id INTEGER PRIMARY KEY,
  base_currency TEXT NOT NULL,
  quote_currency TEXT NOT NULL,
  rate REAL NOT NULL,
  rate_date TEXT NOT NULL,
  source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quotation (
  id INTEGER PRIMARY KEY,
  product_code TEXT NOT NULL,
  product_name TEXT NOT NULL,
  biz_group TEXT NOT NULL,
  customer_tier TEXT,
  currency TEXT NOT NULL,
  unit_price REAL NOT NULL,
  valid_from TEXT NOT NULL,
  valid_to TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS incoterms_cost (
  id INTEGER PRIMARY KEY,
  incoterm TEXT NOT NULL,
  route_from TEXT NOT NULL,
  route_to TEXT NOT NULL,
  freight REAL NOT NULL,
  insurance REAL NOT NULL,
  handling REAL NOT NULL,
  currency TEXT NOT NULL,
  valid_from TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tariff_rate (
  id INTEGER PRIMARY KEY,
  dest_country TEXT NOT NULL,
  hs_code TEXT NOT NULL,
  tariff_rate REAL NOT NULL,
  note TEXT,
  effective_date TEXT NOT NULL,
  source TEXT NOT NULL
);
"""

# 视图只暴露非敏感列：客户名/联系人/合同金额一律不出现（铁律5）
VIEWS = """
DROP VIEW IF EXISTS v_fx_rate;
CREATE VIEW v_fx_rate AS
  SELECT base_currency, quote_currency, rate, rate_date, source FROM fx_rate;
DROP VIEW IF EXISTS v_quotation;
CREATE VIEW v_quotation AS
  SELECT product_code, product_name, biz_group, customer_tier, currency, unit_price, valid_from, valid_to
  FROM quotation;
DROP VIEW IF EXISTS v_incoterms_cost;
CREATE VIEW v_incoterms_cost AS
  SELECT incoterm, route_from, route_to, freight, insurance, handling, currency, valid_from
  FROM incoterms_cost;
DROP VIEW IF EXISTS v_tariff_rate;
CREATE VIEW v_tariff_rate AS
  SELECT dest_country, hs_code, tariff_rate, note, effective_date, source FROM tariff_rate;
"""


def trade_db_path() -> Path:
    p = Path(get_settings().trade_db_path)
    return p if p.is_absolute() else BASE_DIR / p


def init_trade_db(force: bool = False) -> None:
    """建表 + 视图 + 演示数据（幂等：已有数据则跳过）。"""
    path = trade_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.executescript(VIEWS)

        if force:
            for t in ("fx_rate", "quotation", "incoterms_cost", "tariff_rate"):
                conn.execute(f"DELETE FROM {t}")

        today = date.today()
        d = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731

        if not conn.execute("SELECT 1 FROM fx_rate LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO fx_rate(base_currency,quote_currency,rate,rate_date,source) VALUES(?,?,?,?,?)",
                [
                    ("USD", "CNY", 7.0850, d(2), "中国外汇交易中心中间价"),
                    ("USD", "CNY", 7.1020, d(3), "中国外汇交易中心中间价"),
                    ("EUR", "CNY", 8.2310, d(2), "中国外汇交易中心中间价"),
                    ("GBP", "CNY", 9.4120, d(2), "中国外汇交易中心中间价"),
                    ("USD", "EUR", 0.8605, d(2), "中国外汇交易中心中间价"),
                    ("SAR", "CNY", 1.8890, d(25), "中国外汇交易中心中间价"),  # 故意放旧，用于"过期禁算"校验
                ],
            )

        if not conn.execute("SELECT 1 FROM quotation LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO quotation(product_code,product_name,biz_group,customer_tier,currency,unit_price,valid_from,valid_to) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("SKU-1001", "不锈钢法兰 DN50 304", "default", "A", "USD", 12.80, "2026-01-01", "2026-12-31"),
                    ("SKU-1001", "不锈钢法兰 DN50 304", "biz_a", "A", "USD", 12.50, "2026-01-01", "2026-12-31"),
                    ("SKU-1001", "不锈钢法兰 DN50 304", "biz_b", "B", "USD", 13.60, "2026-01-01", "2026-12-31"),
                    ("SKU-2002", "碳钢法兰 DN80", "default", "B", "USD", 9.40, "2026-01-01", "2026-12-31"),
                    ("SKU-3003", "LED 灯串 5m", "default", "A", "USD", 3.20, "2026-03-01", "2026-12-31"),
                ],
            )

        if not conn.execute("SELECT 1 FROM incoterms_cost LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO incoterms_cost(incoterm,route_from,route_to,freight,insurance,handling,currency,valid_from) "
                "VALUES(?,?,?,?,?,?,?,?)",
                [
                    ("FOB", "Shanghai", "Hamburg", 1650.0, 0.0, 240.0, "USD", "2026-01-01"),
                    ("CIF", "Shanghai", "Hamburg", 1650.0, 132.0, 240.0, "USD", "2026-01-01"),
                    ("CFR", "Shanghai", "Hamburg", 1650.0, 0.0, 240.0, "USD", "2026-01-01"),
                    ("FOB", "Ningbo", "Jeddah", 1420.0, 0.0, 210.0, "USD", "2026-01-01"),
                    ("CIF", "Ningbo", "Jeddah", 1420.0, 118.0, 210.0, "USD", "2026-01-01"),
                ],
            )

        if not conn.execute("SELECT 1 FROM tariff_rate LIMIT 1").fetchone():
            conn.executemany(
                "INSERT INTO tariff_rate(dest_country,hs_code,tariff_rate,note,effective_date,source) VALUES(?,?,?,?,?,?)",
                [
                    ("US", "7307210000", 2.5, "不锈钢法兰，最惠国税率", "2026-01-01", "USITC HTS"),
                    ("DE", "7307210000", 0.0, "欧盟 CN 编码，最惠国零关税", "2026-01-01", "EU TARIC"),
                    ("SA", "85395000", 5.0, "LED 灯具，需 SASO/SABER 认证", "2026-01-01", "ZATCA"),
                    ("US", "85395000", 3.9, "LED 灯具", "2026-01-01", "USITC HTS"),
                ],
            )
        conn.commit()
        logger.info("业务库初始化完成：%s", path)
    finally:
        conn.close()


if __name__ == "__main__":  # 手动重置演示数据：python -m app.db.trade_seed
    logging.basicConfig(level=logging.INFO)
    init_trade_db(force=True)
