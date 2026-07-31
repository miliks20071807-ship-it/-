"""
Базові тести storage.py.

Навмисно перший тестовий файл у проєкті: без нього деплой/CI-агентам
нема що ганяти, а pytest -q виходив би з кодом 5 (no tests collected),
який деплой-агент трактував би як "тести не пройшли".
"""

import importlib

import pytest

import storage


@pytest.fixture(autouse=True)
def isolated_data_file(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "DATA_FILE", tmp_path / "tasks.json")
    yield


def test_add_and_list_task():
    task_id = storage.add_task("полагодити авторизацію", author="Аліса")
    tasks = storage.list_tasks(status="open")

    assert len(tasks) == 1
    assert tasks[0]["id"] == task_id
    assert tasks[0]["text"] == "полагодити авторизацію"
    assert tasks[0]["author"] == "Аліса"


def test_complete_task():
    task_id = storage.add_task("зробити щось", author="Боб")

    assert storage.complete_task(task_id) is True
    assert storage.list_tasks(status="open") == []
    assert storage.list_tasks(status="done")[0]["id"] == task_id


def test_complete_unknown_task_returns_false():
    assert storage.complete_task(9999) is False


def test_module_reimport_is_safe():
    importlib.reload(storage)
