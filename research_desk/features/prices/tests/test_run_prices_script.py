"""scripts/run-prices.ps1: the daily schedule's entry for ``prices update`` and then ``flows update``.

Windows PowerShell 5.1, saved as UTF-8 with a BOM (5.1 reads a BOM-less script in the system
code page and breaks the Korean text). It moves to the repository folder and runs
``<python> -u -m research_desk prices update``. Only when that ends with 0 does it go on to
``<python> -u -m research_desk flows update`` and give back that exit code; otherwise it prints a
Korean line saying the flows update is skipped and gives back the price update's code (one KIS
window a day, and no new token request on a day the price update failed). Without a python it runs
nothing and ends with 4.

The python it runs here is a stand-in batch file that records its arguments and folder on every
call and exits with the code the test asks for, one code per command: the real commands would read
the real .env and reach the real DB and KIS.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import research_desk

REPOSITORY = Path(research_desk.__file__).resolve().parent.parent
SCRIPT = REPOSITORY / "scripts" / "run-prices.ps1"
POWERSHELL = shutil.which("powershell")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell is not on this PC")

PRICES_UPDATE = "-u -m research_desk prices update"
FLOWS_UPDATE = "-u -m research_desk flows update"
FLOWS_SKIPPED = "수급 갱신을 건너뜁니다"

# One line per call in args.txt and cwd.txt. %4 is the command after "-u -m research_desk":
# prices and flows end with the code the test set for each, anything else with 9.
FAKE_PYTHON = ('@echo off\r\n'
               '>>"%~dp0args.txt" echo %*\r\n'
               '>>"%~dp0cwd.txt" cd\r\n'
               'if "%4"=="prices" exit /b %FAKE_PRICES_EXIT%\r\n'
               'if "%4"=="flows" exit /b %FAKE_FLOWS_EXIT%\r\n'
               'exit /b 9\r\n')


def run_script(tmp_path: Path, python: Path, prices_exit: int = 0,
               flows_exit: int = 0) -> subprocess.CompletedProcess:
    environment = dict(os.environ, FAKE_PRICES_EXIT=str(prices_exit), FAKE_FLOWS_EXIT=str(flows_exit))
    environment.pop("RESEARCH_DESK_PY", None)
    return subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
                           "-Python", str(python)],
                          cwd=tmp_path, env=environment, capture_output=True, timeout=120)


def fake_python(tmp_path: Path) -> Path:
    fake = tmp_path / "python.cmd"
    fake.write_text(FAKE_PYTHON, encoding="ascii")
    return fake


def calls(tmp_path: Path) -> list[str]:
    """The arguments of each call of the stand-in python, in call order."""
    return (tmp_path / "args.txt").read_text(encoding="ascii").splitlines()


def call_folders(tmp_path: Path) -> list[Path]:
    """The folder each call of the stand-in python ran in, in call order."""
    return [Path(line.strip()) for line in (tmp_path / "cwd.txt").read_text(encoding="mbcs").splitlines()]


def shown(output: bytes) -> str:
    """What the script printed. Windows PowerShell 5.1 writes in the console's code page: the OEM
    code page (949 on this PC), or UTF-8 on a console switched to 65001."""
    try:
        return output.decode("utf-8")
    except UnicodeDecodeError:
        return output.decode("oem", errors="replace")


def same_folder(a: Path, b: Path) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def test_the_script_is_utf8_with_a_bom_and_runs_prices_update_then_flows_update():
    data = SCRIPT.read_bytes()
    assert data[:3] == b"\xef\xbb\xbf"
    text = data[3:].decode("utf-8")
    assert "-m research_desk prices update" in text
    assert "-m research_desk flows update" in text
    assert text.index("-m research_desk prices update") < text.index("-m research_desk flows update")
    assert "주가" in text and "수급" in text   # Korean guidance


@needs_powershell
@pytest.mark.parametrize("flows_exit", [0, 1, 4])
def test_after_the_price_update_ends_with_0_the_script_runs_the_flows_update_and_gives_back_its_exit_code(
        tmp_path, flows_exit):
    result = run_script(tmp_path, fake_python(tmp_path), prices_exit=0, flows_exit=flows_exit)
    printed = shown(result.stdout + result.stderr)
    assert result.returncode == flows_exit, printed
    assert calls(tmp_path) == [PRICES_UPDATE, FLOWS_UPDATE]
    assert [same_folder(folder, REPOSITORY) for folder in call_folders(tmp_path)] == [True, True]
    assert FLOWS_SKIPPED not in printed


@needs_powershell
@pytest.mark.parametrize("prices_exit", [1, 4])
def test_when_the_price_update_does_not_end_with_0_the_script_skips_the_flows_update_and_gives_back_that_code(
        tmp_path, prices_exit):
    result = run_script(tmp_path, fake_python(tmp_path), prices_exit=prices_exit, flows_exit=0)
    printed = shown(result.stdout + result.stderr)
    assert result.returncode == prices_exit, printed
    assert calls(tmp_path) == [PRICES_UPDATE]
    assert [same_folder(folder, REPOSITORY) for folder in call_folders(tmp_path)] == [True]
    assert FLOWS_SKIPPED in printed   # the Korean line that says the flows update is skipped


@needs_powershell
def test_without_a_python_the_script_runs_nothing_and_ends_with_4(tmp_path):
    result = run_script(tmp_path, tmp_path / "missing" / "python.exe")
    assert result.returncode == 4
    assert not (tmp_path / "args.txt").exists()
