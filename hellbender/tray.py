"""Native Windows notification-area lifecycle, without third-party dependencies.

The native message thread emits strings only; Tk is touched solely by the UI
thread. Unsupported platforms keep their ordinary, accessible taskbar window.
"""
from __future__ import annotations
import ctypes
import hashlib
import struct
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

WM_TRAY = 0x8000 + 20
WM_RESTORE = 0x8000 + 21
WM_CLOSE = 0x0010
MENU_ITEMS = ((1, "显示窗口"), (2, "隐藏到托盘"), (3, "退出 Hellbender Viewer"))

def window_class(database: Path) -> str:
    key = hashlib.sha256(str(Path(database).resolve()).casefold().encode()).hexdigest()[:20]
    return "HellbenderViewer.Tray." + key

def _api():
    from ctypes import wintypes as w
    u = ctypes.WinDLL("user32", use_last_error=True)
    s = ctypes.WinDLL("shell32", use_last_error=True)
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
    class GUID(ctypes.Structure):
        _fields_ = [("Data1", w.DWORD), ("Data2", w.WORD), ("Data3", w.WORD), ("Data4", ctypes.c_ubyte * 8)]
    class WindowClass(ctypes.Structure):
        _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", proc),
                    ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", w.HINSTANCE),
                    ("hIcon", w.HICON), ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                    ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR), ("hIconSm", w.HICON)]
    class IconData(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("uFlags", w.UINT),
                    ("uCallbackMessage", w.UINT), ("hIcon", w.HICON), ("szTip", w.WCHAR * 128),
                    ("dwState", w.DWORD), ("dwStateMask", w.DWORD), ("szInfo", w.WCHAR * 256),
                    ("uVersion", w.UINT), ("szInfoTitle", w.WCHAR * 64), ("dwInfoFlags", w.DWORD),
                    ("guidItem", GUID), ("hBalloonIcon", w.HICON)]
    class IconIdentifier(ctypes.Structure):
        _fields_ = [("cbSize", w.DWORD), ("hWnd", w.HWND), ("uID", w.UINT), ("guidItem", GUID)]
    signatures = {
        "RegisterClassExW": ([ctypes.POINTER(WindowClass)], w.ATOM),
        "UnregisterClassW": ([w.LPCWSTR, w.HINSTANCE], w.BOOL),
        "CreateWindowExW": ([w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
        "DefWindowProcW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], ctypes.c_ssize_t),
        "GetMessageW": ([ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT], ctypes.c_int),
        "TranslateMessage": ([ctypes.POINTER(w.MSG)], w.BOOL),
        "DispatchMessageW": ([ctypes.POINTER(w.MSG)], ctypes.c_ssize_t),
        "PostMessageW": ([w.HWND, w.UINT, w.WPARAM, w.LPARAM], w.BOOL),
        "DestroyWindow": ([w.HWND], w.BOOL), "PostQuitMessage": ([ctypes.c_int], None),
        "RegisterWindowMessageW": ([w.LPCWSTR], w.UINT), "FindWindowW": ([w.LPCWSTR, w.LPCWSTR], w.HWND),
        "CreatePopupMenu": ([], w.HMENU), "AppendMenuW": ([w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR], w.BOOL),
        "TrackPopupMenu": ([w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.POINTER(w.RECT)], w.UINT),
        "DestroyMenu": ([w.HMENU], w.BOOL), "GetCursorPos": ([ctypes.POINTER(w.POINT)], w.BOOL),
        "SetForegroundWindow": ([w.HWND], w.BOOL), "DestroyIcon": ([w.HICON], w.BOOL),
        "CreateIconFromResourceEx": ([ctypes.c_void_p, w.DWORD, w.BOOL, w.DWORD, ctypes.c_int, ctypes.c_int, w.UINT], w.HICON),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(u, name)
        fn.argtypes, fn.restype = args, result
    k.GetModuleHandleW.argtypes, k.GetModuleHandleW.restype = [w.LPCWSTR], w.HMODULE
    s.Shell_NotifyIconW.argtypes, s.Shell_NotifyIconW.restype = [w.DWORD, ctypes.POINTER(IconData)], w.BOOL
    s.Shell_NotifyIconGetRect.argtypes, s.Shell_NotifyIconGetRect.restype = [ctypes.POINTER(IconIdentifier), ctypes.POINTER(w.RECT)], ctypes.c_long
    return SimpleNamespace(user=u, shell=s, kernel=k, types=w, Proc=proc, WindowClass=WindowClass, IconData=IconData, IconIdentifier=IconIdentifier)

def restore_existing(database: Path) -> bool:
    """A second launcher restores the same data profile instead of duplicating it."""
    if sys.platform != "win32":
        return False
    api = _api()
    hwnd = api.user.FindWindowW(window_class(database), None)
    return bool(hwnd and api.user.PostMessageW(hwnd, WM_RESTORE, 0, 0))

class UnavailableTray:
    available = False
    supported = False
    hwnd = None
    def __init__(self, message="Tray is unavailable on this platform; normal taskbar minimize remains available."):
        self.last_error = message
    def ensure_visible(self):
        return False
    def set_tooltip(self, text):
        pass
    def stop(self):
        pass

class WindowsTray:
    supported = True
    def __init__(self, database: Path, emit, demo=False):
        self.api = _api()
        self.class_name = window_class(database)
        self.emit, self.demo = emit, demo
        self.available = False
        self.last_error = ""
        self.hwnd = None
        self.icon = None
        self.data = None
        self._hide_tip_shown = False
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._lock = threading.RLock()
        self._proc = self.api.Proc(self._window_proc)
        self._thread = threading.Thread(target=self._run, name="hellbender-tray", daemon=True)
        self._thread.start()
        if not self._ready.wait(3):
            self.last_error = "Notification-area initialization timed out; the window remains accessible."

    def _create_icon(self):
        pixels, mask = bytearray(), bytearray()
        for y in reversed(range(32)):
            row_mask = 0
            for x in range(32):
                opaque = 2 <= x < 30 and 2 <= y < 30
                glyph = opaque and ((8 <= x < 13 or 19 <= x < 24) and 7 <= y < 25 or 8 <= x < 24 and 13 <= y < 18)
                pixels.extend((204, 216, 110, 255) if glyph else (34, 23, 16, 255) if opaque else (0, 0, 0, 0))
                if not opaque:
                    row_mask |= 1 << (31 - x)
            mask.extend(row_mask.to_bytes(4, "big"))
        dib = struct.pack("<IiiHHIIiiII", 40, 32, 64, 1, 32, 0, len(pixels), 0, 0, 0, 0) + pixels + mask
        buffer = ctypes.create_string_buffer(bytes(dib))
        return self.api.user.CreateIconFromResourceEx(buffer, len(dib), True, 0x00030000, 32, 32, 0)

    def _notify(self, operation):
        with self._lock:
            return bool(self.data and self.api.shell.Shell_NotifyIconW(operation, ctypes.byref(self.data)))

    def _add(self):
        self.available = self._notify(0)
        if self.available:
            self.data.uVersion = 4
            self._notify(4)
            self.last_error = ""
        else:
            self.last_error = "Windows notification area could not register the icon; the window stays accessible."
        return self.available

    def ensure_visible(self):
        if self._stopping.is_set() or not self.hwnd:
            return False
        self.available = self._notify(1)
        if not self.available:
            self.last_error = "Tray icon is unavailable. The viewer has been restored to the taskbar."
        return self.available

    def icon_rect(self):
        """Native shell confirmation, also used by the focused integration tests."""
        identifier = self.api.IconIdentifier()
        identifier.cbSize, identifier.hWnd, identifier.uID = ctypes.sizeof(identifier), self.hwnd, 1
        rect = self.api.types.RECT()
        result = self.api.shell.Shell_NotifyIconGetRect(ctypes.byref(identifier), ctypes.byref(rect))
        return (rect.left, rect.top, rect.right, rect.bottom) if result == 0 else None

    def explain_hide(self):
        """Windows controls overflow placement; give a one-time restore instruction."""
        if self._hide_tip_shown or not self.available:
            return
        self._hide_tip_shown = True
        with self._lock:
            self.data.uFlags |= 0x10
            self.data.szInfoTitle = "Hellbender 仍在运行"
            self.data.szInfo = "图标可能在任务栏 ^ 里。单击 H 恢复窗口，或再打开桌面快捷方式；右键 H → 退出。"
            self.data.dwInfoFlags = 0x01 | 0x10
            self._notify(1)
            self.data.uFlags &= ~0x10
            self.data.szInfo = ""

    def set_tooltip(self, text):
        """Hover text summarizing the last refresh; the icon itself is unchanged."""
        if not self.data:
            return
        with self._lock:
            self.data.szTip = text[:127]
            self._notify(1)

    def _menu(self):
        u = self.api.user
        menu = u.CreatePopupMenu()
        if not menu:
            self.emit("restore")
            return
        try:
            for ident, label in MENU_ITEMS:
                if ident == 3:
                    u.AppendMenuW(menu, 0x0800, 0, None)
                u.AppendMenuW(menu, 0, ident, label)
            point = self.api.types.POINT()
            u.GetCursorPos(ctypes.byref(point))
            u.SetForegroundWindow(self.hwnd)
            selected = u.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, self.hwnd, None)
            action = {1:"restore", 2:"hide", 3:"quit"}.get(selected)
            if action:
                self.emit(action)
            u.PostMessageW(self.hwnd, 0, 0, 0)
            self._notify(3)
        finally:
            u.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == WM_TRAY:
                event = lparam & 0xFFFF
                if event in (0x0202, 0x0203, 0x0400, 0x0401, 0x0405):
                    self.emit("restore")
                elif event in (0x007B, 0x0205):
                    self._menu()
                return 0
            if message == WM_RESTORE:
                self.emit("restore")
                return 0
            if message == getattr(self, "taskbar_created", None):
                if not self._add():
                    self.emit("unavailable")
                return 0
            if message == WM_CLOSE:
                self._notify(2)
                self.available = False
                self.api.user.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:  # WM_DESTROY
                self.api.user.PostQuitMessage(0)
                return 0
        except Exception as exc:
            self.available = False
            self.last_error = f"Tray error: {exc}"
            self.emit("unavailable")
        return self.api.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self):
        api = self.api
        module = api.kernel.GetModuleHandleW(None)
        registered = False
        try:
            wc = api.WindowClass()
            wc.cbSize, wc.lpfnWndProc, wc.hInstance = ctypes.sizeof(wc), self._proc, module
            wc.lpszClassName = self.class_name
            if not api.user.RegisterClassExW(ctypes.byref(wc)):
                raise OSError(ctypes.get_last_error(), "Tray message window registration failed")
            registered = True
            self.taskbar_created = api.user.RegisterWindowMessageW("TaskbarCreated")
            # Hidden top-level window receives TaskbarCreated broadcasts after Explorer restarts.
            self.hwnd = api.user.CreateWindowExW(0, self.class_name, "Hellbender tray controller", 0, 0, 0, 0, 0, None, None, module, None)
            if not self.hwnd:
                raise OSError(ctypes.get_last_error(), "Tray message window creation failed")
            self.icon = self._create_icon()
            if not self.icon:
                raise OSError(ctypes.get_last_error(), "Tray icon creation failed")
            self.data = api.IconData()
            self.data.cbSize = ctypes.sizeof(self.data)
            self.data.hWnd, self.data.uID = self.hwnd, 1
            self.data.uFlags = 1 | 2 | 4 | 0x80  # callback, icon, tooltip, standard version-4 tooltip
            self.data.uCallbackMessage, self.data.hIcon = WM_TRAY, self.icon
            self.data.szTip = "Hellbender Viewer" + (" · 演示" if self.demo else "") + " · 单击恢复，右键退出"
            self._add()
            self._ready.set()
            if self._stopping.is_set():
                api.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            msg = api.types.MSG()
            while api.user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                api.user.TranslateMessage(ctypes.byref(msg))
                api.user.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            self.last_error = f"Tray unavailable: {exc}"
            self.available = False
            self.emit("unavailable")
        finally:
            self._ready.set()
            self._notify(2)
            self.available = False
            if self.hwnd:
                api.user.DestroyWindow(self.hwnd)
                self.hwnd = None
            if self.icon:
                api.user.DestroyIcon(self.icon)
            if registered:
                api.user.UnregisterClassW(self.class_name, module)

    def stop(self):
        self._stopping.set()
        if self.hwnd:
            self.api.user.PostMessageW(self.hwnd, 0x001F, 0, 0)  # dismiss any popup (WM_CANCELMODE)
            self.api.user.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self._thread.join(timeout=2)

def create_tray(database: Path, emit, demo=False):
    if sys.platform != "win32":
        return UnavailableTray()
    try:
        return WindowsTray(database, emit, demo=demo)
    except Exception as exc:
        return UnavailableTray(f"Tray unavailable: {exc}. Normal taskbar minimize remains available.")
