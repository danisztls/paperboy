import os
import time

from main import _prune_old_files


def _touch(path, age_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    mt = time.time() - age_days * 86400
    os.utime(path, (mt, mt))


def test_prune_old_files_removes_files_past_cutoff(tmp_path):
    _touch(tmp_path / "fresh.log", age_days=1)
    _touch(tmp_path / "old.log", age_days=45)
    _touch(tmp_path / "task-a" / "old.jsonl", age_days=60)
    _touch(tmp_path / "task-a" / "fresh.jsonl", age_days=5)

    removed = _prune_old_files(tmp_path, days=30)

    assert removed == 2
    assert (tmp_path / "fresh.log").exists()
    assert not (tmp_path / "old.log").exists()
    assert (tmp_path / "task-a" / "fresh.jsonl").exists()
    assert not (tmp_path / "task-a" / "old.jsonl").exists()


def test_prune_old_files_zero_days_disables(tmp_path):
    _touch(tmp_path / "old.log", age_days=400)
    assert _prune_old_files(tmp_path, days=0) == 0
    assert (tmp_path / "old.log").exists()


def test_prune_old_files_missing_dir(tmp_path):
    assert _prune_old_files(tmp_path / "nonexistent", days=30) == 0
