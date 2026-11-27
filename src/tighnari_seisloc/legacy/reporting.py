"""Portable location map and a persistent Discord delivery ledger."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import hashlib
import math
from pathlib import Path
import httpx

ROOT = Path(__file__).parent
TZ = ZoneInfo('Asia/Taipei')


def local_time(ts):
    return datetime.fromtimestamp(ts, TZ).strftime('%Y-%m-%d %H:%M:%S')


def render_map(event, stations, target):
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new('RGB', (1100, 900), '#0f1113')
    draw = ImageDraw.Draw(image)
    font_path = ROOT/'assets/NotoSansTC-Regular.ttf'
    def font(size):
        return ImageFont.truetype(str(font_path), size)
    def xy(lon, lat):
        return (620+(lon-121)*145*math.cos(math.radians(24)), 475-(lat-23.5)*145)
    for lat in range(20, 28):
        y = xy(121, lat)[1]
        draw.line((380, y, 1070, y), fill='#202932')
        draw.text((1040, y+3), f'{lat}N', font=font(14), fill='#8495a6')
    for lon in range(117, 126):
        x = xy(lon, 24)[0]
        draw.line((x, 110, x, 845), fill='#202932')
    geo = json.loads((ROOT/'assets/taiwan.json').read_text())
    for feature in geo['features']:
        geom = feature['geometry']
        polygons = geom['coordinates'] if geom['type']=='MultiPolygon' else [geom['coordinates']]
        for polygon in polygons:
            for i, ring in enumerate(polygon):
                draw.polygon([xy(*p[:2]) for p in ring], fill='#30363d' if i==0 else '#0f1113', outline='#687585')
    used = set(event['used'])
    for st in stations:
        x, y = xy(st['lon'], st['lat'])
        color = '#67ddce' if st['id'] in used else '#637080'
        draw.polygon([(x,y-5),(x-5,y+4),(x+5,y+4)], fill=color)
    x, y = xy(event['lon'], event['lat'])
    points = [(x+math.sin(i*math.pi/5)*(17 if i%2==0 else 7),
               y-math.cos(i*math.pi/5)*(17 if i%2==0 else 7)) for i in range(10)]
    draw.polygon(points, fill='#ff625f', outline='white')
    # Opaque report column avoids drawing text over the land or stations.
    draw.rectangle((0, 0, 375, 900), fill='#0f1113')
    draw.text((28, 24), 'Tighnari / 地震定位報告', font=font(31), fill='white')
    mag = event.get('magnitude')
    text = [f'規模  {mag:.1f}' if mag is not None else '規模  資料不足',
            event.get('magnitude_type', 'Mpd · 初始 P 波估算'),
            f"深度  {event['depth_km']:.1f} km", '', '震央位置',
            f"{event['lat']:.4f}°N", f"{event['lon']:.4f}°E", '', '發震時間 (UTC+8)',
            local_time(event['origin']), '', f"參與定位  {event['count']} 站",
            f"到時 RMS  {event['rms_s']:.3f} 秒", f"方位缺口  {event['gap_deg']:.0f}°",
            f"深度 σ  {event['depth_sigma_km']:.1f} km"]
    draw.multiline_text((28, 105), '\n'.join(text), font=font(22), fill='#d4dce4', spacing=10)
    note = '深度達搜尋下限，估值約束不足' if event['depth_km'] <= 1.01 else '深度與規模尚未經區域校正'
    draw.text((28, 795), '自動定位 · 研究估算\n'+note, font=font(18), fill='#f4cc67', spacing=8)
    draw.text((395, 850), '★ 震央     ▲ 定位測站   /   IRIS · EarthScope', font=font(19), fill='#b3c1ce')
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    image.save(target)
    return target


def discord_payload(event):
    from tighnari_seisloc.services.notifications import content
    mag = event.get('magnitude')
    return dict(username='Tighnari-SeisLOC', content=content(event), allowed_mentions={'parse': []}, embeds=[dict(
        title='TWERG Tighnari 自動定位報告', color=0x67DDCE,
        description='IRIS / EarthScope 多站波形自動分析',
        fields=[dict(name='規模', value=f"{mag:.1f} ({event.get('magnitude_type', 'Mpd')})" if mag is not None else '資料不足，無法估算', inline=True),
                dict(name='深度', value=f"{event['depth_km']:.1f} 公里", inline=True),
                dict(name='發生時間', value=local_time(event['origin'])+' (UTC+8)', inline=False),
                dict(name='震央位置', value=f"{event['lat']:.4f}, {event['lon']:.4f}", inline=False),
                dict(name='定位品質', value=f"{event['count']} 站\nRMS {event['rms_s']:.3f}s\n方位缺口 {event['gap_deg']:.0f}°\n深度 σ {event['depth_sigma_km']:.1f} km（局部線性估計）", inline=False)],
        image={'url': 'attachment://location.png'},
        footer={'text':'僅供參考，地震資訊應以氣象署為準\n規模與深度未經區域校正'})])


class Reports:
    def __init__(self, directory, webhook=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'ledger.json'
        self.rows = json.loads(self.path.read_text()) if self.path.exists() else []
        self.webhook = webhook

    def save(self):
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.rows, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        temp.replace(self.path)

    def record(self, event, stations):
        from .location import distance
        for row in self.rows:
            prior = row['event']
            if abs(prior['origin']-event['origin'])<30 and distance(prior['lat'],prior['lon'],event['lat'],event['lon'])<50:
                return row
        key = str(int(event['origin']*1000))
        image = render_map(event, stations, self.directory/f'{key}.png')
        row = dict(event=event, image=str(image.resolve()), sent=False, attempts=0)
        self.rows.append(row)
        self.save()
        (self.directory/f'{key}.json').write_text(json.dumps(event, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        return row

    def deliver(self):
        """Retry each destination independently; never log webhook URLs or HTTP bodies."""
        import time
        from tighnari_seisloc.config import webhook_urls
        urls = webhook_urls(self.webhook)
        if not urls:
            return
        for row in self.rows:
            if row['sent']:
                continue
            for url in urls:
                key = hashlib.sha256(url.encode()).hexdigest()
                state = row if len(urls) == 1 and not row.get('deliveries') else row.setdefault('deliveries', {}).setdefault(key, dict(sent=False, attempts=0))
                if state['sent'] or time.time() < state.get('retry_at', 0):
                    continue
                state['attempts'] += 1
                try:
                    with open(row['image'], 'rb') as file:
                        response = httpx.post(url, params={'wait':'true'},
                            data={'payload_json':json.dumps(discord_payload(row['event']), ensure_ascii=False)},
                            files={'files[0]':('location.png',file,'image/png')}, timeout=30)
                    if response.status_code==429:
                        state['retry_at'] = time.time()+max(1, min(float(response.json().get('retry_after',60)),3600))
                    else:
                        response.raise_for_status()
                        state['sent'] = True
                        state.pop('error', None)
                except (httpx.HTTPError, OSError, ValueError):
                    state['error'] = 'Discord 傳送失敗，將重試'
                    state['retry_at'] = time.time()+min(3600, 30*2**min(state['attempts'],6))
                self.save()
            if row.get('deliveries'):
                row['sent'] = all(row['deliveries'][hashlib.sha256(url.encode()).hexdigest()]['sent'] for url in urls)
                self.save()
