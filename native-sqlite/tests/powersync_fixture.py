"""Deterministic real-core fixture, independent of the native wrapper's API.

Call fresh(execute, scalar) on a NEW isolated database after registering the
actual PowerSync extension. Close/reopen it, then call reopened(execute, scalar).
`execute(sql)` must finish the statement or raise, and `scalar(sql)` must return
the first column of the first row, finishing/finalizing its statement.

No cloud access or credentials. Passing this on a host is fixture qualification,
not evidence of Badgeware support.
"""

SCHEMA = (
    '{"tables":[{"name":"items","columns":['
    '{"name":"name","type":"text"},{"name":"value","type":"integer"}]}]}'
)
CHECKPOINT = (
    '{"checkpoint":{"last_op_id":"1","write_checkpoint":"1","buckets":['
    '{"bucket":"hello","checksum":1,"priority":3,"count":1}]}}'
)
DATA = (
    '{"data":{"bucket":"hello","has_more":false,"after":null,"next_after":null,'
    '"data":[{"op_id":"1","op":"PUT","object_type":"items","object_id":"hello",'
    '"checksum":1,"data":"{\\"name\\":\\"Hello world\\",\\"value\\":7}"}]}}'
)
COMPLETE = '{"checkpoint_complete":{"last_op_id":"1"}}'


def quote(text):
    return "NULL" if text is None else "'" + text.replace("'", "''") + "'"


def control(execute, scalar, command, payload=None):
    execute("BEGIN IMMEDIATE")
    try:
        result = scalar("SELECT powersync_control(%s,%s)" % (quote(command), quote(payload)))
        execute("COMMIT")
        return result
    except Exception:
        execute("ROLLBACK")
        raise


def fresh(execute, scalar):
    assert scalar("SELECT powersync_rs_version()") == "0.5.4/cratesio"
    execute("PRAGMA cache_size=2")
    execute("PRAGMA temp_store=MEMORY")
    assert scalar("PRAGMA journal_mode=DELETE") in ("delete", "memory")
    execute("BEGIN")
    scalar("SELECT powersync_replace_schema(%s)" % quote(SCHEMA))
    execute("COMMIT")

    started = control(execute, scalar, "start", '{"checkpoint_mode":"requests"}')
    assert "EstablishSyncStream" in started
    assert control(execute, scalar, "seed_checkpoint_request_id", "1") in (1, "1")
    control(execute, scalar, "line_text", CHECKPOINT)
    control(execute, scalar, "line_text", DATA)
    assert scalar("SELECT count(*) FROM items") == 0
    completed = control(execute, scalar, "line_text", COMPLETE)
    assert "DidCompleteSync" in completed
    assert scalar("SELECT name FROM items WHERE id='hello'") == "Hello world"
    assert scalar("SELECT value FROM items WHERE id='hello'") == 7
    assert scalar("SELECT count(*) FROM ps_crud") == 0

    execute("BEGIN")
    execute("INSERT INTO items(id,name,value) VALUES('local-1','offline',10)")
    execute("UPDATE items SET value=11 WHERE id='local-1'")
    execute("COMMIT")
    execute("BEGIN")
    execute("INSERT INTO items(id,name,value) VALUES('rolled-back','discard',0)")
    execute("ROLLBACK")
    control(execute, scalar, "stop")
    reopened(execute, scalar)
    return "checkpoint published; local PUT/PATCH queued; rollback absent"


def reopened(execute, scalar):
    assert scalar("SELECT name FROM items WHERE id='hello'") == "Hello world"
    assert scalar("SELECT value FROM items WHERE id='hello'") == 7
    assert scalar("SELECT value FROM items WHERE id='local-1'") == 11
    assert scalar("SELECT count(*) FROM items") == 2
    assert scalar("SELECT count(*) FROM ps_oplog") == 1
    assert scalar("SELECT count(*) FROM ps_crud") == 2
    assert scalar("SELECT count(DISTINCT tx_id) FROM ps_crud") == 1
    assert scalar("SELECT json_extract(data,'$.op') FROM ps_crud ORDER BY id LIMIT 1") == "PUT"
    assert scalar("SELECT json_extract(data,'$.op') FROM ps_crud ORDER BY id DESC LIMIT 1") == "PATCH"
    assert scalar("SELECT count(*) FROM ps_crud WHERE json_extract(data,'$.id')='rolled-back'") == 0
    assert scalar("PRAGMA integrity_check") == "ok"
    return "synced row, two queued CRUD operations, and integrity retained"
