import hashlib
import json
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hermes_cli import nfos_reviewer as reviewer
from tools.memory_tool import MemoryStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME',str(tmp_path))
    (tmp_path/'config.yaml').write_text('memory:\n  memory_char_limit: 50000\ntimezone: America/Sao_Paulo\n')
    return tmp_path


def board(home, name):
    path=home/(name+'.db')
    ts=int(datetime(2026,9,15,12,tzinfo=ZoneInfo('America/Sao_Paulo')).timestamp())
    with sqlite3.connect(path) as c:
        c.executescript('CREATE TABLE tasks(id TEXT,title TEXT,body TEXT,created_at INT,completed_at INT,status TEXT);'
                       'CREATE TABLE task_events(id INTEGER PRIMARY KEY,task_id TEXT,kind TEXT,payload TEXT,created_at INT);'
                       'CREATE TABLE task_runs(id INTEGER,task_id TEXT,started_at INT,ended_at INT,summary TEXT);'
                       'CREATE TABLE nfos_decisions(id TEXT,task_id TEXT,question TEXT,answer TEXT);')
        c.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?)',('t1','Fix checkout','Total must be 90',ts-86400,ts,'done'))
        c.execute('INSERT INTO task_runs VALUES(?,?,?,?,?)',(1,'t1',ts-86400,ts,'Retried stale checkout cache'))
        c.execute('INSERT INTO task_events VALUES(?,?,?,?,?)',(1,'t1','rework','Price saved but checkout stale',ts))
        c.execute('INSERT INTO nfos_decisions VALUES(?,?,?,?)',('d1','t1','Close from write receipt?','No; verify actual checkout'))
    return path


def judgment(case,memory,emit):
    refs=[r['ref'] for r in case['records'] if '/nfos_decisions/' in r['ref']]
    emit('observed',project=case['project'],task_id=case['task_id'],source=refs[0])
    return {'summary':'Checkout required its own verification','findings':['Write receipt did not establish checkout behavior'],
            'discarded':['No evidence of a universal cache defect'],'inspected_sources':refs,
            'memory_summary':'Verify the customer checkout after price changes; write receipts do not establish the customer result.',
            'lessons':[{'key':'checkout-readback','kind':'practice','when':'Price changes at checkout',
                        'lesson':'Verify the customer checkout after a price change','because':'Principal found write receipt insufficient',
                        'limits':'Applies to checkout, not every cache','sources':refs}]}


def test_all_boards_full_history_readonly_and_memory_consumption(home):
    boards=[(s,board(home,s)) for s in ['one','two']]
    before={s:hashlib.sha256(p.read_bytes()).hexdigest() for s,p in boards}
    state=reviewer.run('2026-09-15',boards,judgment)
    assert (state['status'],state['reviewed'],state['saved'])==('completed',2,2)
    assert before=={s:hashlib.sha256(p.read_bytes()).hexdigest() for s,p in boards}
    store=MemoryStore(memory_char_limit=50000);store.load_from_disk()
    assert len(store.memory_entries)==2
    assert 'Verify the customer checkout' in store.format_for_system_prompt('memory')
    result=reviewer.status(state['id'])
    assert {'observed','analysed','saved','finished'} <= {e['kind'] for e in result['events']}
    assert reviewer.status(state['id'],result['cursor'])['events']==[]
    again=reviewer.run('2026-09-15',boards,lambda *a:pytest.fail('unchanged trajectory must not be re-analysed'))
    assert again['reviewed']==0


def test_invalid_citation_stays_pending_and_does_not_write_memory(home):
    def bad(case,memory,emit):
        data=judgment(case,memory,emit);data['lessons'][0]['sources']=['invented'];return data
    state=reviewer.run('2026-09-15',[('one',board(home,'one'))],bad)
    assert state['status']=='partial' and state['saved']==0
    assert not (reviewer.root()/'watermark.json').exists()
    events=reviewer.status(state['id'])['events']
    assert any(e['kind']=='error' for e in events)


def test_schedule_native_update_pause_and_weekly(home):
    first=reviewer.configure({'enabled':True,'time':'02:00'})
    second=reviewer.configure({'frequency':'weekly','weekday':3,'time':'03:25'})
    assert first['config']['job_id']==second['config']['job_id']
    assert second['schedule']['next_run_at']
    paused=reviewer.configure({'enabled':False})
    assert paused['schedule']['enabled'] is False
    resumed=reviewer.configure({'enabled':True})
    assert resumed['schedule']['enabled'] is True
    with pytest.raises(ValueError):reviewer.configure({'time':'25:00'})
    assert reviewer.settings()['time']=='03:25'


def test_consolidate_same_key_and_reject_unread_sources(home):
    case=reviewer.collect(board(home,'one'),'one',0,9999999999)[0]
    store=MemoryStore(memory_char_limit=50000);store.load_from_disk()
    data=judgment(case,[],lambda *a,**k:None)
    reviewer.save_lessons(case,data,store,lambda *a,**k:None)
    data['lessons'][0]['limits']='Explicitly restricted to price propagation'
    data['memory_summary']='Verify actual checkout; restricted to price propagation.'
    reviewer.save_lessons(case,data,store,lambda *a,**k:None)
    store.load_from_disk();assert len(store.memory_entries)==1
    assert 'price propagation' in store.memory_entries[0]
    data['inspected_sources']=[]
    with pytest.raises(ValueError):reviewer.save_lessons(case,data,store,lambda *a,**k:None)


def test_initial_weekly_review_covers_seven_days(home,monkeypatch):
    monkeypatch.setattr(reviewer,'settings',lambda:{**reviewer.DEFAULTS,'frequency':'weekly'})
    result=reviewer.run(boards=[],analyzer=judgment)
    assert result['until']-result['since']==7*86400


def test_system_one_config_does_not_touch_retrospective_schedule(home, monkeypatch):
    from cron import jobs
    from hermes_cli import nfos_jev
    calls = []
    monkeypatch.setattr(nfos_jev, 'configure_system_one', lambda policy: calls.append(policy))
    monkeypatch.setattr(nfos_jev, 'system_one_status', lambda: {'policy': {'mode': 'shadow'}})
    monkeypatch.setattr(jobs, 'create_job', lambda **kw: pytest.fail('System One policy must not create cron'))
    monkeypatch.setattr(jobs, 'update_job', lambda *a, **kw: pytest.fail('System One policy must not rewrite cron'))
    result = reviewer.configure({'system_one': {'mode': 'shadow', 'engine': 'laya'}})
    assert calls == [{'mode': 'shadow', 'engine': 'laya'}]
    assert result['system_one']['policy']['mode'] == 'shadow'
    assert not (reviewer.root()/'config.json').exists()


def test_retrospective_consumes_system_one_records_without_re_review(home):
    path = board(home, 'one')
    opportunity = {'opportunity_id': 'o1', 'project_id': 'one', 'task_id': 't1', 'run_id': 1,
                   'lineage_id': 'r1', 'input': {'objective': 'Verify checkout',
                    'options': ['read', 'fallback'], 'constraints': {'target': 'checkout'}},
                   'versions': {'policy': '1'}}
    timestamp = int(datetime(2026,9,15,12,tzinfo=ZoneInfo('America/Sao_Paulo')).timestamp())
    with sqlite3.connect(path) as conn:
        conn.execute('INSERT INTO task_events VALUES(?,?,?,?,?)',
                     (2, 't1', 'nfos_system_one_opportunity', json.dumps(opportunity), timestamp))
    def analysis(case, memory, emit):
        result = judgment(case, memory, emit)
        ref = 'one/t1/task_events/2'
        assert ref in {r['ref'] for r in case['records']}
        result['inspected_sources'].append(ref)
        result['system_one_labels'] = [{'source': ref, 'preferred_option': 'read', 'reason': 'Read actual checkout'}]
        return result
    first = reviewer.run('2026-09-15', [('one',path)], analysis)
    exports = list((reviewer.root()/'runs'/first['id']/'system-one').glob('*.json'))
    assert len(exports) == 1
    row = json.loads(exports[0].read_text())['examples'][0]
    assert row['labels']['reviewer_preference']['option'] == 'read'
    assert row['labels']['verified_outcome'] == 'NOT_PROVEN'
    second = reviewer.run('2026-09-15', [('one',path)], lambda *a: pytest.fail('Unchanged card must not be re-reviewed'))
    assert second['reviewed'] == 0
