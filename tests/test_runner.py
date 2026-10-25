import types

from scaps_mcp import runner


def fake_msvcrt(calls, failures=0):
    def locking(fd, mode, size):
        if mode == 1 and failures > len([c for c in calls if c == "retry"]):
            calls.append("retry")
            raise OSError("locked")
        calls.append((mode, size))

    return types.SimpleNamespace(LK_LOCK=1, LK_UNLCK=0, locking=locking)


def test_installation_lock_uses_msvcrt_on_windows(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(runner.sys, "platform", "win32")
    monkeypatch.setattr(runner, "msvcrt", fake_msvcrt(calls, failures=2), raising=False)
    with runner.installation_lock(tmp_path):
        assert calls == ["retry", "retry", (1, 1)]
    assert calls[-1] == (0, 1)


def test_installation_lock_on_posix(tmp_path):
    with runner.installation_lock(tmp_path):
        assert (tmp_path / runner.LOCK_FILE).exists()


def test_stale_process_is_detected_by_command_line(tmp_path, monkeypatch):
    from scaps_mcp import processes

    killed = []
    monkeypatch.setattr(processes, "_kill", killed.append)
    assert processes.stop_stale(tmp_path, "scaps3312.exe") is False
    processes.remember(tmp_path, 999999999)
    assert processes.stop_stale(tmp_path, "scaps3312.exe") is False
    assert not (tmp_path / processes.PID_FILE).exists()
    processes.remember(tmp_path, 4242)
    monkeypatch.setattr(processes, "_is_scaps", lambda pid, exe: pid == 4242)
    assert processes.stop_stale(tmp_path, "scaps3312.exe") is True
    assert killed == [4242]
