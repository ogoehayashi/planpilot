import sqlite3
from planpilot.backup import backup_database
from planpilot.observability import Metrics
from planpilot.persistence import Database
from planpilot.authority import RuntimeAuthority
from planpilot.security import issue_token, authenticate
from planpilot.domain.importer import factory_from_dict

def test_sqlite_backup_preserves_plan(tmp_path):
    source=tmp_path/'source.db'; target=tmp_path/'backup.db'; db=Database(source); authority=RuntimeAuthority(db); db.close(); backup_database(source,target)
    restored=Database(target); assert RuntimeAuthority(restored).revision==authority.revision; restored.close()

def test_metrics_snapshot_is_stable():
    metrics=Metrics(); metrics.observe('schedule',0.25); metrics.observe('schedule',0.75); assert metrics.snapshot()['schedule']['count']==2; assert metrics.snapshot()['schedule']['total_seconds']==1.0

def test_role_token_enforces_permission():
    token=issue_token('manager','manager','secret'); assert authenticate(token,'secret','approve_overtime')['role']=='manager'
