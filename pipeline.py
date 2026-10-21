import os, subprocess
from pathlib import Path
from faster_whisper import WhisperModel
from core import score_candidate, ass_time

MODEL = os.getenv('WHISPER_MODEL', 'tiny')
_MODEL_INSTANCE = None

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors='replace')
    if p.returncode != 0:
        raise RuntimeError(f'Command failed ({p.returncode}): {p.stderr[-1800:]}')
    return p.stdout.strip()

def duration(path):
    return float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',path]))

def get_model():
    global _MODEL_INSTANCE
    if _MODEL_INSTANCE is None:
        # CPU int8 is the most portable default for inexpensive hosted workers.
        _MODEL_INSTANCE = WhisperModel(MODEL, device=os.getenv('WHISPER_DEVICE', 'cpu'),
                                       compute_type=os.getenv('WHISPER_COMPUTE_TYPE', 'int8'))
    return _MODEL_INSTANCE

def transcript_segments(path):
    model = get_model()
    segments, info = model.transcribe(path, vad_filter=True, word_timestamps=True, beam_size=3)
    out = []
    for s in segments:
        words = []
        if s.words:
            for w in s.words:
                words.append({'start': float(w.start), 'end': float(w.end), 'word': w.word.strip()})
        out.append({'start': float(s.start), 'end': float(s.end), 'text': s.text.strip(), 'words': words})
    return out, info.language


def write_ass(path, words, offset, clip_duration):
    header = '''[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 2\nScaledBorderAndShadow: yes\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,66,&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,1,0,0,0,100,100,0,0,1,5,1,2,70,70,250,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n'''
    events = []
    # Group words in compact 3–5 word caption chunks.
    for i in range(0, len(words), 4):
        group = words[i:i+4]
        if not group: continue
        start = max(0.0, group[0]['start'] - offset)
        end = min(clip_duration, group[-1]['end'] - offset + 0.12)
        if end <= start: continue
        txt = ' '.join(w['word'] for w in group).replace('{','').replace('}','').strip()
        events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{txt}")
    Path(path).write_text(header + '\n'.join(events), encoding='utf-8')


def make_clips(video_path, workdir, target_len=35, count=5):
    work = Path(workdir)
    audio = work / 'audio.wav'
    run(['ffmpeg','-y','-i',video_path,'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)])
    full_duration = duration(video_path)
    segs, lang = transcript_segments(str(audio))
    if not segs or full_duration < 8:
        return []
    # Candidate windows anchored to speech; deduplicate nearly identical transcript windows.
    candidates = []
    stride = max(8, int(target_len * 0.58))
    t = 0.0
    while t < full_duration:
        start = max(0.0, t)
        end = min(full_duration, start + target_len)
        relevant = [s for s in segs if s['end'] > start and s['start'] < end]
        if relevant:
            # Snap boundaries to transcript segment edges to avoid mid-word cuts.
            actual_start = max(0.0, relevant[0]['start'] - 0.15)
            actual_end = min(full_duration, relevant[-1]['end'] + 0.2)
            if actual_end - actual_start >= min(10, target_len * 0.5):
                text = ' '.join(s['text'] for s in relevant)
                speech = sum(max(0, min(end,s['end']) - max(start,s['start'])) for s in relevant)
                coverage = speech / max(1, end-start)
                sc = score_candidate(text, coverage, actual_end-actual_start)
                candidates.append({'start':actual_start,'end':actual_end,'text':text,'score':sc,'relevant':relevant})
        t += stride
    # Select high scores while avoiding heavily overlapping duplicates.
    candidates.sort(key=lambda x: x['score'], reverse=True)
    chosen=[]
    for c in candidates:
        if all(min(c['end'],x['end'])-max(c['start'],x['start']) < 0.35*min(c['end']-c['start'],x['end']-x['start']) for x in chosen):
            chosen.append(c)
        if len(chosen) >= min(count,8): break
    chosen.sort(key=lambda x: x['start'])
    outputs=[]
    for i,c in enumerate(chosen,1):
        clip_path = work / f'clip_{i:02}.mp4'
        ass_path = work / f'clip_{i:02}.ass'
        all_words = [w for s in c['relevant'] for w in s.get('words',[]) if w['end'] >= c['start'] and w['start'] <= c['end']]
        write_ass(ass_path, all_words, c['start'], c['end']-c['start'])
        start = c['start']; length = c['end'] - c['start']
        # Scale/crop to 9:16. Center crop is a safe MVP fallback; smart subject tracking is planned next.
        vf = f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles='{str(ass_path).replace(chr(92),'/').replace(':', r'\\:')}'"
        run(['ffmpeg','-y','-ss',f'{start:.3f}','-i',video_path,'-t',f'{length:.3f}','-vf',vf,'-c:v','libx264','-preset','veryfast','-crf','23','-c:a','aac','-b:a','128k','-movflags','+faststart',str(clip_path)])
        outputs.append({'path':str(clip_path),'score':c['score'],'start':start,'end':c['end'],'text':c['text']})
    return outputs
