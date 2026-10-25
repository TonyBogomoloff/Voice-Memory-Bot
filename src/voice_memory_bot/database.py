"""Synchronous SQLite units of work; never await while a transaction is open."""
from contextlib import contextmanager
from itertools import count

_savepoints = count()


@contextmanager
def transaction(db):
    """The outer operation commits; nested operations own only their savepoint."""
    nested = db.in_transaction
    savepoint = f'unit_{next(_savepoints)}'
    db.execute(f'SAVEPOINT {savepoint}' if nested else 'BEGIN IMMEDIATE')
    try:
        yield db
    except BaseException:
        if nested:
            db.execute(f'ROLLBACK TO {savepoint}')
            db.execute(f'RELEASE {savepoint}')
        else:
            db.rollback()
        raise
    else:
        try:
            if nested:
                db.execute(f'RELEASE {savepoint}')
            else:
                db.commit()
        except BaseException:
            if not nested:
                db.rollback()
            raise
