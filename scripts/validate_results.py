"""Validate actual batch evidence, quote provenance, dimensions, timestamps and privacy boundaries."""
import argparse
import json
from pathlib import Path
import re
from PIL import Image

p=argparse.ArgumentParser();p.add_argument('output',type=Path);p.add_argument('--require-visual',action='store_true');p.add_argument('--report',default='validation.json');args=p.parse_args();out=args.output
status=json.loads((out/'batch_status.json').read_text());records=[]
for row in status:
 d=out/row['id'];record={'id':row['id'],'status':row['status'],'checks':[]}
 if row['status']=='失败':
  assert (d/'ERROR.txt').read_text().strip(),row['id']
  record['failure_reason']=(d/'ERROR.txt').read_text().strip();records.append(record);continue
 assert row['status']=='成功',row
 for f in ['meta.json','transcript.md','frames','frames.json','contact.jpg','ocr.md','ocr.json','summary.md']:
  assert (d/f).exists(),(row['id'],f)
 meta=json.loads((d/'meta.json').read_text());timeline=json.loads((d/'frames.json').read_text())
 frames=timeline['frames'];im=Image.open(d/'contact.jpg')
 assert 2<=len(frames)<=24 and max(im.size)<=2400
 assert all((d/'frames'/f['file']).is_file() for f in frames)
 if meta.get('duration_s') is not None:assert all(0<=f['time_s']<meta['duration_s'] for f in frames)
 summary=(d/'summary.md').read_text();body=summary[summary.index('## 这条讲什么'):]
 if args.require_visual:
  assert (d/'visual.md').exists() and (d/'visual.json').exists() and (d/'summary_v1.md').exists()
  visual=json.loads((d/'visual.json').read_text())
  assert 8<=len(visual['frames'])<=12 or meta.get('media_type')=='note'
  assert all((d/'frames'/f['file']).exists() for f in visual['frames'])
  assert visual['contact']['description'] and visual['elapsed_s']>0 and visual['peak_rss_gb']>0
  assert '画面没看，只看了字' not in body
  assert '[画面' in body and '声音' in summary and 'OCR' in summary
 else:assert '画面没看，只看了字' in summary
 assert meta['allen_context'] in summary[:summary.index('> 证据边界')]
 for heading in ['这条讲什么','结构拆解','拍法和剪法','关键原话','数据','能学的点']:
  assert '## '+heading+'\n' in body,(row['id'],heading)
 quoted=re.search(r'## 关键原话\n(.*?)(?=\n## 数据)',body,re.S).group(1)
 sources=(d/'transcript.md').read_text()+'\n'+'\n'.join(x['text'] for r in json.loads((d/'ocr.json').read_text()) for x in r['lines'])
 for text in re.findall(r'「(.*?)」',quoted):assert text in sources,(row['id'],'quote not verbatim',text)
 for mm,ss in re.findall(r'\b(\d{2}):(\d{2}\.\d{2})\b',body):
  assert meta.get('duration_s') is None or int(mm)*60+float(ss)<=meta['duration_s']+.01,(row['id'],'out-of-range timecode',mm,ss)
 lessons=body.split('## 能学的点\n',1)[1]
 assert len(re.findall(r'^- ',lessons,re.M))<=5
 assert not (d/'ERROR.txt').exists()
 assert not (d/'audio.wav').exists()
 record.update(frame_count=len(frames),contact_size=list(im.size),quote_count=len(re.findall(r'「(.*?)」',quoted)),
  checks=['required files','frame bounds','contact dimensions','verbatim quotes','timecode bounds','Allen context exact','lesson limit','clean success'])
 records.append(record)
(out/args.report).write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({'tasks':len(records),'success':sum(x['status']=='成功' for x in records),'failure':sum(x['status']=='失败' for x in records),'all_checks_passed':True},ensure_ascii=False))
