"""Offline Apple system sound classification (macOS only); no additional model weights."""
from pathlib import Path
try:
    import SoundAnalysis as S
    from Foundation import NSObject, NSURL
    from objc import super, protocolNamed
except ImportError:  # not macOS: classify() raises, status() still works
    S = None

if S is not None:
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
    if S is None:raise RuntimeError('SoundAnalysis is only available on macOS')
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
    """Return (code, segments). Codes are translated by i18n.T(lang,'audio')[code]."""
    if no_audio:return 'no_audio',[]
    stats=(events or {}).get('statistics',{})
    if stats:
        speech,music,singing,rapping=[stats[k] for k in ['speech','music','singing','rapping']]
        if speech['max']<.12 and music['mean']>.35 and max(singing['max'],rapping['max'])<.2:
            return 'music_only',[]
        if max(singing['mean'],rapping['mean'])>.4 and speech['mean']<.65:
            return 'singing',[]
    if not segments:return 'no_speech',segments
    suspicious=[s for s in segments if any(x in s['text'] for x in ['字幕志愿者','字幕由','Amara.org','谢谢观看','感謝觀看','Thanks for watching','Subtitles by'])]
    return ('hallucination' if len(suspicious)==len(segments) else 'speech'),segments
