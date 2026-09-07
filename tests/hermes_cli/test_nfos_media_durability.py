"""An acknowledged media copy must survive a Linux directory metadata flush."""
import hashlib
import os
import stat
from pathlib import Path

import pytest

from hermes_cli import nfos_runtime as runtime


def test_media_and_new_directory_links_are_synced_before_return(tmp_path, monkeypatch):
    source=tmp_path/'input.ogg';source.write_bytes(b'synthetic original bytes')
    destination=tmp_path/'board'/'request-media'
    synced=[]
    def sync(path):
        assert list(destination.glob('*.ogg'))[0].read_bytes()==source.read_bytes()
        synced.append(Path(path))
    monkeypatch.setattr(runtime,'_sync_directory',sync,raising=False)
    attachments=runtime.preserve_attachments([str(source)],['audio/ogg'],directory=destination)
    assert synced==[destination,destination.parent,tmp_path]
    assert attachments[0]['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()


def test_failed_directory_flush_does_not_claim_media_ready_and_retry_keeps_original(tmp_path,monkeypatch):
    source=tmp_path/'input.png';source.write_bytes(b'synthetic image bytes')
    destination=tmp_path/'request-media'
    def fail(path):raise OSError('simulated directory fsync failure')
    monkeypatch.setattr(runtime,'_sync_directory',fail,raising=False)
    with pytest.raises(OSError,match='fsync failure'):
        runtime.preserve_attachments([str(source)],['image/png'],directory=destination)
    copies=list(destination.glob('*.png'))
    assert len(copies)==1 and copies[0].read_bytes()==source.read_bytes()
    synced=[]
    monkeypatch.setattr(runtime,'_sync_directory',lambda path:synced.append(Path(path)),raising=False)
    result=runtime.preserve_attachments([str(source)],['image/png'],directory=destination)
    assert synced==[destination]
    assert Path(result[0]['original'])==copies[0]


@pytest.mark.skipif(os.name!='posix',reason='Production directory fsync is a POSIX contract')
def test_linux_flushes_actual_file_then_directory_descriptors(tmp_path,monkeypatch):
    source=tmp_path/'input.wav';source.write_bytes(b'synthetic original audio')
    destination=tmp_path/'media';flushed=[]
    original=os.fsync
    def sync(fd):
        flushed.append('directory' if stat.S_ISDIR(os.fstat(fd).st_mode) else 'file')
        original(fd)
    monkeypatch.setattr(runtime.os,'fsync',sync)
    runtime.preserve_attachments([str(source)],['audio/wav'],directory=destination)
    assert flushed==['file','directory','directory']
