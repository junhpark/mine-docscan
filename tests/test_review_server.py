"""검수 서버: 포트 0 으로 스레드에 띄워 urllib 로 시험한다."""
import json
import threading
import urllib.error
import urllib.request
from dataclasses import replace
from importlib import resources

import pytest

from conftest import run_day
from minedocscan.review.server import ApiError, ReviewApp, make_server
from minedocscan.review.store import load


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    synth, settings, pipe = run_day(tmp_path_factory.mktemp("review_server"), seed=2)
    app = ReviewApp(pipe.con, pipe.site, settings, "jp", "haul-numbers", {"n": 20, "seed": 1})
    httpd = make_server(app, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"url": f"http://127.0.0.1:{httpd.server_address[1]}", "app": app, "settings": settings, "pipe": pipe,
           "httpd": httpd}
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_binds_localhost_and_page_has_no_external_refs(srv):
    assert srv["httpd"].server_address[0] == "127.0.0.1"
    status, ctype, body = _get(srv["url"] + "/")
    html = body.decode("utf-8")
    assert status == 200 and "text/html" in ctype
    assert "그대로" in html and "/api/review" in html
    for bad in ("http://", "https://", "//cdn", "<link", "src=\"//"):
        assert bad not in html, bad
    # 설치된 패키지에서도 찾는다 (패키지 데이터)
    assert (resources.files("minedocscan.review") / "static" / "index.html").is_file()


def test_queue_crop_review_roundtrip(srv):
    base, settings, con = srv["url"], srv["settings"], srv["pipe"].con
    _s, _c, body = _get(base + "/api/queue")
    q = json.loads(body)
    assert q["name"] == "haul-numbers" and q["total"] == 20 and q["reviewer"] == "jp" and q["show_machine"] is False
    assert all(c["machine"] is None for i in q["items"] for c in i["cells"])
    cell = q["items"][0]["cells"][0]
    fid = cell["field_id"]
    for kind in ("cell", "row"):
        status, ctype, png = _get(f"{base}/crop?field_id={fid}&kind={kind}")
        assert status == 200 and ctype == "image/png" and png[:8] == b"\x89PNG\r\n\x1a\n"

    status, out = _post(base + "/api/review", {"field_id": fid, "verdict": "value", "value": "12", "note": ""})
    assert status == 200 and out["ok"] and out["applied"]
    reviews, skipped = load(settings.reviews)
    assert skipped == 0 and reviews[-1][1].field_id == fid and reviews[-1][1].value == "12"
    assert reviews[-1][1].reviewer == "jp" and reviews[-1][1].machine["backend"] in ("null", "ink")
    row = con.execute("SELECT value_final, has_value, review_status FROM doc_field WHERE field_id=?", (fid,)).fetchone()
    assert tuple(row) == ("12", 1, "reviewed")
    assert con.execute("SELECT trips, review_status FROM prod_haul WHERE haul_id=?", (fid,)).fetchone()[0] == 12

    _s, _c, body = _get(base + "/api/queue")
    q2 = json.loads(body)
    assert fid not in [c["field_id"] for i in q2["items"] for c in i["cells"]]
    assert q2["done"] == 1 and q2["total"] == 20 and len(q2["items"]) == len(q["items"]) - 1

    status, out = _post(base + "/api/review", {"field_id": q2["items"][0]["cells"][0]["field_id"], "verdict": "empty"})
    assert status == 200
    status, _c, body = _get(base + "/api/stats")
    st = json.loads(body)
    assert status == 200 and st["by_verdict"] == {"empty": 1, "value": 1} and st["by_reviewer"] == {"jp": 2}


def test_validation_errors(srv):
    base, con = srv["url"], srv["pipe"].con
    q = json.loads(_get(base + "/api/queue?name=pending&kind=handwritten_number")[2])
    fid = q["items"][0]["cells"][0]["field_id"]
    assert _post(base + "/api/review", {"field_id": fid, "verdict": "value", "value": "7a"})[0] == 400
    assert _post(base + "/api/review", {"field_id": fid, "verdict": "value", "value": ""})[0] == 400
    assert _post(base + "/api/review", {"field_id": fid, "verdict": "maybe", "value": "1"})[0] == 400
    assert _post(base + "/api/review", {"field_id": "no-such-p1:x:y:0", "verdict": "value", "value": "1"})[0] == 404
    check = con.execute("SELECT field_id FROM doc_field WHERE kind='checkmark' LIMIT 1").fetchone()[0]
    assert _post(base + "/api/review", {"field_id": check, "verdict": "value", "value": "1"})[0] == 400
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/crop?field_id=no-such-p1:x:y:0&kind=cell")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(base + "/nope")
    assert e.value.code == 404
    # 숫자 셀에 글자: 저장되지 않았다
    assert con.execute("SELECT COUNT(*) FROM doc_review WHERE field_id=?", (fid,)).fetchone()[0] == 0


def test_machine_values_only_in_pending(srv):
    base = srv["url"]
    for name in ("haul-numbers", "mismatch"):
        q = json.loads(_get(f"{base}/api/queue?name={name}")[2])
        assert q["show_machine"] is False
        assert "value_raw" not in json.dumps(q) and all(c["machine"] is None for i in q["items"] for c in i["cells"])
    q = json.loads(_get(base + "/api/queue?name=pending")[2])
    assert q["show_machine"] is True and all(c["machine"] is not None for i in q["items"] for c in i["cells"])
    assert len(json.loads(_get(base + "/api/queue?name=mismatch")[2])["items"]) > 0


def test_missing_aligned_image_is_a_clear_error(srv, tmp_path):
    app = srv["app"]
    other = ReviewApp(app.con, app.site, replace(srv["settings"], work_root=tmp_path / "elsewhere"), "jp")
    fid = app.con.execute("SELECT field_id FROM doc_field LIMIT 1").fetchone()[0]
    with pytest.raises(ApiError) as e:
        other.crop_png({"field_id": fid, "kind": "cell"})
    assert e.value.status == 409 and "정합 이미지" in str(e.value)
    with pytest.raises(ValueError):
        ReviewApp(app.con, app.site, srv["settings"], "")                 # 검수자 없이는 띄우지 않는다
    with pytest.raises(OSError, match="--port"):
        make_server(app, port=srv["httpd"].server_address[1])            # 쓰이고 있는 포트
