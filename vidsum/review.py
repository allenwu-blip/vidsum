"""Deterministic evidence sections: do not let an LLM invent visual facts or quotes."""
import json
from pathlib import Path
import re

def normalize_headings(body):
    for heading in ['这条讲什么','结构拆解','拍法和剪法','关键原话','数据','能学的点']:
        body=re.sub(r'^## '+heading+r'[^\n]*', '## '+heading, body, flags=re.M)
    return body

def replace_section(body,heading,content):
    pattern=r'^## '+re.escape(heading)+r'\n.*?(?=^## |\Z)'
    replacement='## '+heading+'\n\n'+content.strip()+'\n\n'
    if re.search(pattern,body,re.M|re.S):
        return re.sub(pattern,lambda _:replacement,body,flags=re.M|re.S)
    return body+'\n'+replacement

def spread(items,n=3):
    if len(items)<=n:return items
    return [items[round(i*(len(items)-1)/(n-1))] for i in range(n)]

def evidence_sections(body,d: Path,use_visual=True):
    body=normalize_headings(body)
    editorial=d/'editorial_review.json'
    overrides=json.loads(editorial.read_text()) if editorial.exists() else {}
    def select_quotes(items,key):
        patterns=overrides.get(key)
        if patterns is None:return spread(items)
        selected=[]
        for pattern in patterns:
            found=next((item for item in items if re.search(pattern,item[1]) and item not in selected),None)
            if found:selected.append(found)
        return selected[:3]
    meta=json.loads((d/'meta.json').read_text())
    timeline=json.loads((d/'frames.json').read_text())
    a=json.loads((d/'asr.json').read_text())
    ocr=json.loads((d/'ocr.json').read_text())
    meta['analysis']={'scene_change_candidates':timeline['detected_scene_changes'],'sampled_frames':len(timeline['frames']),
        'scene_threshold':timeline['scene_threshold'],'method':'FFmpeg pixel scene scores + uniform sampling; not verified cuts'}
    characters=sum(len(re.sub(r'\s+','',seg['text'])) for seg in a['segments'])
    meta['analysis']['transcript_characters']=characters
    meta['analysis']['characters_per_second']=round(characters/meta['duration_s'],2) if meta.get('duration_s') else None
    (d/'meta.json').write_text(json.dumps(meta,ensure_ascii=False,indent=2)+'\n')
    shots='画面没看，只看了字。机位、运镜、人物动作、构图、调色、转场、是否录屏或静态图拼接均无法判断。\n\n'
    shots+=f"FFmpeg 检出 {meta['analysis']['scene_change_candidates']} 个像素变化候选，保留 {meta['analysis']['sampled_frames']} 张场景/均匀采样帧（计数已写入 meta.json 的 analysis）。像素变化可能来自镜头运动、文字或对象变化，不能当作实际剪辑切镜。OCR 只能证明识别到了文字，不能确认其所处的画面类型。"
    if use_visual and (d/'visual.md').exists():
        found=re.search(r'^## 拍法和剪法\n(.*?)(?=^## |\Z)',body,re.M|re.S)
        shots=(found.group(1).strip() if found else '[画面] 视觉证据见 visual.md。')+'\n\n逐帧原始观察见 [visual.md](visual.md)。单帧无法证实运镜；联系表网格与时间码由工具后加，不能量出真实剪辑节奏。'
    if use_visual and (d/'visual_review.json').exists():
        review=json.loads((d/'visual_review.json').read_text())
        if review.get('corrections'):
            label='联系表抽查校正' if str(review.get('review_scope','')).startswith('contact_only') else '逐帧抽查校正'
            note='[画面·'+label+'] '+review['corrections']+'\n\n完整原始描述见 [visual.md](visual.md)。和模型原文冲突时，以这段校正为准。'
            shots=note+'\n\n'+shots
    body=replace_section(body,'拍法和剪法',shots)
    transcript=(d/'transcript.md').read_text()
    quotes=[]
    for line in transcript.splitlines():
        m=re.match(r'- \[([^]]+)\] (.+)',line)
        if m and 6<=len(m.group(2).strip())<=180 and not any(x in m.group(2) for x in ['明镜与点点栏目','Amara.org','字幕志愿者','字幕由']):quotes.append((m.group(1),m.group(2)))
    q=['### 音轨转写（逐字取自 transcript.md）','',a['status']+'。原始 ASR 可能错；歌曲不当作作者口播。','']
    for t,text in select_quotes(quotes,'audio_quote_patterns'):q.append(f'- [{t}] 「{text}」')
    if not quotes:q.append('无可靠口播原话。')
    candidates=[]
    for row in ocr:
        lines=[x['text'] for x in row['lines'] if x.get('confidence',0)>=.5 and 6<=len(x['text'])<=180
               and not re.fullmatch(r'[@©\s]*[A-Za-z0-9_ .]+',x['text'])]
        if lines:candidates.append((row['timecode'],max(lines,key=len)))
    q+=['','### 画面 OCR（逐字取自 ocr.json；未人工校正的识别字可能有错）','']
    # Editorial patterns may select any original OCR line, not just the longest.
    if 'ocr_quote_patterns' in overrides:
        candidates=[(row['timecode'],line['text']) for row in ocr for line in row['lines'] if 4<=len(line['text'])<=180]
    chosen=select_quotes(candidates,'ocr_quote_patterns')
    for t,text in chosen:q.append(f'- [{t}] 「{text}」')
    if not chosen:q.append('未识别到适合引用的连续文字。')
    body=replace_section(body,'关键原话','\n'.join(q))
    table=['| 字段 | meta.json 记录 |','|---|---|']
    for key,label in [('author','作者'),('followers','粉丝'),('published_at','发布时间'),('likes','点赞'),('comments','评论'),('collects','收藏'),('shares','转发'),('duration_s','总时长（秒）')]:
        value=meta.get(key);table.append('| '+label+' | '+('未提供' if value is None else str(value).replace('|','/'))+' |')
    table+=['','以上为抓取时记录；播放量未由接口提供，不从互动量推算。作品内金额和增长数字均为视频自述，未独立核实。']
    body=replace_section(body,'数据','\n'.join(table))
    if editorial.exists() and not (use_visual and (d/'visual.md').exists()):
        for heading in ['这条讲什么','结构拆解','能学的点']:
            if heading in overrides:body=replace_section(body,heading,overrides[heading])
    if use_visual and (d/'visual_review.json').exists():
        review=json.loads((d/'visual_review.json').read_text())
        for heading,content in review.get('summary_overrides',{}).items():
            if heading in ['这条讲什么','结构拆解','能学的点']:
                body=replace_section(body,heading,content)
    return body.rstrip()+'\n'
