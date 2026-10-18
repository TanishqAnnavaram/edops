"""Shared test helpers: bounded polling, real subprocesses, and a per-test broker namespace."""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pika

from app import broker, relay
from app.db import get_conn

ROOT = Path(__file__).resolve().parent.parent
POLL_INTERVAL = 0.05


def wait_until(predicate, timeout: float, message: str, on_timeout=None):
    """Poll predicate until it returns a truthy value; fail with message after timeout seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            detail = f"\n{on_timeout()}" if on_timeout else ""
            raise AssertionError(f"timed out after {timeout}s: {message}{detail}")
        time.sleep(POLL_INTERVAL)


def scalar(sql: str, params=()):
    """One value from a fresh connection, so the read sees every other process's commits."""
    with get_conn() as c:
        return c.execute(sql, params).fetchone()[0]


class Proc:
    def __init__(self, module: str, log_path: Path, env: dict[str, str]):
        self.module = module
        self.log_path = log_path
        self._log_file = open(log_path, "w")
        self.popen = subprocess.Popen(
            [sys.executable, "-m", module],
            cwd=ROOT,
            env=os.environ | {"PYTHONUNBUFFERED": "1"} | env,
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )

    def logs(self) -> str:
        return self.log_path.read_text()

    def json_lines(self) -> list[dict]:
        lines = []
        for line in self.logs().splitlines():
            if line.startswith("{"):
                lines.append(json.loads(line))
        return lines

    def wait_for_log(self, text: str, timeout: float = 15) -> None:
        wait_until(
            lambda: text in self.logs(),
            timeout,
            f"{self.module} never logged {text!r}",
            on_timeout=lambda: self.tail(),
        )

    def wait_exit(self, timeout: float = 15) -> int:
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired as e:
            raise AssertionError(
                f"{self.module} did not exit within {timeout}s\n{self.tail()}"
            ) from e

    def kill(self) -> None:
        # SIGKILL on Linux, TerminateProcess on Windows: no cleanup code runs in the child.
        if self.popen.poll() is None:
            self.popen.kill()
        self.popen.wait(10)

    def tail(self, lines: int = 30) -> str:
        text = "\n".join(self.logs().splitlines()[-lines:])
        return f"--- last log lines of {self.module} ---\n{text}"

    def close(self) -> None:
        self.kill()
        self._log_file.close()


class Procs:
    """Starts app processes for one test and always kills them at teardown."""

    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.started: list[Proc] = []

    def start(self, module: str, ready: str | None = None, **env: str) -> Proc:
        name = f"{len(self.started):02d}-{module.rsplit('.', 1)[-1]}.log"
        proc = Proc(module, self.log_dir / name, env)
        self.started.append(proc)
        if ready:
            proc.wait_for_log(ready)
        return proc

    def dump(self) -> str:
        return "\n".join(p.tail() for p in self.started)

    def all_json_lines(self) -> list[dict]:
        return [line for p in self.started for line in p.json_lines()]

    def close(self) -> None:
        for p in self.started:
            p.close()


class Bus:
    """The test's own exchange and queues (unique prefix), deleted at teardown."""

    def __init__(self):
        self.prefix = f"t{uuid.uuid4().hex[:10]}."
        self.connection: pika.BlockingConnection | None = None
        self.probes: list[str] = []

    def open(self) -> None:
        self.connection = broker.connect()
        self.channel = self.connection.channel()
        self.channel.confirm_delivery()
        broker.declare_topology(self.channel)

    def probe(self, routing_key: str = "#") -> str:
        """A queue that receives a copy of every matching event, for counting deliveries."""
        name = f"{self.prefix}probe{len(self.probes)}"
        self.channel.queue_declare(name, durable=True)
        self.channel.queue_bind(name, broker.exchange_name(), routing_key=routing_key)
        self.probes.append(name)
        return name

    def drain(self, queue: str) -> list[tuple[pika.BasicProperties, dict]]:
        messages = []
        while True:
            method, properties, body = self.channel.basic_get(queue, auto_ack=True)
            if method is None:
                return messages
            messages.append((properties, json.loads(body)))

    def depth(self, queue: str) -> int:
        return self.channel.queue_declare(queue, passive=True).method.message_count

    def publish(self, event_type: str, payload: dict, message_id: str | None = None) -> str:
        """Publish exactly as the relay does. Reusing a message_id simulates a redelivery."""
        message_id = message_id or str(uuid.uuid4())
        relay.publish(self.channel, message_id, event_type, payload)
        return message_id

    def close(self) -> None:
        if self.connection is None:
            return
        for queue in [broker.queue_name(q) for q in broker.QUEUES] + self.probes:
            self.channel.queue_delete(queue)
        self.channel.exchange_delete(broker.exchange_name())
        self.connection.close()
