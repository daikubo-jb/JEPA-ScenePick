from pathlib import Path
import cv2, numpy as np

def make_video(path: Path, duration: float=9, fps: int=15, width: int=320, height: int=180) -> Path:
    path=path.expanduser().absolute()
    resolved=path.resolve()
    if any(part.name.casefold()=='movies' for part in (resolved,*resolved.parents)):
        raise ValueError('synthetic output is not allowed under a Movies directory')
    if path.exists() or path.is_symlink(): raise FileExistsError('synthetic output already exists; refusing overwrite')
    path.parent.mkdir(parents=True,exist_ok=True)
    writer=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'mp4v'),fps,(width,height))
    if not writer.isOpened(): raise RuntimeError('OpenCV cannot create synthetic MP4')
    for i in range(int(duration*fps)):
        phase=int(i/(duration*fps/3)); colors=[(32,70,210),(40,190,60),(210,90,35)]; frame=np.full((height,width,3),(18,18,18),dtype=np.uint8)
        x=(i*4)%width; y=height//2+int(28*np.sin(i/5))
        cv2.rectangle(frame,(max(0,x-30),max(0,y-20)),(min(width-1,x+30),min(height-1,y+20)),colors[phase],-1)
        cv2.circle(frame,(width-x,height//3),12,colors[(phase+1)%3],-1)
        if i in (int(duration*fps/3),int(2*duration*fps/3)): cv2.line(frame,(0,0),(width,height),(255,255,255),3)
        writer.write(frame)
    writer.release(); return path
