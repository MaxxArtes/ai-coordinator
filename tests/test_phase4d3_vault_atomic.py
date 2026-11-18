"""Etapa 7: atomic encrypted-vault transactions, stale-confirmation fencing and unreadable stores.

All credentials and profiles are fixtures in temporary directories; no operator vault is opened.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop.auth.vault as vault_mod
from desktop.auth.vault import AESGCMVault, VaultError, UnavailableVault, create_vault


@pytest.fixture(params=['aes_gcm', 'dpapi'])
def vault(request, tmp_path):
    if request.param == 'dpapi' and sys.platform != 'win32':
        pytest.skip('DPAPI requires Windows')
    return create_vault(tmp_path, 'fixture', backend=request.param, master_password='fixture-only-password')


@pytest.mark.parametrize('raw', [b'', b'x', b'\x00' * 27, b'\x00' * 43])
def test_truncated_vault_is_not_empty_and_cannot_be_overwritten(tmp_path, raw):
    vault = AESGCMVault('fixture', tmp_path / 'vault.enc', master_password='fixture-only-password')
    vault.vault_path.write_bytes(raw)
    with pytest.raises(VaultError):
        vault.list_keys()
    with pytest.raises(VaultError):
        vault.set_secret('NEW_KEY', 'fixture-new-secret')
    assert vault.vault_path.read_bytes() == raw


@pytest.mark.parametrize('plain', [b'[]', b'null', b'{"KEY":42}', b'{"KEY":"fixture-sensitive",}', b'{"KEY":"one","KEY":"two"}'])
def test_decryptable_but_malformed_vault_is_not_replaced_or_echoed(tmp_path, plain):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    vault = AESGCMVault('fixture', tmp_path / 'vault.enc', master_password='fixture-only-password')
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    encrypted = salt + nonce + AESGCM(vault._derive_key(salt)).encrypt(nonce, plain, associated_data=b'fixture')
    vault.vault_path.write_bytes(encrypted)
    for operation in [vault.list_keys, lambda: vault.set_secret('NEW_KEY', 'fixture-new-secret')]:
        with pytest.raises(VaultError) as error:
            operation()
        assert 'fixture-sensitive' not in str(error.value)
    assert vault.vault_path.read_bytes() == encrypted


def test_snapshot_and_conditional_replacement_use_ciphertext_revision(vault):
    assert vault.get_keys_snapshot() == {'keys': [], 'revision': 'absent'}
    vault.set_secret_conditional('KEY', 'fixture-initial')
    snapshot = vault.get_keys_snapshot()
    assert snapshot['keys'] == ['KEY']
    assert snapshot['revision'] == hashlib.sha256(vault.vault_path.read_bytes()).hexdigest()
    assert snapshot['revision'] != hashlib.sha256(b'fixture-initial').hexdigest()
    with pytest.raises(vault_mod.VaultConflictError):
        vault.set_secret_conditional('KEY', 'fixture-unconfirmed')
    assert vault.get_secret('KEY') == 'fixture-initial'
    vault.set_secret_conditional('KEY', 'fixture-replaced', expected_revision=snapshot['revision'])
    assert vault.get_secret('KEY') == 'fixture-replaced'
    assert vault.get_keys_snapshot()['revision'] != snapshot['revision']


def test_any_intervening_transaction_invalidates_confirmation_without_losing_keys(vault):
    vault.set_secret('KEY', 'fixture-initial')
    snapshot = vault.get_keys_snapshot()
    vault.set_secret('OTHER', 'fixture-other')
    with pytest.raises(vault_mod.VaultConflictError):
        vault.set_secret_conditional('KEY', 'fixture-replaced', expected_revision=snapshot['revision'])
    assert vault.get_secret('KEY') == 'fixture-initial'
    assert vault.get_secret('OTHER') == 'fixture-other'
    assert vault.delete_secret('OTHER') is True
    assert vault.list_keys() == ['KEY']


@pytest.mark.parametrize('bad', ['', 'absent', '\u00e1' * 64, 'not-a-revision'])
def test_invalid_confirmation_is_a_safe_conflict(vault, bad):
    vault.set_secret('KEY', 'fixture-sensitive')
    with pytest.raises(vault_mod.VaultConflictError) as error:
        vault.set_secret_conditional('KEY', 'fixture-new', expected_revision=bad)
    assert 'fixture-sensitive' not in str(error.value)
    assert vault.get_secret('KEY') == 'fixture-sensitive'


@pytest.mark.parametrize('stage', ['replace', 'fsync'])
def test_failed_atomic_save_preserves_ciphertext_and_does_not_echo_error_payload(vault, monkeypatch, stage):
    vault.set_secret('KEY', 'fixture-initial')
    previous = vault.vault_path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('fixture-sensitive-error')
    monkeypatch.setattr(vault_mod.os, stage, fail)
    with pytest.raises(VaultError) as error:
        vault.set_secret('KEY', 'fixture-new')
    assert 'fixture-sensitive-error' not in str(error.value)
    assert vault.vault_path.read_bytes() == previous
    assert list(vault.vault_path.parent.glob('vault_*.tmp')) == []


def test_unavailable_vault_refuses_new_operations_without_writing(tmp_path):
    vault = UnavailableVault('fixture', tmp_path / 'vault.enc')
    with pytest.raises(vault_mod.VaultUnavailableError):
        vault.get_keys_snapshot()
    with pytest.raises(vault_mod.VaultUnavailableError):
        vault.set_secret_conditional('KEY', 'fixture-new')
    assert not list(tmp_path.iterdir())


WORKER = r"""
import os,sys,time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from desktop.auth.vault import AESGCMVault, VaultConflictError
folder,index,mode,revision=Path(sys.argv[2]),sys.argv[3],sys.argv[4],sys.argv[5]
class SlowVault(AESGCMVault):
    def _save_dict(self,data):
        time.sleep(0.03)
        return super()._save_dict(data)
vault=SlowVault('fixture',folder/'vault.enc',master_password='fixture-only-password')
(folder/('ready-'+index)).write_text('ready')
deadline=time.monotonic()+15
while not (folder/'start').exists():
    if time.monotonic()>deadline: raise RuntimeError('start timeout')
    time.sleep(.01)
if mode=='independent':
    for n in range(6): vault.set_secret('KEY_'+index+'_'+str(n),'fixture-value-'+index+'-'+str(n))
else:
    try:
        vault.set_secret_conditional('SAME','fixture-value-'+index,expected_revision=revision or None)
    except VaultConflictError: print('conflict')
    else: print('stored')
"""


def workers(tmp_path, mode, count, revision=''):
    env = dict(os.environ)
    env['USERPROFILE'] = str(tmp_path)
    processes = [subprocess.Popen([sys.executable, '-B', '-c', WORKER, str(ROOT), str(tmp_path), str(n), mode, revision],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in range(count)]
    try:
        deadline = time.monotonic() + 20
        while not all((tmp_path / f'ready-{n}').exists() for n in range(count)):
            if time.monotonic() > deadline:
                raise AssertionError('workers did not become ready')
            time.sleep(.01)
        (tmp_path / 'start').write_text('start')
        results = []
        for process in processes:
            out, err = process.communicate(timeout=35)
            assert process.returncode == 0, err
            results.append(out.strip())
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


def test_four_processes_keep_all_independent_credentials(tmp_path):
    workers(tmp_path, 'independent', 4)
    vault = AESGCMVault('fixture', tmp_path / 'vault.enc', master_password='fixture-only-password')
    assert len(vault.list_keys()) == 24
    for n in range(4):
        for k in range(6):
            assert vault.get_secret(f'KEY_{n}_{k}') == f'fixture-value-{n}-{k}'
    assert b'fixture-value' not in vault.vault_path.read_bytes()


@pytest.mark.parametrize('replace', [False, True])
def test_competing_creates_or_replacements_have_only_one_winner(tmp_path, replace):
    vault = AESGCMVault('fixture', tmp_path / 'vault.enc', master_password='fixture-only-password')
    revision = ''
    if replace:
        vault.set_secret('SAME', 'fixture-initial')
        revision = vault.get_keys_snapshot()['revision']
    result = workers(tmp_path, 'conditional', 2, revision)
    assert sorted(result) == ['conflict', 'stored']
    assert vault.get_secret('SAME') in ['fixture-value-0', 'fixture-value-1']


def test_shared_lock_is_reentrant_and_excludes_another_process(tmp_path):
    from desktop.auth.file_lock import interprocess_lock
    target = tmp_path / 'shared.lock'
    script = r"""
import sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from desktop.auth.file_lock import interprocess_lock, FileLockTimeoutError
try:
    with interprocess_lock(Path(sys.argv[2]), timeout=.1): pass
except FileLockTimeoutError: print('excluded')
else: print('entered')
"""
    with interprocess_lock(target):
        with interprocess_lock(target, timeout=.1):
            result = subprocess.run([sys.executable, '-B', '-c', script, str(ROOT), str(target)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'excluded'
    with interprocess_lock(target, timeout=.1):
        pass
    assert target.exists()  # Stable sidecar must not be unlinked after releasing.
