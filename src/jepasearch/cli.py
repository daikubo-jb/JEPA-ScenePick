from __future__ import annotations
import argparse,json,os,resource,time
from pathlib import Path
from .core import index_video,search,decide,references,runtime_info,SearchError
from .synthetic import make_video

def main(argv=None):
 p=argparse.ArgumentParser(prog='jepa-search'); sub=p.add_subparsers(dest='cmd',required=True)
 s=sub.add_parser('synth'); s.add_argument('--output',type=Path,required=True)
 i=sub.add_parser('index'); i.add_argument('video',type=Path); i.add_argument('--store',type=Path,required=True); i.add_argument('--no-resume',action='store_true'); i.add_argument('--engine',choices=['color-motion','vjepa2'],default='color-motion'); i.add_argument('--checkpoint')
 q=sub.add_parser('search'); q.add_argument('video',type=Path); q.add_argument('--start',type=float,required=True); q.add_argument('--end',type=float,required=True); q.add_argument('--store',type=Path,required=True); q.add_argument('--limit',type=int,default=20); q.add_argument('--engine',choices=['color-motion','vjepa2'],default='color-motion'); q.add_argument('--checkpoint')
 d=sub.add_parser('decide'); d.add_argument('--store',type=Path,required=True); d.add_argument('--media-id',required=True); d.add_argument('--start',type=float,required=True); d.add_argument('--decision',choices=['keep','hold'],required=True); d.add_argument('--engine',choices=['color-motion','vjepa2'],default='color-motion'); d.add_argument('--pipeline'); d.add_argument('--checkpoint')
 r=sub.add_parser('references'); r.add_argument('--store',type=Path,required=True)
 b=sub.add_parser('bench'); b.add_argument('--output',type=Path,default=Path('benchmark.json'))
 a=sub.add_parser('runtime')
 args=p.parse_args(argv)
 try:
  if args.cmd=='synth': print(json.dumps({'synthetic_video':str(make_video(args.output))})); return 0
  if args.cmd=='index': print(json.dumps(index_video(args.video,args.store,resume=not args.no_resume,engine=args.engine,checkpoint_path=args.checkpoint),indent=2)); return 0
  if args.cmd=='search': print(json.dumps(search(args.video,args.start,args.end,args.store,limit=args.limit,engine=args.engine,checkpoint_path=args.checkpoint),indent=2)); return 0
  if args.cmd=='decide': decide(args.store,args.media_id,args.start,args.decision,engine=args.engine,pipeline=args.pipeline,checkpoint_path=args.checkpoint); print('{"saved":true}'); return 0
  if args.cmd=='references': print(json.dumps(references(args.store),indent=2)); return 0
  if args.cmd=='runtime': print(json.dumps(runtime_info(),indent=2)); return 0
  if args.cmd=='bench':
   import tempfile
   with tempfile.TemporaryDirectory(prefix='jepa-synth-') as tmp:
    root=Path(tmp); video=make_video(root/'synthetic.mp4',duration=6); store=root/'index.sqlite'
    t=time.perf_counter(); indexed=index_video(video,store,reserve_bytes=0); index_s=time.perf_counter()-t
    t=time.perf_counter(); result=search(video,0,2,store,limit=8); search_s=time.perf_counter()-t
    index_bytes=sum(p.stat().st_size for p in root.glob('index.sqlite*') if p.is_file())
    rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if os.uname().sysname!='Darwin': rss*=1024
    output={**runtime_info(),'synthetic_only':True,'video_bytes':video.stat().st_size,'index_bytes':index_bytes,'windows':indexed['windows_added'],'decode_seconds':round(indexed['decode_seconds'],4),'baseline_feature_seconds':round(indexed['feature_seconds'],4),'model_inference_seconds':None,'model_inference_status':'not measured; pinned model dependencies unavailable','persistence_seconds':round(indexed['persist_seconds'],4),'index_total_seconds':round(index_s,4),'search_seconds':round(search_s,4),'peak_rss_bytes':int(rss),'bytes_per_window':round(index_bytes/max(1,indexed['windows_added'])),'candidate_count':len(result),'quality_claim':'none; synthetic functional smoke only'}
    args.output.write_text(json.dumps(output,indent=2)+'\n'); print(json.dumps(output,indent=2)); return 0
 except (SearchError,OSError,RuntimeError,ValueError) as e: print(f'error: {e}',file=__import__('sys').stderr); return 2
if __name__=='__main__': raise SystemExit(main())
