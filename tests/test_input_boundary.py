from pathlib import Path
import tempfile
from unittest.mock import patch
import pytest
from jepasearch.core import SearchError, _open_local_video, index_video, resolve_local_media
from jepasearch.synthetic import make_video

@pytest.mark.parametrize('uri',[
    'https://example.invalid/game.mp4',
    'rtsp://camera.invalid/live.mp4',
    'tcp://stream.invalid/game.ts',
    'file:///tmp/clip.mp4',
    '//server/share/game.mp4',
])
def test_rejects_url_and_network_stream_inputs(uri):
    with pytest.raises(SearchError,match='local video file path'):
        resolve_local_media(uri)

@pytest.mark.parametrize('name',['playlist.m3u8','playlist.m3u','stream.pls','stream.asx','stream.xspf','link.url','capture.webloc'])
def test_rejects_playlist_and_link_suffixes_before_open(name):
    with pytest.raises(SearchError,match='playlist'):
        resolve_local_media(f'/tmp/{name}')

@pytest.mark.parametrize('payload', [
    '<?xml version="1.0"?><smil><body><video src="https://example.invalid/remote.mp4"/></body></smil>',
    '<ASX version="3.0"><Entry><Ref href="https://example.invalid/live"/></Entry></ASX>',
    '#EXTM3U\nhttps://example.invalid/segment.ts\n',
])
def test_rejects_xml_and_playlist_content_before_decoder(payload):
    with tempfile.TemporaryDirectory(prefix='jepa-synthetic-input-') as directory:
        renamed=Path(directory)/'not-a-video.mp4'
        renamed.write_text(payload)
        with patch('jepasearch.core._open_local_video') as decoder:
            with pytest.raises(SearchError,match='container signature'):
                index_video(renamed,Path(directory)/'index.sqlite',reserve_bytes=0)
            decoder.assert_not_called()

def test_accepts_generated_local_video():
    with tempfile.TemporaryDirectory(prefix='jepa-synthetic-input-') as directory:
        video=make_video(Path(directory)/'synthetic.mp4',duration=3)
        assert resolve_local_media(video)==video.resolve()

def test_ffmpeg_open_is_limited_to_file_protocol_and_restores_environment(monkeypatch):
    import cv2
    observed={}
    monkeypatch.setattr('jepasearch.core.cv2.VideoCapture',lambda path,backend: observed.update(path=path,backend=backend,options=__import__('os').environ.get('OPENCV_FFMPEG_CAPTURE_OPTIONS')) or object())
    monkeypatch.setenv('OPENCV_FFMPEG_CAPTURE_OPTIONS','prior;value')
    _open_local_video(Path('/tmp/synthetic.mp4'))
    assert observed['backend']==cv2.CAP_FFMPEG
    assert observed['options']=='protocol_whitelist;file'
    assert __import__('os').environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']=='prior;value'
