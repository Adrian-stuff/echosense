"""Device-owned, persisted training with an explicitly supervised audio lease."""
from __future__ import annotations

import copy
import json
import math
import random
import re
import threading
import time
import uuid

SUPERVISION_TIMEOUT = 10.0


def identifier(value=None):
    value = value or uuid.uuid4().hex
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        raise ValueError("Invalid training request identifier.")
    return value


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"Enter a number between {low} and {high}.")
    return float(value)


class SupervisedTraining:
    def __init__(self, app, store, timeout=SUPERVISION_TIMEOUT):
        self.app, self.store, self.timeout = app, store, timeout
        self.lock = threading.RLock()
        self.client_id = None
        self.deadline = 0.0
        self.segment = None
        self.current = None
        with store.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS training_sessions(id TEXT PRIMARY KEY, created REAL, data TEXT)")
            rows = db.execute("SELECT data FROM training_sessions ORDER BY created DESC").fetchall()
            for row in rows:
                session = json.loads(row["data"])
                if session["status"] in ("active", "paused"):
                    session["status"] = "paused"
                    session["reason"] = "Device restarted. Resume explicitly when ready."
                    db.execute("UPDATE training_sessions SET data=? WHERE id=?", (json.dumps(session), session["id"]))
                    if self.current is None:
                        self.current = session

    def _save(self):
        # Checkpoint active time, so a restart loses at most time since the last heartbeat.
        if self.segment is not None and self.current["status"] == "active":
            now = time.monotonic()
            self.current["seconds"] += max(0, now - self.segment)
            self.segment = now
        try:
            with self.store.connect() as db:
                db.execute("INSERT OR REPLACE INTO training_sessions VALUES(?,?,?)",
                           (self.current["id"], self.current["created"], json.dumps(self.current, allow_nan=False)))
        except Exception:
            # An unpersisted result must not keep a virtual obstacle sounding without acknowledgement.
            self._stop_sound()
            if self.current["status"] == "active":
                self.current.update(status="paused", reason="Device storage failed. Practice paused; retry after storage recovers.")
            self.segment = None
            self.client_id = None
            raise

    def _public(self, session):
        result = copy.deepcopy(session)
        seconds = result["seconds"]
        if session is self.current and self.segment is not None:
            seconds += time.monotonic() - self.segment
        result["seconds"] = round(seconds, 2)
        errors = [trial["error_deg"] for trial in result["results"]]
        result["trials"] = len(errors)
        result["error"] = round(sum(errors) / len(errors), 2) if errors else None
        return result

    def snapshot(self):
        with self.lock:
            if not self.current:
                return None
            return {key: self.current.get(key) for key in
                    ("id", "status", "owner_id", "owner_name", "reason", "trial_id")}

    def history(self):
        with self.lock, self.store.connect() as db:
            return [self._public(self.current if self.current and self.current["id"] == row["id"]
                                  else json.loads(row["data"]))
                    for row in db.execute("SELECT id,data FROM training_sessions ORDER BY created DESC")]

    def _stop_sound(self):
        if getattr(getattr(self.app, "trainer", None), "active", False):
            self.app.training_command({"cmd": "stop"})

    def _pause(self, reason):
        if self.current and self.current["status"] == "active":
            self._stop_sound()
            if self.segment is not None:
                self.current["seconds"] += time.monotonic() - self.segment
            self.segment = None
            self.current.update(status="paused", reason=reason)
            self.client_id = None
            self._save()
            return True
        return False

    def disconnected(self, client_id):
        with self.lock:
            if self.client_id == client_id:
                return self._pause("Supervising connection ended. Obstacle cues restored; resume explicitly.")
        return False

    def tick(self):
        with self.lock:
            if not self.current or self.current["status"] != "active":
                return False
            if time.monotonic() >= self.deadline:
                return self._pause("Supervision timed out. Obstacle cues restored; resume explicitly.")
            if not getattr(getattr(self.app, "trainer", None), "active", False):
                return self._pause("Training stopped on the device. Resume explicitly when ready.")
        return False

    def handle(self, client_id, user, message):
        cmd = message.get("cmd")
        with self.lock:
            self.tick()
            if cmd == "start":
                request_id = identifier(message.get("request_id"))
                if self.current and self.current["status"] != "completed":
                    if self.current.get("start_request_id") == request_id and self.current["owner_id"] == user["user_id"]:
                        self._save()
                        return self._public(self.current)
                    raise ValueError("Finish the existing session, or resume it explicitly.")
                if getattr(getattr(self.app, "trainer", None), "active", False):
                    raise ValueError("Stop existing device training before starting supervised practice.")
                session = {"id": uuid.uuid4().hex, "created": time.time() * 1000,
                           "owner_id": user["user_id"], "owner_name": user["username"],
                           "status": "active", "reason": "", "seconds": 0.0,
                           "results": [], "note": "", "goalId": str(message.get("goal_id", ""))[:100],
                           "child": str(message.get("child", ""))[:60],
                           "start_request_id": request_id, "trial_id": None, "target": None}
                self.current = session
                self.client_id = client_id
                self.segment = time.monotonic()
                self.deadline = time.monotonic() + self.timeout
                self.app.training_command({"cmd": "start"})
                self._save()
                return self._public(session)

            session_id = message.get("session_id") or (self.current or {}).get("id")
            if not self.current or session_id != self.current["id"]:
                # Replayed Finish after a later session started still returns its persisted result.
                if cmd in ("stop", "finish", "respond") and session_id:
                    with self.store.connect() as db:
                        row = db.execute("SELECT data FROM training_sessions WHERE id=?", (session_id,)).fetchone()
                    prior = json.loads(row["data"]) if row else None
                    if prior and prior["owner_id"] == user["user_id"] and prior["status"] == "completed":
                        return self._public(prior)
                raise ValueError("No matching training session. Reload the practice history.")
            session = self.current
            if session["owner_id"] != user["user_id"] and user["role"] != "owner":
                raise PermissionError("This session belongs to another caregiver.")

            if cmd == "respond":
                response_id = identifier(message.get("response_id"))
                existing = next((r for r in session["results"] if r["response_id"] == response_id), None)
                if existing:
                    if message.get("az") != existing["response_az"] or message.get("trial_id", existing["trial_id"]) != existing["trial_id"]:
                        raise ValueError("This response identifier was already used for another answer.")
                    self._save()
                    return self._public(session)

            if cmd in ("stop", "finish"):
                if session["status"] != "completed":
                    self._pause("Practice finished.")
                    session["status"] = "completed"
                    session["completed"] = time.time() * 1000
                    session["note"] = str(message.get("note", session["note"]))[:2000]
                self._save()
                return self._public(session)
            if cmd == "note":
                session["note"] = str(message.get("note", ""))[:2000]
                self._save()
                return self._public(session)
            if cmd == "pause":
                if self.client_id not in (None, client_id) and user["role"] != "owner":
                    raise PermissionError("Another connection is supervising this session.")
                self._pause("Practice paused. Obstacle cues restored.")
                return self._public(session)
            if cmd == "resume":
                if session["status"] != "paused":
                    raise ValueError("Only paused sessions can be resumed.")
                if getattr(getattr(self.app, "trainer", None), "active", False):
                    raise ValueError("Stop device training before resuming this session.")
                session.update(owner_id=user["user_id"], owner_name=user["username"], status="active",
                               reason="")
                self.client_id = client_id
                self.segment = time.monotonic()
                self.deadline = time.monotonic() + self.timeout
                self.app.training_command({"cmd": "start"})
                if session["target"]:
                    self.app.training_command({"cmd": "target", **session["target"]})
                self._save()
                return self._public(session)
            if session["status"] != "active" or self.client_id != client_id:
                raise PermissionError("Resume practice on this connection before playing sounds or responding.")
            self.deadline = time.monotonic() + self.timeout
            if cmd == "heartbeat":
                self._save()
                return self._public(session)
            if cmd in ("target", "random"):
                trial_id = identifier(message.get("trial_id"))
                if trial_id == session["trial_id"]:
                    return self._public(session)
                if len(session["results"]) >= 1000:
                    raise ValueError("Finish this session before starting more trials.")
                target = {"az": number(message.get("az", random.choice((-60, -30, 0, 30, 60))), -90, 90),
                          "r": number(message.get("r", 1.5), 0.25, 4.0),
                          "kind": message.get("kind", "body")}
                if target["kind"] not in ("body", "head", "low", "drop"):
                    raise ValueError("Unknown training sound.")
                self.app.training_command({"cmd": "target", **target})
                session.update(trial_id=trial_id, target=target)
                self._save()
            elif cmd == "respond":
                trial_id = message.get("trial_id") or session["trial_id"]
                if not trial_id or trial_id != session["trial_id"]:
                    raise ValueError("This trial is no longer current. Play a new direction.")
                az = number(message.get("az"), -90, 90)
                if any(r["trial_id"] == trial_id for r in session["results"]):
                    raise ValueError("A response has already been saved for this trial.")
                trial = self.app.training_command({"cmd": "respond", "az": az})
                session["results"].append({**trial, "trial_id": trial_id, "response_id": response_id,
                                            "created": time.time() * 1000})
                self._save()
            else:
                raise ValueError("Unknown training command.")
            return self._public(session)
