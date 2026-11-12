import json
from http.client import RemoteDisconnected
import subprocess

import pytest

from scripts import backup
from scripts.check_reliability import http, sample_summary


@pytest.mark.parametrize("name", ["seps", "postgres", "template1", "seps_restore_",
                                  "seps_restore_bad;drop", "seps_restore_" + "a" * 64])
def test_restore_rejects_unsafe_targets(name):
    with pytest.raises(ValueError):
        backup.validate_restore_database(name, "seps")


def test_restore_rejects_production_even_with_test_prefix():
    with pytest.raises(ValueError):
        backup.validate_restore_database("seps_restore_current", "seps_restore_current")


def make_pair(directory, day):
    archive = directory / f"seps-202611{day:02d}T020000000000Z-aaaaaaaa.dump"
    archive.write_bytes(b"PGDMP-example")
    metadata = {"format": 1, "archive": archive.name, "bytes": archive.stat().st_size,
                "sha256": backup.checksum(archive)}
    archive.with_suffix(".dump.json").write_text(json.dumps(metadata))
    return archive


def test_retention_keeps_seven_completed_pairs_and_unrelated_files(tmp_path):
    for day in range(1, 10):
        make_pair(tmp_path, day)
    unrelated = tmp_path / "do-not-delete.dump"
    unrelated.write_bytes(b"unrelated")
    backup.prune(tmp_path, 7)
    assert len(list(tmp_path.glob("seps-*.dump"))) == 7
    assert not (tmp_path / "seps-20261101T020000000000Z-aaaaaaaa.dump").exists()
    assert unrelated.exists()


def test_failed_export_does_not_prune_or_publish_partial_archive(tmp_path, monkeypatch):
    old = make_pair(tmp_path, 1)
    monkeypatch.setattr(backup, "source_database", lambda: "seps")

    def fail(*args, **kwargs):
        kwargs["destination"].write(b"incomplete")
        raise RuntimeError("export failed")

    monkeypatch.setattr(backup, "pg", fail)
    with pytest.raises(RuntimeError):
        backup.create(tmp_path, keep=1)
    assert list(tmp_path.glob("*.dump")) == [old]
    assert not list(tmp_path.glob("*.partial"))
    assert not (tmp_path / ".backup.lock").exists()


def test_checksum_failure_is_detected_before_pg_restore(tmp_path, monkeypatch):
    archive = make_pair(tmp_path, 1)
    archive.write_bytes(b"PGDMP-corrupt")
    calls = []
    monkeypatch.setattr(backup, "pg", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        backup.verify(archive)
    assert calls == []


def test_existing_database_is_not_overwritten(tmp_path, monkeypatch):
    archive = make_pair(tmp_path, 1)
    monkeypatch.setattr(backup, "source_database", lambda: "seps")
    monkeypatch.setattr(backup, "verify", lambda path: {})
    calls = []

    def pg(script, *args, **kwargs):
        calls.append(script)
        raise subprocess.CalledProcessError(1, "createdb: already exists")

    monkeypatch.setattr(backup, "pg", pg)
    with pytest.raises(subprocess.CalledProcessError):
        backup.restore(archive, "seps_restore_existing")
    assert len(calls) == 1 and "createdb" in calls[0]


def test_directory_lock_prevents_overlapping_jobs(tmp_path):
    with backup.directory_lock(tmp_path):
        with pytest.raises(RuntimeError):
            with backup.directory_lock(tmp_path):
                pytest.fail("Second backup entered the locked directory")


def test_measured_sli_uses_observed_samples():
    result = sample_summary([{"good": True, "seconds": .1},
                             {"good": False, "seconds": .3},
                             {"good": True, "seconds": .2}])
    assert result["requests"] == 3
    assert result["availability"] == pytest.approx(2 / 3)
    assert result["p95_seconds"] == .2


@pytest.mark.parametrize("error", [RemoteDisconnected(), ConnectionResetError(), TimeoutError()])
def test_connection_loss_during_crash_counts_as_failed_sample(monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr("urllib.request.urlopen", fail)
    assert http("http://127.0.0.1:8000", "/health")[0] == 0


def test_backup_cli_path_survives_powershell_5_code_page_boundary(monkeypatch, capsys, tmp_path):
    path = tmp_path / "БВС проект" / "backup.dump"
    monkeypatch.setattr(backup, "create", lambda *args: path)
    monkeypatch.setattr("sys.argv", ["backup", "create"])
    backup.main()
    native_pipeline = capsys.readouterr().out.encode("cp1251").decode("cp866")
    assert json.loads(native_pipeline)["archive"] == str(path)
