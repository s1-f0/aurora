"""Pins for the shader harness (arsenal/shaders/serve.py, render.py, headless.mjs): the lab server, the receipts, the verbs,
and the SwiftShader rule that keeps every frame off this host's GPU.

WHY THESE PINS. The harness renders hundreds of strangers' shaders on a machine with an AMD display-driver crash history,
and it serves a corpus of other people's code that must never leave this machine. So the tests guard four promises:

  * the server listens on 127.0.0.1 only, refuses port 8793 (Daniel's server), answers loopback Host names only, and
    serves nothing outside its roots however the path is spelled;
  * a renderer string that is not SwiftShader, or that names hardware, renders nothing: judge_renderer (Python) and
    judgeRenderer (headless.mjs) are twins, and both are run over the same strings here;
  * a receipt has one shape, and "ok" in it means every frame was written on a software renderer;
  * the verbs parse their arguments the way the contract names them.

And the house rules the harness keeps on the way: the probe page refuses hardware before any GL work, pictures of a
corpus shader never land outside state/, the driver loads nothing but the lab server on loopback, a burst stays under
three minutes counting launch, relaunch and shutdown, and the house lock is released only after our Chrome is dead.

No test launches Chrome or takes the house GPU lock (one test takes a lock of its own in a temp directory): the live
path is proved by running the verbs (see render.py's docstring). Every shader here is synthetic; the real corpus lives
only under state/ and no test reads it.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from arsenal.shaders import cli
from arsenal.shaders import corpus as C
from arsenal.shaders import render as R
from arsenal.shaders import serve as S

ROOT = Path(__file__).resolve().parents[1]
HEADLESS = ROOT / "arsenal" / "shaders" / "headless.mjs"
GPU_LOCK = ROOT / "arsenal" / "lanes" / "gpu_lock.mjs"
PROBE = ROOT / "arsenal" / "web" / "shaderlab" / "_harness_probe.html"
RUNTIME = ROOT / "arsenal" / "web" / "shaderlab" / "toy-runtime.js"
STATE = ROOT / "state"
NOT_STATE = ROOT / "design" / "vfx-sketches" / "never-written-by-a-test"  # tracked territory; no test may write here
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not on PATH")

SWIFTSHADER = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)"
AMD = "ANGLE (AMD, AMD Radeon RX 7900 XTX Direct3D11 vs_5_0 ps_5_0, D3D11)"
SECRET = "TOPSECRET-not-for-the-lab"

# (renderer string, should it render?) -- the same table drives both twins.
RENDERERS = [
    (SWIFTSHADER, True),
    ("ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (LLVM 10.0.0) (0x0000C0DE)), SwiftShader driver)", True),
    ("Google SwiftShader", True),
    (AMD, False),
    ("ANGLE (NVIDIA, NVIDIA GeForce RTX 4090 Direct3D11 vs_5_0 ps_5_0, D3D11)", False),
    ("ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)", False),
    ("SwiftShader, but on an AMD Radeon", False),  # a mixed string is hardware until proved otherwise
    ("ANGLE (Google, SwiftShader via Direct3D11)", False),
    ("WebKit WebGL", False),  # a masked renderer proves nothing
    ("", False),
    ("   ", False),
    (None, False),
]


def _shader(sid="abcDEF", name="Harness test", code=None, passes=None):
    code = code or "void mainImage(out vec4 o, in vec2 u) { o = vec4(u / iResolution.xy, 0.5, 1.0); }"
    passes = passes or [
        {"name": "Image", "type": "image", "code": code, "inputs": [], "outputs": [{"id": "4dfGRr", "channel": 0}]}
    ]
    return {
        "ver": "0.1",
        "info": {
            "id": sid,
            "name": name,
            "username": "someone",
            "date": "1700000000",
            "likes": 1,
            "viewed": 2,
            "tags": [],
            "description": "",
        },
        "renderpass": passes,
    }


def _png(path: Path, flat: bool = False) -> Path:
    from PIL import Image

    im = Image.new("RGB", (8, 4), (20, 30, 40))
    if not flat:
        for x in range(8):
            im.putpixel((x, 0), (x * 30, 0, 0))
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path)
    return path


# ------------------------------------------------------------------------------------------------- the lab server
@pytest.fixture
def lab(tmp_path):
    """A server on an ephemeral loopback port over temp roots, with a secret file just outside them."""
    (tmp_path / "secret.txt").write_text(SECRET, encoding="utf-8")
    lab_dir = tmp_path / "lab"
    lab_dir.mkdir(parents=True)
    (lab_dir / "render.html").write_text("<!doctype html><title>r</title>", encoding="utf-8")
    lib_dir = tmp_path / "glsl"  # the house library's stand-in (the real one is arsenal/web/glsl/)
    (lib_dir / "sdf").mkdir(parents=True)
    # Served bytes only: plain comments, so the fixture shares no token run with any corpus shader (licence line).
    (lib_dir / "sdf.glsl").write_text("// house library route probe: a flat file\n", encoding="utf-8")
    (lib_dir / "sdf" / "box.glsl").write_text("// house library route probe: a nested file\n", encoding="utf-8")
    corpus = tmp_path / "corpus"
    C.save_shader(_shader("abcDEF"), corpus)
    (corpus / "media").mkdir()
    (corpus / "media" / "0123abcd.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(64)))
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    (jobs / "local-x-0123456789.json").write_text(json.dumps(_shader()), encoding="utf-8")
    server = S.start_in_thread(0, lab=lab_dir, lib=lib_dir, corpus=corpus, jobs=jobs)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _get(server, path, method="GET", headers=None):
    """A raw request: http.client sends the path exactly as written (no client-side normalisation of '..')."""
    conn = http.client.HTTPConnection(S.HOST, server.server_address[1], timeout=10)
    try:
        conn.request(method, path, headers=headers or {})
        res = conn.getresponse()
        return res.status, dict(res.getheaders()), res.read()
    finally:
        conn.close()


def test_server_binds_loopback_only(lab):
    assert lab.server_address[0] == "127.0.0.1"
    assert lab.base_url.startswith("http://127.0.0.1:")


@pytest.mark.parametrize(
    ("host", "port"), [("0.0.0.0", 8797), ("localhost", 8797), ("::1", 8797), ("192.168.1.10", 8797), ("", 8797)]
)
def test_validate_bind_refuses_any_address_but_127_0_0_1(host, port):
    with pytest.raises(S.BindRefused):
        S.validate_bind(host, port)


def test_port_8793_is_refused_everywhere():
    with pytest.raises(S.BindRefused, match="8793"):
        S.validate_bind("127.0.0.1", 8793)
    with pytest.raises(S.BindRefused):
        S.ShaderServer(8793)  # refused before any socket is opened
    with pytest.raises(S.BindRefused):
        S.probe(8793)  # not even a health probe touches Daniel's server
    with pytest.raises(ValueError, match="is refused"):
        R.parse_port("8793")


def test_default_port_is_8797_and_allowed():
    assert S.DEFAULT_PORT == 8797
    S.validate_bind("127.0.0.1", S.DEFAULT_PORT)


def test_health_names_the_api_and_roots(lab):
    status, _, body = _get(lab, "/health")
    data = json.loads(body)
    assert status == 200
    assert data["api"] == S.SERVE_API
    assert "render.html" in data["lab_pages"]


@pytest.mark.parametrize(
    ("path", "needle"),
    [
        ("/lab/render.html", b"<title>r</title>"),
        ("/lib/sdf.glsl", b"a flat file"),
        ("/glsl/sdf.glsl", b"a flat file"),
        ("/lib/sdf/box.glsl", b"a nested file"),
        ("/glsl/sdf/box.glsl", b"a nested file"),
        ("/corpus/abcDEF/shader.json", b"Harness test"),
        ("/corpus/media/0123abcd.png", b"\x89PNG"),
        ("/jobs/local-x-0123456789.json", b"Harness test"),
    ],
)
def test_routes_serve_their_roots(lab, path, needle):
    status, headers, body = _get(lab, path)
    assert status == 200, (path, body[:200])
    assert needle in body
    assert headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.parametrize(
    "path",
    [
        "/lab/../secret.txt",
        "/lab/%2e%2e/secret.txt",
        "/lab/..%2fsecret.txt",
        "/lab/..%5csecret.txt",
        "/lab/glsl/../../secret.txt",
        "/lab/./render.html",
        "/lab//render.html",
        "/lab/%2e%2e%2f%2e%2e%2fsecret.txt",
        "/lab/C:/Windows/win.ini",
        "/lab/render.html::$DATA",
        "/lab/render.html%00.png",
        "/lib/../../secret.txt",
        "/lib/%2e%2e/render.html",
        "/glsl/../secret.txt",
        "/glsl/%2e%2e/secret.txt",
        "/glsl/..%2fsecret.txt",
        "/glsl/sdf/../../secret.txt",
        "/glsl/C:/Windows/win.ini",
        "/glsl/sdf.glsl%00.png",
        "/glsl",
        "/glsl/",
        "/corpus/../secret.txt",
        "/corpus/abcDEF/../../secret.txt",
        "/corpus/abc/shader.json",  # not a Shadertoy id
        "/corpus/..%2f..%2fsecret.txt/shader.json",
        "/corpus/abcDEF/meta.json",  # only shader.json is served from a shader's directory
        "/corpus/abcDEF/image.glsl",
        "/corpus/media/..%2f..%2fsecret.txt",
        "/corpus/media/../../secret.txt",
        "/corpus/sets.json",
        "/corpus/harvests/x.json",
        "/jobs/../secret.txt",
        "/jobs/..%2fsecret.txt",
        "/jobs/local-x-0123456789.txt",
        "/secret.txt",
        "/etc/passwd",
    ],
)
def test_traversal_and_off_route_paths_are_refused(lab, path):
    status, _, body = _get(lab, path)
    assert status in (403, 404), (path, status)
    assert SECRET.encode() not in body


def test_a_symlink_out_of_a_root_is_refused(lab, tmp_path):
    link = Path(lab.roots["lab"]) / "escape.txt"
    try:
        os.symlink(tmp_path / "secret.txt", link)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    status, _, body = _get(lab, "/lab/escape.txt")
    assert status == 404
    assert SECRET.encode() not in body


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1", True),
        ("127.0.0.1:8797", True),
        ("localhost", True),
        ("LOCALHOST:8797", True),
        ("[::1]:8797", True),
        ("evil.example", False),
        ("evil.example:8797", False),
        ("127.0.0.1.evil.example", False),
        ("localhost.evil.example:80", False),
        ("192.168.1.10:8797", False),
    ],
)
def test_host_header_must_name_loopback(lab, host, ok):
    assert S.host_is_loopback(host) is ok
    status, _, _ = _get(lab, "/lab/render.html", headers={"Host": host})
    assert status == (200 if ok else 403)


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_server_is_read_only(lab, method):
    status, headers, _ = _get(lab, "/lab/render.html", method=method)
    assert status == 405
    assert "GET" in headers.get("Allow", "")


def test_head_has_no_body_and_range_is_honoured(lab):
    status, headers, body = _get(lab, "/corpus/media/0123abcd.png", method="HEAD")
    assert status == 200
    assert body == b""
    assert headers["Content-Length"] == "72"
    status, headers, body = _get(lab, "/corpus/media/0123abcd.png", headers={"Range": "bytes=8-11"})
    assert status == 206
    assert body == bytes(range(4))
    assert headers["Content-Range"] == "bytes 8-11/72"
    status, _, _ = _get(lab, "/corpus/media/0123abcd.png", headers={"Range": "bytes=500-"})
    assert status == 416


def test_safe_file_unit(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f.txt").write_text("x", encoding="utf-8")
    assert S.safe_file(tmp_path, "a/f.txt") == (tmp_path / "a" / "f.txt").resolve()
    for bad in ["", "/a/f.txt", "a/../a/f.txt", "a\\f.txt", "C:/x", "a//f.txt", "a/./f.txt", "a/f.txt\x00", "a"]:
        assert S.safe_file(tmp_path, bad) is None, bad


def test_base_for_never_reuses_a_server_over_other_roots(lab):
    """A lab server over a test's temp roots must not be mistaken for the real one: render starts a private server."""
    server, base = S.base_for(lab.server_address[1])
    try:
        assert server is not None
        assert base != lab.base_url
        assert S.probe(server.server_address[1])["roots"]["corpus"] == str(C.CORPUS)
    finally:
        if server:
            server.shutdown()
            server.server_close()


def test_base_for_never_reuses_a_server_over_another_library(tmp_path):
    """Same lab, corpus and jobs, but a different house library: still not ours, so render starts a private server."""
    other = S.start_in_thread(0, lib=tmp_path)
    try:
        server, base = S.base_for(other.server_address[1])
        try:
            assert server is not None
            assert base != other.base_url
            assert S.probe(server.server_address[1])["roots"]["lib"] == str(S.LIB)
        finally:
            if server:
                server.shutdown()
                server.server_close()
    finally:
        other.shutdown()
        other.server_close()


def test_the_house_library_is_arsenal_web_glsl_on_lib_and_glsl():
    """The house library moved to arsenal/web/glsl/ (build spec, ruling 1); /lib/ serves it and /glsl/ is its alias,
    the path modlib.js's own usage note fetches (/glsl/modlib.js, /glsl/index.json)."""
    assert S.LIB == ROOT / "arsenal" / "web" / "glsl"
    assert "/glsl/<file>" in S.ROUTES
    assert "/lib/<file>" in S.ROUTES
    server = S.start_in_thread(0)
    try:
        assert Path(server.roots["lib"]) == S.LIB
        assert server.health()["roots"]["lib"] == str(S.LIB)
        on_disk = (S.LIB / "modlib.js").read_bytes()
        for route in ("/lib/modlib.js", "/glsl/modlib.js"):
            status, headers, body = _get(server, route)
            assert status == 200, route
            assert body == on_disk, route
            assert headers["Content-Type"].startswith("text/javascript")
        status, _, _ = _get(server, "/lab/glsl/modlib.js")  # the old home is gone, and nothing serves it
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_serve_banner_says_who_the_server_is_for(lab):
    lines = S.banner(lab)
    assert any(S.HEADLESS_ONLY in line for line in lines)
    assert S.HEADLESS_ONLY == "for the headless driver; do not open in a browser"
    assert any("/glsl/" in line for line in lines)
    assert S.HEADLESS_ONLY in R.__doc__  # the render module's docstring says it of the serve verb too


# --------------------------------------------------------------------------------------- the SwiftShader rule
@pytest.mark.parametrize(("renderer", "ok"), RENDERERS)
def test_judge_renderer_python(renderer, ok):
    got, reason = R.judge_renderer(renderer)
    assert got is ok
    assert (reason is None) is ok
    if not ok:
        with pytest.raises(R.NotSoftwareRenderer):
            R.assert_software_renderer(renderer)
    else:
        assert R.assert_software_renderer(renderer) == renderer


def _node(code: str):
    """Run an ES module in node; its last stdout line is JSON, returned parsed."""
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60, cwd=str(ROOT)
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def _node_module(expr: str):
    """Evaluate an expression against headless.mjs's exports in node (importing it runs no CLI and no Chrome)."""
    url = HEADLESS.resolve().as_uri()
    return _node(f"import * as H from {json.dumps(url)}; console.log(JSON.stringify({expr}));")


@needs_node
def test_judge_renderer_twins_agree():
    strings = [r for r, _ in RENDERERS]
    got = _node_module(f"{json.dumps(strings)}.map((s) => H.judgeRenderer(s))")
    for (renderer, ok), js in zip(RENDERERS, got, strict=False):
        assert js["ok"] is ok, renderer
        assert js["ok"] is R.judge_renderer(renderer)[0], renderer
    assert _node_module("H.HARDWARE_HINT.source") == R.HARDWARE_HINT.pattern


@needs_node
def test_driver_cli_check_renderer():
    out = subprocess.run([NODE, str(HEADLESS), "--check-renderer", AMD], capture_output=True, text=True, timeout=60)
    assert json.loads(out.stdout)["ok"] is False
    out = subprocess.run(
        [NODE, str(HEADLESS), "--check-renderer", SWIFTSHADER], capture_output=True, text=True, timeout=60
    )
    assert json.loads(out.stdout) == {"ok": True, "reason": None}


@needs_node
def test_driver_flags_force_swiftshader_and_its_own_profile():
    flags = _node_module("H.chromeFlags('PROFILE')")
    for required in (
        "--headless=new",
        "--use-angle=swiftshader",
        "--enable-unsafe-swiftshader",
        "--disable-d3d11",
        "--remote-debugging-port=0",
        "--user-data-dir=PROFILE",
    ):
        assert required in flags
    assert not any(f.startswith(("--use-gl=", "--enable-gpu", "--ignore-gpu-blocklist")) for f in flags)
    assert "--version" not in flags  # chrome.exe --version opens a visible window on Windows


@needs_node
def test_driver_and_python_share_their_constants():
    c = _node_module(
        "{plan: H.PLAN_API, result: H.RESULT_API, exit: H.EXIT, burst: H.BURST_MS, "
        "hold: H.LOCK_HOLD_MS, chrome: H.CHROME_EXE}"
    )
    assert c["plan"] == R.PLAN_API
    assert c["result"] == R.RESULT_API
    assert c["exit"]["refused"] == R.EXIT_REFUSED
    assert c["exit"]["lock"] == R.EXIT_LOCK
    assert R.BURST_CAP_S * 1000 <= c["burst"] < 180_000  # GPU-LOCK.md: bursts under three minutes
    assert c["hold"] > c["burst"]
    assert c["chrome"] == r"C:\Program Files\Google\Chrome\Application\chrome.exe"


@needs_node
def test_driver_refuses_a_bad_plan_without_launching_anything(tmp_path):
    out = subprocess.run([NODE, str(HEADLESS)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 2
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"api": "something-else", "jobs": []}), encoding="utf-8")
    out = subprocess.run(
        [NODE, str(HEADLESS), "--plan", str(plan), "--result", str(tmp_path / "r.json")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 2
    assert not (tmp_path / "r.json").exists()


# ------------------------------------------------------------------------------- the probe page's own renderer gate
# The probe page's script, run in node against a fake WebGL2 context that reports a given renderer string and records
# every GL call. Nothing here reaches a GPU: there is no browser, only the page's own logic.
_PROBE_RUNNER = r"""
import { readFileSync } from "node:fs";
import vm from "node:vm";
const html = readFileSync(__PROBE__, "utf8");
const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
const out = [];
for (const renderer of __RENDERERS__) {
  const calls = [];
  const consts = { VERTEX_SHADER: 0x8B31, FRAGMENT_SHADER: 0x8B30, COMPILE_STATUS: 0x8B81, LINK_STATUS: 0x8B82,
                   TRIANGLES: 4, NO_ERROR: 0, RENDERER: 0x1F01 };
  const gl = new Proxy({}, { get(_, name) {
    if (name in consts) return consts[name];
    return (...args) => {
      calls.push(String(name));
      if (name === "getExtension") return args[0] === "WEBGL_debug_renderer_info" ? { UNMASKED_RENDERER_WEBGL: 0x9246 } : null;
      if (name === "getParameter") return args[0] === 0x9246 || args[0] === 0x1F01 ? renderer : null;
      if (name === "getError") return 0;
      if (name === "getShaderParameter" || name === "getProgramParameter") return true;
      if (name === "getShaderInfoLog" || name === "getProgramInfoLog") return "";
      return {};
    };
  } });
  const canvas = { width: 0, height: 0, style: {}, getContext: () => gl, toDataURL: () => "data:image/png;base64,AA==" };
  const sandbox = { window: {}, location: { search: "?t=0,1&w=8&h=4" }, document: { getElementById: () => canvas },
                    URLSearchParams, performance, console, setTimeout: () => 0, fetch: () => new Promise(() => {}) };
  vm.runInNewContext(script, sandbox);
  const r = sandbox.window.__render;
  out.push({ renderer, done: r.done, ok: r.ok, refused: r.refused === true, errors: r.errors, frames: r.frames.length,
             gl_work: calls.filter((c) => /^(createShader|shaderSource|compileShader|createProgram|linkProgram|useProgram|drawArrays|clear)$/.test(c)) });
}
console.log(JSON.stringify(out));
"""


@needs_node
def test_probe_page_refuses_hardware_before_any_gl_work():
    """The probe page is served under /lab/, so a person could open it in an ordinary browser on this host's AMD GPU.
    It must refuse there before it compiles or draws anything, by the same rule as the two twins."""
    code = _PROBE_RUNNER.replace("__PROBE__", json.dumps(str(PROBE))).replace(
        "__RENDERERS__", json.dumps([r for r, _ in RENDERERS])
    )
    for (_renderer, ok), got in zip(RENDERERS, _node(code), strict=False):
        if ok:
            assert got["ok"], got
            assert not got["refused"], got
            assert got["frames"] == 2, got
            assert "drawArrays" in got["gl_work"], got
        else:
            assert got["done"], got
            assert not got["ok"], got
            assert got["refused"], got
            assert got["gl_work"] == [], got
            assert got["frames"] == 0, got
            assert any("refused" in e for e in got["errors"]), got


def test_probe_page_gate_is_the_twins_rule_and_comes_first():
    text = PROBE.read_text(encoding="utf-8")
    m = re.search(r"const HARDWARE_HINT = /(.+)/i;", text)
    assert m
    assert m.group(1) == R.HARDWARE_HINT.pattern
    gate = text.index("judgeRenderer(result.renderer)")
    for gl_work in ("gl.compileShader(", "gl.linkProgram(", "gl.drawArrays("):
        assert gate < text.index(gl_work), gl_work
    assert S.HEADLESS_ONLY in text


# ------------------------------------------------------------------------------- what the driver agrees to load
def _url(port=51234, src="/corpus/abcDEF/shader.json", host="127.0.0.1", page="/lab/render.html", scheme="http"):
    base = f"{scheme}://{host}:{port}" if port else f"{scheme}://{host}"
    return f"{base}{page}?src={base}{src}&t=0&w=8&h=4&fps=60&media={base}/corpus/media/&budget=25000"


# Under state/ by a path that crosses none of this checkout's links: state/arsenal may lead into the world's state/,
# which the world-state pins at the end of this file cover. These rows pin the driver's rule, not this machine's layout.
IN_STATE = str(STATE / "driver-pins" / "renders" / "abcDEF")
DRIVER_JOBS = [  # (job, should the driver load it?)
    ({"url": _url(), "out_dir": IN_STATE, "prefix": ""}, True),
    ({"url": _url(), "out_dir": str(STATE / "driver-pins" / "check" / "x"), "prefix": "abcDEF_"}, True),
    ({"url": _url(port=8793), "out_dir": IN_STATE}, False),  # Daniel's server
    ({"url": _url(host="localhost"), "out_dir": IN_STATE}, False),  # 127.0.0.1, spelled exactly
    ({"url": _url(host="evil.example"), "out_dir": IN_STATE}, False),
    ({"url": _url(host="192.168.1.10"), "out_dir": IN_STATE}, False),
    ({"url": _url(scheme="https"), "out_dir": IN_STATE}, False),
    ({"url": _url(port=None), "out_dir": IN_STATE}, False),  # an explicit port, always
    ({"url": _url(page="/corpus/abcDEF/shader.json"), "out_dir": IN_STATE}, False),  # a lab page, nothing else
    ({"url": "http://user:pw@127.0.0.1:51234/lab/render.html", "out_dir": IN_STATE}, False),
    ({"url": "file:///C:/Windows/win.ini", "out_dir": IN_STATE}, False),
    ({"url": "not a url", "out_dir": IN_STATE}, False),
    ({"url": _url().replace("src=http://127.0.0.1:51234", "src=http://evil.example"), "out_dir": IN_STATE}, False),
    ({"url": _url().replace("media=http://127.0.0.1:51234", "media=http://evil.example"), "out_dir": IN_STATE}, False),
    ({"url": _url(), "out_dir": str(NOT_STATE)}, False),  # a corpus picture in a tracked folder
    ({"url": _url(), "out_dir": str(NOT_STATE), "allow_tracked": True}, False),  # ... and no flag lets it
    ({"url": _url(), "out_dir": str(STATE / ".." / "design")}, False),
    ({"url": _url(), "out_dir": "state/arsenal/shaders/renders/x"}, False),  # absolute paths only
    ({"url": _url(), "out_dir": None}, False),
    ({"url": _url(), "out_dir": IN_STATE, "prefix": "../../design/x_"}, False),  # a prefix is a name, not a path
    ({"url": _url(src="/jobs/local-x-0123456789.json"), "out_dir": str(NOT_STATE), "allow_tracked": True}, True),
    ({"url": _url(src="/jobs/local-x-0123456789.json"), "out_dir": str(NOT_STATE)}, False),
    ({"url": _url(src="/jobs/local-x-0123456789.json"), "out_dir": str(NOT_STATE), "allow_tracked": "yes"}, False),
    # serve.py unquotes a path before it routes it, so a corpus src spelled with escapes (%2F, %63 for c) is served from
    # the corpus all the same: still a corpus picture, and no flag lets it out. A path that cannot be unquoted is refused.
    ({"url": _url(src="/corpus%252FabcDEF/shader.json"), "out_dir": str(NOT_STATE), "allow_tracked": True}, False),
    ({"url": _url(src="/%2563orpus/abcDEF/shader.json"), "out_dir": str(NOT_STATE), "allow_tracked": True}, False),
    ({"url": _url(src="/jobs/local-x-%25E0%25A4%25A.json"), "out_dir": str(NOT_STATE), "allow_tracked": True}, False),
]


@needs_node
def test_driver_loads_only_the_lab_on_loopback_and_writes_only_under_state():
    got = _node_module(f"{json.dumps([j for j, _ in DRIVER_JOBS])}.map((j) => H.jobRefusal(j))")
    for (job, ok), why in zip(DRIVER_JOBS, got, strict=False):
        assert (why is None) is ok, (job, why)
    assert {int(k) for k in _node_module("H.FORBIDDEN_PORTS")} == set(S.FORBIDDEN_PORTS)
    assert _node_module("H.STATE_ROOT").rstrip("\\/").lower() == str(STATE).lower()


@needs_node
def test_driver_refuses_a_plan_with_a_bad_job_before_the_lock_or_chrome(tmp_path):
    """A refused plan is answered before the GPU lock is taken or Chrome launched. The plan names a Chrome that does not
    exist, so even a driver that skipped the check could only fail on that, never render."""
    plan = tmp_path / "plan.json"
    jobs = [
        {"key": "aaaaaa", "url": _url(), "out_dir": IN_STATE, "prefix": "", "timeout_ms": 30000, "w": 8, "h": 4},
        {"key": "abcDEF", "url": _url(), "out_dir": str(NOT_STATE), "prefix": "", "timeout_ms": 30000, "w": 8, "h": 4},
    ]
    plan.write_text(
        json.dumps(
            {
                "api": R.PLAN_API,
                "label": "pytest",
                "lock_wait_ms": 0,
                "jobs": jobs,
                "chrome": str(tmp_path / "no-chrome.exe"),
            }
        ),
        encoding="utf-8",
    )
    result = tmp_path / "r.json"
    out = subprocess.run(
        [NODE, str(HEADLESS), "--plan", str(plan), "--result", str(result)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == R.EXIT_REFUSED, out.stdout + out.stderr
    r = json.loads(result.read_text(encoding="utf-8"))
    assert r["api"] == R.RESULT_API
    assert "abcDEF" in r["refused"]
    assert r["lock"] is None
    assert [j["status"] for j in r["jobs"]] == ["refused", "refused"]
    assert not NOT_STATE.exists()


# ----------------------------------------------------------------------------------- the burst and the lock
@needs_node
def test_lock_options_take_signals_away_and_honour_a_zero_wait():
    got = _node_module(
        "[{}, {lock_wait_ms: 0}, {lock_wait_ms: 90000}, {lock_wait_ms: null}, {lock_wait_ms: -5}, "
        "{lock_wait_ms: 'abc'}, {label: 'x'}].map((p) => H.lockOptions(p))"
    )
    assert [g["maxWaitMs"] for g in got] == [600000, 0, 90000, 600000, 600000, 600000, 600000]
    assert all(g["signals"] is False for g in got)
    hold = _node_module("H.LOCK_HOLD_MS")
    assert all(g["maxHoldMs"] == hold for g in got)
    assert got[6]["label"] == "x"


_GUARD_RUNNER = r"""
import * as H from __HEADLESS__;
import { acquireGpuLock } from __GPU_LOCK__;
import { existsSync, writeFileSync } from "node:fs";
const [mode, dir, log] = __ARGS__;
const seen = [];
const report = (code) => writeFileSync(log, JSON.stringify({ code, seen }));
const kill = () => seen.push(["kill", existsSync(dir)]);
if (mode === "old") {  // the wiring before the fix: the lock's own signal handlers, ours registered after acquire
  const gpu = await acquireGpuLock({ dir, label: "pytest guard", yieldToJam: false, maxWaitMs: 5000, log: () => {} });
  process.once("SIGINT", () => { kill(); gpu.release(); report(130); process.exit(130); });
  process.emit("SIGINT");
} else {
  const gpu = await acquireGpuLock({ ...H.lockOptions({ label: "pytest guard", lock_wait_ms: 5000 }), dir,
                                     yieldToJam: false, log: () => {} });
  H.guardHolder({ kill, release: () => { gpu.release(); seen.push(["release", existsSync(dir)]); },
                  exit: (code) => { report(code); process.exit(code); } });
  if (mode === "signal") process.emit("SIGINT");
  else { process.on("exit", (code) => report(code)); process.exit(1); }  // a crash exit: no finally runs
}
"""


@needs_node
@pytest.mark.parametrize(("mode", "lock_at_kill"), [("old", False), ("signal", True), ("exit", True)])
def test_our_chrome_dies_before_the_lock_is_released(tmp_path, mode, lock_at_kill):
    """On a signal or a crash exit, kill first and release second, so no waiter (a load-sensitive jam lane) takes the
    lock while our SwiftShader Chrome still burns CPU. "old" replays the wiring before the fix and shows the defect:
    the lock's own prepended handler released first. The lock here is a private one in a temp directory."""
    lock_dir, log = tmp_path / "gpu-render.lock", tmp_path / "seen.json"
    code = (
        _GUARD_RUNNER.replace("__HEADLESS__", json.dumps(HEADLESS.resolve().as_uri()))
        .replace("__GPU_LOCK__", json.dumps(GPU_LOCK.resolve().as_uri()))
        .replace("__ARGS__", json.dumps([mode, str(lock_dir), str(log)]))
    )
    env = {k: v for k, v in os.environ.items() if k != "ARSENAL_GPU_LOCK_TOKEN"}  # never inherit a live lock
    subprocess.run(
        [NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60, cwd=str(ROOT), env=env
    )
    seen = json.loads(log.read_text(encoding="utf-8"))
    assert seen["seen"][0] == ["kill", lock_at_kill], seen
    assert not lock_dir.exists()
    if mode != "old":
        assert seen["seen"][:2] == [["kill", True], ["release", False]]
        assert seen["code"] in (130, 1)


@needs_node
def test_burst_budget_counts_launch_relaunch_and_shutdown():
    c = _node_module("""({
      burst: H.BURST_MS, margin: H.SHUTDOWN_MARGIN_MS, reserve: H.LAUNCH_RESERVE_MS, devtools: H.DEVTOOLS_WAIT_MS,
      w: [[0, 300, 30000], [0, 50000, 165000], [0, 50000, 30000], [3, 100000, 30000], [3, 130000, 30000],
          [0, 169000, 30000], [0, 160000, 30000]].map(([index, elapsedMs, timeoutMs]) =>
            ({ index, elapsedMs, timeoutMs, ...H.jobWindow({ index, elapsedMs, timeoutMs }) })),
      relaunch: [0, 60000, 100000, 106000, 150000].map((e) => H.mayRelaunch(e)),
      clip: [H.clipBudget("http://127.0.0.1:1/lab/render.html?src=x&budget=145000&t=0", 100000),
             H.clipBudget("http://127.0.0.1:1/lab/render.html?src=x&budget=25000&t=0", 100000)],
    })""")
    burst, margin = c["burst"], c["margin"]
    assert burst < 180_000
    assert margin >= 13_000
    assert c["reserve"] >= c["devtools"] + 30_000  # a launch: DevTools port, then two 15 s software checks
    for w in c["w"]:
        if w["start"]:
            assert w["elapsedMs"] + w["deadlineMs"] + margin <= burst, w  # no job's deadline eats the shutdown
            assert w["deadlineMs"] <= w["timeoutMs"]
    first, first_late, first_short, later_fits, later_late, first_none, first_tight = c["w"]
    assert first["start"]
    assert first["deadlineMs"] == 30000
    assert first_late["start"]
    assert first_late["deadlineMs"] == burst - margin - 50000
    assert first_short["start"]
    assert first_short["deadlineMs"] == 30000
    assert later_fits["start"]
    assert later_fits["deadlineMs"] == 30000
    assert not later_late["start"]  # a later job starts only if its whole deadline fits
    assert not first_none["start"]
    assert first_tight["start"] == (burst - margin - 160000 >= 1000)
    assert c["relaunch"] == [e + c["reserve"] + margin <= burst for e in (0, 60000, 100000, 106000, 150000)]
    assert c["clip"][0].endswith("budget=95000&t=0")
    assert c["clip"][1].endswith("budget=25000&t=0")
    assert burst - margin >= R.BURST_CAP_S * 1000  # render.py never asks a job for more than a burst can give


def test_the_kill_timer_never_fires_inside_a_burst_that_keeps_its_rules():
    """render.py's last-resort kill of the driver: the lock wait asked for, plus one jam-lane poll (60 s) and one jam scan
    (up to 30 s) the waiter may still be in when its wait runs out, plus a whole burst and the slowest shutdown."""
    for lock_wait_ms in (0, 60_000, 600_000):
        assert R.driver_limit_s(lock_wait_ms) * 1000 >= lock_wait_ms + 60_000 + 30_000 + 170_000 + 30_000


@needs_node
def test_default_fps_is_shadertoys_60_as_in_the_runtime():
    """Stills step at Shadertoy's ~60 fps: at t=4 iFrame is 240, not 120 (Hex Glitch and Dry ice count frames)."""
    assert R.DEFAULT_FPS == 60
    url = RUNTIME.resolve().as_uri()
    assert _node(f"import * as T from {json.dumps(url)}; console.log(JSON.stringify(T.DEFAULT_FPS));") == R.DEFAULT_FPS


# ----------------------------------------------------------------------------------------------------- receipts
def _source(tmp_path):
    meta = C.meta_for(_shader())
    return R.Source(
        key="abcDEF", kind="corpus", path=tmp_path / "shader.json", src="/corpus/abcDEF/shader.json", meta=meta
    )


def _row(tmp_path, renderer=SWIFTSHADER, times=(0.0, 1.5), status="done", page_ok=True):
    frames = [
        {
            "t": t,
            "ms": 4.5,
            "file": str(_png(tmp_path / f"t{t:.2f}.png")),
            "bytes": 100,
            "frame": i * 45,
            "frames": 1 + i * 45,
            "reset": i == 0,
        }
        for i, t in enumerate(times)
    ]
    return {
        "key": "abcDEF",
        "status": status,
        "frames": frames,
        "console": [],
        "error": None,
        "wall_ms": 321,
        "render": {
            "done": True,
            "ok": page_ok,
            "renderer": renderer,
            "frames": [{"t": t} for t in times],
            "compile": {
                "ok": page_ok,
                "passes": [
                    {
                        "name": "Image",
                        "ok": page_ok,
                        "firstError": None if page_ok else "ERROR: 0:3: 'x' : undeclared identifier",
                    }
                ],
            },
            "substituted": [{"src": "/media/a/x.png", "reason": "missing media: procedural stand-in"}],
            "unsupported": [],
            "errors": [],
        },
    }


def _merged(renderer=SWIFTSHADER):
    return {
        "run_dir": "runs/x",
        "software": {"ok": True, "webgl": {"renderer": renderer}, "feature_status": {}},
        "chrome": "Chrome/155.0.0.0",
        "refused": None,
        "error": None,
        "bursts": [{"n": 1, "lock": {"wait_ms": 800}, "chrome_launches": [{"start_ms": 300}]}],
    }


def test_receipt_schema_for_a_good_render(tmp_path):
    request = {"t": [0.0, 1.5], "w": 480, "h": 270, "fps": 30, "page": "render.html", "timeout_ms": 120000}
    rec = R.build_receipt(_source(tmp_path), request, _row(tmp_path), _merged(), "2026-10-13T00:00:00")
    assert R.validate_receipt(rec) == []
    assert rec["ok"] is True
    assert rec["status"] == "done"
    assert rec["software_renderer"] is True
    assert [f["t"] for f in rec["frames"]] == [0.0, 1.5]
    assert rec["frames"][1]["frames_walked"] == 46
    assert rec["frames"][0]["reset"] is True
    assert rec["frames"][0]["flat"] is False
    assert rec["first_error"] is None
    assert rec["license"]
    assert rec["author"] == "someone"
    assert rec["substituted"]
    assert rec["timings"]["lock_wait_ms"] == [800]
    json.dumps(rec)  # a receipt is JSON


def test_receipt_refuses_frames_from_a_hardware_renderer(tmp_path):
    request = {"t": [0.0, 1.5], "w": 480, "h": 270, "fps": 30, "page": "render.html", "timeout_ms": 120000}
    rec = R.build_receipt(_source(tmp_path), request, _row(tmp_path, renderer=AMD), _merged(AMD), "2026-10-13T00:00:00")
    assert R.validate_receipt(rec) == []
    assert rec["ok"] is False
    assert rec["status"] == "refused"
    assert rec["frames"] == []
    assert rec["software_renderer"] is False
    assert any("not counted" in e for e in rec["errors"])


def test_receipt_is_not_ok_when_a_frame_is_missing_or_compile_failed(tmp_path):
    request = {"t": [0.0, 1.5, 4.0], "w": 480, "h": 270, "fps": 30, "page": "render.html", "timeout_ms": 120000}
    rec = R.build_receipt(_source(tmp_path), request, _row(tmp_path), _merged(), "2026-10-13T00:00:00")
    assert rec["ok"] is False
    assert R.validate_receipt(rec) == []
    request["t"] = [0.0]
    rec = R.build_receipt(
        _source(tmp_path), request, _row(tmp_path, times=(0.0,), page_ok=False), _merged(), "2026-10-13T00:00:00"
    )
    assert rec["ok"] is False
    assert "undeclared identifier" in rec["first_error"]


def test_validate_receipt_catches_bad_shapes(tmp_path):
    assert "missing api" in R.validate_receipt({})
    request = {"t": [0.0], "w": 480, "h": 270, "fps": 30, "page": "render.html", "timeout_ms": 120000}
    rec = R.build_receipt(_source(tmp_path), request, _row(tmp_path, times=(0.0,)), _merged(), "2026-10-13T00:00:00")
    forged = dict(rec, renderer=AMD, software_renderer=False)
    assert "ok without a software renderer" in R.validate_receipt(forged)
    assert any("status" in p for p in R.validate_receipt(dict(rec, status="finished")))
    assert any("frames[0]" in p for p in R.validate_receipt(dict(rec, frames=[{"t": "zero"}])))


def test_frame_facts_reads_flat_frames(tmp_path):
    assert R.frame_facts(str(_png(tmp_path / "flat.png", flat=True)))["flat"] is True
    assert R.frame_facts(str(_png(tmp_path / "busy.png")))["flat"] is False
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png")
    assert "decode_error" in R.frame_facts(str(bad))
    assert R.frame_facts(None) == {"mean_luma": None, "flat": None}


def test_census_row_and_first_error(tmp_path):
    meta = C.meta_for(_shader())
    row = R.census_row(meta, _row(tmp_path, times=(0.0,)), _merged())
    assert row["ok"] is True
    assert row["ms_per_frame"] == 4.5
    assert row["substituted"]
    bad = R.census_row(meta, _row(tmp_path, times=(0.0,), page_ok=False), _merged())
    assert bad["ok"] is False
    assert bad["pass_errors"][0]["pass"] == "Image"
    hw = R.census_row(meta, _row(tmp_path, times=(0.0,), renderer=AMD), _merged(AMD))
    assert hw["ok"] is False
    assert hw["status"] == "refused"
    assert hw["png"] is None
    timeout = R.census_row(meta, {"status": "timeout", "error": "the page did not finish"}, _merged())
    assert timeout["ok"] is False
    assert timeout["first_error"] == "the page did not finish"


def test_census_cost_is_per_drawn_frame_not_per_still(tmp_path):
    """A still at t=1 and 30 fps is 31 drawn frames; its ms is the whole walk, so one frame costs ms / 31."""
    row = _row(tmp_path, times=(1.0,))
    row["frames"][0].update(ms=310, frames=31)
    got = R.census_row(C.meta_for(_shader()), row, _merged())
    assert got["ms_per_frame"] == 10.0
    assert got["ms_per_still"] == 310
    assert got["frames_walked"] == 31
    del row["frames"][0]["frames"]  # the probe page reports no walk: each still is one frame
    assert R.census_row(C.meta_for(_shader()), row, _merged())["ms_per_frame"] == 310


def test_jumped_stills_and_the_warm_frame_reach_receipt_and_census(tmp_path):
    """A shader without history jumps to each t (one drawn frame); render.js times one warm frame after the stills.
    Both facts ride from the page's report into the receipt and the census row."""
    row = _row(tmp_path, times=(1.0, 4.0))
    for f in row["frames"]:
        f.update(frames=1, reset=False, jumped=True)
    row["render"].update(warmMs=12.5, history=False)
    request = {"t": [1.0, 4.0], "w": 480, "h": 270, "fps": 30, "page": "render.html", "timeout_ms": 120000}
    rec = R.build_receipt(_source(tmp_path), request, row, _merged(), "2026-10-13T00:00:00")
    assert R.validate_receipt(rec) == []
    assert all(f["jumped"] is True and f["frames_walked"] == 1 for f in rec["frames"])
    assert rec["timings"]["warm_ms"] == 12.5
    assert rec["history"] is False
    got = R.census_row(C.meta_for(_shader()), row, _merged())
    assert got["warm_ms"] == 12.5
    assert got["history"] is False
    assert got["frames_walked"] == 2
    row["render"]["warmMs"] = None  # no budget left for the warm frame: absent, not zero
    assert R.census_row(C.meta_for(_shader()), row, _merged())["warm_ms"] is None


def test_page_url_names_parity_only_when_it_is_off():
    on = R.page_url("http://127.0.0.1:8797", "render.html", "/corpus/abc/shader.json", [1.0], (480, 270), 30, 9000)
    off = R.page_url(
        "http://127.0.0.1:8797", "render.html", "/corpus/abc/shader.json", [1.0], (480, 270), 30, 9000, parity=False
    )
    assert "parity" not in on
    assert "parity=0" in off


def test_contact_sheet(tmp_path):
    frames = [
        {"t": 0.0, "png": str(_png(tmp_path / "a.png"))},
        {"t": 1.0, "png": str(_png(tmp_path / "b.png"))},
        {"t": 2.0, "png": None},
    ]
    out = R.contact_sheet(frames, "synthetic | test", tmp_path / "sheet.png", cols=2, cell=(32, 18))
    from PIL import Image

    with Image.open(out) as im:
        assert im.size == (6 + 2 * (32 + 6), 26 + 6 + 2 * (18 + 6))


# -------------------------------------------------------------------------------------------- bursts and plans
def _job(key):
    return R.Job(
        key, f"http://127.0.0.1:1/lab/render.html?src=/corpus/{key}/shader.json", Path("out"), "", 30000, (320, 180)
    )


def test_plan_matches_the_driver_contract():
    plan = R.build_plan("label", [_job("aaaaaa")], 60000)
    assert plan["api"] == R.PLAN_API
    assert plan["lock_wait_ms"] == 60000
    assert set(plan["jobs"][0]) == {
        "key",
        "url",
        "out_dir",
        "prefix",
        "timeout_ms",
        "w",
        "h",
        "screenshot",
        "allow_tracked",
    }
    assert plan["jobs"][0]["allow_tracked"] is False


def test_run_jobs_splits_work_into_bursts(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(R, "BURST_GAP_S", 0)
    calls = []

    def fake_burst(plan, run_dir, n, lock_wait_ms, quiet):
        keys = [j["key"] for j in plan["jobs"]]
        calls.append(keys)
        rows = [{"key": keys[0], "status": "done"}] + [{"key": k, "status": "deferred"} for k in keys[1:]]
        return 0, {"jobs": rows, "software": {"ok": True}, "chrome": {"version": "Chrome/155"}, "lock": {"wait_ms": 1}}

    monkeypatch.setattr(R, "_run_burst", fake_burst)
    merged = R.run_jobs([_job("aaaaaa"), _job("bbbbbb"), _job("cccccc")], "t", quiet=True)
    assert calls == [["aaaaaa", "bbbbbb", "cccccc"], ["bbbbbb", "cccccc"], ["cccccc"]]
    assert all(r["status"] == "done" for r in merged["rows"].values())
    assert len(merged["bursts"]) == 3


def test_run_jobs_stops_on_refusal_and_on_no_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(R, "BURST_GAP_S", 0)
    monkeypatch.setattr(
        R,
        "_run_burst",
        lambda plan, *a: (
            R.EXIT_REFUSED,
            {
                "refused": "the renderer is not SwiftShader",
                "jobs": [{"key": j["key"], "status": "refused"} for j in plan["jobs"]],
            },
        ),
    )
    merged = R.run_jobs([_job("aaaaaa"), _job("bbbbbb")], "t", quiet=True)
    assert merged["refused"]
    assert len(merged["bursts"]) == 1

    monkeypatch.setattr(
        R, "_run_burst", lambda plan, *a: (0, {"jobs": [{"key": j["key"], "status": "deferred"} for j in plan["jobs"]]})
    )
    merged = R.run_jobs([_job("aaaaaa")], "t", quiet=True)
    assert len(merged["bursts"]) == 1
    assert "no job" in merged["error"]


def test_page_url_carries_the_contract_query():
    url = R.page_url(
        "http://127.0.0.1:8797", "render.html", "/corpus/abcDEF/shader.json", [0, 1.5, 4], (480, 270), 30, 115000
    )
    assert url.startswith("http://127.0.0.1:8797/lab/render.html?")
    for part in (
        "src=http://127.0.0.1:8797/corpus/abcDEF/shader.json",
        "t=0,1.5,4",
        "w=480",
        "h=270",
        "fps=30",
        "media=http://127.0.0.1:8797/corpus/media/",
        "budget=115000",
    ):
        assert part in url


# --------------------------------------------------------------------------------------------- verbs and sources
@pytest.fixture
def parser():
    ap = argparse.ArgumentParser(prog="shader")
    sub = ap.add_subparsers(dest="verb", required=True)
    for register in (R.register_render, R.register_check, R.register_filmstrip, R.register_serve):
        register(sub)
    return ap


def test_verbs_register_into_the_cli():
    for name in ("render", "check", "filmstrip", "serve"):
        assert name in cli.VERBS


def test_render_verb_parsing(parser):
    ns = parser.parse_args(["render", "abcDEF", "--t", "0,1.5,4", "--size", "640x360", "--out", "x"])
    assert ns.t == [0.0, 1.5, 4.0]
    assert ns.size == (640, 360)
    assert ns.out == "x"
    assert ns.fps == R.DEFAULT_FPS
    ns = parser.parse_args(["render", "sketch.frag"])
    assert ns.t == R.DEFAULT_T
    assert ns.size == R.DEFAULT_SIZE
    assert ns.page == "render.html"
    assert ns.port == 8797
    for bad in (
        ["render", "x", "--t", "-1"],
        ["render", "x", "--t", "nan"],
        ["render", "x", "--size", "480"],
        ["render", "x", "--size", "0x10"],
        ["render", "x", "--fps", "0"],
        ["render", "x", "--port", "8793"],
        ["render", "x", "--page", "../secret.html"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(bad)


def test_check_filmstrip_serve_parsing(parser):
    ns = parser.parse_args(["check", "aaaaaa", "bbbbbb", "--set", "loved"])
    assert ns.ids == ["aaaaaa", "bbbbbb"]
    assert ns.set == "loved"
    assert ns.t == [0.0]
    assert ns.size == R.CHECK_SIZE
    assert parser.parse_args(["check", "--all"]).all is True
    ns = parser.parse_args(["filmstrip", "abcDEF"])
    assert ns.frames == 8
    assert ns.to == 8.0
    ns = parser.parse_args(["filmstrip", "abcDEF", "--frames", "4", "--to", "2"])
    assert R.filmstrip_times(ns.frames, ns.to) == [0.0, 0.6667, 1.3333, 2.0]
    assert parser.parse_args(["serve"]).port == 8797
    assert parser.parse_args(["serve", "--port", "8800"]).port == 8800
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--port", "8793"])


def test_parse_helpers():
    assert R.parse_times(" 0 , 2,4 ") == [0.0, 2.0, 4.0]
    for bad in ("", ",", "a", "-0.5", "inf", ",".join(["1"] * (R.MAX_TIMES + 1))):
        with pytest.raises(ValueError):  # noqa: PT011  # each bad input words its own refusal; refusing is the pin
            R.parse_times(bad)
    assert R.parse_size("1920X1080") == (1920, 1080)
    with pytest.raises(ValueError, match="each side must be"):
        R.parse_size(f"{R.MAX_SIDE + 1}x10")
    assert R.filmstrip_times(1, 3) == [3.0]
    with pytest.raises(ValueError, match="frames must be"):
        R.filmstrip_times(0, 3)


def test_resolve_source_frag_json_and_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "JOBS", tmp_path / "jobs")
    frag = tmp_path / "My Sketch.frag"
    frag.write_text("void mainImage(out vec4 o, in vec2 u) { o = vec4(1.0); }\n", encoding="utf-8")
    src = R.resolve_source(str(frag))
    assert src.kind == "frag"
    assert src.key == "local-my-sketch"
    assert src.src.startswith("/jobs/local-my-sketch-")
    staged = tmp_path / "jobs" / src.src[len("/jobs/") :]
    assert S.JOB_NAME.fullmatch(staged.name)
    assert json.loads(staged.read_text(encoding="utf-8"))["renderpass"][0]["type"] == "image"
    assert src.meta["license"]["license"] == "not stated (local file)"

    house = tmp_path / "house.frag"
    house.write_text("uniform vec2 u_res; void main() {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mainImage"):
        R.resolve_source(str(house))

    wrapped = tmp_path / "api.json"
    wrapped.write_text(json.dumps({"Shader": _shader()}), encoding="utf-8")
    assert R.resolve_source(str(wrapped)).kind == "json"

    corpus = tmp_path / "corpus"
    C.save_shader(_shader("abcDEF", name="Unique Name"), corpus)
    C.save_shader(_shader("ghiJKL", name="Other"), corpus)
    got = R.resolve_source("abcDEF", corpus)
    assert got.kind == "corpus"
    assert got.src == "/corpus/abcDEF/shader.json"
    assert R.resolve_source("unique", corpus).key == "abcDEF"
    with pytest.raises(LookupError):
        R.resolve_source("nothing-matches", corpus)


def test_check_ids_reads_sets_in_order(tmp_path):
    corpus = tmp_path / "corpus"
    for sid in ("aaaaaa", "bbbbbb", "cccccc"):
        C.save_shader(_shader(sid), corpus)
    (corpus / "sets.json").write_text(json.dumps({"loved": ["cccccc", "zzzzzz", "aaaaaa"]}), encoding="utf-8")
    ns = argparse.Namespace(ids=[], set="loved", all=False, limit=0)
    assert R.check_ids(ns, corpus) == (["cccccc", "aaaaaa"], ["zzzzzz"])
    ns = argparse.Namespace(ids=["bbbbbb"], set=None, all=False, limit=0)
    assert R.check_ids(ns, corpus) == (["bbbbbb"], [])
    ns = argparse.Namespace(ids=[], set=None, all=True, limit=2)
    assert R.check_ids(ns, corpus)[0] == ["aaaaaa", "bbbbbb"]
    with pytest.raises(LookupError):
        R.check_ids(argparse.Namespace(ids=[], set="nope", all=False, limit=0), corpus)
    with pytest.raises(LookupError):
        R.check_ids(argparse.Namespace(ids=[], set=None, all=False, limit=0), corpus)


# ------------------------------------------------------------------------------------- where pictures may land
def _corpus_here(tmp_path, monkeypatch) -> Path:
    corpus = tmp_path / "corpus"
    C.save_shader(_shader("abcDEF", name="Someone's shader"), corpus)
    monkeypatch.setattr(C, "CORPUS", corpus)
    monkeypatch.setattr(S, "JOBS", tmp_path / "jobs")
    return corpus


def _own_frag(tmp_path) -> Path:
    frag = tmp_path / "mine.frag"
    frag.write_text("void mainImage(out vec4 o, in vec2 u) { o = vec4(u.x, 0.0, 0.0, 1.0); }\n", encoding="utf-8")
    return frag


def test_out_dir_policy_keeps_corpus_pictures_under_state(tmp_path, monkeypatch):
    corpus = _corpus_here(tmp_path, monkeypatch)
    theirs = R.resolve_source("abcDEF", corpus)
    assert theirs.corpus_subject
    inside = STATE / "policy-pins" / "renders" / "abcDEF"  # crosses no link (see IN_STATE); the world's state/: below
    assert R.out_dir_refusal(inside, theirs) is None
    for bad in (NOT_STATE, STATE / ".." / "design", tmp_path / "anywhere"):
        for allow in (False, True):
            why = R.out_dir_refusal(bad, theirs, allow_tracked=allow)
            assert why, (bad, allow)
            assert "state" in why, (bad, allow)
    # A file that IS someone's shader is a corpus subject too: by where it lies, or by its Shadertoy-shaped id.
    assert R.resolve_source(str(corpus / "shaders" / "abcDEF" / "shader.json"), corpus).corpus_subject
    copy = tmp_path / "copy.json"
    copy.write_text(json.dumps(_shader("ghiJKL")), encoding="utf-8")
    assert R.resolve_source(str(copy), corpus).corpus_subject
    own = R.resolve_source(str(_own_frag(tmp_path)), corpus)
    assert not own.corpus_subject
    assert R.out_dir_refusal(inside, own) is None
    assert "--allow-tracked" in R.out_dir_refusal(NOT_STATE, own)
    assert R.out_dir_refusal(NOT_STATE, own, allow_tracked=True) is None


@pytest.mark.parametrize("verb", ["render", "filmstrip"])
def test_render_verbs_refuse_an_out_dir_outside_state_before_any_work(tmp_path, monkeypatch, parser, capsys, verb):
    _corpus_here(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "base_for", lambda port: pytest.fail("a lab server was started"))
    monkeypatch.setattr(R, "run_jobs", lambda *a, **k: pytest.fail("the driver ran"))
    for extra in ([], ["--allow-tracked"]):
        ns = parser.parse_args([verb, "abcDEF", "--out", str(NOT_STATE), *extra])
        ns.json = True
        assert ns.fn(ns) == 2
        assert "state" in capsys.readouterr().err
    ns = parser.parse_args([verb, str(_own_frag(tmp_path)), "--out", str(NOT_STATE)])
    ns.json = True
    assert ns.fn(ns) == 2
    assert "--allow-tracked" in capsys.readouterr().err
    assert not NOT_STATE.exists()


@pytest.mark.parametrize("verb", ["render", "filmstrip"])
def test_allow_tracked_lets_your_own_shader_out_and_tells_the_driver(tmp_path, monkeypatch, parser, verb):
    _corpus_here(tmp_path, monkeypatch)
    jobs_seen = []
    monkeypatch.setattr(S, "base_for", lambda port: (None, "http://127.0.0.1:1"))

    def fake_run(jobs, label, lock_wait_min=10, quiet=False):
        jobs_seen.extend(jobs)
        return {
            "run_dir": None,
            "software": None,
            "chrome": None,
            "bursts": [],
            "refused": None,
            "error": None,
            "rows": {j.key: {"key": j.key, "status": "error", "error": "not rendered in a test"} for j in jobs},
        }

    monkeypatch.setattr(R, "run_jobs", fake_run)
    out = tmp_path / "elsewhere"
    ns = parser.parse_args([verb, str(_own_frag(tmp_path)), "--out", str(out), "--allow-tracked"])
    ns.json = True
    ns.fn(ns)
    (job,) = jobs_seen
    assert job.allow_tracked is True
    assert job.plan_row()["allow_tracked"] is True
    assert Path(job.out_dir).is_absolute()
    assert Path(job.out_dir).is_relative_to(out.resolve())
    # inside state/ the flag is moot: the driver is told nothing it would have to trust (a temp state/ stands in, so
    # this test writes nothing under the real one)
    monkeypatch.setattr(R, "STATE_ROOT", tmp_path / "state")
    monkeypatch.setattr(R, "RENDERS", tmp_path / "state" / "arsenal" / "shaders" / "renders")
    jobs_seen.clear()
    ns = parser.parse_args([verb, str(_own_frag(tmp_path)), "--allow-tracked"])
    ns.json = True
    ns.fn(ns)
    assert jobs_seen[0].allow_tracked is False


# ---------------------------------------------------------------------------- the world's state, beside state/
# On a machine whose world has a home (~/.aurora/<world>), state/arsenal/shaders is a link into that home's state/, which
# every checkout shares. The licence line accepts that root too (render.py extra_state_roots), and only while no .git
# lies at or above it: a picture still lands only where git cannot commit it. core.paths.state_root() answers the root;
# here a bare AI_SETUP names a temp world, so no pin reads or writes the real one.
LAB_SHOT = ROOT / "arsenal" / "shaders" / "lab_shot.mjs"


def _git_free(path: Path) -> bool:
    return not any((p / ".git").exists() for p in (path, *path.parents))


def _link(target: Path, link: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A checkout's state/ whose arsenal/shaders links to a temp world's state/, and a tracked repository beside both."""
    if not _git_free(tmp_path):
        pytest.skip("the temp directory lies inside a git work tree")
    repo_state = tmp_path / "repo" / "state"
    world_state = tmp_path / "world" / "state"
    (world_state / "arsenal" / "shaders").mkdir(parents=True)
    (repo_state / "arsenal").mkdir(parents=True)
    tracked = tmp_path / "tracked"
    (tracked / ".git").mkdir(parents=True)
    (tracked / "design").mkdir()
    _link(world_state / "arsenal" / "shaders", repo_state / "arsenal" / "shaders")
    monkeypatch.setattr(R, "STATE_ROOT", repo_state)
    monkeypatch.setattr(R, "RENDERS", repo_state / "arsenal" / "shaders" / "renders")
    monkeypatch.setenv("AI_SETUP", str(tmp_path / "world"))  # a bare data dir: core.paths.state_root() answers it
    return {"tmp": tmp_path, "repo_state": repo_state, "world_state": world_state, "tracked": tracked}


def _sources(world, monkeypatch):
    corpus = _corpus_here(world["tmp"], monkeypatch)
    return R.resolve_source("abcDEF", corpus), R.resolve_source(str(_own_frag(world["tmp"])), corpus)


def test_a_state_dir_linked_into_the_world_state_is_accepted(world, monkeypatch):
    theirs, own = _sources(world, monkeypatch)
    linked = world["repo_state"] / "arsenal" / "shaders" / "renders" / "abcDEF"
    assert not linked.resolve().is_relative_to(world["repo_state"].resolve())  # the link really leads out of state/
    for target in (linked, world["world_state"] / "arsenal" / "x"):
        for source in (theirs, own):
            assert R.out_dir_refusal(target, source) is None, (target, source.key)
        assert R.leaves_state(target) is False  # the driver is not told allow_tracked: it is given the root instead
    assert R.build_plan("pytest", [], 0)["state_roots"] == [str(world["world_state"].resolve())]


@pytest.mark.parametrize("verb", ["render", "filmstrip"])
def test_a_default_render_through_the_link_reaches_the_driver(world, monkeypatch, parser, verb):
    _corpus_here(world["tmp"], monkeypatch)
    plans = []
    monkeypatch.setattr(S, "base_for", lambda port: (None, "http://127.0.0.1:1"))

    def fake_run(jobs, label, lock_wait_min=10, quiet=False):
        plans.append(R.build_plan(label, jobs, 0))
        rows = {j.key: {"key": j.key, "status": "error", "error": "not rendered in a test"} for j in jobs}
        return {
            "run_dir": None,
            "software": None,
            "chrome": None,
            "bursts": [],
            "refused": None,
            "error": None,
            "rows": rows,
        }

    monkeypatch.setattr(R, "run_jobs", fake_run)
    ns = parser.parse_args([verb, "abcDEF"])
    ns.json = True
    ns.fn(ns)
    (plan,) = plans
    (job,) = plan["jobs"]
    assert job["allow_tracked"] is False
    assert Path(job["out_dir"]).is_relative_to(world["world_state"].resolve())
    assert plan["state_roots"] == [str(world["world_state"].resolve())]


@pytest.mark.parametrize("via", ["AI_SETUP", "AURORA_HOME"])
def test_a_world_state_inside_a_git_work_tree_is_not_accepted(world, monkeypatch, via):
    from core import paths
    from core.world import current

    theirs, own = _sources(world, monkeypatch)
    if via == "AI_SETUP":
        data = world["tracked"] / "data"  # a bare data dir, but inside a repository
        monkeypatch.setenv("AI_SETUP", str(data))
    else:  # the world's home, ~/.aurora/<world> by default, moved into a repository
        if current().name == "unknown":
            pytest.skip("no world is named here, so no world home is looked for")
        monkeypatch.delenv("AI_SETUP", raising=False)
        monkeypatch.setenv("AURORA_HOME", str(world["tracked"] / "home"))
        data = world["tracked"] / "home" / current().name
    (data / "state").mkdir(parents=True)
    assert paths.state_root() == data  # core.paths really names a root inside the repository
    target = data / "state" / "arsenal" / "renders"
    for allow in (False, True):
        assert "state" in (R.out_dir_refusal(target, theirs, allow_tracked=allow) or "")
    assert "--allow-tracked" in (R.out_dir_refusal(target, own) or "")
    assert R.leaves_state(target) is True
    assert R.build_plan("pytest", [], 0).get("state_roots", []) == []


def test_a_link_into_a_tracked_tree_is_still_refused(world, monkeypatch):
    theirs, own = _sources(world, monkeypatch)
    design = world["tracked"] / "design"
    _link(design, world["repo_state"] / "out")  # state/out -> a tracked folder
    _link(design, world["world_state"] / "arsenal" / "shaders" / "out")  # ... and the same, inside the world's state/
    clone = world["world_state"] / "clone"  # a repository cloned inside the world's state/ is tracked territory too
    (clone / ".git").mkdir(parents=True)
    for target in (
        world["repo_state"] / "out" / "x",
        world["repo_state"] / "arsenal" / "shaders" / "out" / "x",
        world["world_state"] / "arsenal" / "shaders" / "out" / "x",
        clone / "renders",
    ):
        for allow in (False, True):
            why = R.out_dir_refusal(target, theirs, allow_tracked=allow)
            assert why, (target, allow)
            assert "state" in why, (target, allow)
        assert "--allow-tracked" in (R.out_dir_refusal(target, own) or ""), target
        assert R.leaves_state(target) is True, target


@pytest.mark.parametrize("verb", ["render", "filmstrip"])
def test_allow_tracked_never_lets_a_corpus_picture_into_a_tracked_tree(world, monkeypatch, parser, capsys, verb):
    _corpus_here(world["tmp"], monkeypatch)
    monkeypatch.setattr(S, "base_for", lambda port: pytest.fail("a lab server was started"))
    monkeypatch.setattr(R, "run_jobs", lambda *a, **k: pytest.fail("the driver ran"))
    for out in (world["tracked"] / "design" / "x", NOT_STATE):
        ns = parser.parse_args([verb, "abcDEF", "--out", str(out), "--allow-tracked"])
        ns.json = True
        assert ns.fn(ns) == 2
        assert "state" in capsys.readouterr().err
        assert not out.exists()


@needs_node
def test_the_driver_accepts_only_the_state_roots_its_plan_names(world):
    ws, tracked = world["world_state"], world["tracked"]
    roots = [str(ws.resolve())]
    _link(tracked / "design", ws / "arsenal" / "shaders" / "out")
    (ws / "clone" / ".git").mkdir(parents=True)
    (tracked / "data" / "state").mkdir(parents=True)
    own = _url(src="/jobs/local-x-0123456789.json")

    def job(out, url=None, **extra):
        return {"url": url or _url(), "out_dir": str(out), "prefix": "", **extra}

    in_world = job(ws / "arsenal" / "shaders" / "renders" / "abcDEF")
    cases = [  # (plan, should the driver load it?)
        ({"jobs": [in_world], "state_roots": roots}, True),  # the root render.py gives
        ({"jobs": [in_world, job(IN_STATE)], "state_roots": roots}, True),  # <repo>/state/ still counts
        ({"jobs": [job(ws / "x", url=own)], "state_roots": roots}, True),
        ({"jobs": [in_world]}, False),  # no root given: the driver derives none of its own
        ({"jobs": [in_world], "state_roots": []}, False),
        ({"jobs": [in_world], "state_roots": ["state"]}, False),  # a relative root
        ({"jobs": [in_world], "state_roots": [roots[0], "../state"]}, False),
        ({"jobs": [in_world], "state_roots": roots[0]}, False),  # a list, not a string
        ({"jobs": [in_world], "state_roots": [None]}, False),
        ({"jobs": [job(ws / "arsenal" / "shaders" / "out" / "x")], "state_roots": roots}, False),  # a link out
        ({"jobs": [job(ws / "clone" / "renders")], "state_roots": roots}, False),  # a repository inside the root
        ({"jobs": [job(tracked / "data" / "state" / "x")], "state_roots": [str(tracked / "data" / "state")]}, False),
        ({"jobs": [job(tracked / "design", allow_tracked=True)], "state_roots": roots}, False),  # corpus: never
        ({"jobs": [job(tracked / "design", url=own, allow_tracked=True)], "state_roots": roots}, True),  # our own
    ]
    got = _node_module(f"{json.dumps([p for p, _ in cases])}.map((p) => H.planRefusal(p))")
    for (plan, ok), why in zip(cases, got, strict=True):
        assert (why is None) is ok, (plan, why)


@needs_node
def test_the_driver_refuses_a_relative_state_root_before_the_lock_or_chrome(world):
    plan = world["tmp"] / "plan.json"
    out = world["world_state"] / "arsenal" / "shaders" / "renders" / "abcDEF"
    job = {"key": "abcDEF", "url": _url(), "out_dir": str(out), "prefix": "", "timeout_ms": 30000, "w": 8, "h": 4}
    plan.write_text(
        json.dumps(
            {
                "api": R.PLAN_API,
                "label": "pytest",
                "lock_wait_ms": 0,
                "jobs": [job],
                "state_roots": ["state"],
                "chrome": str(world["tmp"] / "no-chrome.exe"),
            }
        ),
        encoding="utf-8",
    )
    result = world["tmp"] / "r.json"
    run = subprocess.run(
        [NODE, str(HEADLESS), "--plan", str(plan), "--result", str(result)], capture_output=True, text=True, timeout=60
    )
    assert run.returncode == R.EXIT_REFUSED, run.stdout + run.stderr
    r = json.loads(result.read_text(encoding="utf-8"))
    assert "state_roots" in r["refused"]
    assert r["lock"] is None
    assert [j["status"] for j in r["jobs"]] == ["refused"]
    assert not out.exists()


@needs_node
def test_the_git_tree_rule_has_twins(world):
    ws, tracked = world["world_state"], world["tracked"]
    (ws / "wt").mkdir()
    (ws / "wt" / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n", encoding="utf-8")  # a worktree's file
    (ws / "dangling").mkdir()
    try:
        (ws / "dangling" / ".git").symlink_to(world["tmp"] / "nowhere")  # a broken link named .git counts too
    except (OSError, NotImplementedError):
        pytest.skip("this account cannot create symlinks")
    _link(tracked / "design", ws / "into-tracked")  # both twins climb the real directories, not the link
    # A dangling directory link: Python's resolve follows it to its target, Node's realish keeps it as a plain name, so
    # the twins would judge different directories. Both refuse it instead, wherever it points. Likewise '..' after a
    # link: the OS climbs the link's target, path.resolve climbs the name; both refuse a '..' in the path as given.
    _link(tracked / "missing", ws / "dangling-into-tracked")
    _link(world["tmp"] / "nowhere-dir", ws / "dangling-nowhere")
    _link(tracked / "design", ws / "up")
    cases = [
        (tracked, True),
        (tracked / "design" / "not" / "made", True),
        (ws / "wt" / "x", True),
        (ws / "dangling", True),
        (ws / "into-tracked" / "x", True),
        (ws / "dangling-into-tracked", True),
        (ws / "dangling-into-tracked" / "x" / "y", True),
        (ws / "dangling-nowhere", True),
        (ws / "dangling-nowhere" / "x", True),
        (ws / "up" / ".." / "x", True),  # tracked/x to the OS
        (ws / "arsenal" / ".." / "x", True),  # no link crossed, but a '..' all the same
        (ws, False),
        (ws / "arsenal" / "not" / "made", False),
        (world["repo_state"] / "arsenal" / "shaders" / "x", False),  # a link into the world's state/
        (world["tmp"], False),
    ]
    assert [R.in_git_tree(p) for p, _ in cases] == [ok for _, ok in cases]
    got = _node_module(f"{json.dumps([str(p) for p, _ in cases])}.map((p) => H.inGitTree(p))")
    assert got == [ok for _, ok in cases]
    # ... and the state/ rule built on it: under the world's root only through real, existing directories.
    roots = [str(ws.resolve())]
    under = [
        (ws / "x", True),
        (ws / "arsenal" / "not" / "made", True),
        (ws / "dangling-into-tracked" / "x", False),
        (ws / "dangling-nowhere" / "x", False),
        (ws / "up" / ".." / "x", False),
        (ws / "into-tracked" / "x", False),
        (ws / "wt" / "x", False),
    ]
    assert [R.under_state(p) for p, _ in under] == [ok for _, ok in under]
    got = _node_module(f"{json.dumps([str(p) for p, _ in under])}.map((p) => H.underState(p, {json.dumps(roots)}))")
    assert got == [ok for _, ok in under]


@needs_node
def test_lab_shot_asks_render_py_for_the_state_roots(world):
    """lab_shot.mjs writes its screenshots under the same roots the render verbs accept: it asks render.py for them
    (it cannot import core.paths) and checks --out against them with headless.mjs's underState."""
    url = LAB_SHOT.resolve().as_uri()
    got = _node(f"import * as L from {json.dumps(url)}; console.log(JSON.stringify(L.stateRoots()));")
    assert got == [str(p) for p in R.extra_state_roots()] == [str(world["world_state"].resolve())]


@needs_node
def test_the_default_dirs_are_accepted_where_this_checkouts_state_really_leads(tmp_path, monkeypatch):
    """The pins above stand a temp world in; this one asks the real one, read-only (paths are resolved, nothing is
    written). Wherever state/arsenal/shaders really leads (inside this checkout's state/, or into the world's state/ on
    a machine whose world has a home), a default render of a corpus shader is accepted by render.py, and by the driver
    given the plan render.py writes. Red on such a machine while <repo>/state/ was the only root."""
    monkeypatch.delenv("AI_SETUP", raising=False)  # the suite's temp data dir is not this machine's world
    theirs = R.resolve_source("abcDEF", _corpus_here(tmp_path, monkeypatch))
    plan = R.build_plan("pytest", [], 0)
    picked = [R.RENDERS / "abcDEF", R.RENDERS / "abcDEF" / "filmstrip"]  # render, filmstrip: through out_dir_for
    for d in picked:
        assert R.out_dir_refusal(d, theirs) is None, (d, d.resolve(), plan.get("state_roots"))
        assert R.leaves_state(d) is False, d
    outs = [str(d.resolve()) for d in picked] + [str(R.CHECKS / "20261019-000000")]  # check: as written, unresolved
    plan["jobs"] = [{"key": "abcDEF", "url": _url(), "out_dir": out, "prefix": ""} for out in outs]
    assert _node_module(f"H.planRefusal({json.dumps(plan)})") is None, plan


# ------------------------------------------------------------------- at the moment of writing, not only at plan time
# The realpath rule judges a directory when the plan is checked. Two ways round it remained: a link at a picture's own
# file name inside an accepted out_dir (followed by a plain write), and a link swapped in for the out_dir while the
# driver waited for the GPU lock or rendered (minutes). Each picture write is judged again and never follows a link at
# its own name.
_PNG = "data:image/png;base64,iVBORw0KGgo="  # the eight-byte PNG signature: enough for runJob's check


def _fake_browser_js() -> str:
    """A stand-in for headless.mjs's Browser that answers runJob's three page calls: one frame at t=0, SwiftShader."""
    return (
        "const fake = { events: [], renderer: 'ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero)), "
        "SwiftShader driver)', page: { send: async () => ({}) }, evaluate: async (expr) => "
        "expr.startsWith('window.__render.frames[') ? " + json.dumps(_PNG) + " : expr.includes('has_png') ? "
        "{ renderer: fake.renderer, frames: [{ t: 0, ms: 1, has_png: true }] } : 'done' };"
    )


@needs_node
def test_a_link_at_a_pictures_own_name_is_removed_not_followed(world):
    pytest.importorskip("PIL")
    out = world["world_state"] / "arsenal" / "shaders" / "renders" / "abcDEF"
    out.mkdir(parents=True)
    design = world["tracked"] / "design"
    (design / "kept.png").write_bytes(b"theirs")
    names = ("t0.00.png", "screenshot.png", "t1.00.png")
    for name, target in zip(names, ("t0.00.png", "screenshot.png", "kept.png"), strict=True):
        (out / name).symlink_to(design / target)  # dangling, dangling, and a link to a file that exists
    url = HEADLESS.resolve().as_uri()
    files = [str(out / n) for n in names]
    _node(
        f"import * as H from {json.dumps(url)}; for (const f of {json.dumps(files)}) H.writePicture(f, Buffer.from('png'));"
        " console.log('null');"
    )
    (out / "filmstrip.png").symlink_to(design / "sheet.png")
    R.contact_sheet([], "a header", out / "filmstrip.png")
    for name in (*names, "filmstrip.png"):
        assert not (out / name).is_symlink(), name
        assert (out / name).is_file(), name
    assert sorted(p.name for p in design.iterdir()) == ["kept.png"]
    assert (design / "kept.png").read_bytes() == b"theirs"


@needs_node
@pytest.mark.parametrize("swap", [False, True])
def test_the_driver_judges_the_out_dir_again_when_it_writes(world, swap):
    out = world["world_state"] / "arsenal" / "shaders" / "renders" / "abcDEF"
    design = world["tracked"] / "design"
    roots = [str(world["world_state"].resolve())]
    job = {"key": "abcDEF", "url": _url(), "out_dir": str(out), "prefix": "", "w": 8, "h": 4, "timeout_ms": 5000}
    url = HEADLESS.resolve().as_uri()
    got = _node(
        f"import * as H from {json.dumps(url)}; import * as fs from 'node:fs';"
        f"const job = {json.dumps(job)}, roots = {json.dumps(roots)};"
        + _fake_browser_js()
        + "const planned = H.jobRefusal(job, roots);"
        + (
            f"fs.mkdirSync(job.out_dir, {{ recursive: true }}); fs.rmSync(job.out_dir, {{ recursive: true }});"
            f"fs.symlinkSync({json.dumps(str(design))}, job.out_dir);"  # swapped while the driver waited for the lock
            if swap
            else ""
        )
        + "const row = await H.runJob(fake, job, Date.now() + 5000, roots);"
        "console.log(JSON.stringify({ planned, status: row.status, error: row.error, files: row.frames.map((f) => f.file) }));"
    )
    assert got["planned"] is None  # the plan passed its check before the lock
    if swap:
        assert got["status"] == "error", got
        assert "refused at write time" in got["error"], got
        assert list(design.iterdir()) == []
    else:
        assert got["status"] == "done", got
        assert got["files"] == [str(out / "t0.00.png")]
        assert (out / "t0.00.png").read_bytes()[:4] == b"\x89PNG"


# ------------------------------------------------------------------- under state/, only where git ignores it
# .gitignore ignores state/* but re-includes parts of it (state/coord/**, state/drills/** and state/rewrites/ today),
# so "under state/" is not "ignored". A directory there counts only while git itself says a file in it is ignored (git
# check-ignore --no-index from the root of the work tree that holds it, the caller's own git settings left out); no git,
# or a git that cannot answer, refuses. These read this checkout's real .gitignore and write nothing.
REINCLUDED = [
    STATE / "coord" / "licence-pins",
    STATE / "drills" / "licence-pins",
    STATE / "rewrites" / "licence-pins",
    STATE / "drills",
]
IGNORED = [STATE, STATE / "ci" / "licence-pins", STATE / "policy-pins" / "renders"]


def _hostile_git_env(tmp_path, monkeypatch) -> None:
    """Git settings of the caller's own that must not decide the verdict: a GIT_DIR and a work tree elsewhere (as a git
    hook has), and a global excludes file that ignores everything (it ignores state/ itself, re-includes and all)."""
    (tmp_path / "xdg" / "git").mkdir(parents=True)
    (tmp_path / "xdg" / "git" / "ignore").write_text("*\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repo"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path))


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")


@needs_git
@pytest.mark.parametrize("env", ["plain", "hostile"])
def test_a_dir_git_would_commit_under_state_is_refused(tmp_path, monkeypatch, env):
    corpus = _corpus_here(tmp_path, monkeypatch)
    theirs = R.resolve_source("abcDEF", corpus)
    own = R.resolve_source(str(_own_frag(tmp_path)), corpus)
    if env == "hostile":
        _hostile_git_env(tmp_path, monkeypatch)
    for d in REINCLUDED:
        for allow in (False, True):
            why = R.out_dir_refusal(d, theirs, allow_tracked=allow)
            assert why, (d, allow)
            assert "state" in why, (d, allow)
        assert "--allow-tracked" in (R.out_dir_refusal(d, own) or ""), d
        assert R.leaves_state(d) is True, d  # so the driver is told, and judges it again
    for d in IGNORED:
        assert R.out_dir_refusal(d, theirs) is None, d
        assert R.leaves_state(d) is False, d


@needs_node
@needs_git
@pytest.mark.parametrize("env", ["plain", "hostile"])
def test_the_driver_refuses_a_dir_git_would_commit_under_state(tmp_path, monkeypatch, env):
    if env == "hostile":
        _hostile_git_env(tmp_path, monkeypatch)
    own = _url(src="/jobs/local-x-0123456789.json")
    cases = [({"url": _url(), "out_dir": str(d), "prefix": ""}, False) for d in REINCLUDED]
    cases += [({"url": _url(), "out_dir": str(d), "prefix": "", "allow_tracked": True}, False) for d in REINCLUDED]
    cases += [({"url": own, "out_dir": str(d), "prefix": ""}, False) for d in REINCLUDED]
    cases += [({"url": own, "out_dir": str(REINCLUDED[0]), "prefix": "", "allow_tracked": True}, True)]  # our own
    cases += [({"url": _url(), "out_dir": str(d), "prefix": ""}, True) for d in IGNORED]
    got = _node_module(f"{json.dumps([j for j, _ in cases])}.map((j) => H.jobRefusal(j))")
    for (job, ok), why in zip(cases, got, strict=True):
        assert (why is None) is ok, (job, why)


@needs_node
def test_under_state_is_refused_when_git_cannot_be_asked(tmp_path, monkeypatch):
    """No git on PATH: nothing under <repo>/state/ can be judged, so nothing there is accepted (fail closed)."""
    corpus = _corpus_here(tmp_path, monkeypatch)
    theirs = R.resolve_source("abcDEF", corpus)
    (tmp_path / "no-git").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "no-git"))
    for d in IGNORED:
        assert R.out_dir_refusal(d, theirs), d
        assert R.leaves_state(d) is True, d
    got = _node_module(f"{json.dumps([str(d) for d in IGNORED])}.map((d) => H.underState(d))")
    assert got == [False] * len(IGNORED)


def _git_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A temp repository whose .gitignore ignores state/* and re-includes state/drills/, as this repository's does;
    R.STATE_ROOT is its state/. Refusals are pinned here by what they leave on disk, not only by what they say."""
    if shutil.which("git") is None:
        pytest.skip("git is not on PATH")
    if not _git_free(tmp_path):
        pytest.skip("the temp directory lies inside a git work tree")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, env=_git_env(), timeout=60)
    (repo / ".gitignore").write_text("state/*\n!state/drills/\n!state/drills/**\n", encoding="utf-8")
    (repo / "state").mkdir()
    monkeypatch.setattr(R, "STATE_ROOT", repo / "state")
    monkeypatch.setattr(R, "RENDERS", repo / "state" / "arsenal" / "shaders" / "renders")
    return repo


@pytest.mark.parametrize("verb", ["render", "filmstrip"])
def test_a_render_into_a_dir_git_would_commit_is_refused_before_any_work(
    git_repo, tmp_path, monkeypatch, parser, capsys, verb
):
    _corpus_here(tmp_path, monkeypatch)
    monkeypatch.setattr(S, "base_for", lambda port: pytest.fail("a lab server was started"))
    monkeypatch.setattr(R, "run_jobs", lambda *a, **k: pytest.fail("the driver ran"))
    out = git_repo / "state" / "drills" / "x"
    for allow in ([], ["--allow-tracked"]):
        ns = parser.parse_args([verb, "abcDEF", "--out", str(out), *allow])
        ns.json = True
        assert ns.fn(ns) == 2
        assert "state" in capsys.readouterr().err
    assert not out.exists()
    assert R.leaves_state(git_repo / "state" / "renders" / "x") is False  # an ignored dir of the same tree still counts


def test_under_state_is_refused_when_git_cannot_answer(git_repo, tmp_path):
    """A .git git cannot open (a worktree whose repository was removed): git errors, and the answer is no."""
    broken = tmp_path / "broken"
    (broken / "state").mkdir(parents=True)
    (broken / ".git").write_text(f"gitdir: {tmp_path / 'gone' / '.git'}\n", encoding="utf-8")
    assert R.under_state(broken / "state" / "x", primary=broken / "state") is False
    assert R.under_state(git_repo / "state" / "x") is True  # the same rule over a repository git can open


# ----------------------------------------------------------------------------- the roots a plan may name
# A hand-edited plan could name any absolute directory outside a detectable git tree as a state root ('/' included).
# The driver cannot compute the world's root render.py would give, so it bounds what it accepts: a root named state,
# not a filesystem root, not the user's home nor a directory above it, and not inside a git tree.
@needs_node
def test_the_driver_bounds_the_state_roots_a_plan_may_name(world):
    tmp, ws = world["tmp"], world["world_state"]
    (tmp / "elsewhere").mkdir()
    (tmp / "to-root").mkdir()
    _link(Path(tmp.anchor), tmp / "to-root" / "state")  # named state, and it is '/'
    through = tmp / "to-root" / "state" / ws.resolve().relative_to(tmp.anchor) / "x"

    def plan(out, *roots):
        return {"jobs": [{"url": _url(), "out_dir": str(out), "prefix": ""}], "state_roots": [str(r) for r in roots]}

    cases = [
        (plan(ws / "x", ws), True),  # the root render.py gives
        (plan(ws / "x", tmp.anchor), False),  # a filesystem root
        (plan(tmp / "elsewhere" / "x", tmp / "elsewhere"), False),  # not named state
        (plan(through, tmp / "to-root" / "state"), False),  # named state, but '/' once followed
        (plan(ws / "x", ws, tmp.anchor), False),  # one bad root refuses the plan
    ]
    got = _node_module(f"{json.dumps([p for p, _ in cases])}.map((p) => H.planRefusal(p))")
    for (p, ok), why in zip(cases, got, strict=True):
        assert (why is None) is ok, (p, why)
    assert _node_module(f"H.underState({json.dumps(str(ws / 'x'))}, [{json.dumps(tmp.anchor)}])") is False


@needs_node
@pytest.mark.parametrize("layout", ["home is the root", "home is under the root"])
def test_the_driver_refuses_the_users_home_as_a_state_root(world, monkeypatch, layout):
    home = world["tmp"] / "h" / "state"
    root = home
    if layout == "home is under the root":
        home = home / "me"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    plan = {"jobs": [{"url": _url(), "out_dir": str(root / "x"), "prefix": ""}], "state_roots": [str(root)]}
    assert _node_module(f"H.planRefusal({json.dumps(plan)})") is not None


@pytest.mark.parametrize("layout", ["linked elsewhere", "home is under it"])
def test_the_world_state_root_is_bounded_as_the_drivers_is(world, monkeypatch, layout):
    """render.py names the world's root to the driver only when the driver would accept it: so a world whose state/
    is a link to a directory of another name, or a state/ above the user's home, widens nothing."""
    tmp = world["tmp"]
    data = tmp / "w2"
    data.mkdir()
    if layout == "linked elsewhere":
        (tmp / "elsewhere").mkdir()
        _link(tmp / "elsewhere", data / "state")
    else:
        (data / "state" / "me").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(data / "state" / "me"))
        monkeypatch.setenv("USERPROFILE", str(data / "state" / "me"))
    monkeypatch.setenv("AI_SETUP", str(data))
    assert R.extra_state_roots() == []
    assert R.build_plan("pytest", [], 0)["state_roots"] == []


# ------------------------------------------------------------------ lab_shot, at the moment of each screenshot
# lab_shot.mjs judges --out once, then waits up to ten minutes for the GPU lock before its first screenshot: long
# enough to swap the directory for a link into a tracked tree. Every screenshot write judges it again.
def _fake_lab_browser_js() -> str:
    """A stand-in for lab_shot.mjs's browser that answers shoot's and shootRun's page calls (the run view is refused by
    its pre-flight, so it takes one picture and returns)."""
    return (
        "const png = Buffer.from([0x89, 0x50, 0x4e, 0x47]).toString('base64');"
        "const fake = { page: { send: async (m) => (m === 'Page.captureScreenshot' ? { data: png } : {}) },"
        " evaluate: async (e) => e.includes('canvases') ? { canvases: 0, contexts: [] }"
        " : e.includes('live: !!R.live') ? { ready: true, error: null, live: true }"
        " : e.includes('const R = window.__lab') ? { ready: true, error: null, stage: null }"
        " : e.includes('wb-receipt-sheet') ? null"
        " : e.includes('stage.state()') ? { refused: 'a pin', running: false }"
        " : e.startsWith('[...document.images]') ? 0 : true };"
    )


@needs_node
@pytest.mark.parametrize("view", ["bench", "run"])
@pytest.mark.parametrize("swap", [False, True])
def test_lab_shot_judges_its_out_dir_again_at_every_screenshot(world, view, swap):
    out = world["world_state"] / "arsenal" / "shaders" / "labshots" / "pin"
    out.mkdir(parents=True)
    design = world["tracked"] / "design"
    roots = [str(world["world_state"].resolve())]
    if swap:  # swapped for a link into a tracked tree while lab_shot waited for the GPU lock
        out.rmdir()
        _link(design, out)
    call = (
        "L.shoot(fake, 'http://127.0.0.1:1', 'bench', 8, 4, out, roots)"
        if view == "bench"
        else "L.shootRun(fake, 'http://127.0.0.1:1', 'abcDEF', null, 8, 4, out, 120000, 1, null, roots)"
    )
    got = _node(
        f"import * as L from {json.dumps(LAB_SHOT.resolve().as_uri())};"
        + _fake_lab_browser_js()
        + f"const out = {json.dumps(str(out))}, roots = {json.dumps(roots)};"
        + f"let r; try {{ const row = await {call}; r = {{ files: row.files || [row.file] }}; }}"
        + " catch (e) { r = { error: e.message }; } console.log(JSON.stringify(r));"
    )
    if swap:
        assert "refused at write time" in got.get("error", ""), got
        assert list(design.iterdir()) == []
    else:
        assert got.get("files"), got
        for f in got["files"]:
            assert Path(f).parent == out, f
            assert Path(f).read_bytes()[:4] == b"\x89PNG", f


@needs_node
@needs_git
def test_the_git_ignore_and_state_root_rules_have_twins(world):
    """git_ignores / gitIgnores and state_root_refusal / stateRootRefusal, run over the same paths."""
    tmp, ws, tracked = world["tmp"], world["world_state"], world["tracked"]
    repo = tmp / "ignoring"
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, env=_git_env(), timeout=60)
    (repo / ".gitignore").write_text("state/*\n!state/drills/\n!state/drills/**\n", encoding="utf-8")
    (repo / "state" / "drills" / "named").mkdir(parents=True)
    _link(repo / "state" / "drills", repo / "state" / "to-drills")  # an ignored name that leads into drills
    (repo / "state" / "drills" / "named" / R.GIT_PROBE).symlink_to(repo / "state" / "x" / "f.png")  # a link at the name
    broken = tmp / "broken"
    (broken / "state").mkdir(parents=True)
    (broken / ".git").write_text(f"gitdir: {tmp / 'gone' / '.git'}\n", encoding="utf-8")
    probes = [
        (repo / "state" / "x" / R.GIT_PROBE, True),
        (repo / "state" / R.GIT_PROBE, True),
        (repo / "state" / "drills" / "x" / R.GIT_PROBE, False),
        (repo / "state" / "to-drills" / R.GIT_PROBE, False),  # the directory is resolved: drills is asked about
        (repo / "state" / "drills" / "named" / R.GIT_PROBE, False),  # the name is kept, not followed to state/x
        (repo / "design" / R.GIT_PROBE, False),
        (broken / "state" / R.GIT_PROBE, False),  # git cannot open that repository
        (tmp / "no-repo" / R.GIT_PROBE, False),  # no work tree: nothing to ask, and the caller decides
        (repo / "state" / "x" / ".." / R.GIT_PROBE, False),  # a '..'
    ]
    assert [R.git_ignores(p) for p, _ in probes] == [ok for _, ok in probes]
    got = _node_module(f"{json.dumps([str(p) for p, _ in probes])}.map((p) => H.gitIgnores(p))")
    assert got == [ok for _, ok in probes]
    (tracked / "state").mkdir()
    roots = [
        (ws, True),
        (Path(tmp.anchor), False),
        (tmp / "elsewhere", False),
        (tracked / "state", False),  # inside a git tree
        (ws / ".." / "state", False),  # a '..'
        (Path("state"), False),  # relative
    ]
    assert [R.state_root_refusal(p) is None for p, _ in roots] == [ok for _, ok in roots]
    got = _node_module(f"{json.dumps([str(p) for p, _ in roots])}.map((p) => H.stateRootRefusal(p) === null)")
    assert got == [ok for _, ok in roots]
