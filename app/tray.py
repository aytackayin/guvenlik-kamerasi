"""Bildirim alanı (tray) simgesi ve uygulama penceresinin simge durumuna küçültülünce gizlenmesi.

Ek kütüphane gerektirmez; Windows API'si ctypes ile kullanılır. Kendi iş parçacığında çalışır,
işlemleri geri çağrılarla (asyncio döngüsüne aktararak) yaptırır.
"""
import ctypes
import ctypes.wintypes as wt
import logging
import threading
import time

log = logging.getLogger("tray")

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_TIMER, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0113, 0x0000
WM_SETICON = 0x0080
WM_LBUTTONUP, WM_LBUTTONDBLCLK, WM_RBUTTONUP = 0x0202, 0x0203, 0x0205
WM_APP = 0x8000
WM_TRAY = WM_APP + 1
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
MF_STRING, MF_SEPARATOR = 0x0000, 0x0800
TPM_RIGHTBUTTON, TPM_RETURNCMD, TPM_BOTTOMALIGN = 0x0002, 0x0100, 0x0020
SW_HIDE, SW_RESTORE = 0, 9
IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
SM_CXSMICON, SM_CXICON = 49, 11
TIMER_WATCH, TIMER_CLICK = 1, 2
CMD_OPEN, CMD_TOGGLE, CMD_QUIT = 1, 2, 3
WINDOW_TITLE = "Güvenlik Kamerası"


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("hWnd", wt.HWND), ("uID", wt.UINT), ("uFlags", wt.UINT),
                ("uCallbackMessage", wt.UINT), ("hIcon", wt.HICON), ("szTip", wt.WCHAR * 128),
                ("dwState", wt.DWORD), ("dwStateMask", wt.DWORD), ("szInfo", wt.WCHAR * 256),
                ("uVersion", wt.UINT), ("szInfoTitle", wt.WCHAR * 64), ("dwInfoFlags", wt.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wt.HICON)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.LoadImageW.restype = wt.HANDLE
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CreatePopupMenu.restype = wt.HMENU
user32.AppendMenuW.argtypes = [wt.HMENU, wt.UINT, ctypes.c_size_t, wt.LPCWSTR]
user32.SetMenuDefaultItem.argtypes = [wt.HMENU, wt.UINT, wt.UINT]
user32.TrackPopupMenu.argtypes = [wt.HMENU, wt.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.LPVOID]
user32.DestroyMenu.argtypes = [wt.HMENU]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, wt.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.RegisterWindowMessageW.restype = wt.UINT
shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


class Tray:
    """on_open(): pencere yoksa açar; on_toggle(): bağlantıyı aç/kapat; on_quit(): programı kapatır;
    is_connected(): menüde hangi bağlantı seçeneğinin gösterileceği."""

    def __init__(self, icon_path, on_open, on_toggle, on_quit, is_connected):
        self.icon_path = str(icon_path)
        self.on_open, self.on_toggle, self.on_quit, self.is_connected = on_open, on_toggle, on_quit, is_connected
        self.hwnd = None
        self.app_hwnd = None        # uygulama penceresi (Edge --app)
        self.iconed = set()         # simgesi atanmış pencereler
        self.last_launch = 0
        self._wndproc = WNDPROC(self._proc)   # çöp toplayıcı silmesin
        threading.Thread(target=self._run, name="tray", daemon=True).start()

    # ------------------------------------------------ uygulama penceresi
    def _find_window(self):
        if self.app_hwnd and user32.IsWindow(self.app_hwnd):
            return self.app_hwnd
        found = []
        buf = ctypes.create_unicode_buffer(256)

        def cb(h, _):
            user32.GetClassNameW(h, buf, 256)
            if buf.value == "Chrome_WidgetWin_1":
                user32.GetWindowTextW(h, buf, 256)
                if buf.value == WINDOW_TITLE:
                    found.append(h)
                    return False
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        self.app_hwnd = found[0] if found else None
        return self.app_hwnd

    def has_window(self):
        return bool(self._find_window())

    def _watch(self):
        """Uygulama penceresine program simgesini atar."""
        h = self._find_window()
        if not h:
            return
        if h not in self.iconed:
            self.iconed.add(h)
            user32.SendMessageW(h, WM_SETICON, 0, self.hicon_small or 0)
            user32.SendMessageW(h, WM_SETICON, 1, self.hicon_big or 0)

    def show_window(self):
        h = self._find_window()
        if h:
            user32.ShowWindow(h, SW_RESTORE)
            user32.SetForegroundWindow(h)
        elif time.time() - self.last_launch > 8:   # pencere açılırken tekrar tıklanırsa ikinci pencere açılmasın
            self.last_launch = time.time()
            self.on_open()

    # ------------------------------------------------ tray
    def _nid(self):
        n = NOTIFYICONDATAW()
        n.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        n.hWnd, n.uID = self.hwnd, 1
        n.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        n.uCallbackMessage = WM_TRAY
        n.hIcon = self.hicon_small
        n.szTip = WINDOW_TITLE
        return n

    def _menu(self):
        m = user32.CreatePopupMenu()
        user32.AppendMenuW(m, MF_STRING, CMD_OPEN, "Aç")
        user32.AppendMenuW(m, MF_STRING, CMD_TOGGLE, "Bağlantıyı kapat" if self.is_connected() else "Bağlantıyı aç")
        user32.AppendMenuW(m, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(m, MF_STRING, CMD_QUIT, "Kapat")
        user32.SetMenuDefaultItem(m, CMD_OPEN, 0)
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)     # menü dışına tıklanınca kapansın
        cmd = user32.TrackPopupMenu(m, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_BOTTOMALIGN, pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(m)
        if cmd == CMD_OPEN:
            self.show_window()
        elif cmd == CMD_TOGGLE:
            self.on_toggle()
        elif cmd == CMD_QUIT:
            self.remove()
            self.on_quit()

    def _proc(self, hwnd, msg, wp, lp):
        try:
            if msg == WM_TRAY:
                if lp == WM_LBUTTONUP:          # tek tık: çift tık süresi dolunca menü
                    user32.SetTimer(hwnd, TIMER_CLICK, user32.GetDoubleClickTime(), None)
                elif lp == WM_LBUTTONDBLCLK:
                    user32.KillTimer(hwnd, TIMER_CLICK)
                    self.show_window()
                elif lp == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_TIMER:
                if wp == TIMER_CLICK:
                    user32.KillTimer(hwnd, TIMER_CLICK)
                    self._menu()
                elif wp == TIMER_WATCH:
                    self._watch()
                return 0
            if msg == self.msg_taskbar:          # Explorer yeniden başladı: simgeyi tekrar ekle
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid()))
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            log.exception("tray")
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self.hicon_small = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, user32.GetSystemMetrics(SM_CXSMICON),
                                                 user32.GetSystemMetrics(SM_CXSMICON), LR_LOADFROMFILE)
            self.hicon_big = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, user32.GetSystemMetrics(SM_CXICON),
                                               user32.GetSystemMetrics(SM_CXICON), LR_LOADFROMFILE)
            wc = WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinst, lpszClassName="GuvenlikKamerasiTray")
            user32.RegisterClassW(ctypes.byref(wc))
            self.msg_taskbar = user32.RegisterWindowMessageW("TaskbarCreated")
            self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, WINDOW_TITLE, 0, 0, 0, 0, 0, None, None, hinst, None)
            shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(self._nid()))
            user32.SetTimer(self.hwnd, TIMER_WATCH, 250, None)
            msg = wt.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("tray başlatılamadı")

    def remove(self):
        if self.hwnd:
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid()))
