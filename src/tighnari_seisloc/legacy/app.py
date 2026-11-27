import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import secrets
import time
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from .engine import Engine
from .demo import make_demo
from .models import WavePacket
from .providers import SummarySources

ROOT=Path(__file__).parent


def create_app(mode='iris', replay=None, speed=1, travel_model=None):
    engine=Engine(mode,travel_model); sources=SummarySources()
    monitor=None
    if mode=='iris':
        from .streaming import LocationMonitor
        from .travel import HomogeneousTravelTime
        monitor=LocationMonitor(travel_model or HomogeneousTravelTime(),os.getenv('SAIU_REPORT_DIR','runtime/reports'),os.getenv('SAIU_DISCORD_WEBHOOK'))
    state={'playing':True,'finished':False,'error':None}
    async def playback():
        try:
            if replay:
                from .replay import read_packets, iter_seconds
                packets=list(read_packets(replay))
                if not packets: raise ValueError('沒有波形封包')
                batches=([p] for p in iter_seconds(packets))
                engine.now=min(p.start for p in packets)
            else:
                engine.now=time.time()-12
                batches=make_demo(engine.now)
            last=None
            for batch in batches:
                while not state['playing']: await asyncio.sleep(.1)
                ts=batch[0].start
                if last is not None: await asyncio.sleep(min(60,max(0,ts-last)/speed))
                for packet in batch: engine.ingest(packet)
                last=ts
            if replay and engine.event:
                from .magnitude import local_magnitude
                from .travel import HomogeneousTravelTime
                engine.event.update(await asyncio.to_thread(local_magnitude,packets,engine.event,travel_model or HomogeneousTravelTime()))
            state['finished']=True
        except Exception as exc:
            state['error']=type(exc).__name__+': '+str(exc)

    @asynccontextmanager
    async def lifespan(app):
        tasks=([asyncio.create_task(monitor.run())] if monitor else
               [asyncio.create_task(playback())] if mode!='live' else
               [asyncio.create_task(sources.rf()),asyncio.create_task(sources.trem())])
        yield
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)

    app=FastAPI(title='Saiu 地震定位系統',lifespan=lifespan)
    app.state.engine=engine
    def snapshot():
        if monitor:
            return monitor.state()
        result=engine.snapshot()
        if mode=='live':
            for rows in sources.rows.values():
                result['stations'].extend(dict(s,age_s=max(0,time.time()-s['end']),online=time.time()-s['end']<30) for s in rows)
        result['sources']=sources.status if mode=='live' else {'模擬波形' if mode=='demo' else '波形回放':dict(state='完成' if state['finished'] else '分析中',count=len(result['stations']),updated=engine.now)}
        result['playback']=dict(state)
        return result

    @app.get('/api/state')
    def api_state(): return snapshot()

    @app.get('/api/events')
    async def events(request:Request):
        async def stream():
            while not await request.is_disconnected():
                yield 'data: '+json.dumps(snapshot(),ensure_ascii=False,allow_nan=False)+'\n\n'
                await asyncio.sleep(1)
        return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

    @app.post('/api/playback')
    async def playback_control(request:Request):
        if mode in ('live','iris'): raise HTTPException(409,'即時模式不支援暫停')
        # Do not allow cross-origin state changes from browser forms.
        if request.headers.get('content-type','').split(';')[0]!='application/json': raise HTTPException(415,'需要 JSON')
        data=await request.json()
        if not isinstance(data,dict) or type(data.get('playing')) is not bool: raise HTTPException(422,'需要 playing 布林值')
        state['playing']=data['playing']; return state

    @app.post('/api/waveform')
    async def waveform(request:Request):
        token=os.getenv('SAIU_INGEST_TOKEN')
        if mode!='live' or not token: raise HTTPException(403,'未啟用波形輸入')
        if not secrets.compare_digest(request.headers.get('authorization',''),'Bearer '+token): raise HTTPException(401,'驗證失敗')
        body=await request.body()
        if len(body)>3_000_000: raise HTTPException(413,'封包過大')
        try: packet=WavePacket.model_validate_json(body)
        except ValueError: raise HTTPException(422,'波形格式無效')
        end=packet.start+len(packet.samples)/packet.rate
        if abs(time.time()-end)>15: raise HTTPException(422,'波形已過期或時鐘有誤')
        if len(engine.buffers)>=1000 and packet.station.id not in engine.buffers: raise HTTPException(429,'測站數量已達上限')
        accepted=engine.ingest(packet)
        return {'accepted':accepted}

    @app.get('/api/export')
    def export(): return snapshot()

    @app.get('/taiwan.json')
    def taiwan(): return FileResponse(ROOT/'assets/taiwan.json')

    @app.get('/reports/{name}')
    def report_image(name:str):
        if not monitor or not name.endswith('.png') or not name[:-4].isdigit():
            raise HTTPException(404,'找不到報告圖片')
        path=monitor.reports.directory/name
        if not path.is_file(): raise HTTPException(404,'找不到報告圖片')
        return FileResponse(path,media_type='image/png')

    @app.get('/')
    def index(): return FileResponse(ROOT/'web/index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'web'),name='static')
    return app
