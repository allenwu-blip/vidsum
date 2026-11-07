"""Read-only integration with a caller-owned dy_fetch.py; no vendored private code."""
from types import ModuleType
import json
from pathlib import Path
import time

def fetch_with_existing(script: Path,share: str,d: Path,chrome=None):
    script=script.expanduser().resolve()
    # Compile the caller-owned source directly: importing a .py could create
    # __pycache__ beside it, violating a read-only source-directory boundary.
    module=ModuleType('vidsum_external_dy_fetch');module.__file__=str(script)
    exec(compile(script.read_text(),str(script),'exec'),module.__dict__)
    if chrome:module.CHROME=chrome
    # Only the public capture/download function, never the script's ASR or heavy routine.
    names=module.capture_all([(d.name,share)],d.parent)
    info_path=d/'info.json'
    if d.name not in names or not info_path.exists():
        reason=(d/'ERROR.txt').read_text() if (d/'ERROR.txt').exists() else '指定 dy_fetch.py 未取得视频'
        raise RuntimeError(reason.strip())
    raw=json.loads(info_path.read_text());i=raw['info'];detail=raw.get('detail') or {}
    timestamp=detail.get('create_time')
    meta=dict(source='douyin',link=i.get('link'),aweme_id=i.get('aweme_id'),title=i.get('desc'),author=i.get('author'),
        followers=i.get('author_followers'),published_timestamp=timestamp,
        published_at=time.strftime('%Y-%m-%dT%H:%M:%S%z',time.localtime(timestamp)) if timestamp else i.get('create_time'),
        likes=i.get('likes'),comments=i.get('comments'),collects=i.get('collects'),shares=i.get('shares'),
        reported_duration_s=i.get('duration_s'),captured_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        source_fetcher='caller-provided dy_fetch.py capture_all (read-only)')
    # The original writes a full detail object with expiring CDN URLs. Retain only public fields needed here.
    info_path.write_text(json.dumps({'info':i},ensure_ascii=False,indent=2)+'\n')
    return meta


def download_note(detail, d):
    """Download only public note images; never persist signed CDN locations."""
    from PIL import Image, ImageDraw
    import subprocess
    images = detail.get('images') or []
    if not images:
        raise RuntimeError('图文详情没有公开图片')
    fd = d/'frames';fd.mkdir(exist_ok=True)
    frames = []
    for index, row in enumerate(images, 1):
        urls = row.get('url_list') or (row.get('display_image') or {}).get('url_list') or []
        target = fd/f'{index:02d}_image.jpg'
        for url in urls:
            try:
                subprocess.run(['curl','-fSL','--max-time','45','-e','https://www.douyin.com/','-o',str(d/'image.part'),url],check=True,capture_output=True)
                with Image.open(d/'image.part') as im:
                    im.convert('RGB').save(target,quality=94)
                break
            except Exception:
                continue
        (d/'image.part').unlink(missing_ok=True)
        if not target.exists():raise RuntimeError(f'图文第 {index} 张图片下载失败')
        frames.append({'file':target.name,'time_s':index-1,'timecode':f'图片{index:02d}（无视频时间码）','scene_change':False})
    sheet=Image.new('RGB',(1200,((len(frames)+3)//4)*460),'#161b22');draw=ImageDraw.Draw(sheet)
    for index,row in enumerate(frames):
        with Image.open(fd/row['file']) as im:
            im.thumbnail((288,420));x=(index%4)*300;y=(index//4)*460
            sheet.paste(im,(x+(300-im.width)//2,y+30));draw.text((x+8,y+8),str(index+1),fill='white')
    sheet.thumbnail((2400,2400));sheet.save(d/'contact.jpg',quality=90)
    (d/'frames.json').write_text(json.dumps({'media_type':'note','duration_s':None,'scene_threshold':None,'detected_scene_changes':0,'frames':frames},ensure_ascii=False,indent=2)+'\n')
    return frames
