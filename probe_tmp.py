"""윈도우 탐침 (임시): 소켓 shutdown 이 select/recv 를 깨우는가, 다시 스캔 쪽의 서명 유사도."""
import hashlib
import select
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path


def borrow_shutdown(fd):
    s = socket.socket(fileno=fd)
    try:
        s.shutdown(socket.SHUT_RDWR)
    except OSError as e:
        print("  shutdown error", type(e).__name__, e)
    finally:
        s.detach()


def wake(kind, how):
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
                a.settimeout(15)
                res["got"] = a.recv(1)
            elif kind == "select":
                r, _, x = select.select([a], [], [a], 15)
                res["got"] = ("readable" if r else "") + ("except" if x else "") or "timeout"
                if r:
                    a.setblocking(False)
                    try:
                        res["recv_after"] = a.recv(1)
                    except OSError as e:
                        res["recv_after"] = type(e).__name__ + str(e.args)
            elif kind == "selectors":
                import selectors
                sel = selectors.DefaultSelector()
                sel.register(a, selectors.EVENT_READ)
                ev = sel.select(15)
                res["got"] = "ready" if ev else "timeout"
        except Exception as e:
            res["got"] = f"{type(e).__name__}: {e}"
        res["t"] = round(time.monotonic() - t, 2)

    th = threading.Thread(target=waiter)
    th.start()
    time.sleep(0.5)
    borrow_shutdown(a.fileno())
    th.join(20)
    print(f"  {kind:9} {how:4} -> {res}")
    for s in (a, b):
        try:
            s.close()
        except OSError:
            pass


print(sys.platform, sys.version)
for how in ("pair", "tcp"):
    for kind in ("recv", "select", "selectors"):
        wake(kind, how)

try:
    import psycopg
    print("psycopg", psycopg.__version__, getattr(psycopg.pq, "__impl__", "?"))
    from psycopg import waiting
    print("  wait fn:", waiting.wait.__name__ if hasattr(waiting, "wait") else "?", getattr(waiting, "wait_c", None) is not None)
except Exception as e:
    print("psycopg", type(e).__name__, e)

# 다시 스캔 쪽
sys.path.insert(0, str(Path("tests").resolve()))
from minedocscan.tools.synth import generate
from minedocscan.config import Settings
from minedocscan.forms.sitepack import SitePack
from minedocscan.pipeline import Pipeline
from conftest import split_pages
import shutil

root = Path(tempfile.mkdtemp())
r = generate(root / "rs", days=1, seed=0, rescans=True)
first, rescan = sorted(r.scans.glob("scan_*.pdf"))
for p in (first, rescan):
    print("sha", p.name, hashlib.sha256(p.read_bytes()).hexdigest()[:16], p.stat().st_size)
scans = root / "scans"
split_pages(first, [1, 2, 3, 6], scans / "x_2030-01-07.pdf")
shutil.copyfile(rescan, scans / "y_2030-01-07.pdf")
st = Settings(site=r.site, archive_root=scans, work_root=root / "w", reviews=root / "rv" / "reviews.jsonl", save_aligned=False)
pipe = Pipeline(st, site=SitePack(r.site))
pipe.run([scans])
from minedocscan.imaging.io import load_pages
for name in ("x_2030-01-07.pdf", "y_2030-01-07.pdf"):
    for n, g in load_pages(scans / name, 200):
        print("render", name, n, g.shape, hashlib.sha256(g.tobytes()).hexdigest()[:12])
for row in pipe.con.execute("SELECT d.source_name, p.page_no, p.status, p.duplicate_sim, p.rotation, p.align_inliers, p.align_grid_err, p.template_name "
                            "FROM doc_page p JOIN doc_document d ON p.document_id = d.document_id ORDER BY 1, 2"):
    print("page", tuple(row))
for row in pipe.con.execute("SELECT page_id, sig FROM doc_page_sig ORDER BY 1"):
    print("sig", row[0][-4:], hashlib.sha256(str(row[1]).encode()).hexdigest()[:12])
