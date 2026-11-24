import logging

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from redis.exceptions import RedisError

from .loans import active_loan, dossier
from .scoring import STANDBY_COLUMNS
from .storage import STANDBY_QUEUE, audit, queue


def formal_case(db, loan_id):
    return db.execute('SELECT id,status FROM triage_case WHERE loan_id=%s', (loan_id,)).fetchone()


def enqueue(db, loan_id, actor):
    active_loan(db, loan_id)
    check = db.execute('SELECT * FROM crate_check WHERE loan_id=%s', (loan_id,)).fetchone()
    if not check:
        raise HTTPException(409, '请先完成包装检查')
    formal = formal_case(db, loan_id)
    if not formal:
        raise HTTPException(409, '请先提交正式风险分流')
    if formal['status'] == 'scored':
        raise HTTPException(409, '正式分流已出分，无需备用评分')
    existing = db.execute('SELECT * FROM standby_assessment WHERE loan_id=%s FOR UPDATE',
                          (loan_id,)).fetchone()
    if existing and existing['status'] in ('queued', 'scored'):
        raise HTTPException(409, '此批次已有备用评分，请在分流页查看接管结果')
    snapshot = Jsonb({column: check[column] for column in STANDBY_COLUMNS})
    if existing:
        assessment_id = existing['id']
        db.execute('''UPDATE standby_assessment SET status='queued',input_snapshot=%s,probability=NULL,
                      risk=NULL,evidence=NULL,error=NULL,attempts=0,requested_by=%s,outcome='pending',
                      outcome_case_id=NULL,outcome_at=NULL,computed_at=NULL,witness_by=NULL,
                      witness_note=NULL,witnessed_at=NULL WHERE id=%s''',
                   (snapshot, actor, assessment_id))
    else:
        assessment_id = db.execute('''INSERT INTO standby_assessment (loan_id,status,input_snapshot,requested_by)
                            VALUES (%s,'queued',%s,%s) RETURNING id''',
                                   (loan_id, snapshot, actor)).fetchone()['id']
    audit(db, loan_id, actor, 'standby_retried' if existing else 'standby_queued',
          assessment_id=assessment_id, features=list(STANDBY_COLUMNS))
    return dossier(db, loan_id), assessment_id


def notify_worker(assessment_id):
    try:
        with queue() as redis:
            redis.lpush(STANDBY_QUEUE, assessment_id)
    except RedisError:
        logging.exception('Redis enqueue failed; worker will recover the committed standby assessment')


def witness(db, loan_id, data, actor):
    active_loan(db, loan_id)
    row = db.execute('SELECT * FROM standby_assessment WHERE loan_id=%s FOR UPDATE', (loan_id,)).fetchone()
    if not row or row['status'] != 'scored' or row['risk'] != 'high' or row['outcome'] != 'pending':
        raise HTTPException(409, '本批次没有待见证的备用开箱复核单，请刷新档案')
    formal = formal_case(db, loan_id)
    if formal and formal['status'] == 'scored':
        # The formal verdict may have landed while this request waited for the row lock.
        raise HTTPException(409, '正式分流已出分，备用复核单已接管或撤销，不再接收见证')
    if row['witnessed_at']:
        raise HTTPException(409, '备用开箱见证已经签署')
    db.execute('''UPDATE standby_assessment SET witness_by=%s,witness_note=%s,witnessed_at=now()
               WHERE id=%s''', (actor, data.note, row['id']))
    audit(db, loan_id, actor, 'standby_witnessed', assessment_id=row['id'], observation=data.note)
    return dossier(db, loan_id)


def settle(db, loan_id, case_id, risk):
    """Reconcile a provisional standby review with the formal verdict.

    Runs inside the formal worker's transaction while the loan row is locked, and locks the
    standby row so a concurrent witness request either lands before this settlement or is
    refused after it.
    """
    standby = db.execute('SELECT * FROM standby_assessment WHERE loan_id=%s FOR UPDATE',
                         (loan_id,)).fetchone()
    if not standby or standby['outcome'] != 'pending':
        return None
    if risk == 'high':
        reused = bool(standby['status'] == 'scored' and standby['risk'] == 'high' and standby['witnessed_at'])
        if reused:
            db.execute('''INSERT INTO unpack_review (case_id,status,witness_by,witness_note,witnessed_at)
                       VALUES (%s,'awaiting_supervisor',%s,%s,%s)
                       ON CONFLICT (case_id) DO UPDATE SET status='awaiting_supervisor',
                       witness_by=excluded.witness_by,witness_note=excluded.witness_note,
                       witnessed_at=excluded.witnessed_at''',
                       (case_id, standby['witness_by'], standby['witness_note'], standby['witnessed_at']))
            db.execute("UPDATE standby_assessment SET outcome='promoted',outcome_case_id=%s,outcome_at=now() WHERE id=%s",
                       (case_id, standby['id']))
            audit(db, loan_id, 'worker', 'standby_promoted', assessment_id=standby['id'], case_id=case_id,
                  witness_by=standby['witness_by'])
            return 'promoted'
        db.execute("UPDATE standby_assessment SET outcome='superseded',outcome_case_id=%s,outcome_at=now() WHERE id=%s",
                   (case_id, standby['id']))
        audit(db, loan_id, 'worker', 'standby_superseded', assessment_id=standby['id'], case_id=case_id)
        return 'superseded'
    witnessed = bool(standby['witnessed_at'])
    db.execute("UPDATE standby_assessment SET outcome='revoked',outcome_case_id=%s,outcome_at=now() WHERE id=%s",
               (case_id, standby['id']))
    audit(db, loan_id, 'worker', 'standby_revoked', assessment_id=standby['id'], case_id=case_id,
          witnessed=witnessed)
    return 'revoked'
