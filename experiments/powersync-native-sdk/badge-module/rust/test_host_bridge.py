"""Exercise the actual no_std C ABI bridge with real SQLite/core and mailbox HTTP."""
import base64
import ctypes as c
import json
import os
from pathlib import Path
import subprocess

HERE=Path(__file__).resolve().parent
APP=HERE.parents[3]
NATIVE=APP/"native-sqlite"
LIB=HERE/"target/libbridge_host.dylib"
subprocess.run(["cc","-std=c11","-O1","-dynamiclib","-D_DARWIN_C_SOURCE","-include",str(NATIVE/"tests/host_config.h"),
    "-I",str(NATIVE/"src"),"-I",str(NATIVE/"vendor/sqlite"),str(NATIVE/"vendor/sqlite/sqlite3.c"),str(HERE/"host_shim.c"),
    str(HERE/"target/release/libpsbadge_native.a"),"-lm","-o",str(LIB)],check=True)
lib=c.CDLL(str(LIB))
class Buffer(c.Structure):
    _fields_=[("ptr",c.c_void_p),("len",c.c_size_t),("capacity",c.c_size_t)]
for name,result,args in [
    ("bridge_host_open",c.c_int,[c.POINTER(c.c_void_p)]),
    ("bridge_host_exec",c.c_int,[c.c_void_p,c.c_char_p]),
    ("bridge_host_scalar",c.c_int,[c.c_void_p,c.c_char_p]),
    ("psb_start",c.c_void_p,[c.c_void_p,c.c_char_p,c.c_size_t,c.c_char_p,c.c_size_t,c.c_char_p,c.c_size_t]),
    ("psb_poll",Buffer,[c.c_void_p,c.c_uint,c.c_uint]),
    ("psb_command",c.c_int,[c.c_void_p,c.c_char_p,c.c_size_t]),
    ("psb_buffer_free",None,[Buffer]),
    ("psb_stop",c.c_int,[c.c_void_p]),
    ("psb_destroy",c.c_int,[c.c_void_p]),
    ("psb_sql_enter",c.c_void_p,[c.c_void_p]),
    ("psb_sql_leave",None,[c.c_void_p]),
]:
    fn=getattr(lib,name);fn.restype=result;fn.argtypes=args
fixture=json.loads((HERE.parents[1]/"host/fixture.json").read_text())
fixture["schema"]["tables"].append({"name":"_badge_move_outbox","columns":[
    {"name":"room_id","type":"text"},{"name":"orientation","type":"text"},
    {"name":"row_i","type":"integer"},{"name":"col_i","type":"integer"}]})
db=c.c_void_p();assert lib.bridge_host_open(c.byref(db))==0
for statement in fixture["raw_sql"]:
    # SQLite's update_hook does not report WITHOUT ROWID writes. Keep these
    # game tables raw, but use their rowid-backed physical form for SDK watch.
    assert lib.bridge_host_exec(db,statement.replace(" WITHOUT ROWID", "").encode())==0
schema=json.dumps(fixture["schema"]).encode();endpoint=b"https://bridge.fixture.invalid/";room=fixture["room"].encode()
ctx=lib.psb_start(db,schema,len(schema),endpoint,len(endpoint),room,len(room));assert ctx
streams={}; sent=False;partial_checked=False;iterations=0;requests=0
def command(value):
    raw=json.dumps(value).encode();rc=lib.psb_command(ctx,raw,len(raw));assert rc==0,(value,rc)
def rejected_command(value):
    raw=json.dumps(value).encode();assert lib.psb_command(ctx,raw,len(raw))==-1,value
def scalar(query):
    lease=lib.psb_sql_enter(ctx);assert lease,"SQL lease busy"
    try:return lib.bridge_host_scalar(db,query.encode())
    finally:lib.psb_sql_leave(lease)
def execute(query):
    lease=lib.psb_sql_enter(ctx);assert lease,"SQL lease busy"
    try:assert lib.bridge_host_exec(db,query.encode())==0,query
    finally:lib.psb_sql_leave(lease)
def chunk(id,data):command({"op":"chunk","id":id,"data_b64":base64.b64encode(data).decode()})
wire=(json.dumps(fixture["checkpoint"])+"\n").encode()+fixture["wire"].encode()
command({"op":"watch","id":1,"tables":["rooms","moves"]})
command({"op":"watch","id":2,"tables":["_badge_move_outbox"]})
canonical_watch=False
for iterations in range(1000):
    buffer=lib.psb_poll(ctx,16,6)
    try:status=json.loads(c.string_at(buffer.ptr,buffer.len))
    finally:lib.psb_buffer_free(buffer)
    assert not status["error"],status
    if iterations==0:
        assert status["changed_watches"]==[1,2],"SDK watch did not emit initial snapshots"
    if status["credentials_needed"]:command({"op":"credentials","token":"fixture-only"})
    for request in status["requests"]:
        requests+=1;id=request["id"]
        command({"op":"headers","id":id,"status":200,"content_type":"application/x-ndjson" if request["url"].endswith("sync/stream") else "application/json"})
        if request["url"].endswith("sync/stream"):
            body=json.loads(request["body"])
            assert body["streams"]["include_defaults"] is False
            assert body["streams"]["subscriptions"][0]["stream"]=="badge_game_v2"
            streams[id]=wire
        else:
            assert request["url"].endswith("sync/checkpoint-request"),request
            request_data=json.loads(request["body"])
            data=json.dumps({"data":{"checkpoint_request_id":request_data["checkpoint_request_id"]}}).encode()
            chunk(id,data);command({"op":"end","id":id})
    for id in status["can_feed"]:
        if id in streams and streams[id]:
            chunk(id,streams[id][:2048]);streams[id]=streams[id][2048:]
    if status["full_generation"]:
        canonical_watch=1 in status["changed_watches"]
        assert scalar("SELECT count(*) FROM moves")==49
        assert scalar("SELECT count(*) FROM rooms")==1
        assert scalar("SELECT count(*) FROM ps_crud")==0
        break
else:raise AssertionError("bridge fixture did not publish")
assert canonical_watch,"SDK publication did not invalidate raw-table watch"

# IDs and filters are bounded at the native boundary, and removing a watcher
# drops its SDK subscription rather than leaving a background task behind.
for value in (
    {"op":"watch","id":0,"tables":["rooms"]},
    {"op":"watch","id":2**32,"tables":["rooms"]},
    {"op":"watch","id":2,"tables":["rooms"]},
    {"op":"watch","id":3,"tables":[]},
    {"op":"watch","id":3,"tables":["a","b","c","d","e"]},
    {"op":"watch","id":3,"tables":["x"*65]},
    {"op":"watch","id":3,"tables":["bad-name"]},
    {"op":"watch","id":3,"tables":["rooms","rooms"]},
    {"op":"unwatch","id":0},
):rejected_command(value)
command({"op":"watch","id":3,"tables":["rooms"]})
command({"op":"watch","id":4,"tables":["moves"]})
rejected_command({"op":"watch","id":5,"tables":["rooms"]})
command({"op":"unwatch","id":3})
command({"op":"watch","id":5,"tables":["rooms"]})
command({"op":"unwatch","id":4})
command({"op":"unwatch","id":5})

def watch_poll():
    buffer=lib.psb_poll(ctx,0,6)
    try:return json.loads(c.string_at(buffer.ptr,buffer.len))
    finally:lib.psb_buffer_free(buffer)

watch_poll()
# A rolled-back local command neither creates a pending row nor a dirty watch.
execute("BEGIN IMMEDIATE;INSERT INTO _badge_move_outbox(id,room_id,orientation,row_i,col_i) VALUES('watch-rollback','%s','h',0,0);ROLLBACK" % fixture["room"])
assert scalar("SELECT count(*) FROM _badge_move_outbox")==0
assert scalar("SELECT count(*) FROM ps_crud")==0
assert 2 not in watch_poll()["changed_watches"],"Rollback emitted a committed table invalidation"
# Two committed updates before polling coalesce into one dirty watch ID.
execute("UPDATE rooms SET n=n")
execute("UPDATE rooms SET n=n")
assert watch_poll()["changed_watches"]==[1]
assert watch_poll()["changed_watches"]==[]
command({"op":"unwatch","id":1})
execute("UPDATE rooms SET n=n")
assert watch_poll()["changed_watches"]==[],"Unwatch retained its listener"

# These are actual PowerSync CRUD entries, not a mocked callback queue. The
# ordinary SDK table retains a readable optimistic row until reconciliation.
def queue_move(move_id):
    execute("BEGIN IMMEDIATE;INSERT INTO _badge_move_outbox(id,room_id,orientation,row_i,col_i) VALUES('%s','%s','h',0,0);COMMIT" % (move_id,fixture["room"]))
    assert scalar("SELECT count(*) FROM ps_crud")==1
    assert scalar("SELECT count(*) FROM _badge_move_outbox WHERE id='%s' AND room_id='%s' AND orientation='h' AND row_i=0 AND col_i=0" % (move_id,fixture["room"]))==1

upload_poll_count=0
watched_upload_changes=[]
def drive(elapsed=16,credentials_error=False,reply_credentials=True):
    global upload_poll_count,requests
    upload_poll_count+=1
    buffer=lib.psb_poll(ctx,elapsed,6)
    try:state=json.loads(c.string_at(buffer.ptr,buffer.len))
    finally:lib.psb_buffer_free(buffer)
    watched_upload_changes.extend(state["changed_watches"])
    if state["credentials_needed"] and reply_credentials:
        command({"op":"credentials_error"} if credentials_error else {"op":"credentials","token":"fixture-only"})
    for request in state["requests"]:
        if request["id"] in state["close"]:continue
        requests+=1;identity=request["id"]
        streaming=request["url"].endswith("sync/stream")
        command({"op":"headers","id":identity,"status":200,"content_type":"application/x-ndjson" if streaming else "application/json"})
        if not streaming:
            request_data=json.loads(request["body"])
            data=json.dumps({"data":{"checkpoint_request_id":request_data["checkpoint_request_id"]}}).encode()
            chunk(identity,data);command({"op":"end","id":identity})
    return state

def wait_upload():
    for _ in range(100):
        state=drive()
        if state["upload"]:return state["upload"]
    raise AssertionError("SDK upload actor did not publish its CRUD mailbox")

move_a="550e8400-e29b-41d4-a716-446655440101"
queue_move(move_a)
first=wait_upload()
assert 2 in watched_upload_changes,"SDK ordinary-table INSERT did not invalidate watch"
assert first["move"]=={"id":move_a,"room_id":fixture["room"],"orientation":"h","row":0,"col":0}
command({"op":"upload","id":first["id"],"result":"retry"})
for _ in range(4):state=drive(0)
assert scalar("SELECT count(*) FROM ps_crud")==1
assert state["upload_error"],"SDK upload retry error was not exposed to the UI"
assert first["id"] in state["upload_close"] or state["upload"] is None
state=drive(4999)
assert state["upload"] is None,"SDK ignored its configured retry delay"
state=drive(1)
second=state["upload"] or wait_upload()
assert second["id"]!=first["id"] and second["entry_id"]==first["entry_id"]
assert second["move"]==first["move"],"retry changed the immutable move identity"
command({"op":"upload","id":second["id"],"result":"success"})
for _ in range(100):
    drive()
    if scalar("SELECT count(*) FROM ps_crud")==0:break
else:raise AssertionError("SDK did not complete acknowledged CRUD transaction")
assert scalar("SELECT count(*) FROM _badge_move_outbox WHERE id='%s'" % move_a)==1,"HTTP acceptance incorrectly hid optimistic row"
# A covering SDK checkpoint, not Python cleanup, reconciles the optimistic row.
for _ in range(5):drive()
covering=json.loads(json.dumps(fixture["checkpoint"]))
covering["checkpoint"]["write_checkpoint"]="10000"
covering_wire=(json.dumps(covering)+"\n"+json.dumps({"checkpoint_complete":{"last_op_id":"50"}})+"\n").encode()
stream_id=next(iter(streams))
chunk(stream_id,covering_wire)
watched_upload_changes.clear()
for _ in range(100):
    state=drive()
    if scalar("SELECT count(*) FROM _badge_move_outbox WHERE id='%s'" % move_a)==0:break
else:raise AssertionError("covering full checkpoint did not reconcile ordinary SDK command row")
assert 2 in watched_upload_changes,"SDK reconciliation did not invalidate ordinary-table watch"

move_b="550e8400-e29b-41d4-a716-446655440102"
queue_move(move_b)
rejected=wait_upload()
command({"op":"upload","id":rejected["id"],"result":"rejected"})
for _ in range(100):
    drive()
    if scalar("SELECT count(*) FROM ps_crud")==0:break
else:raise AssertionError("permanent rejection did not retire its processed CRUD")

move_c="550e8400-e29b-41d4-a716-446655440103"
queue_move(move_c)
canceled=wait_upload()
for _ in range(100):
    if lib.psb_stop(ctx):break
else:raise AssertionError("SDK did not stop")
assert watch_poll()["changed_watches"]==[],"Stopped SDK retained a watch"
rejected_command({"op":"watch","id":6,"tables":["rooms"]})
assert lib.psb_destroy(ctx)==1
assert lib.bridge_host_scalar(db,b"SELECT count(*) FROM moves")==49,"borrowed native owner was closed"
assert lib.bridge_host_scalar(db,b"SELECT count(*) FROM ps_crud")==1,"cancellation acknowledged an unsent move"
# A fresh SDK may attach to the same still-open Store after room/pairing exit.
ctx=lib.psb_start(db,schema,len(schema),endpoint,len(endpoint),room,len(room));assert ctx
resumed=wait_upload()
assert resumed["move"]==canceled["move"] and resumed["entry_id"]==canceled["entry_id"]
command({"op":"upload","id":resumed["id"],"result":"success"})
for _ in range(100):
    drive()
    if scalar("SELECT count(*) FROM ps_crud")==0:break
else:raise AssertionError("restart did not complete retained CRUD")
for _ in range(100):
    if lib.psb_stop(ctx):break
else:raise AssertionError("SDK immediate restart/stop did not complete")
assert lib.psb_destroy(ctx)==1
assert lib.bridge_host_scalar(db,b"SELECT count(*) FROM moves")==49
# Cancel before the first SDK poll, including before connector initialization.
ctx=lib.psb_start(db,schema,len(schema),endpoint,len(endpoint),room,len(room));assert ctx
for _ in range(100):
    if lib.psb_stop(ctx):break
else:raise AssertionError("immediate cancellation did not complete")
assert lib.psb_destroy(ctx)==1
# Credential transport errors are returned into the SDK future too. Native
# download/checkpoint actors own when to request credentials again.
ctx=lib.psb_start(db,schema,len(schema),endpoint,len(endpoint),room,len(room));assert ctx
for _ in range(100):
    state=drive(0,reply_credentials=False)
    if state["credentials_needed"]:break
else:raise AssertionError("fresh SDK did not fetch credentials")
command({"op":"credentials_error"})
for _ in range(4):state=drive(0,reply_credentials=False)
assert state["error"],"credential callback error did not reach native status"
for _ in range(100):
    if state["credentials_needed"]:break
    state=drive(100,reply_credentials=False)
else:raise AssertionError("SDK did not retry failed credential callback")
command({"op":"credentials","token":"fixture-only"})
for _ in range(100):
    state=drive()
    if state["requests"]:break
else:raise AssertionError("fresh credentials did not resume native HTTP")
for _ in range(100):
    if lib.psb_stop(ctx):break
else:raise AssertionError("credential retry cancellation did not complete")
assert lib.psb_destroy(ctx)==1
assert lib.sqlite3_close(db)==0
result={"checks":["real no_std SDK C ABI","credential mailbox","SDK-authored requests","bounded base64 HTTP mailbox","full publication","49 moves/room/empty CRUD","SQL lease guard","stop joins actors","borrowed owner remains alive","restart with same native connection","immediate cancellation before first poll","actual ordinary SDK CRUD queue with readable optimistic row","native SDK upload callback mailbox","SDK five-second upload retry","same CRUD entry and move UUID across retry","CrudTransaction.complete on success","processed permanent rejection completes CRUD","canceled upload retains CRUD","restart resumes same unsent CRUD","native credential error propagation and retry","SDK watch initial snapshot","SDK raw-table canonical publication invalidation","bounded watch IDs and table filters","ordinary SDK mutation table readable after local INSERT","SDK watch local ordinary-table INSERT invalidation","no watch invalidation after rollback","SDK watch coalesces multiple commits","SDK unwatch removes listener","HTTP acceptance retains ordinary SDK optimistic row","covering full checkpoint reconciles ordinary SDK command row","SDK reconciliation invalidates ordinary-table watch"],"iterations":iterations+1,"upload_polls":upload_poll_count,"http_requests":requests,"full_generation":status["full_generation"]}
(HERE.parent/"evidence/host-bridge.json").write_text(json.dumps(result,indent=2)+"\n")
print(json.dumps(result,indent=2))
