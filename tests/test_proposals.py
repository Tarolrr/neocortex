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
         'acceptance': ['$ pytest -q', 'Human review']},
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
    listing = capsys.readouterr().out
    assert f'{pid} demo pending tasks=2' in listing
    hint = next(line.strip() for line in listing.splitlines() if 'inspect:' in line)
    assert hint == f'inspect: nc proposal {pid}'
    assert main([*argv, *hint.removeprefix('inspect: nc ').split()]) == 0
    detail = json.loads(capsys.readouterr().out)
    assert detail['spec'] == tasks
    assert detail['plan_review'] is None
    assert state.q('SELECT * FROM task') == []
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


def test_pending_proposal_full_preview_with_review(tmp_path, capsys):
    cfg, state = setup_state(tmp_path)
    tasks = [
        {'project': 'demo', 'id': 'foundation',
         'title': 'Preserve the complete foundation title',
         'objective': 'Build the foundation.\nPreserve this second objective line.',
         'acceptance': ['$ pytest -q tests/test_foundation.py', 'Foundation passes review'],
         'boundaries': ['Existing foundation behavior must remain compatible'],
         'depends_on': []},
        {'project': 'demo', 'id': 'integration',
         'title': 'Preserve the complete integration title',
         'objective': 'Integrate the foundation.\nInclude the complete integration details.',
         'acceptance': ['Integration passes owner review'],
         'boundaries': ['Existing integration data must remain intact'],
         'depends_on': ['foundation']},
    ]
    pid = state.add_proposal('demo', 'planner', 'Complete preview rationale', tasks)
    before = dict(state.one('SELECT * FROM proposal WHERE id=?', (pid,)))
    review = {'status': 'done', 'findings': ['Add an integration machine check'],
              'recommendation': 'Revise acceptance before approval'}
    state.x(
        'INSERT INTO plan_review(proposal_id,spec,status,findings,recommendation) '
        'VALUES(?,?,?,?,?)',
        (pid, before['spec'], review['status'], json.dumps(review['findings']),
         review['recommendation']),
    )
    assert state.q('SELECT * FROM task') == []
    assert main(['--home', str(cfg.home), 'proposal', str(pid)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ''
    detail = json.loads(captured.out)
    assert detail['spec'] == tasks
    assert detail['status'] == 'pending'
    assert detail['rationale'] == 'Complete preview rationale'
    assert detail['findings'] == [
        "task integration: no machine-checkable acceptance criterion starting with '$'",
    ]
    assert detail['plan_review'] == review
    assert dict(state.one('SELECT * FROM proposal WHERE id=?', (pid,))) == before
    assert state.q('SELECT * FROM task') == []
    assert state.q('SELECT * FROM task_seq') == []


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
        state.approve_proposal(pid, force=True)
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


def findings(state, pid):
    return json.loads(state.one('SELECT findings FROM proposal WHERE id=?', (pid,))['findings'])


def test_unknown_dependency_refused_and_force_reports_override(tmp_path, capsys):
    cfg, state = setup_state(tmp_path)
    tasks = specs()
    tasks[0]['depends_on'] = ['missing']
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert any('unknown dependency missing' in f for f in findings(state, pid))
    argv = ['--home', str(cfg.home)]
    assert main([*argv, 'proposals']) == 0
    assert 'unknown dependency missing' in capsys.readouterr().out
    assert main([*argv, 'proposal', str(pid)]) == 0
    assert json.loads(capsys.readouterr().out)['findings'] == findings(state, pid)
    assert main([*argv, 'approve', str(pid)]) == 1
    assert 'unknown dependency missing' in capsys.readouterr().err
    assert not state.q('SELECT * FROM task')
    assert main([*argv, 'approve', str(pid), '--force']) == 0
    assert 'overriding finding: task 1: unknown dependency missing' in capsys.readouterr().err
    assert json.loads(state.one('SELECT spec FROM proposal')['spec']) == tasks


def test_dependency_cycle(tmp_path):
    _, state = setup_state(tmp_path)
    tasks = specs()
    tasks[0].update(id='first', depends_on=['second'])
    tasks[1].update(id='second', depends_on=['first'])
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert any('dependency cycle' in f for f in findings(state, pid))
    assert not any('unknown dependency' in f for f in findings(state, pid))


def test_missing_machine_check(tmp_path):
    _, state = setup_state(tmp_path)
    tasks = specs()
    tasks[0]['acceptance'] = ['Human review']
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert any('no machine-checkable acceptance' in f for f in findings(state, pid))


@pytest.mark.parametrize('boundary', [
    'nc/state.py', 'src/', 'Do not modify nc/state.py',
    '/root/neocortex/nc/state.py', '/src/', 'Do not modify /root/neocortex/nc/state.py',
])
def test_path_boundary(tmp_path, capsys, boundary):
    cfg, state = setup_state(tmp_path)
    tasks = specs()
    tasks[0]['boundaries'] = [boundary]
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert any('boundary names a path' in f for f in findings(state, pid))
    argv = ['--home', str(cfg.home), 'approve', str(pid)]
    assert main(argv) == 1
    assert boundary in capsys.readouterr().err
    assert state.one('SELECT status FROM proposal')['status'] == 'pending'
    assert not state.q('SELECT * FROM task')
    assert main([*argv, '--force']) == 0
    assert f'overriding finding: task 1: boundary names a path instead of an invariant: {boundary}' in (
        capsys.readouterr().err
    )


def test_clean_proposal_with_invariants_and_local_dependencies(tmp_path):
    _, state = setup_state(tmp_path)
    existing = state.add_task('demo', 'Existing', 'Objective', ['$ true'])
    tasks = specs()
    tasks[0].update(id='first', depends_on=[existing],
                    boundaries=['Public API must remain backward compatible',
                                'nc/state.py must preserve database compatibility',
                                '/root/neocortex/nc/state.py must preserve database compatibility'])
    tasks[1]['depends_on'] = ['first']
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert findings(state, pid) == []
    assert state.one('SELECT status FROM proposal')['status'] == 'pending'
    ids = state.approve_proposal(pid)
    assert json.loads(state.one('SELECT depends_on FROM task WHERE id=?',
                               (ids[1],))['depends_on']) == [ids[0]]


def test_direct_textual_conflict(tmp_path):
    _, state = setup_state(tmp_path)
    tasks = specs()
    tasks[0]['boundaries'] = ['Do not change the public API']
    tasks[0]['acceptance'] += ['Change the public API to accept a new argument']
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert any('acceptance conflicts with boundary' in f for f in findings(state, pid))


def test_approval_rechecks_and_persists_findings(tmp_path):
    _, state = setup_state(tmp_path)
    existing = state.add_task('demo', 'Existing', 'Objective', ['$ true'])
    tasks = specs()
    tasks[0]['depends_on'] = [existing]
    pid = state.add_proposal('demo', 'planner', 'Rationale', tasks)
    assert findings(state, pid) == []
    state.x('DELETE FROM task WHERE id=?', (existing,))
    with pytest.raises(ValueError, match='unknown dependency'):
        state.approve_proposal(pid)
    assert findings(state, pid)
    assert state.one('SELECT status FROM proposal')['status'] == 'pending'
    assert not state.q('SELECT * FROM task')
