"""Transactional Vorbis comments on closed FLAC files, before publication."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from mutagen.flac import FLAC
from mutagen import MutagenError

from .decisions import audio_track_title


def audio_frames_hash(path):
    """Hash encoded audio frames, excluding FLAC metadata blocks."""
    with Path(path).open('rb') as stream:
        if stream.read(4) != b'fLaC':
            raise ValueError('Expected native FLAC; original file preserved')
        while True:
            header = stream.read(4)
            if len(header) != 4:
                raise ValueError('Truncated FLAC metadata')
            stream.seek(int.from_bytes(header[1:], 'big'), 1)
            if header[0] & 128:
                break
        digest = hashlib.sha256()
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
        return digest.hexdigest()


def planned_tags(identity, assignment):
    number = assignment['number']
    spoken = identity.get('media_type') == 'audiobook'
    tags = {'ALBUM': str(identity['book' if spoken else 'album']),
            'TRACKNUMBER': str(number), 'TRACKTOTAL': str(len(identity['tracks']))}
    title = audio_track_title(identity, number)
    if title:
        tags['TITLE'] = title
    for field, tag in (('disc', 'DISCNUMBER'), ('disc_total', 'DISCTOTAL'), ('year', 'DATE'),
                       ('author', 'AUTHOR'), ('narrator', 'NARRATOR'), ('performer', 'PERFORMER')):
        if identity.get(field) not in (None, ''):
            tags[tag] = str(identity[field])
    if spoken:
        if identity.get('author'):
            # ABS treats artist/albumartist as author: populate ONLY explicit authorship.
            tags['ARTIST'] = tags['ALBUMARTIST'] = str(identity['author'])
        performer = identity.get('performer') or identity.get('artist')
        if performer:
            tags['PERFORMER'] = str(performer)
    elif identity.get('artist'):
        tags['ARTIST'] = tags['ALBUMARTIST'] = str(identity['artist'])
    return tags


def tag_completed_flac(path, identity, assignment, excluded_fields=()):
    """Copy-stage, write/reopen/verify, then atomically replace only the closed file.

    The caller enforces completion and detention boundaries. Any failure before
    replacement leaves the original byte-for-byte intact; only our stage is removed.
    """
    path = Path(path)
    tags = planned_tags(identity, assignment)
    try:
        original = FLAC(path)
    except MutagenError as error:
        raise ValueError('Invalid FLAC; tagging failed without publication') from error
    before = {key.upper(): list(value) for key, value in original.items()}
    frames = audio_frames_hash(path)
    fd, name = tempfile.mkstemp(prefix='DETAINED_tag_', suffix='.flac', dir=path.parent)
    os.close(fd)
    stage = Path(name)
    try:
        shutil.copyfile(path, stage)
        os.chmod(stage, path.stat().st_mode & 0o777)
        media = FLAC(stage)
        removed = {}
        if identity.get('media_type') == 'audiobook' and not identity.get('author'):
            for key in ('ARTIST', 'ALBUMARTIST'):
                if key in media:
                    removed[key] = list(media[key])
                    del media[key]
        tag_fields = {'author': ('AUTHOR',), 'narrator': ('NARRATOR',), 'performer': ('PERFORMER',),
                      'artist': ('ARTIST', 'ALBUMARTIST'), 'year': ('DATE',), 'track_titles': ('TITLE',)}
        for field in excluded_fields:
            for key in tag_fields.get(field, ()):
                if key in media:
                    removed[key] = list(media[key])
                    del media[key]
        for key, value in tags.items():
            media[key] = [value]
        media.save()
        reopened = FLAC(stage)
        after = {key.upper(): list(value) for key, value in reopened.items()}
        if any(after.get(key) != [value] for key, value in tags.items()):
            raise ValueError('FLAC comment round-trip verification failed')
        if any(key in after for key in removed if key not in tags) or audio_frames_hash(stage) != frames:
            raise ValueError('FLAC audio/semantic verification failed')
        # Encoding remains lossless and frame content is unchanged; decode as an
        # additional integrity check before this required finalization can pass.
        subprocess.run(['flac', '--test', '--silent', str(stage)], check=True,
                       capture_output=True, timeout=120)
        with stage.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(stage, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return {'status': 'verified', 'before': before, 'after': after,
                'removed_role_ambiguous_or_disputed_tags': removed, 'audio_frames_sha256': frames,
                'audio_frames_unchanged': True, 'flac_integrity_ok': True,
                'size_bytes': path.stat().st_size}
    except MutagenError as error:
        raise ValueError('FLAC tagging failed; original file preserved') from error
    finally:
        stage.unlink(missing_ok=True)
