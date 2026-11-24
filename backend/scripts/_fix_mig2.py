"""整理 0002 迁移：类型自包含 + 索引写法与 0001 一致。"""
from pathlib import Path

p = Path("migrations/versions/0002_ingest_job_queue.py")
s = p.read_text(encoding="utf-8")
s = s.replace("app.db.types.TZDateTime(timezone=True)", "sa.DateTime(timezone=True)")

old_up = '''    with op.batch_alter_table('ingest_job', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_ingest_job_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingest_job_doc_id'), ['doc_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingest_job_doc_version_id'), ['doc_version_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingest_job_stage'), ['stage'], unique=False)
        batch_op.create_index(batch_op.f('ix_ingest_job_task_id'), ['task_id'], unique=False)'''
new_up = '''    op.create_index(op.f('ix_ingest_job_created_at'), 'ingest_job', ['created_at'], unique=False)
    op.create_index(op.f('ix_ingest_job_doc_id'), 'ingest_job', ['doc_id'], unique=False)
    op.create_index(op.f('ix_ingest_job_doc_version_id'), 'ingest_job', ['doc_version_id'], unique=False)
    op.create_index(op.f('ix_ingest_job_stage'), 'ingest_job', ['stage'], unique=False)
    op.create_index(op.f('ix_ingest_job_task_id'), 'ingest_job', ['task_id'], unique=False)'''
assert old_up in s
s = s.replace(old_up, new_up)

old_down = '''    with op.batch_alter_table('ingest_job', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_ingest_job_task_id'))
        batch_op.drop_index(batch_op.f('ix_ingest_job_stage'))
        batch_op.drop_index(batch_op.f('ix_ingest_job_doc_version_id'))
        batch_op.drop_index(batch_op.f('ix_ingest_job_doc_id'))
        batch_op.drop_index(batch_op.f('ix_ingest_job_created_at'))'''
new_down = '''    op.drop_index(op.f('ix_ingest_job_task_id'), table_name='ingest_job')
    op.drop_index(op.f('ix_ingest_job_stage'), table_name='ingest_job')
    op.drop_index(op.f('ix_ingest_job_doc_version_id'), table_name='ingest_job')
    op.drop_index(op.f('ix_ingest_job_doc_id'), table_name='ingest_job')
    op.drop_index(op.f('ix_ingest_job_created_at'), table_name='ingest_job')'''
assert old_down in s
s = s.replace(old_down, new_down)

p.write_text(s, encoding="utf-8")
print("0002 cleaned")
