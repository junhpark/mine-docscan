"""윈도우 탐침 2 (임시): CancelIoEx 가 select/recv 를 깨우는가, 정합이 결정적인가 (실행마다·스레드 수)."""
import hashlib
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

WIN = sys.platform == "win32"


def cancel_io(fd):
    import ctypes
    k = ctypes.windll.kernel32
    k.CancelIoEx.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ok = k.CancelIoEx(ctypes.c_void_p(fd), None)
    return ok, k.GetLastError()


def wake(kind, how, method):
    if how == "pair":
        a, b = socket.socketpair()
    else:
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        a = socket.create_connection(srv.getsockname())
        b, _ = srv.accept()
    res = {}

    def waiter():
        t = time.monotonic()
        try:
            if kind == "recv":
                a.settimeout(8)
                res["got"] = a.recv(1)
            else:
                r, _, x = select.select([a], [], [a], 8)
                res["got"] = ("readable" if r else "") + ("except" if x else "") or "timeout"
                if r or x:
                    a.setblocking(False)
                    try:
                        res["recv_after"] = a.recv(1)
                    except OSError as e:
                        res["recv_after"] = f"{type(e).__name__}{e.args}"
        except Exception as e:
            res["got"] = f"{type(e).__name__}: {e}"
        res["t"] = round(time.monotonic() - t, 2)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.5)
    if method == "cancelio":
        res["cancel"] = cancel_io(a.fileno())
    elif method == "cancelio+shutdown":
        s = socket.socket(fileno=a.fileno())
        try:
            s.shutdown(socket.SHUT_RDWR)
        finally:
            s.detach()
        res["cancel"] = cancel_io(a.fileno())
    th.join(12)
    # 끊은 뒤 그 소켓을 다시 쓸 수 있나 (libpq 가 닫을 때까지 기술자는 살아 있어야 한다)
    try:
        a.getsockname()
        res["still_open"] = True
    except OSError as e:
        res["still_open"] = type(e).__name__
    print(f"  {kind:6} {how:4} {method:18} -> {res}", flush=True)
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


print(sys.platform, sys.version, flush=True)
if WIN:
    for how in ("pair", "tcp"):
        for kind in ("recv", "select"):
            for method in ("cancelio", "cancelio+shutdown"):
                wake(kind, how, method)

# 정합의 결정성
if len(sys.argv) > 1 and sys.argv[1] == "align":
    import cv2
    import numpy as np
    from minedocscan.forms.sitepack import SitePack
    from minedocscan.imaging.io import load_pages
    from minedocscan.imaging.align import align_to_template
    site = SitePack(Path(sys.argv[2]))
    tpl = site.templates["synth_inspection"]
    threads = int(sys.argv[4])
    if threads:
        cv2.setNumThreads(threads)
    g = dict(load_pages(Path(sys.argv[3]), 200))[1]
    for i in range(3):
        ar = align_to_template(g, tpl.reference, [], ref_features=tpl.features)
        H = np.asarray(ar.homography)
        print(f"  align threads={threads or cv2.getNumThreads()} run{i}: inliers {ar.n_inliers} H {hashlib.sha256(np.round(H, 9).tobytes()).hexdigest()[:12]} cpu {cv2.checkHardwareSupport(cv2.CPU_AVX512_SKX) if hasattr(cv2, 'CPU_AVX512_SKX') else '?'} {cv2.getNumberOfCPUs()}", flush=True)
    sys.exit(0)

from minedocscan.tools.synth import generate
root = Path(tempfile.mkdtemp())
r = generate(root / "rs", days=1, seed=0, rescans=True)
first = sorted(r.scans.glob("scan_*.pdf"))[0]
for threads in ("0", "0", "1"):
    subprocess.run([sys.executable, __file__, "align", str(r.site), str(first), threads], check=False)
import cv2
print(cv2.getBuildInformation()[:3000])
