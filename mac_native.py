"""macOS desktop integration. Native imports are lazy so source remains portable."""
import os
import threading
import time


def displays():
    from AppKit import NSScreen
    screens = list(NSScreen.screens())
    height = screens[0].frame().size.height
    result = []
    for index, screen in enumerate(screens):
        frame = screen.visibleFrame()
        x, y, width, h = frame.origin.x, frame.origin.y, frame.size.width, frame.size.height
        result.append(dict(device=str(screen.deviceDescription()['NSScreenNumber']),
                           rect=[round(x), round(height-y-h), round(x+width), round(height-y)], primary=index == 0))
    return result


def cocoa_window(window):
    from AppKit import NSApp
    window.update_idletasks()
    return next((item for item in NSApp().windows() if str(item.title()) == window.title()), None)


def move_window(window, x, y):
    from AppKit import NSScreen
    native = cocoa_window(window)
    if native:
        native.setFrameTopLeftPoint_((x, NSScreen.screens()[0].frame().size.height-y))
    else:
        window.geometry(f'+{int(x)}+{int(y)}')


def lock_overlay(window, locked):
    from AppKit import NSColor
    native = cocoa_window(window)
    if native:
        native.setOpaque_(False)
        native.setBackgroundColor_(NSColor.clearColor())
        native.setHasShadow_(False)
        native.setIgnoresMouseEvents_(locked)
        native.setCollectionBehavior_(1 | 256)  # all spaces + fullscreen auxiliary


def capture_permission(request=False):
    import Quartz
    allowed = Quartz.CGPreflightScreenCaptureAccess()
    if not allowed and request:
        allowed = Quartz.CGRequestScreenCaptureAccess()
    if not allowed:
        raise RuntimeError('Macの「システム設定 → プライバシーとセキュリティ → 画面収録」でTerminal／Pythonを許可し、アプリを開き直してください。')


def windows():
    import Quartz as q
    from AppKit import NSRunningApplication
    capture_permission(request=True)
    found = []
    for row in q.CGWindowListCopyWindowInfo(q.kCGWindowListOptionAll, q.kCGNullWindowID):
        title, pid = row.get(q.kCGWindowName, ''), int(row.get(q.kCGWindowOwnerPID, 0))
        if not title or pid == os.getpid() or row.get(q.kCGWindowLayer, 0) != 0:
            continue
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if not app:
            continue
        frame = row[q.kCGWindowBounds]
        created = app.launchDate()
        found.append(dict(kind='window', hwnd=int(row[q.kCGWindowNumber]), pid=pid,
                          created=max(1, int(created.timeIntervalSince1970()*1000)) if created else pid,
                          title=str(title), app=str(row.get(q.kCGWindowOwnerName, '')),
                          exe=str(app.bundleIdentifier() or ''), **{'class': 'macOS'},
                          rect=[int(frame['X']), int(frame['Y']), int(frame['X']+frame['Width']), int(frame['Y']+frame['Height'])],
                          minimized=not bool(row.get(q.kCGWindowIsOnscreen)), hidden=False,
                          label=f"{title} — {row.get(q.kCGWindowOwnerName, '')}"))
    return found


def window_info(identifier):
    return next((item for item in windows() if item['hwnd'] == identifier), None)


def monitors():
    import Quartz as q
    error, ids, count = q.CGGetActiveDisplayList(32, None, None)
    if error:
        raise OSError('Macの画面一覧を取得できません。')
    found = []
    for index, identifier in enumerate(ids[:count]):
        bounds = q.CGDisplayBounds(identifier)
        x, y, width, height = bounds.origin.x, bounds.origin.y, bounds.size.width, bounds.size.height
        found.append(dict(device=str(identifier), rect=[int(x), int(y), int(x+width), int(y+height)],
                          primary=bool(q.CGDisplayIsMain(identifier)), label=f'画面 {index+1}（{int(width)}×{int(height)}）'))
    return sorted(found, key=lambda item: not item['primary'])


def completed(invoke):
    """ScreenCaptureKit completes on its dispatch queue, not the Tk thread."""
    done, answer = threading.Event(), []
    def callback(value, error):
        answer.extend((value, error))
        done.set()
    invoke(callback)
    if not done.wait(10):
        raise RuntimeError('画面の取得がタイムアウトしました。画面収録の許可を確認してください。')
    if answer[1] is not None or answer[0] is None:
        raise RuntimeError('Macの画面を取得できません：' + str(answer[1]))
    return answer[0]


class MacFrames:
    """ScreenCaptureKit exact-window/display capture; never crop a different window."""
    def __init__(self, selected, exclude_pid=None):
        import ScreenCaptureKit as sc
        import Quartz as q
        capture_permission(request=True)
        self.closed = threading.Event()
        content = completed(lambda callback: sc.SCShareableContent.getShareableContentExcludingDesktopWindows_onScreenWindowsOnly_completionHandler_(True, False, callback))
        if selected.get('kind') == 'window':
            target = next((w for w in content.windows() if w.windowID() == selected['hwnd']
                           and w.owningApplication().processID() == selected['pid']), None)
            if target is None:
                raise RuntimeError('選んだウィンドウが閉じられました。選び直してください。')
            self.filter = sc.SCContentFilter.alloc().initWithDesktopIndependentWindow_(target)
            width, height = target.frame().size.width, target.frame().size.height
        else:
            target = next((d for d in content.displays() if str(d.displayID()) == selected['device']), None)
            if target is None:
                raise RuntimeError('録画する画面の接続が変わりました。選び直してください。')
            excluded = [app for app in content.applications() if app.processID() in {os.getpid(), exclude_pid}]
            if exclude_pid and not any(app.processID() == exclude_pid for app in excluded):
                raise RuntimeError('Whisperの字幕・履歴を録画から除外できませんでした。')
            self.filter = sc.SCContentFilter.alloc().initWithDisplay_excludingApplications_exceptingWindows_(target, excluded, [])
            width, height = target.width(), target.height()
        scale = min(1., 1920/width, 1080/height)
        self.config = sc.SCStreamConfiguration.alloc().init()
        self.config.setWidth_(max(2, int(width*scale)//2*2))
        self.config.setHeight_(max(2, int(height*scale)//2*2))
        self.config.setShowsCursor_(False)

    def check(self): pass

    def image(self):
        import objc
        import ScreenCaptureKit as sc
        import Quartz as q
        from PIL import Image
        with objc.autorelease_pool():
            frame = completed(lambda callback: sc.SCScreenshotManager.captureImageWithFilter_configuration_completionHandler_(self.filter, self.config, callback))
            # PNG avoids assumptions about CGImage byte order/color space/stride.
            from Foundation import NSMutableData
            data = NSMutableData.data()
            destination = q.CGImageDestinationCreateWithData(data, 'public.png', 1, None)
            q.CGImageDestinationAddImage(destination, frame, None)
            if not q.CGImageDestinationFinalize(destination):
                raise RuntimeError('録画用の映像を変換できませんでした。')
            import io
            with Image.open(io.BytesIO(bytes(data))) as image:
                return image.convert('RGB')

    def close(self): self.closed.set()


class GlobalHotkey:
    """Register one Carbon hotkey without monitoring arbitrary typed keys."""
    def __init__(self, events):
        import ctypes as c
        class KeyID(c.Structure): _fields_ = [('signature', c.c_uint32), ('id', c.c_uint32)]
        class EventType(c.Structure): _fields_ = [('eventClass', c.c_uint32), ('eventKind', c.c_uint32)]
        self.api = c.CDLL('/System/Library/Frameworks/Carbon.framework/Carbon')
        self.key = c.c_void_p()
        self.handler = c.c_void_p()
        self.api.GetApplicationEventTarget.restype = c.c_void_p
        callback_type = c.CFUNCTYPE(c.c_int32, c.c_void_p, c.c_void_p, c.c_void_p)
        self.callback = callback_type(lambda *args: (events.put({'kind': 'live_hotkey', 'posted': time.monotonic()}) or 0))
        self.api.InstallEventHandler.argtypes = [c.c_void_p, callback_type, c.c_uint32, c.POINTER(EventType), c.c_void_p, c.POINTER(c.c_void_p)]
        self.api.RegisterEventHotKey.argtypes = [c.c_uint32, c.c_uint32, KeyID, c.c_void_p, c.c_uint32, c.POINTER(c.c_void_p)]
        self.api.UnregisterEventHotKey.argtypes = [c.c_void_p]
        self.api.RemoveEventHandler.argtypes = [c.c_void_p]
        target = self.api.GetApplicationEventTarget()
        status = self.api.InstallEventHandler(target, self.callback, 1, c.byref(EventType(0x6b657962, 6)), None, c.byref(self.handler))
        if not status:
            status = self.api.RegisterEventHotKey(101, 4096 | 2048, KeyID(0x57535052, 1), target, 0, c.byref(self.key))
        events.put({'kind': 'live_hotkey_status', 'available': status == 0, 'error_code': status})

    def close(self):
        if self.key.value:
            self.api.UnregisterEventHotKey(self.key)
            self.key.value = None
        if self.handler.value:
            self.api.RemoveEventHandler(self.handler)
            self.handler.value = None
