"""Isolated synthetic browser fixture. Never connects to ARM or models."""
from contextlib import asynccontextmanager
import tempfile
from pathlib import Path
from types import SimpleNamespace

import uvicorn
from recognition_helper import main
from recognition_helper.engine import Engine
from recognition_helper.config import Settings
from tests.test_review import populate

root=Path(tempfile.mkdtemp(prefix='review-browser-'))
password=root/'test-password';password.write_text('synthetic-browser-only')
main.engine.pool.shutdown(wait=True)
main.settings=Settings(state=root/'state',media=root/'media',released=root/'released',password_file=str(password))
main.engine=Engine(main.settings)
main.engine.camera.status={'stage':'waiting','tray':'closed','connected':False,'message':'Synthetic browser fixture'}
main.engine.queue={'drives':[{'mount':'synthetic','state':'ready'}]}
main.engine.backend={'synthetic':True}
c=populate(main.engine)
cleanup=main.engine.store.create(state='completed_detained',local_done=True)
cleanup.update(created=c['created']-1,backend_done=True,recognition_done=True,job_id=100,
               job_guid='22222222-2222-4222-8222-222222222222')
cleanup_root=main.settings.detention/f"job-100-{cleanup['job_guid']}"
cleanup_root.mkdir(parents=True)
cleanup_file=cleanup_root/'DETAINED_test.mkv';cleanup_file.write_bytes(b'synthetic delete GUI test')
cleanup['sources']=[{'source_id':0,'state':'complete','path':str(cleanup_file),'reason':'synthetic unidentified file'}]
main.engine.store.save(cleanup)
removed=False
def delete_check(*args):
    return {'root':str(Path(main.settings.arm_media)/'detention'/cleanup_root.name),
            'job_guid':cleanup['job_guid'],'cycle_id':cleanup['id']}
def delete_finished(*args):
    global removed
    removed=True
    return {'arm':{'success':True}}
ready={'drives':[{'mount':main.settings.drive,'state':'ready','job_id':None}],'optical_processes':[]}
main.engine.arm=SimpleNamespace(deletion_check=delete_check,delete_finished=delete_finished,
    jobs=lambda:[] if removed else [{'job_id':100,'guid':cleanup['job_guid']}],queue_status=lambda:ready)
@main.app.post('/__test__/stale-assignment')
def stale_assignment():
    c['sources'][1]['assignment']={'kind':'tv','number':77}
    return {'synthetic':True}
@asynccontextmanager
async def isolated(app):
    yield
main.app.router.lifespan_context=isolated
if __name__=='__main__':uvicorn.run(main.app,host='0.0.0.0',port=8080)
