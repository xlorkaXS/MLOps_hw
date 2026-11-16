"""FastAPI localhost; HTTP reports the startup snapshot, not the current disk pointer."""
import argparse
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from release import read, validate_release, verify_files


class StubBackend:
    def __init__(self, manifest):
        self.loaded_id = manifest['model_id']
        self.calls = 0

    def generate(self, prompt, limit):
        self.calls += 1
        time.sleep(.01)
        return {'answer': f'STUB ONLY: {self.loaded_id}', 'output_tokens': 1}


class Input(BaseModel):
    model_config = ConfigDict(strict=True, extra='forbid')
    prompt: str = Field(min_length=1, max_length=4000)
    max_new_tokens: int | None = None


def create_app(root, backend_type='stub', warmup_seconds=0):
    lock = asyncio.Lock()
    state = dict(ready=False)

    @asynccontextmanager
    async def lifespan(app):
        manifest = read(root / 'accepted.json')
        if backend_type == 'stub':
            if not manifest['model_id'].startswith('fixture-'):
                raise ValueError('STUB принимает только тестовые версии')
            verify_files(manifest, root)
            backend = StubBackend(manifest)
        else:
            from model_runtime import TransformersBackend, verify_runtime
            verify_runtime(manifest, root)
            validate_release(manifest, read(root / manifest['release_report']['path']))
            backend = TransformersBackend(manifest, root)
            await asyncio.to_thread(backend.generate, 'Выбери вариант: A, B или C.', limit(manifest))
        state.update(manifest=manifest, backend=backend)

        async def finish_warmup():
            await asyncio.sleep(warmup_seconds)
            state['ready'] = True

        task = asyncio.create_task(finish_warmup())
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            state.clear()

    def limit(m):
        return m['runtime']['generation']['max_new_tokens']

    def version():
        return {k: state['manifest'][k] for k in ('model_id', 'runtime_sha256')}

    app = FastAPI(lifespan=lifespan)

    @app.get('/health')
    async def health():
        return {'alive': True, 'backend': 'STUB ONLY' if backend_type == 'stub' else 'Transformers'}

    @app.get('/ready')
    async def ready():
        return JSONResponse({'ready': state['ready'], **version()}, status_code=200 if state['ready'] else 503)

    if backend_type == 'stub':
        @app.get('/debug')
        async def debug():
            return dict(loaded_id=state['backend'].loaded_id, calls=state['backend'].calls,
                        warmup_finished=state['ready'])

    @app.post('/generate')
    async def generate(request: Request):
        started = time.perf_counter()
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 32768:
                raise HTTPException(413, 'Слишком большой запрос')
            data.extend(chunk)
        try:
            item = Input.model_validate_json(data)
        except ValidationError:
            raise HTTPException(422, 'Неверный prompt или тип max_new_tokens')
        token_limit = limit(state['manifest'])
        if not item.prompt.strip() or ('max_new_tokens' in item.model_fields_set and item.max_new_tokens != token_limit):
            raise HTTPException(422, f'Нужен непустой prompt; допустимый max_new_tokens: {token_limit}')
        if not state['ready']:
            raise HTTPException(503, 'Загрузка или прогрев не завершены')
        async with lock:
            begun = time.perf_counter()
            try:
                result = await asyncio.to_thread(state['backend'].generate, item.prompt, token_limit)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            finished = time.perf_counter()
        return dict(result, **version(), request_id=str(uuid.uuid4()),
                    queue_seconds=begun - started, generation_seconds=finished - begun,
                    total_seconds=finished - started)

    return app


def main():
    import uvicorn
    p = argparse.ArgumentParser()
    p.add_argument('--workspace', type=Path, default=Path('.lab'))
    p.add_argument('--port', type=int, default=8091)
    p.add_argument('--warmup-seconds', type=float, default=5)
    p.add_argument('--backend', choices=['stub', 'real'], default='stub')
    a = p.parse_args()
    uvicorn.run(create_app(a.workspace, a.backend, a.warmup_seconds), host='127.0.0.1', port=a.port)


if __name__ == '__main__':
    main()
