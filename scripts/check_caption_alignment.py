"""Compare first 30 seconds against visible captions; this is not a listening/WER test. Caller must hold the shared heavy lock."""
import argparse
import json
from pathlib import Path
from vidsum.cli import run,ocr,tc,save

p=argparse.ArgumentParser()
p.add_argument('directory',type=Path)
p.add_argument('output',type=Path)
a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True);(a.output/'frames').mkdir(exist_ok=True)
items=[]
for t in range(30):
 name=f'{t:02d}.jpg'
 run(['ffmpeg','-v','error','-y','-ss',t,'-i',a.directory/'video.mp4','-frames:v','1','-vf',"scale='min(1280,iw)':-2",a.output/'frames'/name])
 items.append({'time_s':t,'timecode':tc(t),'file':name})
ocr(a.output,items)
r=json.loads((a.directory/'asr.json').read_text());rows=json.loads((a.output/'ocr.json').read_text())
lines=['# 原视频前 30 秒字幕核对','', '方法：原视频每秒一帧，macOS Vision OCR 与重叠时段的原始 ASR 并列。不是人工听音，不是逐字错误率；OCR 也可能错。','', '| 原视频时间 | 画面字（OCR） | 原始转写 |','|---|---|---|']
for row in rows:
 t=row['time_s'];text=' / '.join(x['text'] for x in row['lines'])
 trans=' '.join(x['text'] for x in r['segments'] if x['start']<=t+1 and x['end']>=t)
 lines.append(f'| {tc(t)} | {text.replace(chr(10)," ").replace("|","/")} | {trans.replace("|","/")} |')
(a.output/'comparison.md').write_text('\n'.join(lines)+'\n')
