import asyncio, os, shutil, tempfile, threading, time, uuid, zipfile, subprocess
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from yt_dlp import YoutubeDL
from core import is_http_url
from pipeline import make_clips

APP_DIR = Path(__file__).resolve().parent
WORK_ROOT = Path(os.getenv('CLIPFORGE_WORKDIR', '/tmp/clipforge-jobs'))
WORK_ROOT.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv('WEB_MAX_UPLOAD_MB', '150'))
MAX_URL_MB = int(os.getenv('MAX_URL_MB', '512'))
MAX_CLIPS = int(os.getenv('MAX_CLIPS', '5'))
MAX_JOBS = int(os.getenv('MAX_CONCURRENT_JOBS', '1'))
_jobs = {}
_lock = threading.Lock()
_slots = threading.Semaphore(MAX_JOBS)

app = FastAPI(title='ClipForge', docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=[], allow_methods=['GET','POST'], allow_headers=['*'])

@app.get('/', response_class=HTMLResponse)
def home():
    return HTMLResponse(HTML)

@app.get('/health')
def health():
    return {'ok': True, 'service': 'ClipForge'}

@app.post('/api/jobs')
async def create_job(file: UploadFile | None = File(default=None), url: str = Form(default=''), clip_count: int = Form(default=5), clip_length: int = Form(default=35)):
    url = (url or '').strip().rstrip('.,;!?)]}')
    if file is None and not url:
        raise HTTPException(400, 'Загрузи видео или вставь ссылку.')
    if file is not None and url:
        raise HTTPException(400, 'Выбери видео или ссылку — не оба варианта сразу.')
    if url and not is_http_url(url):
        raise HTTPException(400, 'Ссылка должна быть публичной ссылкой http/https.')
    if url:
        host = (urlparse(url).hostname or '').lower().rstrip('.')
        allowed = ('youtube.com', 'youtu.be', 'vimeo.com', 'tiktok.com', 'instagram.com', 'facebook.com', 'fb.watch', 'twitch.tv', 'twitter.com', 'x.com')
        if not any(host == domain or host.endswith('.' + domain) for domain in allowed):
            raise HTTPException(400, 'Пока поддерживаются публичные ссылки YouTube, Vimeo, TikTok, Instagram, Facebook, Twitch и X.')
    if not 1 <= clip_count <= min(MAX_CLIPS, 8):
        raise HTTPException(400, f'Количество клипов: от 1 до {min(MAX_CLIPS, 8)}.')
    if clip_length not in (20, 30, 35, 45, 60):
        raise HTTPException(400, 'Недопустимая длительность клипа.')
    job_id = uuid.uuid4().hex[:12]
    job_dir = WORK_ROOT / job_id
    job_dir.mkdir(parents=True)
    _jobs[job_id] = {'id': job_id, 'status': 'queued', 'progress': 3, 'message': 'Задание принято', 'created': time.time(), 'dir': str(job_dir)}
    try:
        input_path = job_dir / 'source.mp4'
        if file is not None:
            suffix = Path(file.filename or 'video.mp4').suffix.lower()
            if suffix not in {'.mp4','.mov','.m4v','.webm','.mkv','.avi'}:
                suffix = '.mp4'
            input_path = job_dir / ('source' + suffix)
            size = 0
            with input_path.open('wb') as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk: break
                    size += len(chunk)
                    if size > MAX_UPLOAD_MB * 1024 * 1024:
                        raise HTTPException(413, f'На этом бесплатном сервере лимит загрузки — {MAX_UPLOAD_MB} МБ.')
                    out.write(chunk)
            if size < 1024:
                raise HTTPException(400, 'Файл пустой или слишком маленький.')
        else:
            _jobs[job_id]['message'] = 'Проверяем ссылку и скачиваем видео'
            opts = {'outtmpl': str(input_path), 'format': 'best[ext=mp4]/best', 'merge_output_format':'mp4', 'noplaylist': True,
                    'max_filesize': MAX_URL_MB * 1024 * 1024, 'socket_timeout': 20, 'retries': 2, 'quiet': True,
                    'no_warnings': True, 'restrictfilenames': True}
            try:
                await asyncio.to_thread(_download_url, url, opts)
            except Exception as e:
                raise HTTPException(400, 'Не удалось скачать видео по этой ссылке. Попробуй прямую публичную ссылку на видео.') from e
        _jobs[job_id].update({'input': str(input_path), 'clip_count': clip_count, 'clip_length': clip_length, 'status':'queued', 'message':'Видео готово к анализу'})
        threading.Thread(target=_process_job, args=(job_id,), daemon=True).start()
        return {'id': job_id, 'status': 'queued'}
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        _jobs.pop(job_id, None)
        raise

def _download_url(url, opts):
    with YoutubeDL(opts) as ydl:
        ydl.download([url])

def _process_job(job_id):
    job = _jobs.get(job_id)
    if not job: return
    if not _slots.acquire(blocking=False):
        job.update({'status':'error','message':'Сервер занят. Попробуй ещё раз через пару минут.'})
        return
    try:
        job.update({'status':'processing','progress':10,'message':'Распознаём речь и ищем интересные моменты'})
        outdir = Path(job['dir'])
        clips = make_clips(job['input'], outdir, target_len=job['clip_length'], count=job['clip_count'])
        if not clips:
            raise RuntimeError('Не удалось найти достаточно речи для создания клипов. Попробуй другое видео.')
        job.update({'progress':85,'message':'Упаковываем готовые Shorts'})
        zip_path = outdir / 'clipforge_shorts.zip'
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
            for clip in clips:
                z.write(clip['path'], arcname=Path(clip['path']).name)
            z.writestr('README.txt', 'Clips generated by ClipForge. Vertical 9:16 MP4 with burned-in captions.\n')
        job.update({'status':'done','progress':100,'message':f'Готово! Создано клипов: {len(clips)}','clips':[{'name':Path(c['path']).name,'score':c['score'],'start':round(c['start']),'end':round(c['end']),'text':c['text'][:220]} for c in clips], 'zip':str(zip_path)})
    except Exception as e:
        job.update({'status':'error','message':str(e)[:350] or 'Не удалось обработать видео.'})
    finally:
        _slots.release()

@app.get('/api/jobs/{job_id}')
def job_status(job_id: str):
    job = _jobs.get(job_id)
    if not job: raise HTTPException(404, 'Задание не найдено. Возможно, бесплатный сервер перезапустился.')
    return {k:job[k] for k in ('id','status','progress','message','clips') if k in job}

@app.get('/api/jobs/{job_id}/download')
def download(job_id: str):
    job = _jobs.get(job_id)
    if not job or job.get('status') != 'done' or not Path(job.get('zip','')).is_file():
        raise HTTPException(404, 'Архив ещё не готов или уже удалён.')
    return FileResponse(job['zip'], media_type='application/zip', filename='clipforge-shorts.zip')

HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b0b10"><title>ClipForge — AI Shorts Maker</title><style>
:root{color-scheme:dark;--bg:#09090d;--panel:#12121a;--line:#292936;--muted:#9a9aac;--text:#f7f7fb;--violet:#9b7cff;--cyan:#75e5e0}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 70% -15%,#32234e 0,transparent 40%),var(--bg);color:var(--text);font:15px/1.5 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}button,input,select{font:inherit}button{cursor:pointer}.wrap{max-width:1050px;margin:auto;padding:22px 18px 52px}.nav{display:flex;align-items:center;justify-content:space-between;gap:10px;margin-bottom:50px}.brand{display:flex;align-items:center;gap:11px;font-weight:850;letter-spacing:-.6px;font-size:20px}.mark{width:38px;height:38px;border-radius:13px;background:linear-gradient(135deg,#b49bff,#7052d7);display:grid;place-items:center;color:#fff;font-size:19px;box-shadow:0 7px 30px #7b5ad544}.pill{border:1px solid #343140;border-radius:99px;padding:7px 11px;color:#d7d0ef;font-size:12px}.hero{text-align:center;max-width:740px;margin:0 auto 32px}.eyebrow{display:inline-flex;gap:7px;align-items:center;color:#c5b6ff;background:#a28aff16;border:1px solid #a28aff33;border-radius:99px;padding:6px 11px;font-size:12px;font-weight:700}.dot{width:6px;height:6px;border-radius:50%;background:var(--cyan)}h1{font-size:clamp(37px,7vw,68px);line-height:1.02;letter-spacing:-2.7px;margin:20px 0 15px;font-weight:850}.grad{background:linear-gradient(100deg,#fff 5%,#bdaaff 54%,#7ce6e1 100%);color:transparent;background-clip:text}.sub{color:#b1b0c2;font-size:16px;max-width:570px;margin:auto}.card{max-width:740px;margin:0 auto;background:linear-gradient(160deg,#171721,#101016);border:1px solid #302d40;border-radius:24px;padding:22px;box-shadow:0 25px 80px #0005}.switch{display:grid;grid-template-columns:1fr 1fr;gap:7px;padding:5px;background:#0b0b11;border:1px solid var(--line);border-radius:14px;margin-bottom:16px}.switch button{border:0;border-radius:10px;padding:11px;background:transparent;color:var(--muted);font-weight:700}.switch button.active{background:#29233b;color:#e5ddff;box-shadow:inset 0 0 0 1px #63518a}.drop{display:block;text-align:center;border:1.5px dashed #49415f;border-radius:18px;padding:26px 12px;background:#a38bff08;transition:.2s}.drop:focus-within,.drop:hover{border-color:var(--violet)}.uploadicon{width:50px;height:50px;display:grid;place-items:center;border-radius:16px;margin:0 auto 12px;background:#a58bff1b;color:#c6b5ff;font-size:24px}.drop strong{display:block;font-size:16px}.drop small{display:block;color:var(--muted);margin-top:5px}.fileinput{max-width:100%;margin-top:15px;color:#b9b7c8;font-size:12px}.urlbox{display:none;margin-top:12px}.urlbox input{width:100%;background:#0b0b11;border:1px solid #343342;color:white;border-radius:12px;padding:15px;outline:none}.urlbox input:focus{border-color:var(--violet)}.settings{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:17px 0}.field label{display:block;color:#a8a6b8;font-size:12px;margin:0 0 7px}.field select{width:100%;background:#0b0b11;border:1px solid #343342;color:#eee;border-radius:12px;padding:12px}.primary{width:100%;border:0;border-radius:13px;padding:15px 18px;font-weight:850;color:#100d1a;background:linear-gradient(100deg,#b8a2ff,#8be6e1);box-shadow:0 8px 24px #9c81ff26}.primary:disabled{opacity:.5;cursor:not-allowed}.fine{color:#7f7d90;font-size:11px;text-align:center;margin:12px 0 0}.status{display:none;margin-top:16px;padding:15px;border:1px solid var(--line);background:#0b0b11;border-radius:15px}.statushead{display:flex;justify-content:space-between;gap:10px;font-weight:700}.track{height:6px;border-radius:99px;background:#2a2936;margin:12px 0;overflow:hidden}.bar{height:100%;width:0;background:linear-gradient(90deg,#a88aff,#75e5e0);transition:width .5s}.msg{font-size:13px;color:#b8b6c8}.results{display:none;margin-top:22px}.results h2{font-size:22px;letter-spacing:-.6px}.clip{padding:14px 0;border-top:1px solid var(--line)}.cliptop{display:flex;justify-content:space-between;gap:10px}.clipname{font-weight:800}.score{color:var(--cyan);font-size:12px}.excerpt{color:#aaa8ba;font-size:13px;margin:7px 0 0}.download{display:block;text-align:center;text-decoration:none;margin-top:14px;border:1px solid #5e4c86;color:#e3d9ff;border-radius:12px;padding:13px;font-weight:800}.features{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:44px 0}.feature{border:1px solid #252430;background:#101016a8;border-radius:17px;padding:17px}.ficon{font-size:22px;margin-bottom:10px}.feature strong{display:block;font-size:14px}.feature p{color:var(--muted);font-size:12px;margin:5px 0 0}.footer{text-align:center;color:#777587;font-size:11px}.toast{color:#ffb5b5;font-size:13px;margin-top:10px;display:none}@media(max-width:600px){.wrap{padding:15px 13px 32px}.nav{margin-bottom:35px}.hero{margin-bottom:25px}h1{letter-spacing:-1.7px}.sub{font-size:14px}.card{padding:15px;border-radius:20px}.features{grid-template-columns:1fr;margin:28px 0}.feature{display:flex;gap:12px;align-items:flex-start;padding:13px}.ficon{margin:0}.settings{gap:9px}.pill{font-size:10px}.drop{padding:22px 9px}}
</style></head><body><div class="wrap"><header class="nav"><div class="brand"><div class="mark">✦</div>ClipForge</div><div class="pill">✦ AI VIDEO WORKFLOW</div></header><section class="hero"><div class="eyebrow"><span class="dot"></span> LONG VIDEO → SHORTS</div><h1>Длинное видео.<br><span class="grad">Лучшие моменты.</span></h1><p class="sub">Находим цепляющие фрагменты, добавляем субтитры и собираем вертикальные ролики — без ручной нарезки.</p></section><main class="card"><div class="switch"><button id="fileTab" class="active" type="button">↑ Загрузить видео</button><button id="urlTab" type="button">↗ Вставить ссылку</button></div><form id="jobForm"><label class="drop" id="drop"><span class="uploadicon">↑</span><strong>Выбери видео для нарезки</strong><small>MP4, MOV, MKV, WEBM · лимит бесплатного сервера — ''' + str(MAX_UPLOAD_MB) + r''' МБ</small><input class="fileinput" id="videoFile" type="file" accept="video/*,.mkv,.webm,.avi"></label><div class="urlbox" id="urlBox"><input id="videoUrl" type="url" placeholder="https://youtube.com/watch?v=…" autocomplete="url"></div><div class="settings"><div class="field"><label for="count">Количество Shorts</label><select id="count"><option value="3">3 клипа</option><option value="5" selected>5 клипов</option><option value="8">8 клипов</option></select></div><div class="field"><label for="length">Длительность каждого</label><select id="length"><option value="20">~20 секунд</option><option value="30">~30 секунд</option><option value="35" selected>~35 секунд</option><option value="45">~45 секунд</option><option value="60">~60 секунд</option></select></div></div><button class="primary" id="submit" type="submit">✦ Найти лучшие моменты</button><p class="fine">Видео обрабатывается на сервере и временно хранится для подготовки результата.</p><div class="toast" id="toast"></div></form><section class="status" id="status"><div class="statushead"><span id="statusTitle">Обработка видео</span><span id="pct">0%</span></div><div class="track"><div class="bar" id="bar"></div></div><div class="msg" id="msg">Подготавливаем видео…</div></section><section class="results" id="results"><h2>Твои клипы</h2><div id="clipList"></div><a id="download" class="download" href="#">↓ Скачать все Shorts (.ZIP)</a></section></main><section class="features"><div class="feature"><div class="ficon">⌕</div><div><strong>Умный отбор</strong><p>Ранжирование фрагментов по речи и признакам интересного момента.</p></div></div><div class="feature"><div class="ficon">▣</div><div><strong>Формат 9:16</strong><p>Вертикальный MP4, подготовленный для YouTube Shorts.</p></div></div><div class="feature"><div class="ficon">Aa</div><div><strong>Субтитры внутри видео</strong><p>Автоматические субтитры с крупным контрастным оформлением.</p></div></div></section><footer class="footer">ClipForge · ранняя версия · Бесплатный сервер может ограничивать время обработки и доступность.</footer></div><script>
const fileTab=document.getElementById('fileTab'),urlTab=document.getElementById('urlTab'),drop=document.getElementById('drop'),urlBox=document.getElementById('urlBox'),file=document.getElementById('videoFile'),url=document.getElementById('videoUrl'),form=document.getElementById('jobForm'),submit=document.getElementById('submit'),toast=document.getElementById('toast'),statusBox=document.getElementById('status'),bar=document.getElementById('bar'),pct=document.getElementById('pct'),msg=document.getElementById('msg'),results=document.getElementById('results');let mode='file',busy=false;
function setMode(m){mode=m;fileTab.classList.toggle('active',m==='file');urlTab.classList.toggle('active',m==='url');drop.style.display=m==='file'?'block':'none';urlBox.style.display=m==='url'?'block':'block';urlBox.style.display=m==='url'?'block':'none';file.required=m==='file';url.required=m==='url'}fileTab.onclick=()=>setMode('file');urlTab.onclick=()=>setMode('url');setMode('file');
form.addEventListener('submit',async e=>{e.preventDefault();if(busy)return;toast.style.display='none';results.style.display='none';statusBox.style.display='block';msg.textContent='Отправляем видео…';bar.style.width='3%';pct.textContent='3%';submit.disabled=true;busy=true;let data=new FormData();if(mode==='file'){if(!file.files[0]){showError('Сначала выбери видео.');return}data.append('file',file.files[0])}else{if(!url.value.trim()){showError('Вставь ссылку на видео.');return}data.append('url',url.value.trim())}data.append('clip_count',document.getElementById('count').value);data.append('clip_length',document.getElementById('length').value);try{const r=await fetch('/api/jobs',{method:'POST',body:data});const d=await r.json();if(!r.ok)throw Error(d.detail||'Не удалось создать задание');poll(d.id)}catch(err){showError(err.message)}});
function showError(s){toast.textContent=s;toast.style.display='block';submit.disabled=false;busy=false;statusBox.style.display='none'}
async function poll(id){try{const r=await fetch('/api/jobs/'+id);const d=await r.json();if(!r.ok)throw Error(d.detail||'Задание потеряно');let p=d.status==='done'?100:d.status==='processing'?Math.max(15,Math.min(90,Number(d.progress)||20)):d.status==='queued'?8:0;bar.style.width=p+'%';pct.textContent=p+'%';msg.textContent=d.message||'Обработка…';if(d.status==='done'){document.getElementById('statusTitle').textContent='Готово';results.style.display='block';document.getElementById('download').href='/api/jobs/'+id+'/download';document.getElementById('clipList').innerHTML=(d.clips||[]).map((c,i)=>`<div class="clip"><div class="cliptop"><span class="clipname">Short ${i+1} · ${fmt(c.start)}–${fmt(c.end)}</span><span class="score">Оценка ${c.score}/100</span></div><p class="excerpt">${escapeHtml(c.text)}</p></div>`).join('');submit.disabled=false;busy=false;return}if(d.status==='error')throw Error(d.message);setTimeout(()=>poll(id),2500)}catch(err){showError(err.message)}}
function fmt(s){return Math.floor(s/60)+':'+String(s%60).padStart(2,'0')}function escapeHtml(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
</script></body></html>'''
