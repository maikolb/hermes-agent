import hashlib
import json
import sqlite3
from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from hermes_cli import nfos_reviewer as reviewer
from tools.memory_tool import ENTRY_DELIMITER, MemoryStore

DAY_TS = int(datetime(2026,9,15,12,tzinfo=ZoneInfo('America/Sao_Paulo')).timestamp())


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME',str(tmp_path))
    monkeypatch.setenv('HERMES_KANBAN_HOME',str(tmp_path))
    (tmp_path/'config.yaml').write_text('memory:\n  memory_char_limit: 50000\ntimezone: America/Sao_Paulo\n')
    return tmp_path


def board(home, name, ts=DAY_TS):
    path=home/(name+'.db')
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


def noop(*args, **kwargs):
    return None


def pointer(slug):
    return f'[NFOS Revisor:{slug}] Aprendizados do Revisor neste projeto: {reviewer.lessons_path(slug)}'


def principal_memory(home, total):
    """Principal entries filling MEMORY.md to `total` characters, 104 entries as on the VPS on 2026-10-14."""
    entries=[f'[principal-{i}] '+'x'*460 for i in range(103)]
    last=total-len(ENTRY_DELIMITER.join(entries))-len(ENTRY_DELIMITER)
    entries.append('[principal-last] '+'y'*(last-len('[principal-last] ')))
    path=home/'memories'/'MEMORY.md';path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(ENTRY_DELIMITER.join(entries),encoding='utf-8')
    store=MemoryStore(memory_char_limit=50000);store.load_from_disk()
    assert (len(store.memory_entries),len(ENTRY_DELIMITER.join(store.memory_entries)))==(104,total)
    return path


def test_all_boards_full_history_readonly_and_only_a_pointer_in_principal_memory(home):
    boards=[(s,board(home,s)) for s in ['one','two']]
    before={s:hashlib.sha256(p.read_bytes()).hexdigest() for s,p in boards}
    state=reviewer.run('2026-09-15',boards,judgment)
    assert (state['status'],state['reviewed'],state['saved'])==('completed',2,2)
    assert before=={s:hashlib.sha256(p.read_bytes()).hexdigest() for s,p in boards}
    store=MemoryStore(memory_char_limit=50000);store.load_from_disk()
    # With room, the Principal's memory gets one short pointer per project and no lesson text.
    assert 'Verify the customer checkout' not in store.format_for_system_prompt('memory')
    assert store.memory_entries==[pointer('one'),pointer('two')]
    assert 'Verify the customer checkout' in reviewer.lessons_path('one').read_text(encoding='utf-8')
    result=reviewer.status(state['id'])
    assert {'observed','analysed','saved','finished'} <= {e['kind'] for e in result['events']}
    assert reviewer.status(state['id'],result['cursor'])['events']==[]
    again=reviewer.run('2026-09-15',boards,lambda *a:pytest.fail('unchanged trajectory must not be re-analysed'))
    assert again['reviewed']==0


def test_full_principal_memory_is_never_written_and_refusal_is_the_real_one(home):
    """MEMORY.md at 49,998 of 50,000 characters, the state measured on the VPS on 2026-10-14."""
    memory=principal_memory(home,49998)
    before=memory.read_bytes()
    boards=[(s,board(home,s)) for s in ['one','two','three','four','five']]
    state=reviewer.run('2026-09-15',boards,judgment)
    events=reviewer.status(state['id'])['events']
    assert not [e for e in events if 'consolidation failed' in json.dumps(e)]
    assert not [e for e in events if e['kind'] in ('error','memory_pending')]
    consolidated=[e for e in events if e['kind']=='consolidated']
    assert len(consolidated)==5
    assert all(e['pointer']=='not_written' and e['pointer_error'].startswith('Memory at 49,998/50,000 chars.') for e in consolidated)
    assert (state['status'],state['reviewed'],state['saved'],state['errors'])==('completed',5,5,0)
    assert memory.read_bytes()==before
    for slug,_ in boards:
        assert reviewer.project_summary(slug).startswith('Verify the customer checkout')


def test_reviewed_card_whose_write_failed_is_not_reviewed_again(home,monkeypatch):
    db=board(home,'one');boards=[('one',db)]
    save=reviewer.save_lessons
    def broken(*args,**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(reviewer,'save_lessons',broken)
    first=reviewer.run('2026-09-15',boards,judgment)
    assert (first['status'],first['reviewed'],first['saved'])==('partial',1,0)
    assert len(reviewer.read(reviewer.root()/'pending_lessons.json'))==1
    monkeypatch.setattr(reviewer,'save_lessons',save)
    again=reviewer.run('2026-09-15',boards,lambda *a:pytest.fail('a reviewed card must not go back to the model'))
    assert (again['status'],again['reviewed'],again['saved'])==('completed',0,1)
    assert reviewer.read(reviewer.root()/'pending_lessons.json')=={}
    assert 'checkout-readback (practice)' in reviewer.lessons_path('one').read_text(encoding='utf-8')
    # A new record changes the trajectory: the card is reviewed again, with the project's synthesis as memory.
    with sqlite3.connect(db) as c:
        c.execute('INSERT INTO task_events VALUES(?,?,?,?,?)',(2,'t1','comment','Checkout verified by the customer',DAY_TS))
    seen=[]
    third=reviewer.run('2026-09-15',boards,lambda case,memory,emit:(seen.append(memory),judgment(case,memory,emit))[1])
    assert third['reviewed']==1
    assert any('Verify the customer checkout' in entry for entry in seen[0])


def test_mark_per_board_advances_with_write_error_and_holds_on_collection_error(home,monkeypatch):
    tz=ZoneInfo('America/Sao_Paulo')
    until=datetime.combine(datetime.now(tz).date(),time(),tz).timestamp()
    boards=[(s,board(home,s,int(until)-6*3600)) for s in ['one','two']]
    collect=reviewer.collect
    def locked(db,slug,since,end):
        if slug=='two':
            raise sqlite3.OperationalError('database is locked')
        return collect(db,slug,since,end)
    def broken(*args,**kwargs):
        raise OSError('disk full')
    monkeypatch.setattr(reviewer,'collect',locked)
    monkeypatch.setattr(reviewer,'save_lessons',broken)
    state=reviewer.run(boards=boards,analyzer=judgment)
    assert (state['status'],state['reviewed'])==('partial',1)
    assert reviewer.read(reviewer.root()/'watermark.json')['boards']=={'one':until}
    monkeypatch.setattr(reviewer,'collect',collect)
    again=reviewer.run(boards=boards,analyzer=judgment)
    cards={e['project']:e['cards'] for e in reviewer.status(again['id'])['events'] if e['kind']=='project'}
    assert cards=={'one':0,'two':1}
    assert reviewer.read(reviewer.root()/'watermark.json')['boards']=={'one':until,'two':until}



def test_mark_of_a_board_advances_when_the_analysis_of_a_card_fails(home):
    tz=ZoneInfo('America/Sao_Paulo')
    until=datetime.combine(datetime.now(tz).date(),time(),tz).timestamp()
    boards=[('one',board(home,'one',int(until)-6*3600))]
    def refused(case,memory,emit):
        raise ValueError('memory_summary com 1843 caracteres; reescreva com até 1800')
    state=reviewer.run(boards=boards,analyzer=refused)
    assert (state['status'],state['reviewed'],state['errors'])==('partial',0,1)
    # Spec 5b: only a collection error holds the mark, so the failed card does not reopen the window every night.
    assert reviewer.read(reviewer.root()/'watermark.json')['boards']=={'one':until}
    again=reviewer.run(boards=boards,analyzer=lambda *a:pytest.fail('the window of the board already advanced'))
    assert (again['status'],again['cards'])==('completed',0)


def test_failure_after_the_analysis_is_the_card_error_and_the_run_goes_on(home,monkeypatch):
    write=reviewer.write
    failed=[]
    def full_disk(path,data):
        if path.parent.name=='reviews' and not failed:
            failed.append(path);raise OSError('No space left on device')
        return write(path,data)
    monkeypatch.setattr(reviewer,'write',full_disk)
    state=reviewer.run('2026-09-15',[(s,board(home,s)) for s in ['one','two']],judgment)
    # As on the base: the card counts one error and the next board is still reviewed; it is not an interruption.
    assert (state['status'],state['reviewed'],state['saved'],state['errors'])==('partial',1,1,1)
    events=reviewer.status(state['id'])['events']
    assert not [e for e in events if e.get('interrupted')]
    assert [e['project'] for e in events if e['kind']=='error' and 'No space left' in e['error']]==['one']


def test_retried_write_keeps_one_block_of_the_card_in_the_history(home,monkeypatch):
    boards=[('one',board(home,'one'))]
    write=reviewer.write
    def broken(path,data):
        if path.parent.name=='lessons':
            raise OSError('No space left on device')
        return write(path,data)
    # The lessons file is written, then the run record of the write fails: the write stays pending.
    monkeypatch.setattr(reviewer,'write',broken)
    first=reviewer.run('2026-09-15',boards,judgment)
    assert (first['status'],first['saved'])==('partial',0)
    assert reviewer.lessons_path('one').read_text(encoding='utf-8').count('checkout-readback (practice)')==1
    monkeypatch.setattr(reviewer,'write',write)
    again=reviewer.run('2026-09-15',boards,lambda *a:pytest.fail('a reviewed card must not go back to the model'))
    assert (again['status'],again['saved'])==('completed',1)
    assert reviewer.lessons_path('one').read_text(encoding='utf-8').count('checkout-readback (practice)')==1

def test_invalid_lesson_is_discarded_and_the_valid_one_of_the_card_is_saved(home):
    def mixed(case,memory,emit):
        data=judgment(case,memory,emit)
        data['lessons'].append({**data['lessons'][0],'key':'invented-source','sources':['invented']})
        return data
    state=reviewer.run('2026-09-15',[('one',board(home,'one'))],mixed)
    assert (state['status'],state['reviewed'],state['saved'],state['errors'])==('completed',1,1,0)
    review=json.loads(next((reviewer.root()/'runs'/state['id']/'reviews').glob('*.json')).read_text(encoding='utf-8'))
    assert [lesson['key'] for lesson in review['lessons']]==['checkout-readback']
    assert any("'invented-source'" in reason and 'fonte inexistente' in reason for reason in review['discarded'])
    text=reviewer.lessons_path('one').read_text(encoding='utf-8')
    assert 'checkout-readback' in text and 'invented-source' not in text


def test_interruption_closes_the_run_as_interrupted(home):
    def interrupted(case,memory,emit):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        reviewer.run('2026-09-15',[('one',board(home,'one'))],interrupted)
    folder=next((reviewer.root()/'runs').iterdir())
    state=json.loads((folder/'state.json').read_text(encoding='utf-8'))
    assert state['status']=='interrupted' and state['finished_at']
    events=reviewer.status(folder.name)['events']
    assert any(e['kind']=='error' and e.get('interrupted') and e.get('task_id')=='t1' for e in events)
    assert reviewer.run('2026-09-15',[],judgment)['status']=='completed'


def test_validation_goes_back_to_the_same_agent_without_truncation(home,monkeypatch):
    import run_agent
    from hermes_cli import runtime_provider
    case=reviewer.collect(board(home,'one'),'one',0,9999999999)[0]
    decision=next(r['ref'] for r in case['records'] if '/nfos_decisions/' in r['ref'])
    turns=[]
    def agent_with(replies):
        class Agent:
            def __init__(self,**kwargs):
                pass
            def run_conversation(self,message,conversation_history=None):
                turns.append((message,conversation_history))
                reply=replies.pop(0)()
                return {'final_response':json.dumps(reply),'messages':[*(conversation_history or []),{'role':'user','content':message}]}
            def close(self):
                pass
        return Agent
    def answer(summary,read):
        def reply():
            if read:
                run_agent.handle_function_call('read_record',{'ref':decision})
            return {**judgment(case,[],noop),'memory_summary':summary}
        return reply
    monkeypatch.setattr(runtime_provider,'resolve_runtime_provider',lambda **kwargs:{'provider':'test'})
    # The source was not read and the summary is too long: the exact errors go back once and the rewrite is kept whole.
    monkeypatch.setattr(run_agent,'AIAgent',agent_with([answer('a'*1843,False),answer('b'*1700,True)]))
    result=reviewer.analyse(case,[],noop)
    assert len(turns)==2
    assert '1843 caracteres' in turns[1][0] and decision in turns[1][0] and 'não lida' in turns[1][0]
    assert turns[1][1]==[{'role':'user','content':turns[0][0]}]
    assert result['memory_summary']=='b'*1700 and result['inspected_sources']==[decision]
    assert len(result['corrections'])==1
    # An agent that never fixes it gets two corrections, then the run refuses the summary; nothing is cut.
    turns.clear()
    monkeypatch.setattr(run_agent,'AIAgent',agent_with([answer('a'*1843,True) for _ in range(4)]))
    result=reviewer.analyse(case,[],noop)
    assert len(turns)==3 and result['memory_summary']=='a'*1843
    with pytest.raises(ValueError,match='1843 caracteres'):
        reviewer.screen(case,result)


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


def test_consolidated_summary_lives_in_the_reviewer_file_and_old_entry_becomes_pointer(home):
    case=reviewer.collect(board(home,'one'),'one',0,9999999999)[0]
    path=home/'memories'/'MEMORY.md';path.parent.mkdir(parents=True)
    path.write_text(ENTRY_DELIMITER.join(['[one] Principal note',
        '[NFOS Revisor:one] Old long synthesis. Histórico e fontes completos: /runs (projeto one).']),encoding='utf-8')
    store=MemoryStore(memory_char_limit=50000);store.load_from_disk()
    data=judgment(case,[],noop)
    reviewer.save_lessons(case,data,store,noop)
    data['lessons'][0]['limits']='Explicitly restricted to price propagation'
    data['memory_summary']='Verify actual checkout; restricted to price propagation.'
    reviewer.save_lessons(case,data,store,noop)
    store.load_from_disk()
    assert not [e for e in store.memory_entries if 'Verify actual checkout' in e or 'Old long synthesis' in e]
    assert store.memory_entries==['[one] Principal note',pointer('one')]
    assert reviewer.project_summary('one')=='Verify actual checkout; restricted to price propagation.'
    text=reviewer.lessons_path('one').read_text(encoding='utf-8')
    assert text.count('checkout-readback (practice)')==2 and 'Explicitly restricted to price propagation' in text
    data['inspected_sources']=[]
    with pytest.raises(ValueError,match='não lida'):reviewer.save_lessons(case,data,store,noop)


def test_initial_weekly_review_covers_seven_days(home,monkeypatch):
    monkeypatch.setattr(reviewer,'settings',lambda:{**reviewer.DEFAULTS,'frequency':'weekly'})
    result=reviewer.run(boards=[],analyzer=judgment)
    assert result['until']-result['since']==7*86400
