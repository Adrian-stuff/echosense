"""Training audio lease and persisted, idempotent response behavior."""
from types import SimpleNamespace
import json
import sqlite3

import pytest

from echosense.app import EchoSense
from echosense.auth import AuthStore
from echosense.config import load_config
from echosense.device.settings import SettingsStore
from echosense.device.supervision import SupervisedTraining
from echosense.device.training import Trainer
from echosense.processing.pipeline import DepthProcessor

OWNER = {"user_id": 1, "username": "parent", "role": "owner"}
CAREGIVER = {"user_id": 2, "username": "helper", "role": "trainer"}
OTHER = {"user_id": 3, "username": "other", "role": "trainer"}


class TrainingDevice:
    def __init__(self, tmp_path):
        self.trainer = Trainer()
        self.events = []
        self.logger = SimpleNamespace(write=lambda *a, **k: None)
        self.settings = SettingsStore(tmp_path / "settings.json")
        self.cfg = load_config()
        self.processor = DepthProcessor(self.cfg)
        self.sim = None
        self._overhang_default = self.cfg["processing"]["overhang_min_height"]
    def action(self, name, origin="console"):
        assert name == "training"
        self.trainer.stop() if self.trainer.active else self.trainer.start()
    def event(self, *args, **kwargs):
        self.events.append((args, kwargs))
    training_command = EchoSense.training_command
    configure_fitting = EchoSense.configure_fitting
    _apply_mount = EchoSense._apply_mount
    coverage = EchoSense.coverage
    def sensor_spec(self):
        return self.cfg["sensor"]
    def hello(self):
        return {"type": "hello"}
    def set_rgb_viewers(self, count):
        pass


@pytest.fixture
def setup(tmp_path):
    device = TrainingDevice(tmp_path)
    store = AuthStore(tmp_path / "accounts.sqlite3")
    return device, store, SupervisedTraining(device, store)


def test_lease_pause_resume_and_persisted_response_dedup(setup, monkeypatch):
    device, store, training = setup
    clock = [100.0]
    monkeypatch.setattr("echosense.device.supervision.time.monotonic", lambda: clock[0])
    started = training.handle("tab1", CAREGIVER, {"cmd": "start", "request_id": "start-1"})
    training.handle("tab1", CAREGIVER, {"cmd": "target", "trial_id": "trial-1", "az": 30, "r": 1.0, "kind": "head"})
    assert device.trainer.cues()
    with pytest.raises(PermissionError):
        training.handle("tab2", OTHER, {"cmd": "target", "az": 0})
    with pytest.raises(PermissionError):
        training.handle("tab2", CAREGIVER, {"cmd": "target", "az": 0})
    clock[0] += 11
    assert training.tick()
    assert not device.trainer.cues()
    assert training.snapshot()["status"] == "paused"
    with pytest.raises(PermissionError):
        training.handle("tab1", CAREGIVER, {"cmd": "heartbeat"})
    resumed = training.handle("tab2", CAREGIVER, {"cmd": "resume", "session_id": started["id"]})
    assert resumed["trial_id"] == "trial-1"
    answer = {"cmd": "respond", "trial_id": "trial-1", "response_id": "response-1", "az": 0}
    result = training.handle("tab2", CAREGIVER, answer)
    assert result["trials"] == 1 and result["error"] == 30
    repeated = training.handle("tab2", CAREGIVER, answer)
    assert repeated["results"] == result["results"]
    assert len(device.trainer.trials) == 1
    with pytest.raises(ValueError):
        training.handle("tab2", CAREGIVER, {**answer, "az": 60})
    finished = training.handle("tab2", CAREGIVER, {"cmd": "finish", "note": "Needed a break", "session_id": started["id"]})
    assert finished["status"] == "completed" and finished["trials"] == 1
    assert finished["note"] == "Needed a break" and not device.trainer.active
    assert training.handle("tab2", CAREGIVER, {"cmd": "finish"})["trials"] == 1
    restarted = SupervisedTraining(TrainingDevice(store.path.parent), store)
    assert restarted.history()[0]["results"][0]["response_id"] == "response-1"


def test_disconnect_stops_virtual_sound_and_requires_explicit_resume(setup):
    device, store, training = setup
    training.handle("tab1", OWNER, {"cmd": "start"})
    assert not training.disconnected("another-tab")
    assert device.trainer.active
    assert training.disconnected("tab1")
    assert not device.trainer.active
    assert training.snapshot()["status"] == "paused"
    with pytest.raises(ValueError):
        training.handle("tab2", OWNER, {"cmd": "start"})
    training.handle("tab2", OWNER, {"cmd": "resume"})
    assert device.trainer.active
    restarted = SupervisedTraining(TrainingDevice(store.path.parent), store)
    assert restarted.snapshot()["status"] == "paused"
    assert not restarted.app.trainer.active


def test_finish_is_authoritative_even_when_response_ack_was_lost(setup):
    device, store, training = setup
    training.handle("tab1", OWNER, {"cmd": "start"})
    training.handle("tab1", OWNER, {"cmd": "target", "trial_id": "t1", "az": -30})
    training.handle("tab1", OWNER, {"cmd": "respond", "response_id": "r1", "trial_id": "t1", "az": 0})
    finished = training.handle("tab1", OWNER, {"cmd": "finish"})
    assert finished["results"][0]["error_deg"] == 30
    assert finished["trials"] == 1
    # A delayed response retry after Finish must not create a second result.
    assert training.handle("tab1", OWNER, {"cmd": "respond", "response_id": "r1", "trial_id": "t1", "az": 0})["trials"] == 1


def test_child_fitting_updates_processing_audio_and_geometry(tmp_path):
    device = TrainingDevice(tmp_path)
    original = device.coverage()
    device.configure_fitting({"head_height": 1.1, "ear_height": 1.0, "mount_height": 0.8,
                              "mount_pitch": -10, "fitting_checked": True})
    assert device.cfg["audio"]["ear_height"] == 1.0
    assert device.cfg["processing"]["user_height"] == 1.1
    assert device.cfg["processing"]["overhang_min_height"] == 0.9
    assert device.processor.mount_height == 0.8
    assert device.coverage()["head_height"] == 1.1
    assert device.coverage()["floor_from"] < original["floor_from"]
    stored = SettingsStore(tmp_path / "settings.json")
    assert stored.s.head_height == 1.1 and stored.s.fitting_checked
    with pytest.raises(ValueError):
        device.configure_fitting({"head_height": 1.1, "ear_height": 1.2, "mount_height": 0.8,
                                  "mount_pitch": -10, "fitting_checked": True})
    assert stored.to_dict() == SettingsStore(tmp_path / "settings.json").to_dict()


def test_active_duration_is_checkpointed_for_restart(setup, monkeypatch):
    device, store, training = setup
    clock = [100.0]
    monkeypatch.setattr("echosense.device.supervision.time.monotonic", lambda: clock[0])
    training.handle("tab1", OWNER, {"cmd": "start"})
    clock[0] += 8
    training.handle("tab1", OWNER, {"cmd": "heartbeat"})
    restarted = SupervisedTraining(TrainingDevice(store.path.parent), store)
    assert restarted.history()[0]["seconds"] == 8
    assert restarted.history()[0]["status"] == "paused"


def test_failed_result_persistence_pauses_audio_and_can_be_retried(setup, monkeypatch):
    from contextlib import contextmanager
    device, store, training = setup
    training.handle("tab1", OWNER, {"cmd": "start"})
    training.handle("tab1", OWNER, {"cmd": "target", "trial_id": "t1", "az": 30})
    original = store.connect
    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError("disk full")
        yield
    monkeypatch.setattr(store, "connect", unavailable)
    answer = {"cmd": "respond", "trial_id": "t1", "response_id": "r1", "az": 0}
    with pytest.raises(sqlite3.OperationalError):
        training.handle("tab1", OWNER, answer)
    assert not device.trainer.active
    monkeypatch.setattr(store, "connect", original)
    result = training.handle("tab1", OWNER, answer)
    assert result["trials"] == 1 and result["status"] == "paused"
    restarted = SupervisedTraining(TrainingDevice(store.path.parent), store)
    assert restarted.history()[0]["results"][0]["response_id"] == "r1"
