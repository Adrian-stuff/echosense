"""Parent route and training response regression checks without sensor hardware."""
import asyncio
import json
from types import SimpleNamespace

from echosense.app import EchoSense
from echosense.device.monitors import SessionLogger
from echosense.device.training import Trainer
from echosense.server import ConsoleServer


def test_parent_page_preserves_console_route(tmp_path):
    server = ConsoleServer(SimpleNamespace(), "127.0.0.1", 8080, auth_path=tmp_path / "accounts.sqlite3")
    parent = asyncio.run(server.parents(None))
    console = asyncio.run(server.index(None))
    assert parent.status == 200
    assert "Parent space" in parent.text
    assert "/static/parent.js?v=" in parent.text
    assert "Parent space" in console.text
    assert "/static/app.js?v=" in console.text
    assert parent.headers["Cache-Control"] == "no-cache"


def test_training_response_is_logged_and_broadcast(tmp_path):
    logger = SessionLogger(tmp_path)
    logger.start({})
    events = []
    trainer = Trainer()
    trainer.start()
    trainer.set_target(az=30, kind="head")
    app = SimpleNamespace(trainer=trainer, logger=logger,
                          event=lambda *args, **kwargs: events.append((args, kwargs)))
    EchoSense.training_command(app, {"cmd": "respond", "az": 0})
    logger.stop()
    records = [json.loads(line) for line in logger.path.read_text().splitlines()]
    trial = next(r for r in records if r["type"] == "training_trial")
    assert trial["obstacle_kind"] == "head"
    assert trial["error_deg"] == 30
    assert events[0][1]["trial"]["kind"] == "head"
    assert events[0][1]["trial"]["error_deg"] == 30
