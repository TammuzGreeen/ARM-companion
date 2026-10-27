from pathlib import Path

from recognition_helper.config import Settings
from recognition_helper.engine import Engine


def engine_cycle(tmp_path):
    e=Engine(Settings(state=tmp_path/'state',media=tmp_path/'media',released=tmp_path/'released'))
    c=e.store.create(state='test',local_done=True)
    c['locals']=[{'model':name,'findings':{'media_type':'tv','series':'Distant Harbour','season':1,'episodes':[1,2]}} for name in ('a','b')]
    for n in range(3):
        p=e.s.detention/f'DETAINED_{n}.mkv';p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'closed software test output')
        c['sources'].append({'source_id':n,'path':str(p),'state':'complete',**({'is_extra':True} if n==2 else {})})
    return e,c


def test_completed_episodes_release_without_naming_unidentified_extra(tmp_path):
    e,c=engine_cycle(tmp_path)
    try:
        e.decide(c);c['backend_done']=True;e.release(c)
        assert [s['state'] for s in c['sources']]==['released','released','complete']
        assert c['state']=='partially_released'
        assert Path(c['sources'][2]['path']).name=='DETAINED_2.mkv'
        assert not c['sources'][2]['proposed']
    finally:e.pool.shutdown(wait=True)


def test_episode_collision_never_overwrites_existing_published_file(tmp_path):
    e,c=engine_cycle(tmp_path)
    try:
        e.decide(c)
        target=e.s.released/c['sources'][0]['proposed'];target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'previous release')
        original=Path(c['sources'][0]['path']);c['backend_done']=True;e.release(c)
        assert target.read_bytes()==b'previous release'
        assert original.read_bytes()==b'closed software test output'
        assert c['sources'][0]['state']=='complete' and c['sources'][0]['release_error']
        assert c['sources'][1]['state']=='released'
    finally:e.pool.shutdown(wait=True)
