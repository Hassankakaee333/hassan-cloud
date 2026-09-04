from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from codex_persistent_auth import restore_encrypted_codex_home, save_encrypted_codex_home


def test_codex_home_round_trip_is_encrypted(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    secret_text = '{"tokens":{"access_token":"secret-value"}}'
    (home / "auth.json").write_text(secret_text, encoding="utf-8")
    (home / "nested").mkdir()
    (home / "nested" / "state.json").write_text('{"ok":true}', encoding="utf-8")

    cache = tmp_path / "codex-session.enc"
    assert save_encrypted_codex_home(home, cache, "0123456789abcdef-persistent-secret")
    raw = cache.read_bytes()
    assert b"secret-value" not in raw
    assert b"access_token" not in raw

    restored = tmp_path / "restored"
    assert restore_encrypted_codex_home(cache, restored, "0123456789abcdef-persistent-secret")
    assert (restored / "auth.json").read_text(encoding="utf-8") == secret_text
    assert (restored / "nested" / "state.json").read_text(encoding="utf-8") == '{"ok":true}'


def test_wrong_secret_cannot_restore(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "auth.json").write_text("sensitive", encoding="utf-8")
    cache = tmp_path / "codex-session.enc"
    save_encrypted_codex_home(home, cache, "0123456789abcdef-good-secret")
    assert not restore_encrypted_codex_home(cache, tmp_path / "restored", "0123456789abcdef-wrong-secret")
