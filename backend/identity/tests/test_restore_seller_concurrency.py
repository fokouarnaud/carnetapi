"""Two real workers cannot grant twice or overwrite a concurrent profile."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid
from django.db import connection
from django.test import TransactionTestCase
from identity.tests import test_restore_seller_access as fixtures


class RestoreSellerConcurrencyTests(TransactionTestCase):
    setUp=fixtures.RestoreSellerAccessTests.setUp

    def test_two_processes_replay_one_operation_and_conflicting_operations_do_not_overwrite(self):
        backend=Path(__file__).resolve().parents[2]
        worker="""
import json,os,sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
os.environ['APP_DB_PATH']=sys.argv[2]
import django
django.setup()
from identity.models import User,Installation
from identity.services.restore_seller_access import revalidate_seller_access
from common.services.errors import OperationConflict
data=json.loads(sys.stdin.read())
Path(sys.argv[3]).touch()
deadline=time.monotonic()+30
while not Path(sys.argv[4]).exists():
 if time.monotonic()>deadline:raise RuntimeError('worker barrier timed out')
 time.sleep(.01)
try:
 result,created=revalidate_seller_access(shop_id=data['shop_id'],member_id=data['member_id'],
  actor=User.objects.get(id=data['actor']),installation=Installation.objects.get(id=data['installation']),
  session_id=data['session'],data=data['request'])
 print(json.dumps({'created':created,'receipt':result}))
except OperationConflict:
 print(json.dumps({'conflict':True}))
"""
        with TemporaryDirectory(prefix="seller-review-race-") as directory:
            root=Path(directory)
            for same_operation in (True,False):
                with self.subTest(same_operation=same_operation):
                    database=root/f"candidate-{same_operation}.sqlite3"
                    with closing(sqlite3.connect(database)) as target:
                        connection.connection.backup(target)
                    start=root/f"start-{same_operation}"
                    payload={"shop_id":str(self.a.id),"member_id":str(self.members[0].id),"actor":str(self.owner.id),
                        "installation":str(self.installation.id),"session":str(self.session.id),"request":self.data}
                    ready=[root/f"ready-{same_operation}-{index}" for index in range(2)]
                    def run(index):
                        request=dict(self.data)
                        if not same_operation:
                            request["operation_id"]=uuid.uuid4()
                            request["permissions"]={**self.permissions,"can_read_cost":bool(index)}
                        raw=json.dumps({**payload,"request":request},default=str)
                        completed=subprocess.run([sys.executable,"-I","-X","utf8","-c",worker,str(backend),str(database),
                            str(ready[index]),str(start)],input=raw,text=True,capture_output=True,timeout=60)
                        self.assertEqual(completed.returncode,0,completed.stderr)
                        return json.loads(completed.stdout)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        futures=[pool.submit(run,index) for index in range(2)]
                        deadline=time.monotonic()+30
                        while not all(path.exists() for path in ready) and time.monotonic()<deadline:
                            time.sleep(.01)
                        start.touch()
                        self.assertTrue(all(path.exists() for path in ready))
                        results=[future.result() for future in futures]
                    self.assertEqual(sum(item.get("created",False) for item in results),1)
                    if same_operation:
                        self.assertEqual(results[0]["receipt"],results[1]["receipt"])
                    else:
                        self.assertEqual(sum(item.get("conflict",False) for item in results),1)
                    with closing(sqlite3.connect(database)) as db:
                        self.assertEqual(db.execute("SELECT COUNT(*) FROM core_activity WHERE action='restore_seller_access_decided'").fetchone()[0],1)
                        self.assertEqual(db.execute("SELECT version FROM core_membership WHERE id=?",[self.members[0].id.hex]).fetchone()[0],2)
