import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .engine import Engine
from .vision import Findings
from .budget import committed
from . import __version__


settings = Settings()
engine = Engine(settings)
security = HTTPBasic()


def authenticate(credentials: HTTPBasicCredentials = Depends(security)):
    password = Path(settings.password_file).read_text().strip()
    if not (hmac.compare_digest(credentials.username.encode(), settings.user.encode()) and
            hmac.compare_digest(credentials.password.encode(), password.encode())):
        raise HTTPException(401, 'Invalid credentials', headers={'WWW-Authenticate': 'Basic'})


@asynccontextmanager
async def lifespan(app):
    engine.start()
    yield
    engine.close()


app = FastAPI(title='recognition-helper', lifespan=lifespan, dependencies=[Depends(authenticate)])
static = Path(__file__).parent / 'static'


@app.get('/')
def index():
    return FileResponse(static / 'index.html')


@app.get('/health')
def health():
    return {'running': True, 'version': __version__, 'backend_connected': bool(engine.backend), 'error': engine.error}


@app.get('/api/state')
def state():
    cycles = engine.store.snapshot()
    for cycle in cycles:
        for source in cycle['sources']:
            source['reason'] = source.get('reason') or source.get('release_error') or source.get('error')
            if settings.media_host:
                try:
                    source['host_path'] = str(Path(settings.media_host) / Path(source['path']).relative_to(settings.media))
                except ValueError:
                    pass
    return {'camera': engine.camera.status, 'backend': engine.backend, 'queue': engine.queue,
            'error': engine.error, 'cycles': cycles, 'paths': {'released': str(settings.released)},
            'selection_settings': {'tv_min_length_seconds': settings.tv_min_length_seconds},
            'fallback': {'configured_key_file': bool(settings.key_file or settings.external_key), 'call_limit': settings.fallback_limit,
                         'model': settings.external_model, 'spending_limit_usd': settings.spending_limit,
                         'committed_cost_estimate_usd': committed(engine.store.cycles.values()),
                         'runtime_access': engine.vision.access}}


@app.get('/api/preview.jpg')
def preview():
    if engine.camera.preview is None:
        raise HTTPException(404, 'Camera opens during a new tray cycle')
    return Response(engine.camera.preview, media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


def cycle_folder(cycle_id):
    if cycle_id not in engine.store.cycles:
        raise HTTPException(404, 'Unknown cycle')
    return engine.store.root / cycle_id


@app.get('/api/cycles/{cycle_id}/evidence/{name}')
def evidence(cycle_id: str, name: str):
    folder = cycle_folder(cycle_id)
    allowed = set(engine.store.cycles[cycle_id].get('snapshots', [])) | {'model-input-0.jpg', 'model-input-1.jpg'}
    if name not in allowed or Path(name).name != name:
        raise HTTPException(404)
    return FileResponse(folder / name)


@app.get('/api/cycles/{cycle_id}/record/{format}')
def record(cycle_id: str, format: str):
    if format not in ('json', 'md'):
        raise HTTPException(404)
    return FileResponse(cycle_folder(cycle_id) / ('record.' + format), filename=f'{cycle_id}.{format}')


class Correction(BaseModel):
    identity: Findings
    assignments: dict[str, dict] = Field(max_length=1000)


class MasterlistReview(BaseModel):
    reference_id: str = Field(max_length=500)
    action: str


@app.get('/api/cycles/{cycle_id}/review')
def review(cycle_id: str):
    cycle_folder(cycle_id)
    return engine.review(cycle_id)


@app.post('/api/cycles/{cycle_id}/masterlist')
def masterlist_review(cycle_id: str, payload: MasterlistReview):
    cycle_folder(cycle_id)
    try:
        return engine.review_masterlist(cycle_id,payload.reference_id,payload.action)
    except (ValueError,KeyError,TypeError) as error:
        raise HTTPException(400,str(error)) from None


@app.post('/api/cycles/{cycle_id}/preview')
def correction_preview(cycle_id: str, correction: Correction):
    cycle_folder(cycle_id)
    try:
        payload = correction.model_dump(exclude_none=True)
        return {'sources': engine.preview_correction(cycle_id, payload)}
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, str(error)) from None


@app.post('/api/cycles/{cycle_id}/correct')
def correct(cycle_id: str, correction: Correction):
    cycle_folder(cycle_id)
    try:
        return engine.correct(cycle_id, correction.model_dump(exclude_none=True))
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, str(error)) from None


@app.get('/api/cycles/{cycle_id}/export')
def export(cycle_id: str):
    cycle_folder(cycle_id)
    return Response(json.dumps(engine.export(cycle_id), ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': 'attachment; filename="partial-masterlist.json"'})


app.mount('/static', StaticFiles(directory=static), name='static')
