from flask_socketio import SocketIO, emit, join_room
from flask import Flask, render_template, session

from Sequence import Runner
import concurrent.futures
import threading
import asyncio
import config
import uuid
import re

SECRET_KEY = config.web_secret_key
MAX_CONTENT_LENGTH = config.web_max_content_length

app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="gevent", max_http_buffer_size=MAX_CONTENT_LENGTH)

# ? Keep sequence work on its own normal asyncio loop, separate from Socket.IO requests
runner_loop = asyncio.new_event_loop()
runs = {}
runs_lock = threading.RLock()
run_id_pattern = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
ansi_pattern = re.compile(r"\x1B(?:[@-_][0-?]*[ -/]*[@-~]|\[[0-?]*[ -/]*[@-~])")


def run_asyncio_loop():
    asyncio.set_event_loop(runner_loop)
    runner_loop.run_forever()


runner_thread = threading.Thread(
    target=run_asyncio_loop,
    name="ImpaCtODE sequence runner",
    daemon=True,
)
runner_thread.start()


def get_owner_id():
    """Get the browser's temporary ID without using a database.
    """
    owner_id = session.get("impactode_web_owner")
    if not owner_id:
        owner_id = uuid.uuid4().hex
        session["impactode_web_owner"] = owner_id
    return owner_id


def get_run_id(value):
    if isinstance(value, str) and run_id_pattern.fullmatch(value):
        return value
    return None


def run_room(run_id):
    return f"sequence:{run_id}"


def make_snapshot(run):
    return {
        "run_id": run["run_id"],
        "code": run["code"],
        "status_messages": run["status_messages"].copy(),
        "predicted_steps": run["predicted_steps"],
        "environment_output": run["environment_output"],
        "guidance": run["guidance"],
        "state": run["state"],
        "ended": run["ended"],
        "error": run["error"],
    }


def publish_update(run_id, update):
    """Save one Runner update and send the current sequence panel to its tab.
    """
    with runs_lock:
        run = runs.get(run_id)
        if run is None:
            return

        update_type = update.get("type")
        if update_type == "started":
            run["state"] = "running"

        elif update_type == "environment":
            output = update.get("output", "")
            if isinstance(output, str):
                run["environment_output"] = ansi_pattern.sub("", output).replace("\x00", "")[-config.web_output_limit:]

        elif update_type == "guidance":
            guidance = update.get("guidance", "")
            if isinstance(guidance, str):
                run["guidance"] = guidance

        elif update_type == "status":
            run["predicted_steps"] += update.get("predicted_add", 0)
            message = update.get("message")
            replace_index = update.get("replace_index")
            if replace_index is not None and message is not None and replace_index < len(run["status_messages"]):
                run["status_messages"][replace_index] = message
            elif message:
                run["status_messages"].append(message)

            run["predicted_steps"] = max(run["predicted_steps"], len(run["status_messages"]))
            if update.get("end_sequence"):
                run["ended"] = True

        elif update_type == "finished":
            run["ended"] = True
            run["state"] = update.get("state", "completed")

        snapshot = make_snapshot(run)

    socketio.emit("sequence_update", snapshot, room=run_room(run_id))


def runner_failed(run_id, task):
    """Show an unexpected worker error instead of leaving a tab on running.
    """
    try:
        task.result()
    except concurrent.futures.CancelledError:
        with runs_lock:
            run = runs.get(run_id)
            if run is None or run["ended"]:
                return
            run["state"] = "aborted"
            run["ended"] = True
            snapshot = make_snapshot(run)

        socketio.emit("sequence_update", snapshot, room=run_room(run_id))
        return
    except Exception as error:
        with runs_lock:
            run = runs.get(run_id)
            if run is None:
                return
            run["error"] = f"Sequence worker failed: {error}"
            run["state"] = "error"
            run["ended"] = True
            snapshot = make_snapshot(run)

        socketio.emit("sequence_error", {"run_id": run_id, "message": snapshot["error"]}, room=run_room(run_id))
        socketio.emit("sequence_update", snapshot, room=run_room(run_id))


@app.route('/')
def index():
    """Redirect to main page
    """
    get_owner_id()
    return render_template('index.html')


@socketio.on("connect")
def connect():
    get_owner_id()
    emit("web_ready", {"max_code_length": MAX_CONTENT_LENGTH})


@socketio.on("subscribe_runs")
def subscribe_runs(payload):
    """Restore current active tabs after a browser refresh or reconnect.
    """
    run_ids = payload.get("run_ids", []) if isinstance(payload, dict) else []
    if not isinstance(run_ids, list):
        return {"error": "Invalid saved run list."}

    owner_id = get_owner_id()
    snapshots = []
    for value in run_ids:
        run_id = get_run_id(value)
        if not run_id:
            continue

        with runs_lock:
            run = runs.get(run_id)
            if run is None or run["owner_id"] != owner_id:
                emit("sequence_error", {
                    "run_id": run_id,
                    "message": "This active run is no longer available on the server.",
                })
                continue
            snapshot = make_snapshot(run)

        join_room(run_room(run_id))
        snapshots.append(snapshot)

    emit("sequence_snapshot", {"runs": snapshots})


@socketio.on("run_sequence")
def run_sequence(payload):
    """Start a sequence without blocking the WebSocket connection.
    """
    if not isinstance(payload, dict):
        return {"error": "Invalid run request."}

    run_id = get_run_id(payload.get("run_id"))
    code = payload.get("code")
    if not run_id:
        return {"error": "Invalid run ID."}
    if not isinstance(code, str) or len(code.strip()) <= 5:
        return {"error": "Please provide code or instructions to run."}
    if len(code.encode("utf-8")) > MAX_CONTENT_LENGTH:
        return {"error": "The submitted code is too large."}

    with runs_lock:
        if run_id in runs:
            return {"error": "This run ID already exists."}

        run = {
            "run_id": run_id,
            "owner_id": get_owner_id(),
            "code": code,
            "status_messages": [],
            "predicted_steps": len(config.main_sequence) + 7,
            "environment_output": "",
            "guidance": "",
            "state": "queued",
            "ended": False,
            "error": "",
            "task": None,
        }
        runs[run_id] = run
        snapshot = make_snapshot(run)

    join_room(run_room(run_id))
    emit("sequence_snapshot", {"runs": [snapshot]})

    def receive_runner_update(update):
        publish_update(run_id, update)

    task = asyncio.run_coroutine_threadsafe(
        Runner.running_sequence(code, receive_runner_update),
        runner_loop,
    )
    with runs_lock:
        runs[run_id]["task"] = task
    task.add_done_callback(lambda completed_task: runner_failed(run_id, completed_task))

    return snapshot


@socketio.on("abort_sequence")
def abort_sequence(payload):
    """Stop the current sequence belonging to this browser.
    """
    run_id = get_run_id(payload.get("run_id")) if isinstance(payload, dict) else None
    if not run_id:
        return {"error": "Invalid run ID."}

    with runs_lock:
        run = runs.get(run_id)
        if run is None or run["owner_id"] != get_owner_id():
            return {"error": "This run is no longer available."}

        task = run["task"]
        if task is None or task.done() or run["ended"]:
            return {"error": "This run is no longer active."}

        run["state"] = "aborting"
        snapshot = make_snapshot(run)

    socketio.emit("sequence_update", snapshot, room=run_room(run_id))
    task.cancel()
    emit("abort_requested", {"run_id": run_id})
    return snapshot


if __name__ == '__main__':
    try:
        socketio.run(app, host=config.web_listen_ip, port=config.web_listen_port, allow_unsafe_werkzeug=True)
    finally:
        runner_loop.call_soon_threadsafe(runner_loop.stop)
        runner_thread.join(timeout=3)
