import subprocess

import pytest

from tests.support import shell


@pytest.mark.parametrize(
    "platform,installation,relative",
    [
        ("darwin", "Git with spaces", "bin/bash.exe"),
        ("win32", "Git with spaces", "bin/bash.exe"),
        ("win32", "Git with spaces", "usr/bin/bash.exe"),
        ("win32", "Windows/System32", "bin/bash.exe"),
        ("win32", "Git with spaces", None),
    ],
)
def test_bash_executable_uses_git_installation_on_windows(
    platform, installation, relative, tmp_path, monkeypatch
):
    monkeypatch.setattr(shell.sys, "platform", platform)
    install = tmp_path / installation
    if relative:
        bash = install / relative
        bash.parent.mkdir(parents=True)
        bash.touch()
    monkeypatch.setattr(
        shell.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, str(install / "mingw64/libexec/git-core") + "\n"
        ),
    )
    if platform == "win32" and ("System32" in installation or relative is None):
        with pytest.raises(FileNotFoundError, match="Git Bash"):
            shell.bash_executable()
    else:
        assert shell.bash_executable() == (str(bash) if platform == "win32" else "bash")
