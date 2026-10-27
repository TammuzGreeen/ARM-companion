"""Isolated synthetic browser fixture. Never connects to ARM or models."""
from contextlib import asynccontextmanager
import tempfile
from pathlib import Path

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
@main.app.post('/__test__/stale-assignment')
def stale_assignment():
    c['sources'][1]['assignment']={'kind':'tv','number':77}
    return {'synthetic':True}
@asynccontextmanager
async def isolated(app):
    yield
main.app.router.lifespan_context=isolated
if __name__=='__main__':uvicorn.run(main.app,host='0.0.0.0',port=8080)
