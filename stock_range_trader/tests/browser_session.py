"""Opt-in POSIX Chromium harness: local files, private pipe, explicit shutdown.

No WebDriver server, dependency download, real profile, or DOM success shortcut.
Normal success requires Browser.close, exit 0, and an empty process group.
"""

import json
import os
import select
import signal
import subprocess
import sys
import time
from contextlib import contextmanager

FLAGS = (
    "--headless",
    "--disable-gpu",
    "--disable-background-networking",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-extensions",
    "--disable-component-update",
    "--disable-default-apps",
    "--no-proxy-server",
    "--host-resolver-rules=MAP * ~NOTFOUND",
    "--disable-crashpad-for-testing",
    "--remote-debugging-pipe",
)

# Run AFTER subprocess closes unlisted descriptors, not in preexec_fn. Duplicate
# above fd 4 first so arbitrary parent descriptor allocations cannot collide.
PIPE_EXEC = """import fcntl,os,sys
a=fcntl.fcntl(int(sys.argv[1]),fcntl.F_DUPFD,10)
b=fcntl.fcntl(int(sys.argv[2]),fcntl.F_DUPFD,10)
os.dup2(a,3);os.dup2(b,4)
for fd in {a,b,int(sys.argv[1]),int(sys.argv[2])}-{3,4}: os.close(fd)
os.execv(sys.argv[3],sys.argv[3:])
"""


class BrowserFailure(AssertionError):
    pass


class BrowserSession:
    def __init__(self, executable, directory):
        self.directory = directory
        self.command = [str(executable), *FLAGS]
        if sys.platform == "darwin":
            self.command.append("--use-mock-keychain")
        self.command += ["--user-data-dir=" + str(directory / "profile"), "about:blank"]
        self.process = None
        self.handles = []
        self.fds = []
        self.sequence = 0
        self.buffer = b""
        self.session = None
        self.started = time.monotonic()
        self.deadline = self.started + 40
        self.report = dict(
            options=self.command, stages=[], passed=False, forced_cleanup=False
        )
        self.stage("startup")

    def stage(self, name):
        self.phase = name
        self.phase_deadline = min(self.deadline, time.monotonic() + 10)
        self.report["stages"].append(
            dict(name=name, seconds=round(time.monotonic() - self.started, 3))
        )

    def remaining(self):
        seconds = self.phase_deadline - time.monotonic()
        if seconds <= 0:
            raise BrowserFailure("browser timeout: " + self.phase)
        return seconds

    def start(self):
        self.directory.mkdir()
        read_in, self.write_in = os.pipe()
        self.read_out, write_out = os.pipe()
        self.fds.extend((read_in, self.write_in, self.read_out, write_out))
        self.handles = [
            (self.directory / name).open("wb") for name in ("stdout.log", "stderr.log")
        ]
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                PIPE_EXEC,
                str(read_in),
                str(write_out),
                *self.command,
            ],
            pass_fds=(read_in, write_out),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=self.handles[0],
            stderr=self.handles[1],
        )
        for fd in (read_in, write_out):
            os.close(fd)
            self.fds.remove(fd)
        self.report["pid"] = self.process.pid
        self.report["version"] = self.call("Browser.getVersion")
        target = self.call("Target.createTarget", dict(url="about:blank"))["targetId"]
        self.session = self.call(
            "Target.attachToTarget", dict(targetId=target, flatten=True)
        )["sessionId"]
        self.call("Page.enable", page=True)
        self.call("Network.enable", page=True)
        self.call(
            "Network.setBlockedURLs",
            dict(urls=["http://*", "https://*", "ws://*", "wss://*"]),
            page=True,
        )
        self.call(
            "Emulation.setDeviceMetricsOverride",
            dict(width=1280, height=900, deviceScaleFactor=1, mobile=False),
            page=True,
        )

    def call(self, method, params=None, *, page=False):
        self.sequence += 1
        request = dict(id=self.sequence, method=method, params=params or {})
        if page:
            request["sessionId"] = self.session
        self.remaining()
        os.write(self.write_in, json.dumps(request).encode() + b"\0")
        while True:
            while b"\0" in self.buffer:
                raw, self.buffer = self.buffer.split(b"\0", 1)
                message = json.loads(raw)
                if message.get("id") == self.sequence:
                    if "error" in message:
                        raise BrowserFailure("CDP error: " + self.phase + ": " + method)
                    return message["result"]
            if not select.select([self.read_out], [], [], self.remaining())[0]:
                raise BrowserFailure("browser timeout: " + self.phase + ": " + method)
            chunk = os.read(self.read_out, 65536)
            if not chunk:
                raise BrowserFailure(
                    "browser pipe closed: " + self.phase + ": " + method
                )
            self.buffer += chunk

    def evaluate(self, expression):
        result = self.call(
            "Runtime.evaluate",
            dict(expression=expression, returnByValue=True, awaitPromise=True),
            page=True,
        )
        if "exceptionDetails" in result:
            raise BrowserFailure("JavaScript assertion/error: " + self.phase)
        return result["result"].get("value")

    def load(self, url, phase):
        if url != "about:blank" and not url.startswith("file:///"):
            raise BrowserFailure("local page required")
        self.stage(phase)
        result = self.call("Page.navigate", dict(url=url), page=True)
        if "errorText" in result:
            raise BrowserFailure("page navigation failed: " + phase)
        while not self.evaluate(
            "location.href === "
            + json.dumps(url)
            + " && document.readyState === 'complete'"
        ):
            time.sleep(min(0.02, self.remaining()))

    def click(self, selector):
        point = self.evaluate(
            """(async () => {
          const e=document.querySelector("""
            + json.dumps(selector)
            + """);
          if(!e) throw new Error('missing control');
          e.scrollIntoView({block:'center',inline:'center'});
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          const r=e.getBoundingClientRect();
          const x=(Math.max(0,r.left)+Math.min(innerWidth,r.right))/2;
          const y=(Math.max(0,r.top)+Math.min(innerHeight,r.bottom))/2;
          if(!e.contains(document.elementFromPoint(x,y))) throw new Error('control obscured');
          return {x,y};
        })()"""
        )
        for event in ("mousePressed", "mouseReleased"):
            self.call(
                "Input.dispatchMouseEvent",
                dict(type=event, button="left", clickCount=1, **point),
                page=True,
            )
        self.evaluate("new Promise(resolve=>requestAnimationFrame(()=>resolve(true)))")

    def key(self, key, code, virtual_key, *, modifiers=0):
        for event in ("keyDown", "keyUp"):
            self.call(
                "Input.dispatchKeyEvent",
                dict(
                    type=event,
                    key=key,
                    code=code,
                    windowsVirtualKeyCode=virtual_key,
                    modifiers=modifiers,
                ),
                page=True,
            )

    def character(self, text):
        assert len(text) == 1 and text.isascii() and text.isalnum()
        code = ("Digit" if text.isdigit() else "Key") + text.upper()
        for event in ("keyDown", "keyUp"):
            params = dict(
                type=event, key=text, code=code, windowsVirtualKeyCode=ord(text.upper())
            )
            if event == "keyDown":
                params["text"] = text
            self.call("Input.dispatchKeyEvent", params, page=True)

    def group_alive(self):
        if self.process is None:
            return False
        self.process.poll()  # Reap an exited parent before inspecting its group.
        # macOS may return EPERM to killpg(..., 0) while sandboxed helpers exit.
        # Read the process table instead; do not infer absence from permissions.
        rows = subprocess.check_output(
            ["ps", "-axo", "pid=,pgid="], text=True, timeout=2
        )
        return any(row.split()[1] == str(self.process.pid) for row in rows.splitlines())

    def signal_group(self, signum):
        try:
            os.killpg(self.process.pid, signum)
        except (ProcessLookupError, PermissionError):
            if self.group_alive():
                raise

    def close(self):
        self.stage("shutdown")
        self.call("Browser.close")
        try:
            code = self.process.wait(timeout=self.remaining())
        except subprocess.TimeoutExpired as exc:
            raise BrowserFailure("browser timeout: shutdown") from exc
        if code != 0:
            raise BrowserFailure("browser nonzero exit: " + str(code))
        while self.group_alive():
            time.sleep(min(0.02, self.remaining()))
        self.report.update(passed=True, returncode=code, remaining_process_group=False)

    def finish(self, error):
        if error is not None:
            self.report.update(
                passed=False, failure_phase=self.phase, error_type=type(error).__name__
            )
        try:
            if self.group_alive():
                self.report["forced_cleanup"] = True
                self.signal_group(signal.SIGTERM)
                until = time.monotonic() + 2
                while self.group_alive() and time.monotonic() < until:
                    self.process.poll()
                    time.sleep(0.02)
                if self.group_alive():
                    self.signal_group(signal.SIGKILL)
            if self.process is not None:
                self.report["returncode"] = self.process.wait(timeout=5)
                until = time.monotonic() + 2
                while self.group_alive() and time.monotonic() < until:
                    time.sleep(0.02)
                self.report["remaining_process_group"] = self.group_alive()
        finally:
            for fd in self.fds:
                os.close(fd)
            for handle in self.handles:
                handle.close()
            self.report["seconds"] = round(time.monotonic() - self.started, 3)
            if self.report["forced_cleanup"] or self.report.get(
                "remaining_process_group"
            ):
                self.report["passed"] = False
            if self.directory.exists():
                (self.directory / "lifecycle.json").write_text(
                    json.dumps(self.report, indent=2)
                )


@contextmanager
def browser_session(executable, directory):
    browser = BrowserSession(executable, directory)
    error = None
    try:
        browser.start()
        yield browser
        browser.close()
    except BaseException as exc:
        error = exc
        raise
    finally:
        browser.finish(error)
    if not browser.report["passed"]:
        raise BrowserFailure("browser lifecycle did not complete normally")
