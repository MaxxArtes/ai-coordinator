"""Etapa 7: settings migrations never discard existing preferences or profile permissions."""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from desktop.auth.profile import DesktopProfileManager, ProfileError, ProfileValidationError
from desktop.settings.store import SettingsStore, SettingsValidationError


@pytest.fixture
def profiles(tmp_path):
    return DesktopProfileManager(tmp_path / "profiles")


def payload(category="terminal", **values):
    return {"schema_version": 1, "category": category, "values": values}


def profile_file(profiles):
    return profiles.root_dir / "default" / "profile.json"


@pytest.mark.parametrize("version", [None, 0])
def test_legacy_values_survive_explicit_migration_on_write(profiles, version):
    old = {"values": {"terminal": {"font_size": 18}, "plugin": {"flag": "keep"}}, "extensions": {"future": [1, 2]}}
    if version is not None:
        old["schema_version"] = version
    profiles.update_settings("default", old)
    before = profile_file(profiles).read_bytes()
    store = SettingsStore(profiles)
    snapshot = store.snapshot()
    assert snapshot["categories"]["terminal"]["values"]["font_size"] == 18
    assert snapshot["migration"]["required"] is True
    assert profile_file(profiles).read_bytes() == before  # reading never rewrites an old profile
    store.update(payload(cursor_blink=False))
    saved = profiles.get_active_profile().settings
    assert saved["schema_version"] == 1
    assert saved["values"]["terminal"] == {"font_size": 18, "cursor_blink": False}
    assert saved["values"]["plugin"] == {"flag": "keep"}
    assert saved["extensions"] == {"future": [1, 2]}


def test_legacy_flat_known_categories_are_migrated_without_discarding_extensions(profiles):
    profiles.update_settings("default", {"terminal": {"font_size": 17}, "legacy_plugin": {"custom": "keep"}})
    store = SettingsStore(profiles)
    assert store.snapshot()["categories"]["terminal"]["values"]["font_size"] == 17
    store.update(payload(cursor_blink=False))
    saved = profiles.get_active_profile().settings
    assert saved["values"]["terminal"]["font_size"] == 17
    assert saved["legacy_plugin"] == {"custom": "keep"}


def test_future_schema_is_read_only_and_preserves_exact_bytes(profiles):
    profiles.update_settings("default", {"schema_version": 2, "values": {"terminal": {"font_size": 21}}, "future": {"keep": True}})
    before = profile_file(profiles).read_bytes()
    store = SettingsStore(profiles)
    snapshot = store.snapshot()  # K1: reading never fails because of stored data
    assert snapshot["read_only"] is True
    assert snapshot["stored_schema_version"] == 2
    assert snapshot["stored_issue"]["reason"] == "incompatible_version"
    assert snapshot["migration"]["required"] is False
    assert all(category["validated"] is False for category in snapshot["categories"].values())
    with pytest.raises(SettingsValidationError) as error:
        store.update(payload(font_size=15))
    assert error.value.field == "settings"
    assert error.value.reason == "read_only_stored_schema"
    assert str(error.value) == "As configurações foram gravadas por outra versão e estão protegidas. Nada foi alterado."
    assert profile_file(profiles).read_bytes() == before


def test_read_only_snapshot_shows_the_defaults_it_says_are_in_use(profiles):
    """Contracts #6: "Os padrões estão em uso" must be true. Values stored by another version are neither shown nor
    applied by this one; the fields that are stored are still listed, so no category looks unsaved."""
    profiles.update_settings("default", {"schema_version": 2, "values": {
        "aparencia": {"stimulation": "low", "density": "compact"},
        "terminal": {"font_size": 21, "scrollback_lines": 100},
        "perfil": {"display_name": "Nome da v2"},
        "geral": {"open_last_project_on_start": False}}})
    before = profile_file(profiles).read_bytes()
    snapshot = SettingsStore(profiles).snapshot()
    assert snapshot["read_only"] is True and "Os padrões estão em uso" in snapshot["stored_issue"]["message"]
    categories = snapshot["categories"]
    assert categories["aparencia"]["values"] == {"stimulation": "standard", "density": "comfortable"}
    assert categories["terminal"]["values"] == {"font_size": 13, "cursor_blink": True, "scrollback_lines": 1000}
    assert categories["perfil"]["values"] == {"display_name": None}
    assert categories["geral"]["values"] == {"open_last_project_on_start": True}
    assert "Nome da v2" not in json.dumps(categories, ensure_ascii=False)
    assert categories["aparencia"]["saved_fields"] == ["density", "stimulation"]
    assert categories["terminal"]["saved_fields"] == ["font_size", "scrollback_lines"]
    assert categories["perfil"]["saved_fields"] == ["display_name"]
    assert categories["geral"]["saved_fields"] == ["open_last_project_on_start"]
    assert all(c["invalid_fields"] == [] and c["validated"] is False for c in categories.values())
    assert profile_file(profiles).read_bytes() == before


def test_malformed_category_makes_its_valid_neighbours_show_defaults_too(profiles):
    profiles.update_settings("default", {"schema_version": 1, "values": {"terminal": 5, "aparencia": {"stimulation": "low"}}})
    snapshot = SettingsStore(profiles).snapshot()
    assert snapshot["read_only"] is True and snapshot["stored_issue"]["reason"] == "invalid_stored_value"
    assert "Os padrões estão em uso" in snapshot["stored_issue"]["message"]
    aparencia = snapshot["categories"]["aparencia"]
    assert aparencia["values"] == {"stimulation": "standard", "density": "comfortable"}
    assert aparencia["saved_fields"] == ["stimulation"] and aparencia["validated"] is False
    assert snapshot["categories"]["terminal"]["values"]["font_size"] == 13


@pytest.mark.parametrize("stored", [["not", "an", "object"], {"schema_version": 1, "values": []},
                                    {"schema_version": 1}, {"schema_version": 1, "values": {"terminal": 5}}])
def test_malformed_envelope_or_category_is_read_only_and_keeps_the_profile(profiles, tmp_path, stored):
    project = tmp_path / "kept"
    project.mkdir()
    profiles.authorize_project("default", project)
    profiles.update_settings("default", stored)
    before = profile_file(profiles).read_bytes()
    snapshot = SettingsStore(profiles).snapshot()
    assert snapshot["read_only"] is True
    assert snapshot["stored_issue"]["reason"] == "invalid_stored_value"
    assert snapshot["categories"]["terminal"]["values"]["font_size"] == 13
    with pytest.raises(SettingsValidationError) as error:
        SettingsStore(profiles).update(payload(font_size=15))
    assert error.value.reason == "read_only_stored_schema"
    assert profile_file(profiles).read_bytes() == before
    assert profiles.get_active_profile().authorized_projects == [str(project.resolve())]


def test_unknown_fields_categories_and_envelope_survive_partial_update(profiles):
    old = {"schema_version": 1, "values": {"terminal": {"font_size": 16, "future_field": {"a": 1}},
           "future_category": {"a": [1, 2]}}, "extension": "keep"}
    profiles.update_settings("default", old)
    SettingsStore(profiles).update(payload(cursor_blink=False))
    saved = profiles.get_active_profile().settings
    assert saved["extension"] == "keep"
    assert saved["values"]["future_category"] == {"a": [1, 2]}
    assert saved["values"]["terminal"] == {"font_size": 16, "future_field": {"a": 1}, "cursor_blink": False}


@pytest.mark.parametrize("bad", [99, "18", True, None])
def test_invalid_persisted_field_never_claims_validated_and_is_kept_until_rewritten(profiles, bad):
    profiles.update_settings("default", {"schema_version": 1, "values": {"terminal": {"font_size": bad, "cursor_blink": False}}})
    before = profile_file(profiles).read_bytes()
    snapshot = SettingsStore(profiles).snapshot()
    terminal = snapshot["categories"]["terminal"]
    assert terminal["invalid_fields"] == ["font_size"]
    assert terminal["values"]["font_size"] == 13 and terminal["values"]["cursor_blink"] is False
    assert terminal["validated"] is False and snapshot["read_only"] is False
    assert snapshot["stored_issue"]["reason"] == "invalid_stored_value"
    assert snapshot["categories"]["aparencia"]["validated"] is True
    assert profile_file(profiles).read_bytes() == before  # reading never rewrites
    # Another field is writable; the invalid stored value stays exactly as it was (BE-05).
    SettingsStore(profiles).update(payload(category="aparencia", density="compact"))
    saved = profiles.get_active_profile().settings["values"]
    assert saved["terminal"] == {"font_size": bad, "cursor_blink": False}
    assert saved["aparencia"] == {"density": "compact"}
    # Writing the field itself replaces the invalid value.
    fixed = SettingsStore(profiles).update(payload(font_size=14))
    assert fixed["categories"]["terminal"]["invalid_fields"] == []
    assert fixed["categories"]["terminal"]["validated"] is True and fixed["stored_issue"] is None
    assert profiles.get_active_profile().settings["values"]["terminal"] == {"font_size": 14, "cursor_blink": False}


def test_categories_honestly_delegate_application_to_ui(profiles):
    categories = SettingsStore(profiles).snapshot()["categories"]
    for name in ["perfil", "geral", "aparencia", "terminal"]:
        assert categories[name]["application"] == "interface"
        assert categories[name]["applied"] is False
    assert categories["coordinator"]["application"] == "unsupported"


def quarantined(profiles):
    return sorted(profile_file(profiles).parent.glob("profile.json.corrupt-*"))


@pytest.mark.parametrize("contents", [b'{"name":', b'[]', b'', b'{"name":"someone-else","data_dir":"x"}'])
def test_corrupt_active_profile_is_quarantined_byte_for_byte_and_recreated_empty(profiles, tmp_path, contents):
    """E2-6/K2: opening is never blocked; the unreadable bytes are kept in a quarantine copy, never overwritten."""
    p = profile_file(profiles)
    p.write_bytes(contents)
    manager = DesktopProfileManager(profiles.root_dir)  # does not raise
    active = manager.get_active_profile()
    assert active.name == "default" and active.authorized_projects == [] and active.settings == {}
    copies = quarantined(profiles)
    assert len(copies) == 1 and copies[0].read_bytes() == contents
    assert manager.recovery == {"reason": "profile_unreadable", "backup_file": copies[0].name,
                                "message": manager.recovery["message"]}
    assert [q.name for q in quarantined(profiles)] == [copies[0].name]
    # A second corruption gets its own copy; the first quarantine is never replaced.
    p.write_bytes(b"[1]")
    DesktopProfileManager(profiles.root_dir).get_active_profile()
    copies_after = quarantined(profiles)
    assert len(copies_after) == 2 and copies[0].read_bytes() == contents


def test_recovery_whose_rewrite_fails_leaves_no_quarantine_copy(profiles, monkeypatch):
    """A recovery that cannot write the new profile must not add one more copy per call (integrity #1)."""
    import desktop.auth.profile as module
    p = profile_file(profiles)
    p.write_bytes(b'{"name":')
    real_replace = module.os.replace
    def fail(source, target):
        if Path(target).name == "profile.json":
            raise OSError("synthetic replace failure")
        return real_replace(source, target)
    monkeypatch.setattr(module.os, "replace", fail)
    for _ in range(3):
        with pytest.raises(ProfileError):
            DesktopProfileManager(profiles.root_dir).get_active_profile()
    assert quarantined(profiles) == [] and p.read_bytes() == b'{"name":'
    assert list(p.parent.glob("profile_*.tmp")) == []
    monkeypatch.undo()
    manager = DesktopProfileManager(profiles.root_dir)
    assert manager.get_active_profile().name == "default"
    copies = quarantined(profiles)
    assert len(copies) == 1 and copies[0].read_bytes() == b'{"name":'
    assert manager.recovery["backup_file"] == copies[0].name


@pytest.mark.skipif(os.name != "nt", reason="the read-only attribute makes os.replace fail only on Windows")
def test_read_only_corrupt_profile_is_not_copied_again_on_every_call(profiles):
    """Integrity #1 with the real cause: profile.json truncated and marked read-only on Windows."""
    p = profile_file(profiles)
    p.write_bytes(b'{"name":')
    os.chmod(p, stat.S_IREAD)
    try:
        for _ in range(3):
            with pytest.raises(ProfileError):
                DesktopProfileManager(profiles.root_dir).get_active_profile()
            assert quarantined(profiles) == []
        assert p.read_bytes() == b'{"name":'
    finally:
        os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
    manager = DesktopProfileManager(profiles.root_dir)
    assert manager.get_active_profile().name == "default"
    copies = quarantined(profiles)
    assert len(copies) == 1 and copies[0].read_bytes() == b'{"name":'
    assert manager.recovery["backup_file"] == copies[0].name


def test_corrupt_profile_recovery_does_not_echo_data(profiles):
    p = profile_file(profiles)
    p.write_bytes(b'{"name": "SYNTHETIC_SECRET", "data_dir": ["SYNTHETIC_SECRET"]}')
    profiles.get_active_profile()
    assert "SYNTHETIC_SECRET" not in json.dumps(profiles.recovery)
    assert b"SYNTHETIC_SECRET" not in p.read_bytes()
    assert quarantined(profiles)[0].read_bytes() == b'{"name": "SYNTHETIC_SECRET", "data_dir": ["SYNTHETIC_SECRET"]}'


def test_unreadable_inactive_profile_is_ignored_without_quarantine(profiles):
    other = profiles.root_dir / "work" / "profile.json"
    other.parent.mkdir()
    other.write_bytes(b'{"name":')
    manager = DesktopProfileManager(profiles.root_dir)
    assert [p.name for p in manager.list_profiles()] == ["default"]
    assert manager.get_active_profile().name == "default" and manager.recovery is None
    assert other.read_bytes() == b'{"name":'
    assert list(other.parent.glob("profile.json.*")) == []


def test_moved_profiles_folder_still_opens_with_the_real_data_dir(profiles, tmp_path):
    import shutil
    profiles.update_settings("default", {"schema_version": 1, "values": {"terminal": {"font_size": 18}}})
    moved = tmp_path / "moved" / "profiles"
    shutil.copytree(profiles.root_dir, moved)
    manager = DesktopProfileManager(moved)
    active = manager.get_active_profile()
    assert Path(active.data_dir) == (moved / "default").resolve()
    assert manager.recovery is None
    assert active.settings["values"]["terminal"]["font_size"] == 18


def test_profile_saved_with_bom_is_read_without_quarantine(profiles):
    p = profile_file(profiles)
    p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    assert profiles.get_active_profile().name == "default"
    assert quarantined(profiles) == [] and profiles.recovery is None


def test_first_migrating_write_keeps_a_pre_v1_copy_once(profiles):
    profiles.update_settings("default", {"terminal": {"font_size": 17}})
    legacy = profile_file(profiles).read_bytes()
    backup = profile_file(profiles).with_name("profile.json.pre-v1.bak")
    SettingsStore(profiles).update(payload(cursor_blink=False))
    assert backup.read_bytes() == legacy
    SettingsStore(profiles).update(payload(font_size=12))  # already v1: no new copy, the first one is kept
    assert backup.read_bytes() == legacy
    profiles.update_settings("default", {"schema_version": 0, "values": {"terminal": {"font_size": 16}}})
    SettingsStore(profiles).update(payload(cursor_blink=True))
    assert backup.read_bytes() == legacy  # never overwritten


def test_interrupted_pre_v1_copy_is_redone_before_migrating(profiles):
    """A 0-byte pre-v1 copy (write cut short) holds nothing: it must not count as the one-time backup."""
    profiles.update_settings("default", {"terminal": {"font_size": 17}})
    legacy = profile_file(profiles).read_bytes()
    backup = profile_file(profiles).with_name("profile.json.pre-v1.bak")
    backup.write_bytes(b"")
    SettingsStore(profiles).update(payload(cursor_blink=False))
    assert backup.read_bytes() == legacy


def test_interrupted_pre_v1_copy_that_cannot_be_removed_blocks_the_migration(profiles, monkeypatch):
    """Migrating without any copy would lose the legacy bytes: refuse and leave the profile untouched."""
    import desktop.auth.profile as profile_module

    profiles.update_settings("default", {"terminal": {"font_size": 17}})
    legacy = profile_file(profiles).read_bytes()
    backup = profile_file(profiles).with_name("profile.json.pre-v1.bak")
    backup.write_bytes(b"")
    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if Path(path) == backup:
            raise PermissionError(13, "held by another program")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(profile_module.os, "unlink", refuse)
    with pytest.raises(ProfileError):
        SettingsStore(profiles).update(payload(cursor_blink=False))
    assert profile_file(profiles).read_bytes() == legacy
    assert backup.read_bytes() == b""


def test_v1_profile_write_makes_no_backup(profiles):
    SettingsStore(profiles).update(payload(font_size=15))
    assert not profile_file(profiles).with_name("profile.json.pre-v1.bak").exists()


def test_failed_atomic_write_preserves_existing_profile_and_cleans_own_temp(profiles, monkeypatch):
    import desktop.auth.profile as module
    before = profile_file(profiles).read_bytes()
    real_replace = module.os.replace
    def fail(source, target):
        if Path(target).name == "profile.json":
            raise OSError("synthetic replace failure")
        return real_replace(source, target)
    monkeypatch.setattr(module.os, "replace", fail)
    with pytest.raises(ProfileError):
        profiles.update_settings("default", {"sentinel": "do not store"})
    assert profile_file(profiles).read_bytes() == before
    assert list(profile_file(profiles).parent.glob("profile_*.tmp")) == []


def test_last_project_requires_existing_authorization_and_revoke_preserves_preferences(profiles, tmp_path):
    project = tmp_path / "repo"
    project.mkdir()
    profiles.update_settings("default", {"schema_version": 1, "values": {"terminal": {"font_size": 18}}})
    before = profile_file(profiles).read_bytes()
    with pytest.raises(ProfileValidationError):
        profiles.remember_project("default", project)
    assert profile_file(profiles).read_bytes() == before
    profiles.authorize_project("default", project)
    profiles.remember_project("default", project)
    stored = profiles.get_active_profile()
    assert stored.last_project == str(project.resolve())
    assert stored.to_dict()["last_project"] == str(project.resolve())
    assert profiles.revoke_project("default", project) is True
    stored = profiles.get_active_profile()
    assert stored.last_project is None  # K4/BE-16: a revoked directory is not kept as the last project
    assert stored.settings["values"]["terminal"]["font_size"] == 18
    assert profiles.is_project_authorized("default", project) is False


def test_unknown_profile_fields_survive_preferences_update(profiles):
    p = profile_file(profiles)
    data = json.loads(p.read_text(encoding="utf-8"))
    data["future_profile_extension"] = {"retain": [1, 2]}
    p.write_text(json.dumps(data), encoding="utf-8")
    SettingsStore(profiles).update(payload(font_size=17))
    assert json.loads(p.read_text(encoding="utf-8"))["future_profile_extension"] == {"retain": [1, 2]}


def _concurrent_update(root, project, barrier, action):
    profiles = DesktopProfileManager(Path(root))
    barrier.wait(timeout=15)
    if action == "revoke":
        profiles.revoke_project("default", project)
    else:
        SettingsStore(profiles).update(payload(category="aparencia", density="compact"))


def test_concurrent_preference_update_and_revocation_do_not_lose_each_other(profiles, tmp_path):
    project = tmp_path / "concurrent"
    project.mkdir()
    profiles.authorize_project("default", project)
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(2)
    jobs = [ctx.Process(target=_concurrent_update, args=(str(profiles.root_dir), str(project), barrier, action)) for action in ["revoke", "settings"]]
    for p in jobs: p.start()
    for p in jobs: p.join(timeout=20)
    for p in jobs:
        if p.is_alive(): p.terminate(); p.join()
        assert p.exitcode == 0
    stored = profiles.get_active_profile()
    assert str(project.resolve()) not in stored.authorized_projects
    assert stored.settings["values"]["aparencia"]["density"] == "compact"



WRITER = r"""
import sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/src"]
from pathlib import Path
from desktop.auth.profile import DesktopProfileManager
profiles = DesktopProfileManager(Path(sys.argv[2]))
failures = 0
for n in range(int(sys.argv[3])):
    try:
        profiles.update_settings("default", {"schema_version": 1, "values": {"terminal": {"font_size": 10 + n % 10}}})
    except Exception:
        failures += 1
print(failures)
"""


def test_reader_and_writer_processes_never_fail_on_a_sharing_violation(profiles):
    """BE-06: on Windows a reader without FILE_SHARE_DELETE briefly blocks os.replace in another process, and vice
    versa. Both sides retry for a short, bounded time instead of reporting an unreadable or unwritable profile."""
    root = Path(__file__).resolve().parents[1]
    writer = subprocess.Popen([sys.executable, "-B", "-c", WRITER, str(root), str(profiles.root_dir), "60"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    reads = failures = 0
    deadline = time.monotonic() + 90
    try:
        while writer.poll() is None and time.monotonic() < deadline:
            try:
                profiles.get_active_profile()
                reads += 1
            except ProfileError:
                failures += 1
        out, err = writer.communicate(timeout=60)
    finally:
        if writer.poll() is None:
            writer.kill()
            writer.communicate(timeout=10)
    assert writer.returncode == 0, err
    assert int(out.strip()) == 0 and failures == 0 and reads > 0
    assert profiles.recovery is None and quarantined(profiles) == []


def test_invalid_utf8_profile_is_quarantined_exactly_without_echo(profiles):
    p = profile_file(profiles)
    p.write_bytes(b'\xff\xfeSYNTHETIC_SECRET')
    active = profiles.get_active_profile()
    assert "SYNTHETIC_SECRET" not in json.dumps(profiles.recovery) + json.dumps(active.to_dict())
    assert quarantined(profiles)[0].read_bytes() == b'\xff\xfeSYNTHETIC_SECRET'


def test_unreadable_file_is_not_quarantined_or_overwritten(profiles, monkeypatch):
    """Bytes that cannot be read cannot be copied: the profile stays as it is and a ProfileError is reported."""
    before = profile_file(profiles).read_bytes()
    real = Path.read_bytes
    def deny(self):
        if self.name == "profile.json":
            raise OSError("synthetic read failure")
        return real(self)
    monkeypatch.setattr(Path, "read_bytes", deny)
    with pytest.raises(ProfileError):
        profiles.get_active_profile()
    monkeypatch.undo()
    assert profile_file(profiles).read_bytes() == before and quarantined(profiles) == []


def selection_copies(profiles):
    return sorted(profiles.root_dir.glob("active_profile.txt.corrupt-*"))


@pytest.mark.parametrize("contents", [b"missing", b"", b"  \r\n", b"\xff\xfe\x00", b"\x00\x00", b"not a valid name!",
                                      b"..", "default".encode("utf-16")],
                         ids=["dangling", "empty", "blank", "invalid_utf8", "nul", "invalid_name", "dotdot", "utf16"])
def test_invalid_or_dangling_selection_opens_default_and_keeps_a_copy(profiles, contents):
    """E2-6 (security #4): the active selection is no longer a single point of failure. Its bytes are kept, the
    default profile is opened and the missing profile named in it is never created."""
    before = profile_file(profiles).read_bytes()
    profiles.active_file.write_bytes(contents)
    manager = DesktopProfileManager(profiles.root_dir)
    active = manager.get_active_profile()
    assert active.name == "default"
    assert not (profiles.root_dir / "missing").exists()
    assert profile_file(profiles).read_bytes() == before and quarantined(profiles) == []
    copies = selection_copies(profiles)
    assert len(copies) == 1 and copies[0].read_bytes() == contents
    assert manager.recovery == {"reason": "active_selection_invalid", "backup_file": copies[0].name,
                                "message": manager.recovery["message"]}
    assert manager.recovery["message"] and "missing" not in manager.recovery["message"]
    assert profiles.active_file.read_bytes() == b"default"
    # From then on the selection is ordinary: no new copy and no recovery notice.
    again = DesktopProfileManager(profiles.root_dir)
    assert again.get_active_profile().name == "default" and again.recovery is None
    assert selection_copies(profiles) == copies


@pytest.mark.parametrize("contents", [b"\xef\xbb\xbfdefault", b"default\r\n", b"\xef\xbb\xbfdefault\r\n"])
def test_selection_with_bom_or_line_break_is_read_as_is(profiles, contents):
    profiles.active_file.write_bytes(contents)
    manager = DesktopProfileManager(profiles.root_dir)
    assert manager.get_active_profile().name == "default" and manager.recovery is None
    assert profiles.active_file.read_bytes() == contents and selection_copies(profiles) == []


def test_selection_in_other_letter_case_never_quarantines_a_healthy_profile(profiles, tmp_path):
    """Review finding: "Work" in active_profile.txt must not turn the healthy "work" profile into a corrupt one.
    Case-insensitive file systems (Windows, macOS) open "work" under its stored name; case-sensitive ones treat the
    selection as dangling. Either way the profile keeps its bytes, its authorizations and its vault name."""
    project = tmp_path / "proj"
    project.mkdir()
    profiles.create_profile("work")
    profiles.authorize_project("work", project)
    work_file = profiles.root_dir / "work" / "profile.json"
    before = work_file.read_bytes()
    profiles.active_file.write_bytes(b"Work")
    case_insensitive = (profiles.root_dir / "WORK").exists()
    manager = DesktopProfileManager(profiles.root_dir)
    active = manager.get_active_profile()
    assert work_file.read_bytes() == before
    assert sorted((profiles.root_dir / "work").glob("profile.json.corrupt-*")) == []
    if case_insensitive:
        assert active.name == "work" and manager.recovery is None
        assert Path(active.data_dir) == (profiles.root_dir / "work")
        assert active.authorized_projects == [str(project.resolve())]
        assert manager.set_active_profile("WORK").name == "work"
        assert profiles.active_file.read_bytes() == b"work"  # the stored spelling is written back
        # Writes requested in another case lock by the stored name (macOS: two lock keys for one file would wait 10 s).
        other = tmp_path / "other"
        other.mkdir()
        started = time.monotonic()
        manager.authorize_project("WORK", other)
        assert time.monotonic() - started < 5
        assert str(other.resolve()) in manager.get_profile("work").authorized_projects
        project_count = 2
    else:
        project_count = 1
    if not case_insensitive:
        assert active.name == "default"
        assert manager.recovery["reason"] == "active_selection_invalid"
    stored = DesktopProfileManager(profiles.root_dir).get_profile("work").authorized_projects
    assert str(project.resolve()) in stored and len(stored) == project_count


@pytest.mark.skipif(os.name != "nt", reason="Windows drops a trailing dot from a folder name")
def test_selection_with_a_trailing_dot_opens_the_same_profile_on_windows(profiles, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    profiles.create_profile("work")
    profiles.authorize_project("work", project)
    before = (profiles.root_dir / "work" / "profile.json").read_bytes()
    profiles.active_file.write_bytes(b"work.")
    manager = DesktopProfileManager(profiles.root_dir)
    active = manager.get_active_profile()
    assert active.name == "work" and manager.recovery is None
    assert active.authorized_projects == [str(project.resolve())]
    assert (profiles.root_dir / "work" / "profile.json").read_bytes() == before
    assert sorted((profiles.root_dir / "work").glob("profile.json.corrupt-*")) == []


def test_dangling_selection_without_a_default_profile_creates_only_the_default(profiles):
    profiles.create_profile("work")
    shutil.rmtree(profiles.root_dir / "default")
    profiles.active_file.write_bytes(b"gone")
    manager = DesktopProfileManager(profiles.root_dir)
    active = manager.get_active_profile()
    assert active.name == "default" and active.authorized_projects == [] and active.settings == {}
    assert sorted(p.name for p in profiles.root_dir.iterdir() if p.is_dir()) == ["default", "work"]
    assert manager.recovery["reason"] == "active_selection_invalid"
    assert selection_copies(profiles)[0].read_bytes() == b"gone"


def test_dangling_selection_with_an_unreadable_default_reports_the_profile_recovery(profiles):
    profile_file(profiles).write_bytes(b'{"name":')
    profiles.active_file.write_bytes(b"gone")
    manager = DesktopProfileManager(profiles.root_dir)
    assert manager.get_active_profile().name == "default"
    # Re-authorizing the projects is what the operator must do: that notice is the one reported.
    assert manager.recovery["reason"] == "profile_unreadable"
    assert quarantined(profiles)[0].read_bytes() == b'{"name":'
    assert selection_copies(profiles)[0].read_bytes() == b"gone"
    assert profiles.active_file.read_bytes() == b"default"


def test_selection_rewrite_failure_leaves_no_copy_and_changes_nothing(profiles, monkeypatch):
    import desktop.auth.profile as module
    profiles.active_file.write_bytes(b"gone")
    real_replace = module.os.replace
    def fail(source, target):
        if Path(target).name == "active_profile.txt":
            raise OSError("synthetic replace failure")
        return real_replace(source, target)
    monkeypatch.setattr(module.os, "replace", fail)
    for _ in range(2):
        with pytest.raises(ProfileError):
            DesktopProfileManager(profiles.root_dir).get_active_profile()
    assert selection_copies(profiles) == [] and profiles.active_file.read_bytes() == b"gone"
    assert not (profiles.root_dir / "gone").exists()


def test_budget_extensions_survive_updates_but_are_not_returned_as_core_fields(profiles):
    profiles.update_settings("default", {"schema_version": 1, "values": {"coordinator": {
        "flight_budget": {"max_steps": 20, "future_field": "keep"}}}})
    store = SettingsStore(profiles)
    assert "future_field" not in store.snapshot()["categories"]["coordinator"]["values"]["flight_budget"]
    store.update(payload(category="coordinator", flight_budget={"max_steps": 30}))
    stored = profiles.get_active_profile().settings["values"]["coordinator"]["flight_budget"]
    assert stored["future_field"] == "keep"
    assert stored["max_steps"] == 30
