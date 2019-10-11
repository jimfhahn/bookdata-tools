import os
import sys
import re
import time
import logging
import hashlib
from pathlib import Path
from contextlib import contextmanager
from datetime import timedelta
from typing import NamedTuple, List
from docopt import docopt

from more_itertools import peekable
import psycopg2, psycopg2.errorcodes
import sqlparse

_log = logging.getLogger(__name__)

# Meta-schema for storing stage and file status in the database
meta_schema = """
CREATE TABLE IF NOT EXISTS stage_status (
    stage_name VARCHAR PRIMARY KEY,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP NULL,
    stage_key VARCHAR NULL
);

CREATE TABLE IF NOT EXISTS source_file (
  filename VARCHAR NOT NULL PRIMARY KEY,
  reg_time TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  checksum VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_file (
  stage_name VARCHAR NOT NULL REFERENCES stage_status,
  filename VARCHAR NOT NULL REFERENCES source_file,
  PRIMARY KEY (stage_status, source_file)
);

INSERT INTO stage_status (stage_name, started_at, finished_at, stage_key)
VALUES ('init', NOW(), NOW(), uuid_generate_v4())
ON CONFLICT (stage_name) DO NOTHING;
"""


def db_url():
    if 'DB_URL' in os.environ:
        return os.environ['DB_URL']

    host = os.environ.get('PGHOST', 'localhost')
    port = os.environ.get('PGPORT', None)
    db = os.environ['PGDATABASE']
    user = os.environ.get('PGUSER', None)
    pw = os.environ.get('PGPASSWORD', None)

    url = 'postgresql://'
    if user:
        url += user
        if pw:
            url += ':' + pw
        url += '@'
    url += host
    if port:
        url += ':' + port
    url += '/' + db
    return url


@contextmanager
def connect():
    "Connect to a database. This context manager yields the connection, and closes it when exited."
    _log.info('connecting to %s', db_url())
    conn = psycopg2.connect(db_url())
    try:
        yield conn
    finally:
        conn.close()


def save_file(cur, path, stage=None):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        data = f.read(8192 * 4)
        while data:
            h.update(data)
            data = f.read(8192 * 4)
    hash = h.hexdigest()
    path = Path(path).as_posix()
    _log.info('recording file %s with hash %s', path, hash)
    cur.execute('''
        INSERT INTO source_file (filename, checksum)
        VALUES (%(fn)s, %(hash)s)
        ON CONFLICT (filename)
        DO UPDATE SET reg_time = now(), checksum = %(hash)s
    ''', {'fn': path, 'hash': hash})
    if stage:
        cur.execute('''
            INSERT INTO stage_file (stage_name, filename)
            VALUES (%s, %s)
        ''', [stage, path])


def start_stage(cur, stage):
    if hasattr(cur, 'cursor'):
        # this is a connection
        with cur, cur.cursor() as c:
            start_stage(c, stage)
    _log.info('starting or resetting stage %s', stage)
    cur.execute('''
        INSERT INTO stage_status (stage_name)
        VALUES (%s)
        ON CONFLICT (stage_name)
        DO UPDATE SET started_at = now(), finished_at = NULL, stage_key = NULL
    ''', [stage])
    cur.execute('DELETE FROM stage_file WHERE stage_name = %s', [stage])


def finish_stage(cur, stage, key=None):
    if hasattr(cur, 'cursor'):
        # this is a connection
        with cur, cur.cursor() as c:
            finish_stage(c, stage, key)
    _log.info('finishing stage %s', stage)
    cur.execute('''
        UPDATE stage_status
        SET finished_at = NOW(), stage_key = %(key)s
        WHERE stage_name = %(stage)s
    ''', {'stage': stage, 'key': key})


def _tokens(s, start=-1, skip_ws=True, skip_cm=True):
    i, t = s.token_next(start, skip_ws=skip_ws, skip_cm=skip_cm)
    while t is not None:
        yield t
        i, t = s.token_next(i, skip_ws=skip_ws, skip_cm=skip_cm)


def describe_statement(s):
    "Describe an SQL statement"
    label = s.get_type()
    li, lt = s.token_next(-1, skip_cm=True)
    if lt is None:
        return None
    if lt and lt.ttype == sqlparse.tokens.DDL:
        # DDL - build up!
        parts = []
        first = True
        skipping = False
        for t in _tokens(s, li):
            if not first:
                if isinstance(t, sqlparse.sql.Identifier) or isinstance(t, sqlparse.sql.Function):
                    parts.append(t.normalized)
                    break
                elif t.ttype != sqlparse.tokens.Keyword:
                    break

            first = False

            if t.normalized == 'IF':
                skipping = True

            if not skipping:
                parts.append(t.normalized)

        label = label + ' ' + ' '.join(parts)
    elif label == 'UNKNOWN':
        ls = []
        for t in _tokens(s):
            if t.ttype == sqlparse.tokens.Keyword:
                ls.append(t.normalized)
            else:
                break
        if ls:
            label = ' '.join(ls)

        name = s.get_real_name()
        if name:
            label += f' {name}'

    return label


def is_empty(s):
    "check if an SQL statement is empty"
    lt = s.token_first(skip_cm=True, skip_ws=True)
    return lt is None


class ScriptChunk(NamedTuple):
    "A single chunk of an SQL script."
    label: str
    allowed_errors: List[str]
    src: str
    use_transaction: bool = True

    @property
    def statements(self):
        return [s for s in sqlparse.parse(self.src) if not is_empty(s)]


class SqlScript:
    """
    Class for processing & executing SQL scripts.
    """

    _sep_re = re.compile(r'^---\s*(?P<inst>.*)')
    _icode_re = re.compile(r'#(?P<code>\w+)\s*(?P<args>.*\S)?\s*$')

    chunks: List[ScriptChunk]

    def __init__(self, file):
        if hasattr(file, 'read'):
            self._parse(peekable(file))
        else:
            with open(file, 'r', encoding='utf8') as f:
                self._parse(peekable(f))

    def _parse(self, lines):
        self.chunks = []
        next_chunk = self._parse_chunk(lines, len(self.chunks) + 1)
        while next_chunk is not None:
            if next_chunk:
                self.chunks.append(next_chunk)
            next_chunk = self._parse_chunk(lines, len(self.chunks) + 1)

    @classmethod
    def _parse_chunk(cls, lines: peekable, n: int):
        qlines = []

        chunk = cls._read_header(lines)
        qlines = cls._read_query(lines)

        # end of file, do we have a chunk?
        if qlines:
            if chunk.label is None:
                chunk = chunk._replace(label=f'Step {n}')
            return chunk._replace(src='\n'.join(qlines))
        elif qlines is not None:
            return False  # empty chunk

    @classmethod
    def _read_header(cls, lines: peekable):
        label = None
        errs = []
        tx = True

        line = lines.peek(None)
        while line is not None:
            hm = cls._sep_re.match(line)
            if hm is None:
                break

            next(lines)  # eat line
            line = lines.peek(None)

            inst = hm.group('inst')
            cm = cls._icode_re.match(inst)
            if cm is None:
                continue
            code = cm.group('code')
            args = cm.group('args')
            if code == 'step':
                label = args
            elif code == 'allow':
                err = getattr(psycopg2.errorcodes, args.upper())
                _log.debug('step allows error %s (%s)', args, err)
                errs.append(err)
            elif code == 'notx':
                _log.debug('chunk will run outside a transaction')
                tx = False
            else:
                _log.error('unrecognized query instruction %s', code)
                raise ValueError(f'invalid query instruction {code}')

        return ScriptChunk(label=label, allowed_errors=errs, src=None,
                           use_transaction=tx)

    @classmethod
    def _read_query(cls, lines: peekable):
        qls = []

        line = lines.peek(None)
        while line is not None and not cls._sep_re.match(line):
            qls.append(next(lines))
            line = lines.peek(None)

        # trim lines
        while qls and not qls[0].strip():
            qls.pop(0)
        while qls and not qls[-1].strip():
            qls.pop(-1)

        if qls or line is not None:
            return qls
        else:
            return None  # end of file

    def execute(self, dbc, transcript=None):
        """
        Execute the SQL script.

        Args:
            dbc: the database connection.
            transcript: a file to receive the run transcript.
        """
        for step in self.chunks:
            start = time.perf_counter()
            _log.info('Running ‘%s’', step.label)
            if transcript is not None:
                print('CHUNK', step.label, file=transcript)
            if step.use_transaction:
                with dbc, dbc.cursor() as cur:
                    self._run_step(step, dbc, cur, True, transcript)
            else:
                ac = dbc.autocommit
                try:
                    dbc.autocommit = True
                    with dbc.cursor() as cur:
                        self._run_step(step, dbc, cur, False, transcript)
                finally:
                    dbc.autocommit = ac

            elapsed = time.perf_counter() - start
            elapsed = timedelta(seconds=elapsed)
            _log.info('Finished ‘%s’ in %s', step.label, elapsed)

    def describe(self):
        for step in self.chunks:
            _log.info('Chunk ‘%s’', step.label)
            for s in step.statements:
                _log.info('Statement %s', describe_statement(s))

    def _run_step(self, step, dbc, cur, commit, transcript):
        try:
            for sql in step.statements:
                start = time.perf_counter()
                _log.debug('Executing %s', describe_statement(sql))
                _log.debug('Query: %s', sql)
                if transcript is not None:
                    print('STMT', describe_statement(sql), file=transcript)
                cur.execute(str(sql))
                elapsed = time.perf_counter() - start
                rows = cur.rowcount
                if transcript is not None:
                    print('ELAPSED', elapsed, file=transcript)
                if rows is not None and rows >= 0:
                    if transcript is not None:
                        print('ROWS', rows, file=transcript)
                    _log.info('finished %s in %s (%d rows)', describe_statement(sql),
                              timedelta(seconds=elapsed), rows)
                else:
                    _log.info('finished %s in %s (%d rows)', describe_statement(sql),
                              timedelta(seconds=elapsed), rows)
            if commit:
                dbc.commit()
        except psycopg2.Error as e:
            if e.pgcode in step.allowed_errors:
                _log.info('Failed with acceptable error %s (%s)',
                          e.pgcode, psycopg2.errorcodes.lookup(e.pgcode))
                if transcript is not None:
                    print('ERROR', e.pgcode, psycopg2.errorcodes.lookup(e.pgcode), file=transcript)
            else:
                _log.error('Error in "%s" %s: %s: %s',
                           step.label, describe_statement(sql),
                           psycopg2.errorcodes.lookup(e.pgcode), e)
                if e.pgerror:
                    _log.info('Query diagnostics:\n%s', e.pgerror)
                raise e
