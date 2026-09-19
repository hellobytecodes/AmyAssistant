"""
system_control.py
------------------
Everything that lets Amy actually *do* things on the machine:
run shell commands and Python, manage files, launch apps, look at the screen,
and (with permission) drive the keyboard and mouse.

Every function here is registered as a Gemini "tool" (the function-calling
declarations live in core/gemini_live_client.py) so the model can call them by
name during a live conversation.

Safety notes:
- Shell commands are checked against config.DANGEROUS_COMMAND_PATTERNS (always blocked).
- Ordinary actions just run. DANGEROUS ones (deleting, force-killing, power actions,
  overwriting files, risky commands/code) go through `confirmation_hook`: the live
  session installs a hook that makes Amy ask OUT LOUD and only runs the action after
  the user's own transcribed "yes" (config.CONFIRM_METHOD = "voice"), or the GUI
  installs a Yes/No popup ("dialog"). The default hook DENIES, so nothing risky runs
  when no UI is attached.
- "Close this" means closing a WINDOW gracefully (close_window), never killing a
  process. kill_process is a last resort for frozen programs and can't touch
  explorer.exe, system processes or Amy herself.
"""

from __future__ import annotations

import fnmatch
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import config

OS_NAME = platform.system()  # "Windows", "Darwin", "Linux"
_PROJECT_DIR = Path(__file__).resolve().parents[1]


@dataclass
class ActionResult:
    ok: bool
    message: str
    data: Optional[dict] = None

    def to_dict(self) -> dict:
        return {"ok": self.ok, "message": self.message, "data": self.data or {}}


# ---------------------------------------------------------------------------
# Confirmation plumbing
# ---------------------------------------------------------------------------
class NeedsConfirmation(Exception):
    """Raised by the voice-confirmation hook: the action was NOT executed yet.
    The message tells the model to ask the user aloud and to retry after a spoken yes."""


# A hook that decides whether a dangerous action may run. It is called from a worker
# thread and either returns True/False or raises NeedsConfirmation. Default = deny
# (safe when running headless).
confirmation_hook: Callable[[str], bool] = lambda description: False


def set_confirmation_hook(fn: Callable[[str], bool]) -> None:
    global confirmation_hook
    confirmation_hook = fn


def _confirm(description: str) -> bool:
    text = description if len(description) <= 900 else description[:900] + "\n..."
    try:
        return bool(confirmation_hook(text))
    except NeedsConfirmation:
        raise
    except Exception:  # noqa: BLE001 - a broken hook must never mean "yes"
        return False


def reset_session_approvals() -> None:
    """Kept for compatibility: nothing is remembered between conversations any more."""


# ---------------------------------------------------------------------------
# Environment description (given to the model so it uses the right syntax/paths)
# ---------------------------------------------------------------------------
def _special_folder(key: str) -> Path:
    home = Path.home()
    if key == "home":
        return home
    names = {"desktop": "Desktop", "downloads": "Downloads", "documents": "Documents",
             "pictures": "Pictures", "music": "Music", "videos": "Videos"}
    name = names[key]

    if OS_NAME == "Linux" and shutil.which("xdg-user-dir"):
        xdg_key = {"desktop": "DESKTOP", "downloads": "DOWNLOAD", "documents": "DOCUMENTS",
                   "pictures": "PICTURES", "music": "MUSIC", "videos": "VIDEOS"}[key]
        try:
            out = subprocess.run(["xdg-user-dir", xdg_key], capture_output=True, text=True, timeout=2).stdout.strip()
            if out and Path(out).is_dir():
                return Path(out)
        except Exception:  # noqa: BLE001
            pass

    candidates = [home / name]
    for env_name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if os.environ.get(env_name):
            candidates.append(Path(os.environ[env_name]) / name)  # Windows often redirects Desktop/Documents here
    candidates.append(home / "OneDrive" / name)
    for c in candidates:
        if c.is_dir():
            return c
    return home / name


def describe_environment() -> str:
    """A short plain-text description of this computer for the model's system prompt."""
    if OS_NAME == "Windows":
        shell = ("Windows Command Prompt (cmd.exe). For PowerShell use: powershell -NoProfile -Command \"...\". "
                 "Use backslash paths.")
    elif OS_NAME == "Darwin":
        shell = "macOS zsh/bash. AppleScript is available through `osascript -e`."
    else:
        shell = "Linux sh/bash."
    now = datetime.now().astimezone()
    try:
        user = os.getlogin()
    except OSError:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    return (
        f"- Operating system: {OS_NAME} ({platform.release()}), machine {platform.machine()}\n"
        f"- Shell for run_shell_command: {shell}\n"
        f"- Python for run_python_code: {platform.python_version()}\n"
        f"- User: {user}\n"
        f"- Home: {Path.home()}\n"
        f"- Desktop: {_special_folder('desktop')}\n"
        f"- Downloads: {_special_folder('downloads')}\n"
        f"- Documents: {_special_folder('documents')}\n"
        f"- Current date/time when this conversation started: {now.strftime('%A %Y-%m-%d %H:%M %Z (UTC%z)')}"
    )


def get_datetime() -> ActionResult:
    now = datetime.now().astimezone()
    return ActionResult(True, now.strftime("%A %Y-%m-%d %H:%M:%S %Z (UTC%z)"), {"iso": now.isoformat()})


# ---------------------------------------------------------------------------
# Process running helpers
# ---------------------------------------------------------------------------
def _decode(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if OS_NAME == "Windows":
        try:  # cmd.exe writes in the console's OEM code page
            import ctypes

            return data.decode(f"cp{ctypes.windll.kernel32.GetOEMCP()}", errors="replace")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
    return data.decode("utf-8", errors="replace")


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        import psutil

        parent = psutil.Process(proc.pid)
        for child in parent.children(recursive=True):
            try:
                child.kill()
            except psutil.Error:
                pass
        parent.kill()
        return
    except Exception:  # noqa: BLE001
        pass
    try:
        if OS_NAME == "Windows":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=5)
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_process(cmd, *, shell: bool, timeout: int, cwd: Optional[str] = None, env: Optional[dict] = None):
    """Run a process with no stdin, merged output, bounded time. -> (returncode|None, text, timed_out)"""
    kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=env)
    if OS_NAME == "Windows":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True  # own process group so a timeout can kill the whole tree
    proc = subprocess.Popen(cmd, shell=shell, **kwargs)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, _decode(out), False
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=3)
        except Exception:  # noqa: BLE001
            out = b""
        return None, _decode(out), True


def _spawn_detached(args: list[str], label: str) -> ActionResult:
    """Start a GUI program and return immediately (never block until it exits)."""
    try:
        kwargs: dict = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if OS_NAME == "Windows":
            kwargs["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(args, **kwargs)
        time.sleep(0.6)
        code = proc.poll()
        if code not in (None, 0):
            return ActionResult(False, f"{label} exited immediately with code {code}.")
        return ActionResult(True, f"Opened {label}.")
    except FileNotFoundError:
        return ActionResult(False, f"'{args[0]}' was not found on this computer.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not open {label}: {exc}")


# ---------------------------------------------------------------------------
# Shell commands + Python
# ---------------------------------------------------------------------------
def _normalize_command(command: str) -> str:
    return " ".join(command.lower().split())


def _is_dangerous(command: str) -> bool:
    norm = _normalize_command(command)
    return any(re.search(pattern, norm, re.IGNORECASE) for pattern in config.DANGEROUS_COMMAND_PATTERNS)


_SEGMENT_SPLIT = re.compile(r"&&|\|\||[;&|\n\r]")
_WRAPPER_TOKENS = {"sudo", "call", "start", "cmd", "cmd.exe", "/c", "/k", "powershell", "powershell.exe", "pwsh",
                   "-command", "-c", "-noprofile", "-nologo", "-executionpolicy", "bypass", "env", "nohup",
                   "time", "command", "exec", "xargs"}


def _is_risky(command: str) -> bool:
    """Would this terminal command delete / kill / reconfigure something? (Needs a spoken yes.)"""
    if not config.CONFIRM_RISKY_ACTIONS:
        return False
    norm = _normalize_command(command)
    if any(re.search(pattern, norm) for pattern in config.RISKY_COMMAND_PATTERNS):
        return True
    for segment in _SEGMENT_SPLIT.split(norm):
        tokens = [t.strip("\"'`()") for t in segment.split()]
        while tokens and (not tokens[0] or tokens[0] in _WRAPPER_TOKENS):
            tokens.pop(0)
        if tokens and tokens[0] in config.RISKY_COMMAND_WORDS:
            return True
    return False


def _is_risky_python(code: str) -> bool:
    if not config.CONFIRM_RISKY_ACTIONS:
        return False
    return any(re.search(pattern, code) for pattern in config.RISKY_PYTHON_PATTERNS)


def _format_run(returncode, output: str, timed_out: bool, timeout: int) -> ActionResult:
    output = output.strip()
    if len(output) > 4000:
        output = "...(earlier output cut)...\n" + output[-4000:]  # keep the tail, it's usually the useful part
    if timed_out:
        return ActionResult(False, f"Timed out after {timeout}s and was stopped.\n{output}".strip())
    return ActionResult(returncode == 0, output or f"(exit code {returncode}, no output)")


def run_shell_command(command: str, timeout_seconds: int = 30) -> ActionResult:
    """Execute a shell/terminal command and return its output.

    Args:
        command: the full command line to execute.
        timeout_seconds: max seconds to wait before killing it (1-120).
    """
    if not command or not command.strip():
        return ActionResult(False, "Empty command.")

    if _is_dangerous(command):
        return ActionResult(False, f"Refused: '{command}' matches a blocked destructive pattern.")

    if _is_risky(command):
        if not _confirm(f"Run this risky terminal command:\n{command}"):
            return ActionResult(False, "User declined to run this command.")

    timeout = max(1, min(int(timeout_seconds or 30), 120))
    try:
        code, out, timed_out = _run_process(command, shell=True, timeout=timeout, cwd=str(Path.home()))
        return _format_run(code, out, timed_out, timeout)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Failed to run command: {exc}")


def run_python_code(code: str, timeout_seconds: int = 60) -> ActionResult:
    """Run a Python script (using the same interpreter/venv as Amy) and return its output.
    Lets Amy do things no dedicated tool covers: parse files, call APIs, automate apps..."""
    if not code or not code.strip():
        return ActionResult(False, "Empty code.")

    if _is_risky_python(code):
        preview = code.strip()
        if len(preview) > 700:
            preview = preview[:700] + "\n..."
        if not _confirm(f"Run this Python code (it can delete files or run programs):\n\n{preview}"):
            return ActionResult(False, "User declined to run this code.")

    timeout = max(1, min(int(timeout_seconds or 60), 300))
    tmp_dir = tempfile.mkdtemp(prefix="amy_py_")
    script = Path(tmp_dir) / "script.py"
    try:
        script.write_text(code, encoding="utf-8")
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        rc, out, timed_out = _run_process([sys.executable, str(script)], shell=False, timeout=timeout,
                                          cwd=str(Path.home()), env=env)
        return _format_run(rc, out, timed_out, timeout)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Failed to run Python code: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Application launching
# ---------------------------------------------------------------------------
_WIN_ALIASES = {
    "calculator": "calc", "calc": "calc", "ماشین حساب": "calc",
    "notepad": "notepad", "نوت پد": "notepad", "paint": "mspaint",
    "task manager": "taskmgr", "explorer": "explorer", "file explorer": "explorer",
    "cmd": "cmd", "command prompt": "cmd", "terminal": "wt", "powershell": "powershell",
    "settings": "ms-settings:", "control panel": "control",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt",
    "vscode": "code", "vs code": "code", "visual studio code": "code",
    "edge": "msedge", "chrome": "chrome", "google chrome": "chrome", "firefox": "firefox",
    "snipping tool": "snippingtool",
}
_MAC_ALIASES = {
    "chrome": "Google Chrome", "google chrome": "Google Chrome", "vscode": "Visual Studio Code",
    "vs code": "Visual Studio Code", "word": "Microsoft Word", "excel": "Microsoft Excel",
    "powerpoint": "Microsoft PowerPoint", "terminal": "Terminal", "calculator": "Calculator",
    "settings": "System Settings", "files": "Finder",
}
_LINUX_ALIASES = {
    "chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "google chrome": ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"],
    "calculator": ["gnome-calculator", "kcalc", "galculator", "mate-calc"],
    "terminal": ["gnome-terminal", "konsole", "xfce4-terminal", "x-terminal-emulator"],
    "files": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm"],
    "file manager": ["nautilus", "dolphin", "thunar", "nemo", "pcmanfm"],
    "text editor": ["gedit", "kate", "mousepad", "gnome-text-editor"],
    "vscode": ["code"], "vs code": ["code"], "visual studio code": ["code"],
    "settings": ["gnome-control-center", "systemsettings", "xfce4-settings-manager"],
}


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[\s_\-.]+", text.lower()) if t]


def _find_start_menu_shortcut(name: str) -> Optional[Path]:
    roots = []
    for env_name in ("ProgramData", "APPDATA"):
        base = os.environ.get(env_name)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    want = _tokens(name)
    best: Optional[tuple[int, Path]] = None
    for root in roots:
        if not root.is_dir():
            continue
        for lnk in root.rglob("*.lnk"):
            stem_tokens = _tokens(lnk.stem)
            if want and all(any(w in s for s in stem_tokens) for w in want):
                score = abs(len(lnk.stem) - len(name))  # closest name wins
                if best is None or score < best[0]:
                    best = (score, lnk)
    return best[1] if best else None


def _find_desktop_file(name: str) -> Optional[Path]:
    dirs = [Path("/usr/share/applications"), Path("/usr/local/share/applications"),
            Path.home() / ".local/share/applications", Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications",
            Path("/var/lib/snapd/desktop/applications")]
    want = _tokens(name)
    best: Optional[tuple[int, Path]] = None
    for d in dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.desktop"):
            try:
                display = ""
                for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if line.startswith("Name="):
                        display = line[5:].strip()
                        break
            except OSError:
                continue
            hay = _tokens(display + " " + f.stem)
            if want and all(any(w in h for h in hay) for w in want):
                score = len(display or f.stem)
                if best is None or score < best[0]:
                    best = (score, f)
    return best[1] if best else None


def open_application(name: str) -> ActionResult:
    """Launch a desktop application by name (cross-platform best-effort)."""
    name_clean = (name or "").strip()
    if not name_clean:
        return ActionResult(False, "No application name given.")
    key = name_clean.lower()

    if key in ("browser", "web browser", "default browser", "internet"):
        webbrowser.open("about:blank")
        return ActionResult(True, "Opened the default web browser.")

    try:
        if OS_NAME == "Windows":
            target = _WIN_ALIASES.get(key, name_clean)
            try:
                os.startfile(target)  # type: ignore[attr-defined]
                return ActionResult(True, f"Opened {name_clean}.")
            except OSError:
                pass
            lnk = _find_start_menu_shortcut(name_clean)
            if lnk is not None:
                os.startfile(str(lnk))  # type: ignore[attr-defined]
                return ActionResult(True, f"Opened {lnk.stem}.")
            rc, out, _ = _run_process(f'start "" "{target}"', shell=True, timeout=8)
            if rc == 0:
                return ActionResult(True, f"Opened {name_clean}.")
            return ActionResult(False, f"Could not find an application called '{name_clean}'. {out.strip()}")

        if OS_NAME == "Darwin":
            target = _MAC_ALIASES.get(key, name_clean)
            rc, out, _ = _run_process(["open", "-a", target], shell=False, timeout=10)
            if rc == 0:
                return ActionResult(True, f"Opened {target}.")
            return ActionResult(False, f"Could not open '{target}': {out.strip()}")

        # Linux
        for candidate in _LINUX_ALIASES.get(key, []) + [key, key.replace(" ", "-")]:
            exe = shutil.which(candidate)
            if exe:
                return _spawn_detached([exe], name_clean)
        desktop = _find_desktop_file(name_clean)
        if desktop is not None:
            for launcher in (["gtk-launch", desktop.stem], ["gio", "launch", str(desktop)]):
                if shutil.which(launcher[0]):
                    return _spawn_detached(launcher, desktop.stem)
        return ActionResult(False, f"Could not find an application called '{name_clean}'.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not open '{name_clean}': {exc}")


def _resolve(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path or "~"))).resolve()


def open_path(path: str) -> ActionResult:
    """Open a file or folder with the OS default handler."""
    p = _resolve(path)
    if not p.exists():
        return ActionResult(False, f"Path does not exist: {p}")
    try:
        if OS_NAME == "Windows":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif OS_NAME == "Darwin":
            subprocess.Popen(["open", str(p)])
        else:
            return _spawn_detached(["xdg-open", str(p)], str(p))
        return ActionResult(True, f"Opened {p}.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not open path: {exc}")


def open_url(url: str) -> ActionResult:
    """Open a web address in the default browser (http/https only)."""
    url = (url or "").strip()
    if not url:
        return ActionResult(False, "No URL given.")
    if not re.match(r"^[a-z][a-z0-9+.\-]*:", url, re.IGNORECASE):
        url = "https://" + url
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return ActionResult(False, "Only http/https links can be opened.")
    try:
        webbrowser.open(url)
        return ActionResult(True, f"Opened {url} in the browser.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not open the browser: {exc}")


def web_search(query: str) -> ActionResult:
    """Open a Google search for `query` in the default browser."""
    from urllib.parse import quote_plus

    if not (query or "").strip():
        return ActionResult(False, "Empty search query.")
    return open_url("https://www.google.com/search?q=" + quote_plus(query.strip()))


# ---------------------------------------------------------------------------
# File management
# ---------------------------------------------------------------------------
def list_directory(path: str = ".") -> ActionResult:
    p = _resolve(path)
    if not p.is_dir():
        return ActionResult(False, f"Not a directory: {p}")
    entries = []
    try:
        items = sorted(p.iterdir(), key=lambda i: (not i.is_dir(), i.name.lower()))
    except OSError as exc:
        return ActionResult(False, f"Could not list {p}: {exc}")
    for item in items[:200]:
        try:
            is_dir = item.is_dir()
            size = None if is_dir else item.stat().st_size
        except OSError:
            is_dir, size = False, None  # broken link / no permission -- still list it
        entries.append({"name": item.name, "type": "dir" if is_dir else "file", "size": size})
    more = f" (showing first 200 of {len(items)})" if len(items) > 200 else ""
    return ActionResult(True, f"{len(items)} items in {p}{more}", {"entries": entries})


def read_text_file(path: str, max_chars: int = 8000) -> ActionResult:
    p = _resolve(path)
    if not p.is_file():
        return ActionResult(False, f"Not a file: {p}")
    try:
        max_chars = max(1, int(max_chars))
        with open(p, "rb") as fb:
            head = fb.read(2048)
        if b"\x00" in head:
            return ActionResult(False, f"{p.name} looks like a binary file, not text.")
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:  # explicit UTF-8 so Persian text survives on Windows
            text = f.read(max_chars + 1)
        note = ""
        if len(text) > max_chars:
            text = text[:max_chars]
            note = f"\n...(truncated to the first {max_chars} characters)"
        return ActionResult(True, text + note)
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not read file: {exc}")


def write_text_file(path: str, content: str, overwrite: bool = False) -> ActionResult:
    p = _resolve(path)
    if p.exists():
        if not overwrite:
            return ActionResult(False, f"File already exists (set overwrite=true to replace): {p}")
        if not p.is_file():
            return ActionResult(False, f"Not a file: {p}")
        if not _confirm(f"Overwrite existing file:\n{p}"):
            return ActionResult(False, "User declined overwriting the file.")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")  # explicit UTF-8: the Windows default codec can't store Persian
        return ActionResult(True, f"Wrote {len(content)} chars to {p}.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not write file: {exc}")


def move_or_rename(source: str, destination: str) -> ActionResult:
    src = _resolve(source)
    dst = _resolve(destination)
    if not src.exists():
        return ActionResult(False, f"Source does not exist: {src}")
    final = dst / src.name if dst.is_dir() and dst != src else dst  # "move X into folder Y"
    if final.exists():
        return ActionResult(False, f"Refusing to overwrite existing item: {final}")
    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(final))
        return ActionResult(True, f"Moved {src} -> {final}")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Move failed: {exc}")


def _is_protected_path(p: Path) -> bool:
    """Folders that must never be deleted wholesale, however politely asked."""
    home = Path.home().resolve()
    protected = {home, _PROJECT_DIR}
    for key in ("desktop", "downloads", "documents", "pictures", "music", "videos"):
        try:
            protected.add(_special_folder(key).resolve())
        except Exception:  # noqa: BLE001
            pass
    if p in protected or p.parent == p:  # also any drive root / "/"
        return True
    if OS_NAME == "Windows":
        drive = Path(p.anchor)
        top_level = {drive / "Users", drive / "ProgramData", drive / "Program Files",
                     drive / "Program Files (x86)", drive / "Windows"}
        return p in top_level or (drive / "Windows") in p.parents  # nothing inside C:\Windows either
    system_dirs = [Path(x) for x in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run",
                                     "/sbin", "/sys", "/usr", "/var", "/opt", "/home", "/Users", "/Applications",
                                     "/System", "/Library", "/Volumes", "/mnt", "/media", "/snap")]
    return p in system_dirs


def delete_path(path: str, confirm: bool = True) -> ActionResult:
    """Delete a file or folder (always needs the user's spoken yes; `confirm` is ignored and only
    kept so older calls don't break). Goes to the Recycle Bin/Trash when possible."""
    p = _resolve(path)
    if not p.exists() and not p.is_symlink():
        return ActionResult(False, f"Path does not exist: {p}")
    if _is_protected_path(p):
        return ActionResult(False, f"Refused: {p} is a protected folder (home, a system folder, a drive root or Amy's own folder).")

    recycle = bool(config.USE_RECYCLE_BIN)
    trash = None
    if recycle:
        try:
            from send2trash import send2trash as trash  # type: ignore
        except ImportError:
            recycle = False
    where = "to the Recycle Bin" if recycle else "PERMANENTLY (cannot be undone)"
    if not _confirm(f"Delete {where}:\n{p}"):
        return ActionResult(False, "User declined the deletion.")
    try:
        if recycle and trash is not None:
            trash(str(p))
        elif p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        return ActionResult(True, f"Deleted {p} ({'moved to Recycle Bin' if recycle else 'permanently'}).")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Delete failed: {exc}")


def create_folder(path: str) -> ActionResult:
    p = _resolve(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return ActionResult(True, f"Created folder {p}.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not create folder: {exc}")


def open_special_folder(name: str) -> ActionResult:
    """Open a well-known folder by friendly name: home, desktop, downloads,
    documents, pictures, music, or videos."""
    key = (name or "").strip().lower()
    valid = ("home", "desktop", "downloads", "documents", "pictures", "music", "videos")
    if key not in valid:
        return ActionResult(False, f"Unknown special folder '{name}'. Try: {', '.join(valid)}")
    return open_path(str(_special_folder(key)))


def search_files(query: str, root: Optional[str] = None) -> ActionResult:
    """Search for files/folders whose name matches `query` (case-insensitive; `*` wildcards
    supported; otherwise every word of the query must appear in the name), starting from
    `root` (defaults to the user's home directory). Stops early once
    FILE_SEARCH_MAX_RESULTS matches are found or FILE_SEARCH_TIMEOUT seconds have passed."""
    if not query or not query.strip():
        return ActionResult(False, "Empty search query.")

    start_dir = _resolve(root) if root else Path.home()
    if not start_dir.exists():
        return ActionResult(False, f"Search root does not exist: {start_dir}")

    wildcard = "*" in query or "?" in query
    pattern = query.lower()
    words = _tokens(query)
    matches: list[str] = []
    deadline = time.time() + config.FILE_SEARCH_TIMEOUT
    timed_out = False

    def name_matches(name: str) -> bool:
        lowered = name.lower()
        if wildcard:
            return fnmatch.fnmatch(lowered, pattern)
        normalized = " ".join(_tokens(lowered))  # "my_resume.pdf" -> "my resume pdf"
        return all(w in normalized for w in words)

    # Skip noisy / huge system-ish directories so search stays fast and relevant.
    skip_dirs = {".git", "node_modules", "__pycache__", "$RECYCLE.BIN", ".Trash", "AppData", "Library",
                 "site-packages", ".venv", "venv"}

    for dirpath, dirnames, filenames in os.walk(start_dir, onerror=lambda e: None):
        if time.time() > deadline:
            timed_out = True
            break
        dirnames[:] = [d for d in dirnames if d not in skip_dirs and not d.startswith(".")]
        for name in dirnames + filenames:
            if name_matches(name):
                matches.append(str(Path(dirpath) / name))
                if len(matches) >= config.FILE_SEARCH_MAX_RESULTS:
                    break
        if len(matches) >= config.FILE_SEARCH_MAX_RESULTS:
            break

    suffix = " (search stopped at the time limit; results may be incomplete)" if timed_out else ""
    if not matches:
        return ActionResult(True, f"No files matching '{query}' found under {start_dir}.{suffix}")
    return ActionResult(True, f"Found {len(matches)} match(es).{suffix}", {"matches": matches})


# ---------------------------------------------------------------------------
# Screen awareness
# ---------------------------------------------------------------------------
_last_shot_size: Optional[tuple[int, int]] = None  # size of the image last shown to the model


def screenshot_for_model(max_width: int = 1280) -> tuple[ActionResult, Optional[tuple[bytes, str]]]:
    """Capture the primary screen, downscale it and return (result, (image_bytes, mime)).
    Full-resolution PNGs can be many MB, which is slow to upload and wasteful for the model."""
    global _last_shot_size
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            shot = sct.grab(monitor)
            width, height = shot.size
            rgb = shot.rgb
        try:
            import io

            from PIL import Image

            img = Image.frombytes("RGB", (width, height), rgb)
            if width > max_width:
                img = img.resize((max_width, max(1, round(height * max_width / width))), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=75)
            data, mime, sent = buf.getvalue(), "image/jpeg", img.size
        except ImportError:  # Pillow missing: fall back to full-size PNG
            data, mime, sent = mss.tools.to_png(rgb, (width, height)), "image/png", (width, height)
        _last_shot_size = sent
        msg = (f"Screenshot captured ({sent[0]}x{sent[1]} px) and shown to you. Pixel coordinates for "
               f"mouse_click refer to this image.")
        return ActionResult(True, msg, {"width": sent[0], "height": sent[1]}), (data, mime)
    except ImportError:
        return ActionResult(False, "Install 'mss' for screenshots: pip install mss"), None
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Screenshot failed: {exc}"), None


def take_screenshot(save_path: Optional[str] = None) -> ActionResult:
    """Capture the screen to an image file (default: a temp file) and return its path."""
    result, payload = screenshot_for_model(max_width=10_000)
    if payload is None:
        return result
    data, mime = payload
    suffix = ".jpg" if mime == "image/jpeg" else ".png"
    out = Path(save_path).resolve() if save_path else Path(tempfile.gettempdir()) / f"amy_screen{suffix}"
    try:
        out.write_bytes(data)
    except OSError as exc:
        return ActionResult(False, f"Could not save screenshot: {exc}")
    return ActionResult(True, f"Screenshot saved to {out}", {"path": str(out)})


def get_clipboard() -> ActionResult:
    try:
        import pyperclip  # local import: optional dependency

        text = pyperclip.paste() or ""
        note = "" if len(text) <= 4000 else f"\n...(truncated, {len(text)} chars total)"
        return ActionResult(True, text[:4000] + note)
    except ImportError:
        return ActionResult(False, "Install 'pyperclip' for clipboard access.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Clipboard read failed: {exc}")


def set_clipboard(text: str) -> ActionResult:
    try:
        import pyperclip

        pyperclip.copy(text)
        return ActionResult(True, "Clipboard updated.")
    except ImportError:
        return ActionResult(False, "Install 'pyperclip' for clipboard access.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Clipboard write failed: {exc}")


# ---------------------------------------------------------------------------
# Keyboard & mouse (needs one-time approval per conversation)
# ---------------------------------------------------------------------------
_KEY_ALIASES = {
    "control": "ctrl", "windows": "win", "super": "win", "meta": "win", "cmd": "command",
    "return": "enter", "escape": "esc", "del": "delete", "spacebar": "space",
    "pageup": "pageup", "page up": "pageup", "pagedown": "pagedown", "page down": "pagedown",
}


def _input_gate():
    """-> (pyautogui module, None) if allowed, else (None, ActionResult explaining why)."""
    if not config.ALLOW_INPUT_CONTROL:
        return None, ActionResult(False, "Keyboard/mouse control is disabled in config.py.")
    try:
        import pyautogui
    except ImportError:
        return None, ActionResult(False, "Install 'pyautogui' for keyboard/mouse control: pip install pyautogui")
    pyautogui.FAILSAFE = True   # slam the mouse into a screen corner to abort everything
    pyautogui.PAUSE = 0.05
    return pyautogui, None


def press_keys(keys: str) -> ActionResult:
    """Press a key or a shortcut, e.g. 'enter', 'ctrl+c', 'alt+tab', 'win+d'."""
    gui, err = _input_gate()
    if err:
        return err
    parts = [_KEY_ALIASES.get(k.strip().lower(), k.strip().lower()) for k in (keys or "").split("+") if k.strip()]
    if not parts:
        return ActionResult(False, "No keys given.")
    if "shift" in parts and "delete" in parts and not _confirm("Press Shift+Delete (permanently deletes the selected item)"):
        return ActionResult(False, "User declined.")
    bad = [k for k in parts if k not in gui.KEYBOARD_KEYS]
    if bad:
        return ActionResult(False, f"Unknown key(s): {', '.join(bad)}")
    try:
        if len(parts) == 1:
            gui.press(parts[0])
        else:
            gui.hotkey(*parts)
        return ActionResult(True, f"Pressed {'+'.join(parts)}.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Key press failed: {exc}")


def type_text(text: str, press_enter: bool = False) -> ActionResult:
    """Type text into whatever window has focus (Persian/Unicode works via the clipboard)."""
    gui, err = _input_gate()
    if err:
        return err
    if not text:
        return ActionResult(False, "No text given.")
    # Typing into a terminal must not become a way around the shell safety checks.
    if _is_dangerous(text):
        return ActionResult(False, "Refused: that text looks like a blocked destructive command.")
    if _is_risky(text) and not _confirm(f"Type this (looks like a risky command):\n{text}"):
        return ActionResult(False, "User declined.")
    try:
        if text.isascii():
            gui.write(text, interval=0.01)
        else:  # pyautogui can only type ASCII; paste everything else
            import pyperclip

            old = None
            try:
                old = pyperclip.paste()
            except Exception:  # noqa: BLE001
                pass
            pyperclip.copy(text)
            time.sleep(0.05)
            gui.hotkey("command" if OS_NAME == "Darwin" else "ctrl", "v")
            time.sleep(0.2)
            if old is not None:
                pyperclip.copy(old)
        if press_enter:
            gui.press("enter")
        return ActionResult(True, f"Typed {len(text)} characters{' and pressed Enter' if press_enter else ''}.")
    except ImportError:
        return ActionResult(False, "Install 'pyperclip' to type non-English text.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Typing failed: {exc}")


def mouse_click(x: int, y: int, button: str = "left", double: bool = False) -> ActionResult:
    """Click at pixel (x, y) of the LAST screenshot you were shown (call look_at_screen first)."""
    gui, err = _input_gate()
    if err:
        return err
    try:
        real_w, real_h = gui.size()
        if _last_shot_size:
            x = x * real_w / _last_shot_size[0]
            y = y * real_h / _last_shot_size[1]
        x = int(max(0, min(real_w - 1, x)))
        y = int(max(0, min(real_h - 1, y)))
        button = button if button in ("left", "right", "middle") else "left"
        gui.click(x, y, clicks=2 if double else 1, button=button)
        return ActionResult(True, f"Clicked ({x}, {y}) with the {button} button.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Click failed: {exc}")


def scroll(amount: int) -> ActionResult:
    """Scroll the window under the mouse: positive = up, negative = down."""
    gui, err = _input_gate()
    if err:
        return err
    try:
        gui.scroll(int(amount) * 100)
        return ActionResult(True, f"Scrolled {'up' if amount > 0 else 'down'}.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Scroll failed: {exc}")


# ---------------------------------------------------------------------------
# Windows (the visible application/folder windows) -- this is how Amy CLOSES things.
#
# Why this exists: a folder such as "C:\" is just a window of explorer.exe, and explorer.exe
# also *is* the taskbar and the desktop. Closing something by killing a process therefore kills
# the wrong thing (or Amy herself). Closing a WINDOW asks only that window to close, exactly like
# clicking its X button, so programs can still ask "save changes?".
# ---------------------------------------------------------------------------
_SHELL_WINDOW_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow"}


def _windows_list_windows() -> list[dict]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    dwmapi = ctypes.windll.dwmapi  # type: ignore[attr-defined]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_APPWINDOW, DWMWA_CLOAKED = -20, 0x80, 0x40000, 14

    found: list[dict] = []

    def callback(hwnd, _lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buf, length + 1)
            title = title_buf.value.strip()
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            if not title or class_buf.value in _SHELL_WINDOW_CLASSES:
                return True
            ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            if ex_style & WS_EX_TOOLWINDOW and not ex_style & WS_EX_APPWINDOW:
                return True
            cloaked = wintypes.DWORD(0)  # UWP apps keep invisible "cloaked" windows around
            dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
            if cloaked.value:
                return True
            pid = wintypes.DWORD(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            app = ""
            try:
                import psutil

                app = psutil.Process(pid.value).name()
            except Exception:  # noqa: BLE001
                pass
            found.append({"id": int(hwnd), "title": title, "app": app, "pid": int(pid.value)})
        except Exception:  # noqa: BLE001 - one odd window must not stop the enumeration
            pass
        return True

    user32.EnumWindows(enum_proc(callback), 0)
    return found


def _windows_close_window(win: dict) -> bool:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    WM_CLOSE = 0x0010
    return bool(user32.PostMessageW(win["id"], WM_CLOSE, 0, 0))  # polite request, same as clicking the X


def _linux_list_windows() -> list[dict]:
    if not shutil.which("wmctrl"):
        raise RuntimeError("Closing windows on Linux needs wmctrl: sudo apt install wmctrl")
    out = subprocess.run(["wmctrl", "-l", "-p"], capture_output=True, text=True, timeout=5).stdout
    found = []
    for line in out.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[2].isdigit():
            continue
        win_id, _desk, pid, _host, title = parts
        if int(pid) == os.getpid() or not title.strip():
            continue
        app = ""
        try:
            import psutil

            app = psutil.Process(int(pid)).name()
        except Exception:  # noqa: BLE001
            pass
        found.append({"id": win_id, "title": title.strip(), "app": app, "pid": int(pid)})
    return found


def _linux_close_window(win: dict) -> bool:
    return subprocess.run(["wmctrl", "-i", "-c", str(win["id"])], capture_output=True, timeout=5).returncode == 0


def _mac_list_windows() -> list[dict]:
    script = ('tell application "System Events"' + chr(10) + ' set out to ""' + chr(10) +
              ' repeat with p in (every process whose background only is false)' + chr(10) +
              '  try' + chr(10) + '   repeat with w in windows of p' + chr(10) +
              '    set out to out & (name of p) & "||" & (name of w) & linefeed' + chr(10) +
              '   end repeat' + chr(10) + '  end try' + chr(10) + ' end repeat' + chr(10) + ' return out' + chr(10) + 'end tell')
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10).stdout
    found = []
    for line in out.splitlines():
        if "||" in line:
            app, title = line.split("||", 1)
            if title.strip():
                found.append({"id": f"{app}||{title}", "title": title.strip(), "app": app.strip(), "pid": 0})
    return found


def _mac_close_window(win: dict) -> bool:
    app, title = win["app"], win["title"].replace('"', chr(92) + '"')
    script = (f'tell application "System Events" to tell process "{app}" to '
              f'click (first button of window "{title}" whose subrole is "AXCloseButton")')
    return subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10).returncode == 0


def _list_windows_raw() -> list[dict]:
    if OS_NAME == "Windows":
        return _windows_list_windows()
    if OS_NAME == "Darwin":
        return _mac_list_windows()
    return _linux_list_windows()


def _close_window_raw(win: dict) -> bool:
    if OS_NAME == "Windows":
        return _windows_close_window(win)
    if OS_NAME == "Darwin":
        return _mac_close_window(win)
    return _linux_close_window(win)


def _match_windows(query: str, windows: list[dict]) -> list[dict]:
    """Windows whose title or program name fits `query`. 'c', 'c:' or 'c:\\\\' means the C: drive's folder window."""
    q = (query or "").strip().lower()
    if not q:
        return []
    drive = re.fullmatch(r"(?:drive\s+)?([a-z]):?[\\/]?", q)
    words = _tokens(q)
    matches = []
    for w in windows:
        title, app = w["title"].lower(), w["app"].lower()
        app_stem = app[:-4] if app.endswith(".exe") else app
        if drive:
            letter = drive.group(1)
            hit = f"({letter}:)" in title or f"{letter}:{chr(92)}" in title or title.startswith(f"{letter}:")
        else:
            hit = q in title or q == app or q == app_stem or bool(words and all(x in title or x in app for x in words))
        if hit:
            matches.append(w)
    return matches


def list_windows() -> ActionResult:
    """List the visible application/folder windows (title + program) so the right one can be closed."""
    try:
        wins = _list_windows_raw()
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not list windows: {exc}")
    rows = [{"title": w["title"][:120], "app": w["app"]} for w in wins[:60]]
    return ActionResult(True, f"{len(wins)} open window(s).", {"windows": rows})


def close_window(title_or_app: str, close_all: bool = False) -> ActionResult:
    """Gracefully close window(s) matching a title fragment or program name (e.g. 'C:', 'Downloads',
    'chrome'). This is the normal way to close anything. Never kills processes and never touches Amy."""
    try:
        windows = _list_windows_raw()
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not list windows: {exc}")
    matches = _match_windows(title_or_app, windows)
    if not matches:
        titles = "; ".join(f"{w['title'][:50]} [{w['app']}]" for w in windows[:15])
        return ActionResult(False, f"No open window matches '{title_or_app}'. Open windows: {titles or 'none'}")
    if len(matches) > 1 and not close_all:
        titles = "; ".join(f"{w['title'][:60]} [{w['app']}]" for w in matches[:10])
        return ActionResult(False, f"{len(matches)} windows match '{title_or_app}': {titles}. "
                                   f"Call again with a more exact title, or close_all=true to close all of them.")
    try:
        for w in matches:
            _close_window_raw(w)
        time.sleep(0.8)
        still_ids = {w["id"] for w in _list_windows_raw()}
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Closing failed: {exc}")
    left = [w for w in matches if w["id"] in still_ids]
    closed = [w for w in matches if w["id"] not in still_ids]
    if left and not closed:
        return ActionResult(False, "Asked to close, but it is still open (it may be waiting for the user to "
                                   f"save or answer a prompt): {'; '.join(w['title'][:60] for w in left)}")
    note = f" ({len(left)} still open, maybe asking to save)" if left else ""
    return ActionResult(True, f"Closed: {'; '.join(w['title'][:60] for w in closed)}{note}")


# ---------------------------------------------------------------------------
# System info
# ---------------------------------------------------------------------------
def get_system_info() -> ActionResult:
    info = {
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "hostname": platform.node(),
        "python": platform.python_version(),
    }
    try:
        import psutil

        info["cpu_percent"] = psutil.cpu_percent(interval=0.3)
        info["ram_percent"] = psutil.virtual_memory().percent
        disk = psutil.disk_usage(str(Path.home().anchor or "/"))
        info["disk_percent"] = disk.percent
        info["disk_free_gb"] = round(disk.free / 1024 ** 3, 1)
        battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
        if battery is not None:
            info["battery_percent"] = round(battery.percent)
            info["battery_plugged_in"] = battery.power_plugged
        info["uptime_hours"] = round((time.time() - psutil.boot_time()) / 3600, 1)
    except ImportError:
        info["note"] = "Install 'psutil' for CPU/RAM/battery details."
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"Some details unavailable: {exc}"
    summary = ", ".join(f"{k}={v}" for k, v in info.items())
    return ActionResult(True, summary, info)


# ---------------------------------------------------------------------------
# Running processes
# ---------------------------------------------------------------------------
_CRITICAL_PROCESSES = {
    "explorer.exe", "system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "init", "systemd", "launchd", "kernel_task", "loginwindow",
    "windowserver", "xorg", "wayland",
}


def list_processes(name_filter: Optional[str] = None) -> ActionResult:
    try:
        import psutil
    except ImportError:
        return ActionResult(False, "Install 'psutil' to list processes.")

    counts: dict[str, int] = {}
    needle = (name_filter or "").lower()
    for p in psutil.process_iter(["name"]):
        try:
            pname = p.info["name"] or ""
            if needle and needle not in pname.lower():
                continue
            counts[pname] = counts.get(pname, 0) + 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    rows = [{"name": n, "instances": c} for n, c in sorted(counts.items(), key=lambda kv: kv[0].lower())][:80]
    return ActionResult(True, f"{len(rows)} distinct program(s) found.", {"processes": rows})


def _matching_processes(name_or_pid: str):
    import psutil

    own = {os.getpid()}
    try:
        me = psutil.Process()
        own.update(p.pid for p in me.parents())   # never kill Amy or the terminal that launched her
        own.update(c.pid for c in me.children(recursive=True))
    except Exception:  # noqa: BLE001
        pass

    def is_amy(proc) -> bool:  # any process running code from Amy's own folder (launchers, re-spawns)
        try:
            return any(str(_PROJECT_DIR).lower() in part.lower() for part in proc.cmdline())
        except Exception:  # noqa: BLE001
            return False

    text = str(name_or_pid).strip()
    if text.isdigit():
        pid = int(text)
        try:
            proc = psutil.Process(pid)
            if pid in own or is_amy(proc) or (proc.name() or "").lower() in _CRITICAL_PROCESSES:
                return []
            return [proc]
        except psutil.NoSuchProcess:
            return []

    needle = text.lower()
    exact, partial = [], []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            pname = (p.info["name"] or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if p.info["pid"] in own or pname in _CRITICAL_PROCESSES or is_amy(p):
            continue
        if pname in (needle, needle + ".exe"):
            exact.append(p)
        elif len(needle) >= 3 and needle in pname:
            partial.append(p)
    return exact or partial  # an exact name beats a fuzzy one ("code" shouldn't also hit "codecs")


def kill_process(name_or_pid: str, confirm: bool = True) -> ActionResult:
    """FORCE-kill a program's processes. Last resort for frozen programs -- to *close* something use
    close_window instead. Always needs the user's spoken yes (`confirm` is ignored)."""
    if not config.ALLOW_PROCESS_CONTROL:
        return ActionResult(False, "Process control is disabled in config.py.")
    if str(name_or_pid).strip().lower() in ("explorer", "explorer.exe"):
        return ActionResult(False, "Refused: explorer.exe runs the taskbar, desktop and every folder window "
                                   "(including 'C:'). To close a folder window use close_window.")
    try:
        import psutil
    except ImportError:
        return ActionResult(False, "Install 'psutil' to manage processes.")

    targets = _matching_processes(str(name_or_pid))
    if not targets:
        return ActionResult(False, f"No closable running process matched '{name_or_pid}'.")

    def label(p) -> str:
        try:
            return f"{p.name()} (PID {p.pid})"
        except psutil.Error:
            return f"PID {p.pid}"

    listing = ", ".join(label(p) for p in targets[:8]) + (" ..." if len(targets) > 8 else "")
    if not _confirm(f"End {len(targets)} process(es): {listing}"):
        return ActionResult(False, "User declined ending the process.")

    ended = []
    for p in targets:
        name = label(p)
        try:
            p.terminate()
            ended.append((p, name))
        except psutil.Error:
            continue
    _gone, alive = psutil.wait_procs([p for p, _ in ended], timeout=2)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    if not ended:
        return ActionResult(False, f"Could not end '{name_or_pid}' (access denied?).")
    return ActionResult(True, f"Ended: {', '.join(n for _, n in ended[:8])}")


# ---------------------------------------------------------------------------
# Volume control (cross-platform)
# ---------------------------------------------------------------------------
def _windows_volume(action: str, level: Optional[int]) -> Optional[ActionResult]:
    """Exact volume control through pycaw. Returns None if pycaw isn't usable."""
    try:
        from ctypes import POINTER, cast

        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        speakers = AudioUtilities.GetSpeakers()
        volume = getattr(speakers, "EndpointVolume", None)  # newer pycaw
        if volume is None:  # older pycaw
            interface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))
        current = volume.GetMasterVolumeLevelScalar()
        if action == "set" and level is not None:
            volume.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100.0, None)
        elif action == "up":
            volume.SetMasterVolumeLevelScalar(min(1.0, current + 0.1), None)
        elif action == "down":
            volume.SetMasterVolumeLevelScalar(max(0.0, current - 0.1), None)
        elif action in ("mute", "unmute"):
            volume.SetMute(1 if action == "mute" else 0, None)
        else:
            return ActionResult(False, f"Unknown volume action: {action}")
        now = round(volume.GetMasterVolumeLevelScalar() * 100)
        return ActionResult(True, f"Volume: {action}. Now at {now}%.")
    except Exception:  # noqa: BLE001 - pycaw missing or API mismatch -> use the key-press fallback
        return None


def control_volume(action: str, level: Optional[int] = None) -> ActionResult:
    """action: 'set' (needs level 0-100), 'up', 'down', 'mute', or 'unmute'."""
    action = (action or "").lower().strip()
    if level is not None:
        level = max(0, min(100, int(level)))
    try:
        if OS_NAME == "Darwin":
            if action == "set" and level is not None:
                cmd = f"set volume output volume {level}"
            elif action == "mute":
                cmd = "set volume with output muted"
            elif action == "unmute":
                cmd = "set volume without output muted"
            elif action in ("up", "down"):
                delta = 10 if action == "up" else -10
                cmd = f"set volume output volume ((output volume of (get volume settings)) + ({delta}))"
            else:
                return ActionResult(False, f"Unknown volume action: {action}")
            subprocess.run(["osascript", "-e", cmd], check=True, capture_output=True, timeout=10)

        elif OS_NAME == "Windows":
            exact = _windows_volume(action, level)
            if exact is not None:
                return exact
            # Fallback without pycaw: simulated media keys (each press = 2%). No exact levels, and
            # the mute key is a toggle, so 'mute'/'unmute' can't be told apart.
            key_map = {"up": ("175", 5), "down": ("174", 5), "mute": ("173", 1), "unmute": ("173", 1)}
            if action == "set":
                return ActionResult(False, "Setting an exact level on Windows needs: pip install pycaw. "
                                           "Use 'up'/'down'/'mute' instead.")
            if action not in key_map:
                return ActionResult(False, f"Unknown volume action: {action}")
            code, repeat = key_map[action]
            ps = f"$w=New-Object -ComObject WScript.Shell; 1..{repeat} | % {{ $w.SendKeys([char]{code}) }}"
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, capture_output=True,
                           timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
            note = " (toggle: press again to undo)" if action in ("mute", "unmute") else ""
            return ActionResult(True, f"Volume: {action}{note}")

        else:  # Linux
            if not shutil.which("pactl"):
                return ActionResult(False, "Install 'pactl' (PulseAudio/PipeWire) for volume control on Linux.")
            if action == "set" and level is not None:
                args = ["set-sink-volume", "@DEFAULT_SINK@", f"{level}%"]
            elif action == "up":
                args = ["set-sink-volume", "@DEFAULT_SINK@", "+10%"]
            elif action == "down":
                args = ["set-sink-volume", "@DEFAULT_SINK@", "-10%"]
            elif action == "mute":
                args = ["set-sink-mute", "@DEFAULT_SINK@", "1"]
            elif action == "unmute":
                args = ["set-sink-mute", "@DEFAULT_SINK@", "0"]
            else:
                return ActionResult(False, f"Unknown volume action: {action}")
            subprocess.run(["pactl", *args], check=True, capture_output=True, timeout=10)

        return ActionResult(True, f"Volume: {action}" + (f" {level}" if level is not None else ""))
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Volume control failed: {exc}")


# ---------------------------------------------------------------------------
# Power control -- always requires explicit confirmation
# ---------------------------------------------------------------------------
def system_power(action: str, confirm: bool = True) -> ActionResult:
    """action: 'shutdown', 'restart', or 'sleep'. Always needs the user's spoken yes
    (`confirm` is ignored)."""
    if not config.ALLOW_POWER_ACTIONS:
        return ActionResult(False, "Power actions are disabled in config.py.")
    action = (action or "").lower().strip()
    if action not in ("shutdown", "restart", "sleep"):
        return ActionResult(False, f"Unknown power action: {action}")
    if not _confirm(f"{action.capitalize()} this computer now"):
        return ActionResult(False, "User declined.")

    try:
        if OS_NAME == "Windows":
            cmd = {
                "shutdown": ["shutdown", "/s", "/t", "5"],
                "restart": ["shutdown", "/r", "/t", "5"],
                "sleep": ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
            }[action]
        elif OS_NAME == "Darwin":
            cmd = {
                "shutdown": ["osascript", "-e", 'tell app "System Events" to shut down'],
                "restart": ["osascript", "-e", 'tell app "System Events" to restart'],
                "sleep": ["pmset", "sleepnow"],
            }[action]
        else:  # Linux
            cmd = {
                "shutdown": ["systemctl", "poweroff"],
                "restart": ["systemctl", "reboot"],
                "sleep": ["systemctl", "suspend"],
            }[action]
        subprocess.Popen(cmd)
        return ActionResult(True, f"{action.capitalize()} initiated.")
    except Exception as exc:  # noqa: BLE001
        return ActionResult(False, f"Could not {action}: {exc}")
