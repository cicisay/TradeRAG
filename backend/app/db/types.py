"""跨数据库类型：统一时区语义（SQLite → PostgreSQL 的关键补丁）。

给新手的说明：
- 项目里所有时间列都是"UTC 时间"。但两种数据库的行为不一样：
  * PostgreSQL 的 `TIMESTAMP WITH TIME ZONE` 会原样保存"带时区的时间"，读回来
    也是带时区的（aware），可以直接比较、可以直接减。
  * SQLite 没有真正的时区类型，`DATETIME` 就是一段文本。SQLAlchemy 写入时会
    **悄悄丢掉时区信息**，读回来是不带时区的（naive）。
- 这会造成一个隐蔽 bug：代码里 `datetime.now(timezone.utc) > row.created_at`
  （aware 比 naive）会直接抛 TypeError；更糟的是，如果两边都"恰好"是 naive，
  比较不会报错，但含义已经漂了（把 UTC 当成服务器本地时间）。
- 因此在 SQLite 上跑得好好的代码，切到 PG 或反过来，都可能在某个角落崩。

解法：自定义一个 `TZDateTime` 类型（SQLAlchemy 的 TypeDecorator）。
- 写库时：不管传进来的是 aware 还是 naive，一律当作 UTC 并转成 UTC；
- 读库时：如果数据库给的是 naive（SQLite 的情况），补上 UTC 时区；
- 在 PG 上它仍然映射成原生 `TIMESTAMP WITH TIME ZONE`，不损失任何能力。

这样"进出一律是 aware UTC"，业务代码不需要关心底层是哪种数据库。
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class TZDateTime(TypeDecorator):
    """始终以 UTC aware datetime 进出的时间类型（跨 SQLite / PostgreSQL 一致）。"""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        # naive 时间按 UTC 解释（历史数据就是按 UTC 写入的，只是丢了标记）
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        # 统一转 UTC 再落库：PG 中存储为 timestamptz，跨时区读写不会漂
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            # SQLite 丢掉了时区，这里补回来（存的时候已统一为 UTC）
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
