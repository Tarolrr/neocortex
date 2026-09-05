import json
import sqlite3

import pytest

from nc.cli import main
from nc.config import Config
from nc.state import SCHEMA, State


def setup_state(tmp_path):
    cfg = Config(home=tmp_path / 'home')
    state = State(cfg.db_path)
    state.add_project('demo', 'Demo', str(tmp_path), None)
    return cfg, state


def specs():
    return [
        {'project': 'demo', 'title': 'First', 'objective': 'Full objective\nsecond line',
         'acceptance': ['$ pytest -q', 'Preserve all criteria'],
         'boundaries': ['Only this repo'], 'priority': 7, 'budget_turns': 3},
        {'project': 'demo', 'title': 'Second', 'objective': 'Another task',
         'acceptance': ['Human review']},
    ]


def test_proposal_gate_and_file_import_parity(tmp_path, capsys):
    cfg, state = setup_state(tmp_path)
    tasks = specs()
    pid = state.add_proposal('demo', 'planner', 'Why these tasks', tasks)
    row = state.one('SELECT * FROM proposal WHERE id=?', (pid,))
    assert row['status'] == 'pending'
    assert row['decided_at'] is None
    assert state.q('SELECT * FROM task') == []
    argv = ['--home', str(cfg.home)]
    assert main([*argv, 'proposals']) == 0
    assert 'pending' in capsys.readouterr().out
    assert main([*argv, 'proposal', str(pid)]) == 0
    assert json.loads(capsys.readouterr().out)['spec'] == tasks
    assert main([*argv, 'approve', str(pid)]) == 0
    approved = state.q('SELECT * FROM task ORDER BY id')
    assert len(approved) == 2
    for row, spec in zip(approved, tasks):
        assert row['status'] == 'queued'
        assert json.loads(row['acceptance']) == spec['acceptance']
    assert main([*argv, 'approve', str(pid)]) == 1
    assert main([*argv, 'reject', str(pid), 'Too late']) == 1
    assert len(state.q('SELECT * FROM task')) == 2
    decision = state.one('SELECT * FROM proposal WHERE id=?', (pid,))
    assert decision['status'] == 'approved'
    assert decision['decided_at'] >= decision['created_at']

    task_file = tmp_path / 'tasks.json'
    task_file.write_text(json.dumps(tasks))
    assert main([*argv, 'task', '--file', str(task_file)]) == 0
    imported = state.q('SELECT * FROM task ORDER BY id')[2:]
    for proposal_task, file_task in zip(approved, imported):
        for key in dict(proposal_task):
            if key not in ('id', 'created_at', 'updated_at'):
                assert proposal_task[key] == file_task[key]


def test_rejection_persists_reason_and_never_queues(tmp_path):
    cfg, state = setup_state(tmp_path)
    pid = state.add_proposal('demo', 'planner', 'Rationale', specs())
    argv = ['--home', str(cfg.home)]
    assert main([*argv, 'reject', str(pid), 'Outside scope']) == 0
    assert main([*argv, 'approve', str(pid)]) == 1
    assert main([*argv, 'reject', str(pid), 'Changed reason']) == 1
    state.db.close()
    state = State(cfg.db_path)
    assert state.q('SELECT * FROM task') == []
    row = state.one('SELECT * FROM proposal WHERE id=?', (pid,))
    assert row['status'] == 'rejected'
    assert row['reason'] == 'Outside scope'
    assert row['decided_at'] >= row['created_at']


def test_failed_approval_rolls_back_entire_batch(tmp_path):
    _, state = setup_state(tmp_path)
    tasks = specs()
    del tasks[1]['acceptance']
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    with pytest.raises(KeyError):
        state.approve_proposal(pid)
    assert state.q('SELECT * FROM task') == []
    assert state.q('SELECT * FROM task_seq') == []
    row = state.one('SELECT * FROM proposal WHERE id=?', (pid,))
    assert row['status'] == 'pending'
    assert row['decided_at'] is None


def test_migrate_existing_database(tmp_path):
    path = tmp_path / 'state.db'
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.execute("INSERT INTO project(id,title,repo_path,created_at) VALUES('demo','Demo','.',0)")
    db.commit()
    db.close()
    state = State(path)
    assert state.one('SELECT title FROM project')['title'] == 'Demo'
    pid = state.add_proposal('demo', 'planner', 'Rationale', specs())
    state.db.close()
    state = State(path)
    assert len(state.approve_proposal(pid)) == 2


@pytest.mark.parametrize('command', ['proposal', 'approve', 'reject'])
def test_unknown_proposal(tmp_path, capsys, command):
    args = ['--home', str(tmp_path), command, '999']
    if command == 'reject':
        args.append('Reason')
    assert main(args) == 1
    assert 'unknown proposal' in capsys.readouterr().err


def test_empty_proposals(tmp_path, capsys):
    assert main(['--home', str(tmp_path), 'proposals']) == 0
    assert '(no proposals)' in capsys.readouterr().out
