'use strict';
const $=id=>document.getElementById(id);
const NS='http://www.w3.org/2000/svg';
const colors={'0':'#688094','1':'#689dbc','2':'#53b4bb','3':'#63c892','4':'#f4cc67','5弱':'#f6a64b','5強':'#f27b46','6弱':'#ec575b','6強':'#dc3e78','7':'#bd7ad7'};
let current=null,selected=null,layer='stations',connected=false;
const fmt=(n,d=1)=>Number.isFinite(n)?n.toFixed(d):'—';
const time=t=>new Date(t*1000).toLocaleTimeString('zh-TW',{timeZone:'Asia/Taipei',hour12:false});
const el=(tag,attrs={},text='')=>{const n=document.createElement(tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);n.textContent=text;return n;};
function svg(tag,attrs){const n=document.createElementNS(NS,tag);for(const[k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;}
// Equirectangular projection with matched km scale in x and y at 24°N.
function xy(lon,lat){return [350+(lon-120.7)*105*.9135,345-(lat-23.65)*105];}
function badge(value){const b=el('span',{class:'badge'},value??'—');b.style.background=colors[value]||'#556675';return b;}
fetch('/taiwan.json').then(r=>{if(!r.ok)throw Error('map');return r.json();}).then(geo=>{for(const f of geo.features){const polygons=f.geometry.type==='MultiPolygon'?f.geometry.coordinates:[f.geometry.coordinates];for(const polygon of polygons){let d='';for(const ring of polygon){d+=ring.map((c,i)=>(i?'L':'M')+xy(...c).join(',')).join('')+'Z';}$('land').append(svg('path',{d,class:'land','fill-rule':'evenodd'}));}}}).catch(()=>{$('map-detail').textContent='底圖載入失敗；測站座標仍可顯示。';});
function render(data){current=data;
 $('clock').textContent=time(data.now)+' UTC+8';
 $('mode').textContent=({demo:'模擬演練',live:'即時觀測',replay:'實測回放',iris:'IRIS 地震定位'})[data.mode];
 $('pause').hidden=['live','iris'].includes(data.mode);$('pause').disabled=data.playback.finished;
 $('pause').textContent=data.playback.finished?'回放完成':data.playback.playing?'暫停回放':'繼續回放';
 const e=data.event,rows=data.stations;
 $('title').textContent=e?e.quality:(rows.length?'監測中':'等待測站資料');
 $('subtitle').textContent=e?`${new Date(e.origin*1000).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false})} 發震 · 第 ${e.revision} 次推估 · 方位缺口 ${fmt(e.gap_deg,0)}° · ${e.travel_model?.startsWith('table:')?'走時表':'固定速度'}${e.magnitude_saturation?' · 規模可能飽和':''}`:'確認多站訊號後，開始定位。';
 $('notice').textContent=(['live','iris'].includes(data.mode)?'自動地震定位':'此為'+(data.mode==='demo'?'模擬資料':'實測回放'))+' · 深度與規模未經台灣區域校正。'+(data.playback.error?' 回放失敗：'+data.playback.error:'');
 $('magnitude').textContent=e?fmt(e.magnitude):'—';$('magnitude-method').textContent=e?.magnitude_type||(['demo','replay','live'].includes(data.mode)?'Mpd · 初始 P 波估算':'ML · 實驗性估算');$('depth').textContent=e?fmt(e.depth_km):'—';$('used').textContent=e?e.count:0;$('rms').textContent=e?fmt(e.rms_s,2):'—';
 const levels=Object.keys(colors);const max=rows.filter(s=>s.online&&s.kind==='waveform').reduce((v,s)=>levels.indexOf(s.intensity)>levels.indexOf(v)?s.intensity:v,null);
 $('max-int').textContent=max??'—';$('max-int').style.color=colors[max]||'';
 $('online').textContent=rows.filter(s=>s.online).length;$('total').textContent=`共 ${rows.length} 站`;$('station-count').textContent=rows.length;
 $('packet-count').textContent=`${data.packets} 個波形封包 · ${data.rejected} 個已拒絕`;
 $('sources').replaceChildren();for(const[name,s]of Object.entries(data.sources)){const row=el('div',{class:'source-row'});row.append(el('strong',{},name));const age=s.updated?data.now-s.updated:Infinity;row.append(el('span',{class:age>30?'warn':''},`${age>30&&s.updated?'資料過期':s.state} · ${s.count} 站`));$('sources').append(row);}
 if(data.mode==='live'){const row=el('div',{class:'source-row'});row.append(el('strong',{},'IRIS / EarthScope'),el('span',{class:'warn'},'以 FDSN 指令下載／回放'));$('sources').append(row);}
 if(!selected&&rows.some(s=>s.kind==='waveform'))selected=rows.find(s=>s.kind==='waveform').id;
 renderStations();renderMap();renderRegions();renderWave();renderConvergence();
}
function renderStations(){if(!current)return;const q=$('search').value.toLowerCase();const rows=current.stations.filter(s=>(s.name+' '+s.id+' '+s.source).toLowerCase().includes(q)).sort((a,b)=>Number(b.online)-Number(a.online)||(b.pga||0)-(a.pga||0));
 const out=[];for(const s of rows){const row=el('button',{class:'station-row'+(selected===s.id?' selected':'')+(!s.online?' stale':''),'aria-pressed':String(selected===s.id)});const name=el('span',{class:'station-name'},s.name);name.append(el('small',{},`${s.source} · ${s.online?(s.picked?'P 波觸發':s.kind==='summary'?'摘要':'分析中'):'已過期'} · ${fmt(s.age_s,0)}s`));row.append(name,el('span',{class:'station-pga'},fmt(s.pga,2)),badge(s.kind==='summary'?s.reported_intensity:s.intensity));row.onclick=()=>{selected=s.id;renderStations();renderMap();renderWave();};out.push(row);}
 $('stations').replaceChildren(...(out.length?out:[el('p',{class:'empty'},q?'找不到測站':'尚無測站資料')]));}
function renderMap(){const e=current.event;const group=$('markers');group.replaceChildren();$('waves').replaceChildren();if(e){const [cx,cy]=xy(e.lon,e.lat);const star=svg('text',{x:cx,y:cy+7,'text-anchor':'middle',fill:'#fff','font-size':27,stroke:'#0b1119','stroke-width':1,'paint-order':'stroke'});star.textContent='★';group.append(star);$('coordinates').textContent=`${fmt(e.lat,3)}°N  ${fmt(e.lon,3)}°E · 水平 σ ${fmt(e.horizontal_sigma_km)} km`;}else $('coordinates').textContent='震央 —';
 const rows=layer==='stations'?current.stations:current.regions;
 for(const s of rows){const [cx,cy]=xy(s.lon,s.lat),color=colors[s.intensity]||'#8096aa';const n=svg('circle',{cx,cy,r:layer==='regions'?7:(s.id===selected?6:3.8),fill:color,stroke:s.id===selected?'#fff':'#0b1119','stroke-width':s.id===selected?2:1,opacity:s.online===false?.3:.95,tabindex:0,role:'button','aria-label':s.name});const title=svg('title',{});title.textContent=s.name+' · '+fmt(s.pga,2)+' gal';n.append(title);const choose=()=>{if(s.id){selected=s.id;renderStations();renderWave();}$('map-detail').textContent=s.name+' · '+fmt(s.pga,2)+' gal'+(s.end?' · '+time(s.end):' · 模型推估');renderMap();};n.addEventListener('click',choose);n.addEventListener('keydown',ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();choose();}});group.append(n);}}
function renderRegions(){
 const reports=current.reports||[];
 $('notification').textContent=current.notification?(current.notification.enabled?`Discord 已啟用 · ${current.notification.pending} 筆待傳送`:'Discord 未設定'):'實測回放 · 本機分析';
 const nodes=reports.map(r=>{const n=el('div',{class:'region'}),e=r.event;
 const a=el('a',{href:r.image,target:'_blank',rel:'noreferrer'},`${new Date(e.origin*1000).toLocaleString('zh-TW',{timeZone:'Asia/Taipei',hour12:false})} · M ${fmt(e.magnitude)} · ${fmt(e.depth_km)} km`);
 n.append(a,el('small',{},r.sent?'Discord 已傳送':r.error||'本機已保存'));return n;});
 $('regions').replaceChildren(...(nodes.length?nodes:[el('p',{class:'empty'},'等待多站定位報告。')]));
}
function canvas(id){const c=$(id),dpr=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight; c.width=w*dpr;c.height=h*dpr;const ctx=c.getContext('2d');ctx.scale(dpr,dpr);ctx.strokeStyle='#223344';ctx.lineWidth=1;for(let y=25;y<h;y+=40){ctx.beginPath();ctx.moveTo(15,y);ctx.lineTo(w-15,y);ctx.stroke();}return [ctx,w,h];}
function line(ctx,w,h,values,color){if(values.length<2)return;const scale=Math.max(.01,...values.map(Math.abs));ctx.strokeStyle=color;ctx.lineWidth=1.4;ctx.beginPath();values.forEach((v,i)=>{const x=18+i/(values.length-1)*(w-36),y=h/2-v/scale*(h*.34);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);});ctx.stroke();}
function renderWave(){const s=current.stations.find(s=>s.id===selected);const [ctx,w,h]=canvas('waveform');if(s&&s.waveform?.length){$('wave-title').textContent=s.name+' · 波形';line(ctx,w,h,s.waveform,'#67ddce');$('wave-label').textContent=`Z 軸 · ±${fmt(Math.max(...s.waveform.map(Math.abs)),2)} gal`;$('wave-note').textContent=`${s.id} · 最後資料 ${time(s.end)} · 最近 20 秒（顯示降採樣）`;}else{$('wave-title').textContent=s?s.name+' · 摘要':'測站波形';$('wave-label').textContent='Z 軸 · cm/s²';$('wave-note').textContent=s?'此來源提供震度摘要，無原始波形。':'選取有波形的測站。';}}
function renderConvergence(){const [ctx,w,h]=canvas('convergence');const history=current.history.filter(e=>e.id===current.event?.id);line(ctx,w,h,history.map(e=>e.depth_km),'#67ddce');line(ctx,w,h,history.map(e=>e.rms_s),'#f7bd65');}
$('search').addEventListener('input',renderStations);
$('pause').onclick=async()=>{try{const r=await fetch('/api/playback',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({playing:!current.playback.playing})});if(!r.ok)throw Error();const s=await r.json();current.playback=s;render(current);}catch{$('notice').textContent='操作失敗，請再試一次。';}};
$('about-button').onclick=()=>$('about').showModal();$('close-about').onclick=()=>$('about').close();
$('export').onclick=()=>{if(!current)return;const blob=new Blob([JSON.stringify(current,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob),a=el('a',{href:url,download:`saiu-${current.mode}-${Math.floor(current.now)}.json`});a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
for(const b of document.querySelectorAll('[data-layer]'))b.onclick=()=>{layer=b.dataset.layer;for(const other of document.querySelectorAll('[data-layer]')){other.classList.toggle('active',other===b);other.setAttribute('aria-pressed',String(other===b));}if(current)renderMap();};
window.addEventListener('resize',()=>{if(current){renderWave();renderConvergence();}});
const stream=new EventSource('/api/events');stream.onmessage=event=>{try{const data=JSON.parse(event.data);render(data);connected=true;$('connection').textContent='連線正常';$('connection').classList.remove('error');}catch{$('connection').textContent='資料格式異常';}};stream.onerror=()=>{connected=false;$('connection').textContent='連線中斷 · 重試中';$('connection').classList.add('error');};
