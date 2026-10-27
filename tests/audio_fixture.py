"""Generate tiny FLACs in temporary directories, never touch retained rips."""
import subprocess
import wave


def make_flac(path):
    wav = path.with_suffix('.wav')
    with wave.open(str(wav), 'wb') as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(44100)
        stream.writeframes(b'\x01\x00\x02\x00' * 4410)
    subprocess.run(['flac', '--silent', '--verify', '-o', str(path), str(wav)], check=True, capture_output=True)
    wav.unlink()
    return path
