import argparse
import json
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description='Saiu · 台灣地震定位系統')
    sub=parser.add_subparsers(dest='command',required=True)
    serve=sub.add_parser('serve'); serve.add_argument('--mode',choices=['iris','demo','live','replay'],default='iris')
    serve.add_argument('--travel-table',type=Path); serve.add_argument('--file',type=Path); serve.add_argument('--speed',type=float,default=1)
    serve.add_argument('--host',default='127.0.0.1'); serve.add_argument('--port',type=int,default=8765)
    demo=sub.add_parser('demo-file'); demo.add_argument('--output',type=Path,default=Path('data/demo.jsonl'))
    fetch=sub.add_parser('fdsn'); fetch.add_argument('--network',default='TW,IU'); fetch.add_argument('--station',default='TATO')
    fetch.add_argument('--start',required=True,help='UTC ISO time'); fetch.add_argument('--end',required=True)
    fetch.add_argument('--base',default='https://service.earthscope.org'); fetch.add_argument('--output',type=Path,default=Path('data/fdsn.jsonl'))
    rf=sub.add_parser('rf-download'); rf.add_argument('--station',required=True)
    rf.add_argument('--start',type=int,required=True,help='Unix milliseconds'); rf.add_argument('--end',type=int,required=True)
    rf.add_argument('--output',type=Path,default=Path('data/rf-raw.json'))
    conv=sub.add_parser('rf-convert'); conv.add_argument('--file',type=Path,required=True)
    conv.add_argument('--metadata',type=Path,required=True); conv.add_argument('--output',type=Path,default=Path('data/rf.jsonl'))
    iris=sub.add_parser('iris-locate',help='台灣周邊測站下載與離線定位')
    iris.add_argument('--start',required=True); iris.add_argument('--end',required=True)
    iris.add_argument('--output',type=Path,default=Path('data/iris-event'))
    iris.add_argument('--network',default='TW,IU'); iris.add_argument('--base',default='https://service.earthscope.org')
    iris.add_argument('--bounds',nargs=4,type=float,default=[21,26.8,118,124],metavar=('MINLAT','MAXLAT','MINLON','MAXLON'))
    iris.add_argument('--max-stations',type=int,default=12); iris.add_argument('--workers',type=int,default=3)
    iris.add_argument('--travel-table',type=Path); iris.add_argument('--homogeneous',action='store_true')
    loc=sub.add_parser('locate',help='依波形自動拾取到時並輸出定位報告')
    loc.add_argument('--file',type=Path,required=True); loc.add_argument('--output',type=Path,default=Path('data/location.json'))
    loc.add_argument('--travel-table',type=Path); loc.add_argument('--catalog',type=Path,help='僅定位後獨立比對的 USGS GeoJSON')
    loc.add_argument('--image',type=Path,help='輸出最後一起地震的定位圖')
    table=sub.add_parser('travel-table',help='下載固定版本 ExpTech 走時表')
    table.add_argument('--output',type=Path,default=Path('data/exptech-time.json'))
    args=parser.parse_args()
    if args.command=='serve':
        if args.speed<=0 or args.speed>100: parser.error('--speed 必須在 0 到 100 之間')
        if args.mode=='replay' and (not args.file or not args.file.is_file()): parser.error('回放需要 --file')
        import uvicorn
        from .app import create_app
        from .travel import TableTravelTime
        table=args.travel_table
        if args.mode=='iris' and table is None:
            import os
            table=Path(os.getenv('SAIU_TRAVEL_TABLE','data/exptech-time.json'))
            if not table.exists():
                from .travel import download_exptech_table
                print('下載定位走時表…',flush=True)
                download_exptech_table(table)
        model=TableTravelTime(table) if table else None
        uvicorn.run(create_app(args.mode,args.file if args.mode=='replay' else None,args.speed,model),host=args.host,port=args.port)
    elif args.command=='travel-table':
        from .travel import download_exptech_table
        model=download_exptech_table(args.output)
        print(f'{model.name} → {args.output}')
    elif args.command in ('iris-locate','locate'):
        from .travel import TableTravelTime, download_exptech_table
        from .replay import analyze, compare_catalog
        if args.command=='iris-locate':
            from .iris import download_dataset
            path,manifest=download_dataset(args.output,args.start,args.end,args.base,args.network,args.bounds,args.max_stations,args.workers)
            if not manifest['location_ready']:
                raise SystemExit(f"只有 {manifest['station_count']} 個可用站；至少需要四站。詳見 manifest.json。")
            if args.homogeneous:
                model=None
            else:
                table_path=args.travel_table or args.output/'travel-time.json'
                model=TableTravelTime(table_path) if args.travel_table or table_path.exists() else download_exptech_table(table_path)
            report=analyze(path,model);target=args.output/'analysis.json'
        else:
            model=TableTravelTime(args.travel_table) if args.travel_table else None
            report=analyze(args.file,model);target=args.output
            if args.catalog:
                report['catalog_comparison']=compare_catalog(report,json.loads(args.catalog.read_text(encoding='utf-8')))
        from .magnitude import local_magnitude
        from .replay import read_packets
        from .travel import HomogeneousTravelTime
        wave_path=path if args.command=='iris-locate' else args.file
        packets=list(read_packets(wave_path))
        for item in report['events']:
            item['last'].update(local_magnitude(packets,item['last'],model or HomogeneousTravelTime()))
        if report['events']:
            report['snapshot']['event']=report['events'][-1]['last']
        image_path=args.output/'location.png' if args.command=='iris-locate' else args.image
        if image_path and report['events']:
            from .reporting import render_map
            render_map(report['events'][-1]['last'],report['snapshot']['stations'],image_path)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
        print(f"{report['station_count']} stations, {len(report['picks'])} picks, {len(report['events'])} events → {target}")
        for event in report['events']:
            e=event['last']
            print(f"{e['lat']:.4f}N {e['lon']:.4f}E, depth {e['depth_km']:.1f} km, magnitude {e['magnitude']}, RMS {e['rms_s']:.3f}s, {e['count']} stations")
        if not report['events']: raise SystemExit('未取得符合多站品質條件的定位；已保存到時與分析報告。')
    elif args.command=='demo-file':
        from .demo import make_demo
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('w') as f:
            for batch in make_demo(1793500000):
                for packet in batch: f.write(packet.model_dump_json()+'\n')
        print(args.output)
    elif args.command=='fdsn':
        from .providers import fdsn_fetch
        packets=fdsn_fetch(args.network,args.station,args.start,args.end,args.base)
        if not packets: raise SystemExit('沒有完整、連續的三軸波形。請調整測站、時段或資料中心。')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text('\n'.join(p.model_dump_json() for p in packets)+'\n')
        print(f'{len(packets)} packets → {args.output}')
    elif args.command=='rf-convert':
        from .providers import rf_normalize
        from .models import Station
        meta=json.loads(args.metadata.read_text(encoding='utf-8'))
        packets=rf_normalize(json.loads(args.file.read_text(encoding='utf-8')),Station.model_validate(meta['station']),float(meta['scale_to_cm_s2']),tuple(meta['axes_zne']))
        if not packets: raise SystemExit('沒有可轉換的連續波形')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(''.join(p.model_dump_json()+'\n' for p in packets),encoding='utf-8')
        print(f'{len(packets)} packets → {args.output}')
    elif args.command=='rf-download':
        if not 0 < args.end-args.start <= 120000: parser.error('每次下載範圍限 120 秒')
        import httpx
        # This consumes the provider's quota; never run on a polling loop.
        response=httpx.get('https://rptes.com/RFEQdatabaseDownload/waveform',params=dict(station=args.station,start=args.start,end=args.end),timeout=30,follow_redirects=True)
        response.raise_for_status(); raw=response.json()
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(raw,ensure_ascii=False),encoding='utf-8')
        print(f'原始資料已存至 {args.output}；請依來源單位、時序與三軸方向轉為 WavePacket 再回放。')

if __name__=='__main__': main()
