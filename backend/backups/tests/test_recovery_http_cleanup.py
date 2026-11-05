"""A failed early HTTP fixture closes its SQLite file before deleting its private directory."""
import json
from pathlib import Path
import subprocess
import sys
from django.test import SimpleTestCase


class RecoveryHttpFixtureCleanupTests(SimpleTestCase):
    def test_original_failure_is_preserved_and_private_sqlite_is_removed(self):
        backend=Path(__file__).resolve().parents[2]
        code="""
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from verification.restore_installation_smoke import disposable_installation_directory
try:
 with disposable_installation_directory() as directory:
  private=Path(directory)
  os.environ['DJANGO_SETTINGS_MODULE']='config.test_settings'
  os.environ['APP_DB_PATH']=str(private/'business.sqlite3')
  import django
  django.setup()
  from django.db import connection
  with connection.cursor() as cursor:cursor.execute('CREATE TABLE cleanup_fixture (id INTEGER PRIMARY KEY)')
  raise RuntimeError('expected-private-fixture-failure')
except RuntimeError as error:
 assert str(error)=='expected-private-fixture-failure'
else:raise AssertionError('original failure disappeared')
assert not private.exists()
print(json.dumps({'original_failure_preserved':True,'private_directory_removed':True}))
"""
        result=subprocess.run([sys.executable,'-I','-X','utf8','-c',code,str(backend)],text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'original_failure_preserved':True,'private_directory_removed':True})
