"""Offline lifecycle regressions using an actual child with a fake CDP pipe.

These are not substitutes for the six opt-in real Chromium UI tests.
"""

import json
import os
import sys
import time

import pytest

from tests.browser_session import BrowserFailure, BrowserSession, browser_session

FAKE = r"""
import json,os,sys,time
mode=sys.argv[1]
buffer=b''
while True:
    while b'\0' not in buffer:
        piece=os.read(3,4096)
        if not piece:sys.exit(0)
        buffer+=piece
    raw,buffer=buffer.split(b'\0',1)
    req=json.loads(raw);method=req['method']
    if mode=='eof':sys.exit(7)
    if mode=='startup_timeout':time.sleep(30)
    if method=='Browser.getVersion':result={'product':'Fake/not-a-browser'}
    elif method=='Target.createTarget':result={'targetId':'target'}
    elif method=='Target.attachToTarget':result={'sessionId':'session'}
    elif method=='Runtime.evaluate':
        result={'exceptionDetails':{}} if mode=='javascript_error' else {'result':{'value':True}}
    else:result={}
    response={'id':req['id'],'result':result}
    if mode=='protocol_error':response={'id':req['id'],'error':{'code':-1}}
    payload=json.dumps({'method':'unrelated.event'}).encode()+b'\0'+json.dumps(response).encode()+b'\0'
    for offset in range(0,len(payload),3):os.write(4,payload[offset:offset+3])
    if method=='Browser.close':
        if mode=='shutdown_timeout':time.sleep(30)
        sys.exit(7 if mode=='nonzero' else 0)
"""


def fake(monkeypatch, mode):
    original = BrowserSession.__init__

    def init(self, executable, directory):
        original(self, executable, directory)
        self.command = [sys.executable, "-c", FAKE, mode]
        if mode == "startup_timeout":
            self.deadline = self.phase_deadline = time.monotonic() + 1

    monkeypatch.setattr(BrowserSession, "__init__", init)


def record(tmp_path):
    return json.loads((tmp_path / "browser/lifecycle.json").read_text())


def test_pipe_fragmentation_events_and_normal_close(tmp_path, monkeypatch):
    fake(monkeypatch, "normal")
    with browser_session(sys.executable, tmp_path / "browser") as ui:
        assert ui.report["version"]["product"] == "Fake/not-a-browser"
        assert ui.evaluate("true") is True
    report = record(tmp_path)
    assert report["passed"] and report["returncode"] == 0
    assert not report["forced_cleanup"] and not report["remaining_process_group"]
    assert not ui.group_alive()
    for fd in ui.fds:
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.parametrize(
    "mode, phase, match",
    [
        ("eof", "startup", "pipe closed"),
        ("protocol_error", "startup", "CDP error"),
        ("startup_timeout", "startup", "timeout"),
        ("nonzero", "shutdown", "nonzero exit"),
        ("shutdown_timeout", "shutdown", "timeout"),
        ("javascript_error", "interaction", "JavaScript"),
    ],
)
def test_failure_never_becomes_success(tmp_path, monkeypatch, mode, phase, match):
    fake(monkeypatch, mode)
    with pytest.raises(BrowserFailure, match=match):
        with browser_session(sys.executable, tmp_path / "browser") as ui:
            ui.stage("interaction")
            assert ui.evaluate("'DOM-success-marker'") is True
            if mode == "shutdown_timeout":
                ui.deadline = time.monotonic() + 0.15
    report = record(tmp_path)
    assert not report["passed"]
    assert report["failure_phase"] == phase
    assert not report["remaining_process_group"]


def test_failed_ui_assertion_is_preserved_and_process_reaped(tmp_path, monkeypatch):
    fake(monkeypatch, "normal")
    with pytest.raises(AssertionError, match="UI deliberately failed"):
        with browser_session(sys.executable, tmp_path / "browser") as ui:
            ui.stage("interaction")
            raise AssertionError("UI deliberately failed")
    report = record(tmp_path)
    assert not report["passed"] and report["forced_cleanup"]
    assert report["failure_phase"] == "interaction"
    assert not report["remaining_process_group"]


def test_external_page_rejected(tmp_path, monkeypatch):
    fake(monkeypatch, "normal")
    with pytest.raises(BrowserFailure, match="local page required"):
        with browser_session(sys.executable, tmp_path / "browser") as ui:
            ui.load("https://example.invalid", "account_load")
    assert not record(tmp_path)["passed"]


def test_explicit_bad_browser_is_not_a_skip(tmp_path, monkeypatch):
    from tests.test_account_view import test_local_browser_interactions

    monkeypatch.setenv("ACCOUNT_VIEW_TEST_BROWSER", str(tmp_path / "absent-browser"))
    with pytest.raises(AssertionError, match="Configured browser must exist"):
        test_local_browser_interactions(tmp_path, None, "empty")
