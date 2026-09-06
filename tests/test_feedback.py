import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from nc.cli import main
from nc.config import Config
from nc.state import State


@pytest.fixture
def project(tmp_path, monkeypatch):
    cfg = Config(home=tmp_path)
    cfg.models['planner'] = 'planner-model'
    cfg.save()
    state = State(cfg.db_path)
    state.add_project('demo', 'Demo', str(tmp_path), None)

    def no_session(*args, **kwargs):
        pytest.fail('owner input must not start the scheduler or a session')

    monkeypatch.setattr('nc.cli.Scheduler', no_session)
    monkeypatch.setattr('nc.turn.run_turn', no_session)
    yield cfg, state
    state.db.close()


def invoke(cfg, *args):
    return main(['--home', str(cfg.home), *args])


def test_feedback_then_plan_share_planner_and_inbox(project):
    cfg, state = project
    assert invoke(cfg, 'feedback', 'Keep the café simple', '--project', 'demo') == 0
    assert invoke(cfg, 'plan', 'demo', '--note', 'Consider tests first') == 0
    agents = state.q("SELECT * FROM agent WHERE project_id='demo'")
    assert len(agents) == 1
    agent = agents[0]
    assert (agent['role'], agent['state'], agent['model']) == (
        'planner', 'runnable', 'planner-model',
    )
    assert agent['task_id'] is None
    inbox = state.inbox(agent['id'])
    assert [json.loads(m['payload'])['text'] for m in inbox] == [
        'Keep the café simple', 'Consider tests first',
    ]
    assert all(m['kind'] == 'feedback' and m['sender'] == 'owner' for m in inbox)
    assert state.q('SELECT * FROM run') == []


@pytest.mark.parametrize('command', [
    ['feedback', 'Lost note', '--project', 'missing'],
    ['plan', 'missing'],
    ['feedback', 'Lost note', '--task', 'missing'],
])
def test_unknown_target_writes_nothing(project, command, capsys):
    cfg, state = project
    before = list(state.db.iterdump())
    assert invoke(cfg, *command) != 0
    assert list(state.db.iterdump()) == before
    assert 'unknown' in capsys.readouterr().err


@pytest.mark.parametrize('previous', ['blocked', 'done', 'failed', 'runnable'])
def test_wakes_existing_planner_preserving_history(project, previous):
    cfg, state = project
    state.add_agent('existing-planner', 'planner', 'demo', None, 'custom')
    state.set_agent('existing-planner', state=previous, memo='remember', turns=3)
    assert invoke(cfg, 'plan', 'demo') == 0
    agent = state.one('SELECT * FROM agent')
    assert len(state.q('SELECT * FROM agent')) == 1
    assert (agent['id'], agent['state'], agent['memo'], agent['turns']) == (
        'existing-planner', 'runnable', 'remember', 3,
    )
    assert json.loads(state.inbox(agent['id'])[0]['payload'])['text']


def test_project_resolution_and_status(project, capsys):
    cfg, state = project
    assert invoke(cfg, 'feedback', 'Sole project note') == 0
    state.add_project('other', 'Other', str(cfg.home), None)
    task = state.add_task('demo', 'Task', 'Objective', [])
    assert invoke(cfg, 'feedback', 'Task note', '--task', task) == 0
    before = list(state.db.iterdump())
    assert invoke(cfg, 'feedback', 'Ambiguous') == 1
    assert invoke(cfg, 'feedback', 'Mismatch', '--task', task, '--project', 'other') == 1
    assert list(state.db.iterdump()) == before
    capsys.readouterr()
    assert invoke(cfg, 'status') == 0
    output = capsys.readouterr().out
    assert 'pending feedback:' in output
    assert 'Sole project note' in output
    assert f'demo ({task}): Task note' in output
    assert len(state.inbox('planner-demo')) == 2
    state.mark_delivered([m['id'] for m in state.inbox('planner-demo')])
    assert invoke(cfg, 'status') == 0
    assert 'pending feedback:' not in capsys.readouterr().out


def test_concurrent_feedback_reuses_planner(project):
    cfg, state = project

    def send(i):
        connection = State(cfg.db_path)
        try:
            return connection.planner_feedback('demo', str(i), 'model')[0]
        finally:
            connection.db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(send, range(4)))
    assert len(set(ids)) == 1
    assert len(state.q("SELECT * FROM agent WHERE role='planner'")) == 1
    assert len(state.inbox(ids[0])) == 4


@pytest.mark.parametrize('target', ['missing', 'mismatch', 'approved', 'rejected', 'superseded'])
def test_invalid_proposal_feedback_is_atomic(project, target, capsys):
    cfg, state = project
    pid = state.add_proposal('demo', 'planner', '', [])
    extra = []
    if target == 'missing':
        pid += 100
    elif target == 'mismatch':
        extra = ['--project', 'other']
    else:
        state.x('UPDATE proposal SET status=? WHERE id=?', (target, pid))
    before = list(state.db.iterdump())
    assert invoke(cfg, 'feedback', '--proposal', str(pid), 'Revise', *extra) == 1
    assert list(state.db.iterdump()) == before
    assert capsys.readouterr().err


def test_proposal_selector_conflict(project):
    cfg, state = project
    before = list(state.db.iterdump())
    with pytest.raises(SystemExit):
        invoke(cfg, 'feedback', '--proposal', '1', '--task', 'task', 'Revise')
    with pytest.raises(ValueError, match='mutually exclusive'):
        state.planner_feedback(None, 'Revise', 'model', 'task', 1)
    assert list(state.db.iterdump()) == before
