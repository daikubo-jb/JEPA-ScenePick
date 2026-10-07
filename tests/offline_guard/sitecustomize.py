"""Fail synthetic CLI subprocesses if Python-level outbound socket APIs are used."""
import socket
import urllib.request

def _blocked(*_args, **_kwargs):
    raise RuntimeError("offline synthetic test blocked a Python network call")

socket.socket.connect = _blocked
socket.create_connection = _blocked
urllib.request.urlopen = _blocked
