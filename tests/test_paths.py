"""Tests for serial_mcp_server.paths — confinement of file reads to one directory."""

from __future__ import annotations

import os
import stat
import sys

import pytest

from serial_mcp_server import paths
from serial_mcp_server.paths import PathRefused, read_confined_file, resolve_confined_file

LIMIT = 1024
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="needs POSIX symlinks and FIFOs")


@pytest.fixture()
def root(tmp_path):
    directory = tmp_path / "files"
    directory.mkdir()
    (directory / "ok.txt").write_bytes(b"hello\n")
    (directory / "sub").mkdir()
    (directory / "sub" / "nested.txt").write_bytes(b"nested")
    return directory


@pytest.fixture()
def secret(tmp_path):
    """A readable file outside the files directory, like an SSH key."""
    path = tmp_path / "id_rsa"
    path.write_bytes(b"PRIVATE KEY")
    return path


class TestAccepted:
    def test_a_file_in_the_directory(self, root):
        assert resolve_confined_file(root, "ok.txt", LIMIT) == (root / "ok.txt").resolve()

    def test_a_file_in_a_subdirectory(self, root):
        assert read_confined_file(root, "sub/nested.txt", LIMIT)[1] == b"nested"

    def test_returns_the_exact_bytes(self, root):
        (root / "bin").write_bytes(bytes(range(256)))
        assert read_confined_file(root, "bin", LIMIT)[1] == bytes(range(256))

    def test_a_file_exactly_at_the_limit(self, root):
        (root / "edge").write_bytes(b"x" * LIMIT)
        assert len(read_confined_file(root, "edge", LIMIT)[1]) == LIMIT

    def test_a_dot_dot_that_stays_inside_is_fine(self, root):
        assert read_confined_file(root, "sub/../ok.txt", LIMIT)[1] == b"hello\n"

    @POSIX_ONLY
    def test_a_symlink_that_stays_inside_is_fine(self, root):
        (root / "alias").symlink_to(root / "ok.txt")
        assert read_confined_file(root, "alias", LIMIT)[1] == b"hello\n"

    @POSIX_ONLY
    def test_a_symlinked_files_directory_is_resolved_first(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (real / "f").write_bytes(b"data")
        link = tmp_path / "link"
        link.symlink_to(real)
        assert read_confined_file(link, "f", LIMIT)[1] == b"data"


class TestRefused:
    @pytest.mark.parametrize("relative", ["../id_rsa", "sub/../../id_rsa", "../../../../etc/passwd"])
    def test_traversal_out_of_the_directory(self, root, secret, relative):
        with pytest.raises(PathRefused, match="inside the files directory"):
            read_confined_file(root, relative, LIMIT)

    @pytest.mark.parametrize(
        "relative",
        [
            "/etc/passwd",
            "\\\\server\\share\\x",
            "C:\\Windows\\win.ini",
            "C:win.ini",
            "\\rooted",
            "//host/share/x",
        ],
    )
    def test_an_absolute_drive_or_unc_path_is_refused_without_resolving_anything(
        self, root, relative, monkeypatch
    ):
        def fail_if_resolved(self, *args, **kwargs):
            raise AssertionError("resolved a path that should have been refused first")

        monkeypatch.setattr(type(root), "resolve", fail_if_resolved)

        with pytest.raises(PathRefused, match="must be relative"):
            read_confined_file(root, relative, LIMIT)

    def test_an_absolute_path_that_points_inside_is_refused_too(self, root):
        with pytest.raises(PathRefused, match="must be relative"):
            read_confined_file(root, str(root / "ok.txt"), LIMIT)

    @pytest.mark.parametrize("relative", [".", "", "   ", "sub/..", "./"])
    def test_the_directory_itself_or_an_empty_path(self, root, relative):
        with pytest.raises(PathRefused):
            read_confined_file(root, relative, LIMIT)

    def test_a_directory(self, root):
        with pytest.raises(PathRefused, match="not a regular file"):
            read_confined_file(root, "sub", LIMIT)

    def test_a_missing_file(self, root):
        with pytest.raises(PathRefused, match="no file"):
            read_confined_file(root, "absent.txt", LIMIT)

    @pytest.mark.parametrize("bad", [None, 5, b"ok.txt", ["ok.txt"]])
    def test_a_path_that_is_not_a_string(self, root, bad):
        with pytest.raises(PathRefused, match="non-empty string"):
            read_confined_file(root, bad, LIMIT)

    def test_a_nul_byte(self, root):
        with pytest.raises(PathRefused):
            read_confined_file(root, "ok.txt\x00../id_rsa", LIMIT)

    def test_an_oversize_file(self, root):
        (root / "big").write_bytes(b"x" * (LIMIT + 1))
        with pytest.raises(PathRefused, match="limit is 1024"):
            read_confined_file(root, "big", LIMIT)

    @POSIX_ONLY
    def test_a_symlink_that_points_outside(self, root, secret):
        (root / "key").symlink_to(secret)
        with pytest.raises(PathRefused, match="inside the files directory") as excinfo:
            read_confined_file(root, "key", LIMIT)
        assert str(secret) not in str(excinfo.value)  # the message must not reveal where the link led

    @POSIX_ONLY
    def test_a_symlinked_directory_that_points_outside(self, root, secret):
        (root / "out").symlink_to(secret.parent)
        with pytest.raises(PathRefused):
            read_confined_file(root, "out/id_rsa", LIMIT)

    @POSIX_ONLY
    def test_a_dangling_symlink(self, root, tmp_path):
        (root / "dangling").symlink_to(tmp_path / "nowhere")
        with pytest.raises(PathRefused):
            read_confined_file(root, "dangling", LIMIT)

    @POSIX_ONLY
    def test_a_symlink_loop(self, root):
        (root / "a").symlink_to(root / "b")
        (root / "b").symlink_to(root / "a")
        with pytest.raises(PathRefused):
            read_confined_file(root, "a", LIMIT)

    @POSIX_ONLY
    def test_a_fifo_is_refused_before_any_read_and_does_not_hang(self, root):
        os.mkfifo(root / "pipe")
        with pytest.raises(PathRefused, match="not a regular file"):
            read_confined_file(root, "pipe", LIMIT)


class TestDeviceFiles:
    @pytest.mark.parametrize("mode", [stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFDIR])
    def test_only_a_regular_file_passes_the_type_check(self, mode):
        info = os.stat_result((mode | 0o600, 1, 1, 1, 0, 0, 10, 0, 0, 0))
        with pytest.raises(PathRefused, match="not a regular file"):
            paths._check_regular_and_size(info, "dev", LIMIT)

    def test_a_regular_file_passes(self):
        info = os.stat_result((stat.S_IFREG | 0o600, 1, 1, 1, 0, 0, 10, 0, 0, 0))
        paths._check_regular_and_size(info, "ok", LIMIT)


class TestRecheckOnTheOpenHandle:
    """Someone with write access to the directory swaps things after the path check passed."""

    @staticmethod
    def swap_after_check(monkeypatch, swap):
        real = paths._resolve_checked

        def check_then_swap(*args, **kwargs):
            result = real(*args, **kwargs)
            swap(result[0])
            return result

        monkeypatch.setattr(paths, "_resolve_checked", check_then_swap)

    @POSIX_ONLY
    def test_a_file_swapped_for_a_fifo_is_refused_without_hanging(self, root, monkeypatch):
        def swap(resolved):
            resolved.unlink()
            os.mkfifo(resolved)

        self.swap_after_check(monkeypatch, swap)

        with pytest.raises(PathRefused, match="not a regular file"):
            read_confined_file(root, "ok.txt", LIMIT)

    @POSIX_ONLY
    def test_a_file_swapped_for_a_symlink_is_not_followed(self, root, secret, monkeypatch):
        def swap(resolved):
            resolved.unlink()
            resolved.symlink_to(secret)

        self.swap_after_check(monkeypatch, swap)

        with pytest.raises(OSError):  # O_NOFOLLOW refuses a link in the final component
            read_confined_file(root, "ok.txt", LIMIT)

    @POSIX_ONLY
    def test_a_parent_directory_swapped_for_a_symlink_is_refused(self, root, secret, tmp_path, monkeypatch):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "nested.txt").write_bytes(b"outside file, same size!")
        (root / "sub" / "nested.txt").write_bytes(b"outside file, same size!")  # same size and type

        def swap(resolved):
            real_sub = resolved.parent
            (real_sub / "nested.txt").unlink()
            real_sub.rmdir()
            real_sub.symlink_to(outside)  # O_NOFOLLOW does not look at this intermediate component

        self.swap_after_check(monkeypatch, swap)

        with pytest.raises(PathRefused):  # a different file (inode) or a path outside the root
            read_confined_file(root, "sub/nested.txt", LIMIT)

    @POSIX_ONLY
    def test_a_file_replaced_by_another_regular_file_is_refused(self, root, monkeypatch):
        def swap(resolved):
            other = resolved.with_name("other.txt")
            other.write_bytes(b"replacement")
            other.replace(resolved)  # same name, new inode

        self.swap_after_check(monkeypatch, swap)

        with pytest.raises(PathRefused, match="changed while it was being read"):
            read_confined_file(root, "ok.txt", LIMIT)

    def test_a_file_that_grew_past_the_limit_after_the_size_check(self, root, monkeypatch):
        (root / "grow").write_bytes(b"x" * 10)
        self.swap_after_check(monkeypatch, lambda resolved: resolved.write_bytes(b"x" * (LIMIT + 50)))

        with pytest.raises(PathRefused, match="limit"):
            read_confined_file(root, "grow", LIMIT)

    @POSIX_ONLY
    def test_the_open_file_must_still_be_inside_the_root_where_the_platform_can_say(self, root, monkeypatch):
        if not os.path.exists("/proc/self/fd"):
            pytest.skip("needs /proc to name an open file")
        monkeypatch.setattr(paths.os, "readlink", lambda path: "/somewhere/else/file")

        with pytest.raises(PathRefused, match="inside the files directory"):
            read_confined_file(root, "ok.txt", LIMIT)

    def test_a_platform_that_cannot_name_open_files_is_accepted(self, root, monkeypatch):
        def no_proc(path):
            raise OSError("no /proc")

        monkeypatch.setattr(paths.os, "readlink", no_proc)

        assert read_confined_file(root, "ok.txt", LIMIT)[1] == b"hello\n"


class TestImports:
    def test_module_imports_no_mcp(self):
        import subprocess

        code = "import sys, serial_mcp_server.paths; sys.exit(any(m == 'mcp' or m.startswith('mcp.') for m in sys.modules))"
        result = subprocess.run([sys.executable, "-I", "-c", code], check=False)  # noqa: S603 - fixed arguments
        assert result.returncode == 0
