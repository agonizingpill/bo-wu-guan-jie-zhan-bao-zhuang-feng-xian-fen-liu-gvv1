import logging
import time
from pathlib import Path

from psycopg import Error as DatabaseError
from psycopg.types.json import Jsonb
from redis.exceptions import RedisError

from . import standby
from .loans import change_status, lock_loan
from .schemas import LoanStatus
from .scoring import score, standby_score
from .storage import QUEUE, STANDBY_QUEUE, audit, connection, initialize, queue

HEARTBEAT = Path('/tmp/museum-heartbeat')


def process(case_id):
    with connection() as db:
        reference = db.execute('SELECT loan_id FROM triage_case WHERE id=%s', (case_id,)).fetchone()
        if not reference:
            return
        loan_id = reference['loan_id']
        lock_loan(db, loan_id)
        case = db.execute('SELECT * FROM triage_case WHERE id=%s', (case_id,)).fetchone()
        if case['status'] != 'queued':
            return
        try:
            probability, risk, evidence = score(case['input_snapshot'])
        except Exception as exc:
            logging.exception('Model failed to score case %s', case_id)
            db.execute("UPDATE triage_case SET status='failed',error=%s,attempts=attempts+1 WHERE id=%s",
                       ('模型计算失败。请检查包装快照后重试。', case_id))
            change_status(db, loan_id, LoanStatus.PACKING)
            audit(db, loan_id, 'worker', 'triage_failed', case_id=case_id, error_type=type(exc).__name__)
            return
        db.execute('''UPDATE triage_case SET status='scored', probability=%s, risk=%s, evidence=%s,
                       attempts=attempts+1,computed_at=now(),error=NULL WHERE id=%s''',
                   (probability, risk, Jsonb(evidence), case_id))
        outcome = standby.settle(db, loan_id, case_id, risk)
        if risk == 'high' and outcome != 'promoted':
            db.execute("INSERT INTO unpack_review (case_id,status) VALUES (%s,'awaiting_witness')", (case_id,))
        change_status(db, loan_id, LoanStatus.REVIEW_REQUIRED if risk == 'high' else LoanStatus.READY)
        audit(db, loan_id, 'worker', 'triage_scored', case_id=case_id, risk=risk, probability=probability)
        print(f'case={case_id} loan={loan_id} risk={risk} probability={probability:.6f}', flush=True)


def process_standby(assessment_id):
    with connection() as db:
        reference = db.execute('SELECT loan_id FROM standby_assessment WHERE id=%s', (assessment_id,)).fetchone()
        if not reference:
            return
        loan_id = reference['loan_id']
        lock_loan(db, loan_id)
        row = db.execute('SELECT * FROM standby_assessment WHERE id=%s', (assessment_id,)).fetchone()
        if row['status'] != 'queued':
            return
        if row['outcome'] != 'pending':
            # The formal verdict landed first; no provisional review should be opened.
            db.execute("UPDATE standby_assessment SET status='cancelled' WHERE id=%s", (assessment_id,))
            audit(db, loan_id, 'worker', 'standby_cancelled', assessment_id=assessment_id, outcome=row['outcome'])
            return
        try:
            probability, risk, evidence = standby_score(row['input_snapshot'])
        except Exception as exc:
            logging.exception('Standby model failed to score assessment %s', assessment_id)
            db.execute("UPDATE standby_assessment SET status='failed',error=%s,attempts=attempts+1 WHERE id=%s",
                       ('备用模型计算失败。请稍后重试。', assessment_id))
            audit(db, loan_id, 'worker', 'standby_failed', assessment_id=assessment_id,
                  error_type=type(exc).__name__)
            return
        db.execute('''UPDATE standby_assessment SET status='scored',probability=%s,risk=%s,evidence=%s,
                      attempts=attempts+1,computed_at=now(),error=NULL WHERE id=%s''',
                   (probability, risk, Jsonb(evidence), assessment_id))
        audit(db, loan_id, 'worker', 'standby_scored', assessment_id=assessment_id, risk=risk,
              probability=probability)
        print(f'standby={assessment_id} loan={loan_id} risk={risk} probability={probability:.6f}', flush=True)


def recover_queue(redis):
    # PostgreSQL keeps the pending intent if a producer or worker exits before completion.
    with connection() as db:
        pending = db.execute("SELECT id FROM triage_case WHERE status='queued' ORDER BY id LIMIT 100").fetchall()
        standby_pending = db.execute("SELECT id FROM standby_assessment WHERE status='queued' ORDER BY id LIMIT 100").fetchall()
    if pending:
        redis.lpush(QUEUE, *(row['id'] for row in pending))
    if standby_pending:
        redis.lpush(STANDBY_QUEUE, *(row['id'] for row in standby_pending))


def main():
    initialize()
    with queue() as redis:
        while True:
            try:
                message = redis.brpop([QUEUE, STANDBY_QUEUE], timeout=2)
                if message:
                    channel, payload = (part.decode() if isinstance(part, bytes) else part for part in message)
                    if channel == STANDBY_QUEUE:
                        process_standby(int(payload))
                    else:
                        process(int(payload))
                else:
                    recover_queue(redis)
                HEARTBEAT.touch()
            except (DatabaseError, RedisError, ValueError):
                logging.exception('Queue operation failed')
                time.sleep(2)


if __name__ == '__main__':
    main()
