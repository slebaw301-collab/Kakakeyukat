"""PostgreSQL WASM adapter. SQL fidelity; not a native multi-session lock test."""
import json
import os
import re
import subprocess
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

class PgBridge:
    def __init__(self):
        self.process = subprocess.Popen(['node', str(ROOT/'tools/pg_bridge.mjs')], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.lock = threading.RLock()
        ready = self.process.stdout.readline()
        if not ready:
            raise RuntimeError(self.process.stderr.read())
        assert json.loads(ready)['ready']

    def query(self, sql, params=()):
        with self.lock:
            if isinstance(params, dict):
                values = []
                def named(m):
                    values.append(params[m.group(1)])
                    return '$'+str(len(values))
                sql = re.sub(r'%\((\w+)\)s', named, sql)
                params = values
            else:
                index=iter(range(1,len(params)+1))
                sql=re.sub(r'%s', lambda _: '$'+str(next(index)), sql)
            self.process.stdin.write(json.dumps({'sql':sql, 'params':params},default=str)+'\n')
            self.process.stdin.flush()
            response=json.loads(self.process.stdout.readline())
            if not response['ok']:
                raise RuntimeError(response)
            return response

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=10)
        assert self.process.returncode == 0, self.process.stderr.read()

class Cursor:
    def __init__(self, bridge): self.bridge=bridge; self.rows=[]; self.rowcount=0
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def execute(self,sql,params=()):
        result=self.bridge.query(sql,params)
        self.rows=result.get('rows',[])
        self.rowcount=result.get('affectedRows',len(self.rows))
    def executemany(self,sql,rows):
        for row in rows: self.execute(sql,row)
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows

class Connection:
    closed=False
    autocommit=True
    def __init__(self, bridge): self.bridge=bridge
    def cursor(self): return Cursor(self.bridge)
    def rollback(self): pass
    def commit(self): pass

class Pool:
    def __init__(self, bridge): self.bridge=bridge; self.conn=Connection(bridge)
    def getconn(self): self.bridge.lock.acquire(); return self.conn
    def putconn(self,conn,close=False): self.bridge.lock.release()
    def closeall(self): pass
