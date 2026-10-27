import subprocess

import pytest
from mutagen.flac import FLAC

from recognition_helper.audio_tags import tag_completed_flac, audio_frames_hash
from recognition_helper.decisions import filename, audio_track_title
from tests.audio_fixture import make_flac


@pytest.mark.parametrize('identity', [
    {'media_type':'music','artist':'Night Band','album':'Evening Sessions','tracks':[1,2]},
    {'media_type':'audiobook','book':'The Long Road','tracks':[1,2]},
    {'media_type':'audiobook','book':'Radio Theatre','artist':'The Players','tracks':[1,2],'year':2004,'disc':2,'disc_total':3},
    {'media_type':'audiobook','book':'Essays','author':'Explicit Writer','narrator':'Reader','tracks':[1,2]}])
def test_actual_flac_round_trip_and_audio_preservation(tmp_path, identity):
    path=make_flac(tmp_path/'DETAINED_fixture.flac')
    media=FLAC(path); media['COMMENT']=['retain me']; media['LANGUAGE']=['deu'];media.save()
    before=audio_frames_hash(path)
    result=tag_completed_flac(path, identity, {'number':1})
    reopened=FLAC(path)
    assert reopened['ALBUM']==[identity.get('book',identity.get('album'))]
    assert reopened['TRACKNUMBER']==['1'] and reopened['TRACKTOTAL']==['2']
    assert reopened['COMMENT']==['retain me'] and reopened['LANGUAGE']==['deu']
    assert audio_frames_hash(path)==before and result['audio_frames_unchanged']
    assert not list(tmp_path.glob('DETAINED_tag_*'))
    if identity.get('media_type')=='audiobook' and not identity.get('author'):
        assert 'ARTIST' not in reopened and 'ALBUMARTIST' not in reopened and 'AUTHOR' not in reopened
        if identity.get('artist'): assert reopened['PERFORMER']==[identity['artist']]
    else:
        assert reopened['ARTIST']==[identity.get('author',identity.get('artist'))]
    # Independent tool reads actual written comments, not just internal state.
    comments=subprocess.check_output(['metaflac','--export-tags-to=-',str(path)],text=True)
    assert 'TRACKTOTAL=2' in comments and 'ALBUM=' in comments


def test_spoken_performer_not_mislabelled_as_author_and_original_tags_audited(tmp_path):
    path=make_flac(tmp_path/'DETAINED_spoken.flac')
    media=FLAC(path);media['ARTIST']=['Voice Actor'];media['ALBUMARTIST']=['Voice Actor'];media['GENRE']=['Drama'];media.save()
    result=tag_completed_flac(path,{'media_type':'audiobook','book':'Drama','artist':'Voice Actor','tracks':[1]}, {'number':1})
    assert FLAC(path)['PERFORMER']==['Voice Actor'] and FLAC(path)['GENRE']==['Drama']
    assert 'ARTIST' not in FLAC(path) and 'AUTHOR' not in FLAC(path)
    assert result['before']['ARTIST']==['Voice Actor']


def test_tag_failure_keeps_original_byte_for_byte(tmp_path,monkeypatch):
    path=make_flac(tmp_path/'DETAINED_failure.flac'); before=path.read_bytes()
    monkeypatch.setattr('recognition_helper.audio_tags.subprocess.run',lambda *a,**k: (_ for _ in ()).throw(subprocess.CalledProcessError(1,'flac')))
    with pytest.raises(subprocess.CalledProcessError):
        tag_completed_flac(path,{'media_type':'audiobook','book':'Talks','tracks':[1]}, {'number':1})
    assert path.read_bytes()==before and not list(tmp_path.glob('DETAINED_tag_*'))


def test_existing_explicit_author_is_preserved_unless_disputed(tmp_path):
    path=make_flac(tmp_path/'DETAINED_author.flac')
    media=FLAC(path);media['AUTHOR']=['Existing explicit credit'];media.save()
    identity={'media_type':'audiobook','book':'Talks','tracks':[1]}
    tag_completed_flac(path,identity,{'number':1})
    assert FLAC(path)['AUTHOR']==['Existing explicit credit']
    tag_completed_flac(path,identity,{'number':1},excluded_fields=['author'])
    assert 'AUTHOR' not in FLAC(path)


def test_explicit_author_can_replace_existing_disputed_spoken_artist_tag(tmp_path):
    path=make_flac(tmp_path/'DETAINED_roles.flac')
    media=FLAC(path);media['ARTIST']=['Disputed actor'];media.save()
    identity={'media_type':'audiobook','book':'A Work','author':'Actual Writer','tracks':[1]}
    tag_completed_flac(path,identity,{'number':1},excluded_fields=['artist'])
    assert FLAC(path)['ARTIST']==['Actual Writer']
    assert FLAC(path)['AUTHOR']==['Actual Writer']


def test_spoken_names_no_author_folder_duplicate_prefix_or_unsafe_characters():
    identity={'media_type':'audiobook','book':'River: A Drama','year':2001,'tracks':[1,2], 'track_titles':{'1':'01-River'}}
    assert str(filename(identity,{'number':1},'.flac'))=='audiobooks/River - A Drama (2001)/01 - River.flac'
    identity.update(disc=2,disc_total=3)
    assert filename(identity,{'number':1},'.flac').name=='Disc 2 - 01 - River.flac'
    assert 'Unknown' not in str(filename(identity,{'number':2},'.flac'))
    assert audio_track_title({'track_titles':{'1':'1984 - A Year'}},1)=='1984 - A Year'
