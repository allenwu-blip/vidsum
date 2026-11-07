"""Offline Apple system sound classification; no additional model weights."""
from pathlib import Path
import SoundAnalysis as S
from Foundation import NSObject, NSURL
from objc import super, protocolNamed

class VidsumSoundObserver(NSObject, protocols=[protocolNamed('SNResultsObserving')]):
    def init(self):
        self=super().init();self.rows=[];self.errors=[];return self
    def request_didProduceResult_(self,request,result):
        # Exceptions must not escape an Objective-C callback (that aborts Python).
        try:
            cs={str(c.identifier()):float(c.confidence()) for c in result.classifications()}
            self.rows.append({'window_index':len(self.rows),
                **{key:cs.get(key,0) for key in ['speech','music','singing','rapping']}})
        except Exception as error:self.errors.append(str(error))
    def request_didFailWithError_(self,request,error):self.errors.append(str(error))
    def requestDidComplete_(self,request):pass

def classify(wav: Path):
    analyzer,error=S.SNAudioFileAnalyzer.alloc().initWithURL_error_(NSURL.fileURLWithPath_(str(wav.resolve())),None)
    if not analyzer:raise RuntimeError(str(error))
    request,error=S.SNClassifySoundRequest.alloc().initWithClassifierIdentifier_error_(S.SNClassifierIdentifierVersion1,None)
    if not request:raise RuntimeError(str(error))
    observer=VidsumSoundObserver.alloc().init()
    ok,error=analyzer.addRequest_withObserver_error_(request,observer,None)
    if not ok:raise RuntimeError(str(error))
    analyzer.analyze()
    if observer.errors:raise RuntimeError('; '.join(observer.errors))
    rows=observer.rows
    if not rows:raise RuntimeError('SoundAnalysis returned no windows')
    stats={k:{'mean':sum(r[k] for r in rows)/len(rows),'max':max(r[k] for r in rows)} for k in ['speech','music','singing','rapping']}
    return {'method':'Apple SoundAnalysis builtin version1; probabilistic, not proof of silence','statistics':stats,'windows':rows}

def status(segments,events,no_audio=False):
    if no_audio:return '无音轨（无人声，亦无配乐）',[]
    stats=(events or {}).get('statistics',{})
    if stats:
        speech,music,singing,rapping=[stats[k] for k in ['speech','music','singing','rapping']]
        if speech['max']<.12 and music['mean']>.35 and max(singing['max'],rapping['max'])<.2:
            return '无人声，只有配乐',[]
        if max(singing['mean'],rapping['mean'])>.4 and speech['mean']<.65:
            return '以配乐歌声为主，未确认口播；歌词不作为作者解说收录',[]
    if not segments:return '未得到可靠语音转写；是否无人声需核对',segments
    suspicious=[s for s in segments if any(x in s['text'] for x in ['字幕志愿者','字幕由','Amara.org','谢谢观看','感謝觀看'])]
    return ('有转写，但疑似配乐幻听，需核对' if len(suspicious)==len(segments) else '检测到语音（可能含歌唱）'),segments
