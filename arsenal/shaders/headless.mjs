// The shader lab's headless driver. It renders shader pages in a private, headless Chrome forced onto SwiftShader (the
// CPU), under the house GPU lock, and writes what each page reported (window.__render) plus its PNG frames.
//
//   node arsenal/shaders/headless.mjs --plan <plan.json> --result <result.json>   one burst; render.py writes the plan
//   node arsenal/shaders/headless.mjs --check-renderer "<renderer string>"         the SwiftShader judgement, as JSON
//   node arsenal/shaders/headless.mjs --print-flags                                the Chrome flags, as JSON
//
// WHY THE CPU. This host has an AMD display-driver crash (TDR) history, and a shader corpus is exactly the workload that
// trips one: hundreds of strangers' raymarchers, some of which loop for seconds per pixel. So no frame here is ever drawn
// on the GPU. Chrome starts headless with ANGLE on SwiftShader, Direct3D 11 off, and hardware video decode and GPU
// compositing off (measured 2026-10-13 on Chrome 155: SystemInfo then lists SwiftShader as the only GPU device, and
// video_decode / gpu_compositing read "disabled_software"). Before any job page loads, the driver reads the browser's own
// GPU report and a WebGL2 context's WEBGL_debug_renderer_info, and refuses to render (exit 3) unless both name
// SwiftShader and neither names hardware. Each page's own renderer string is judged again before its frames are kept.
//
// WHY A FRESH PROFILE PER LAUNCH. A second chrome.exe pointed at a profile a live Chrome still holds hands its command
// line to that Chrome and exits, and the driver would be talking to a browser it did not start. Each launch gets its
// own directory under state/arsenal/shaders/chrome-profile/, removed afterwards. --remote-debugging-port=0 plus the
// DevToolsActivePort file the profile receives means the driver can only ever connect to the Chrome it launched.
//
// WHY BURSTS. arsenal/GPU-LOCK.md: one headless render at a time on this machine, bursts under three minutes, release
// between bursts. A burst is the lock's whole hold: Chrome's launch, the jobs, any relaunch after a timeout, and the
// shutdown. So every job's deadline ends SHUTDOWN_MARGIN_MS before the burst does (jobWindow), a later job starts only
// if its whole deadline still fits, and a relaunch begins only if a slow one (LAUNCH_RESERVE_MS) still leaves room for
// the shutdown (mayRelaunch). The rest come back "deferred" and render.py runs them in the next burst, after this
// process has released the lock.
//
// WHAT A PLAN MAY ASK. Before the lock is taken or Chrome launched, every job is checked (jobRefusal): it loads a lab
// page of the shader server on http://127.0.0.1:<port> (never port 8793, Daniel's server), its shader and media come
// from that same server, its frame prefix is a plain name, and it writes under <repo>/state/ where git ignores it (git
// check-ignore says so: .gitignore re-includes state/coord/**, state/drills/** and state/rewrites/, and no git refuses),
// or under a root the plan's state_roots name (render.py extra_state_roots: the world's gitignored state/,
// ~/.aurora/<world>/state, which state/arsenal/shaders links into when every checkout shares one corpus), absolute,
// named state, not '/', a drive or the user's home, with no .git at or above the out_dir (stateRootRefusal). The driver
// derives no root of its own. Only a job that render.py marks allow_tracked, for a shader that is not from the corpus,
// may write elsewhere: pictures of other people's work never leave state/. One bad job refuses the whole plan (exit 3),
// and every picture write judges its job again (writePicture).
//
// KILLING ONLY OUR CHROME. Browser.close first; if the process is still alive, `taskkill /T /F` on the pid this script
// spawned, which ends that browser and its own children and nothing else (never Daniel's browser, never another lane's).
// Off Windows killTree does the same by hand: the browser's descendants are read from `ps` while they are still its
// children, then the browser and every one of them get SIGKILL. Chrome is not detached there, so it stays in this
// driver's process group, and render.py's last-resort kill of that group (the driver runs in a session of its own)
// reaches our Chrome too. A job that misses its deadline (a page wedged in a long SwiftShader draw) costs a full Chrome
// restart: a fresh GPU process is the only reliable way to stop a draw call that is still running. On a signal or a
// crash exit the order is the same as at the end of a burst: our Chrome dies first, the lock is released second
// (guardHolder).
//
// WHICH CHROME. resolveChrome: the plan's own `chrome`, else $AKASHIC_CHROME, else the platform's Chrome (CHROME_EXE on
// Windows; Google Chrome in /Applications on macOS, when it is there), else off Windows the newest Playwright Chromium in
// its browser cache ($PLAYWRIGHT_BROWSERS_PATH, else ~/.cache/ms-playwright, ~/Library/Caches/ms-playwright on macOS),
// the full browser before the headless shell, else chromium or google-chrome on PATH. On Linux the flags add
// --no-sandbox when the kernel keeps Chrome from building its sandbox (sandboxFlags): Ubuntu 23.10+ restricts the
// unprivileged user namespaces it needs to binaries with an AppArmor profile, which a Playwright Chromium has none of,
// and Chrome then aborts at start ("No usable sandbox!"). $AKASHIC_CHROME_NO_SANDBOX=1 or 0 overrides that judgement.
import { spawn, spawnSync } from "node:child_process";
import {
  accessSync, constants, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, statSync,
  writeFileSync,
} from "node:fs";
import { homedir } from "node:os";
import { basename, delimiter, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { setTimeout as delay } from "node:timers/promises";

const REPO = fileURLToPath(new URL("../..", import.meta.url));
export const PLAN_API = "arsenal.shader-headless-plan/v1";
export const RESULT_API = "arsenal.shader-headless/v1";
export const CHROME_EXE = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";  // the Windows default
export const MAC_CHROME_EXE = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
export const CHROME_ENV = "AKASHIC_CHROME";                 // a Chrome to use instead of the default, on any platform
export const SANDBOX_ENV = "AKASHIC_CHROME_NO_SANDBOX";     // Linux: "1" adds --no-sandbox, "0" never does
export const PATH_CHROMES = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable"];
export const STATE_ROOT = join(REPO, "state");   // gitignored: where every job writes (see WHAT A PLAN MAY ASK)
export const GIT_PROBE = "licence-line-probe.png";  // render.py GIT_PROBE: the file git is asked about in an out_dir
const GIT_TIMEOUT_MS = 30_000;  // render.py GIT_TIMEOUT_S
export const PROFILE_ROOT = join(STATE_ROOT, "arsenal", "shaders", "chrome-profile");
export const BURST_MS = 170_000;           // GPU-LOCK.md: under 3 minutes, launch and shutdown included
export const LOCK_HOLD_MS = 4 * 60_000;    // the cap declared in owner.json: the burst plus shutdown, with margin
export const DEFAULT_LOCK_WAIT_MS = 10 * 60_000;
export const DEVTOOLS_WAIT_MS = 20_000;
// Browser.close (3 s), the wait for its exit (4 s), taskkill (about 1 s) and the wait after it (3 s), the profile's
// removal (3 s): about 13 s when Chrome is wedged. Every job's deadline ends this long before the burst does.
export const SHUTDOWN_MARGIN_MS = 15_000;
// A launch: the DevToolsActivePort file (DEVTOOLS_WAIT_MS), then SystemInfo and the WebGL2 check (15 s each, at most).
export const LAUNCH_RESERVE_MS = DEVTOOLS_WAIT_MS + 30_000;
export const MIN_JOB_MS = 1_000;
export const POLL_MS = 200;
export const EXIT = { ok: 0, error: 1, usage: 2, refused: 3, lock: 4 };
export const FORBIDDEN_PORTS = { 8793: "Daniel's arsenal server (arsenal/serve.py)" };  // serve.py FORBIDDEN_PORTS
const PREFIX = /^[A-Za-z0-9_-]{0,64}$/;

// The three flags the contract requires, then belt and braces: nothing in this browser may open a Direct3D device on
// the AMD adapter (D3D11 off; hardware video decode and encode off; compositing in software).
export const SWIFTSHADER_FLAGS = ["--headless=new", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"];
export const GPU_FENCE_FLAGS = ["--disable-d3d11", "--disable-accelerated-video-decode",
                                "--disable-accelerated-video-encode", "--disable-gpu-compositing"];
export const QUIET_FLAGS = ["--no-first-run", "--no-default-browser-check", "--mute-audio", "--disable-extensions",
                            "--disable-component-update", "--disable-sync", "--disable-default-apps", "--hide-scrollbars",
                            // a background tab throttles timers and rAF; the render page's watchdog must keep time
                            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                            "--disable-backgrounding-occluded-windows"];
export const chromeFlags = (profile) => [...SWIFTSHADER_FLAGS, ...GPU_FENCE_FLAGS, ...QUIET_FLAGS, ...sandboxFlags(),
                                         "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"];

// ------------------------------------------------------------------------------------------- which Chrome, how
const readSysctl = (path) => { try { return readFileSync(path, "utf8").trim(); } catch { return null; } };

// Linux only: whether Chrome must run without its sandbox here (see WHICH CHROME). Empty everywhere else, so the
// Windows and macOS flags are exactly the lists above.
export function sandboxFlags({ platform = process.platform, env = process.env, read = readSysctl } = {}) {
  if (platform !== "linux") return [];
  if (env[SANDBOX_ENV] === "1") return ["--no-sandbox"];
  if (env[SANDBOX_ENV] === "0" || env.CHROME_DEVEL_SANDBOX) return [];  // a setuid sandbox helper is named: Chrome uses it
  const restricted = read("/proc/sys/kernel/apparmor_restrict_unprivileged_userns") === "1"
    || read("/proc/sys/kernel/unprivileged_userns_clone") === "0"
    || read("/proc/sys/user/max_user_namespaces") === "0";
  return restricted ? ["--no-sandbox"] : [];
}

function isExecutable(path) {
  try {
    if (!statSync(path).isFile()) return false;
    if (process.platform !== "win32") accessSync(path, constants.X_OK);
    return true;
  } catch { return false; }
}

// Playwright's browser cache, as Playwright itself finds it.
export function playwrightCache({ env = process.env, platform = process.platform, home = homedir() } = {}) {
  const own = env.PLAYWRIGHT_BROWSERS_PATH;
  if (own && own !== "0") return own;
  if (platform === "darwin") return join(home, "Library", "Caches", "ms-playwright");
  return join(env.XDG_CACHE_HOME || join(home, ".cache"), "ms-playwright");
}

// Where a Playwright browser keeps its executable, per platform: the full Chromium, then the headless shell.
const PLAYWRIGHT_BINARIES = {
  linux: {
    chromium: [["chrome-linux64", "chrome"], ["chrome-linux", "chrome"]],
    chromium_headless_shell: [["chrome-headless-shell-linux64", "chrome-headless-shell"], ["chrome-linux", "headless_shell"]],
  },
  darwin: {
    chromium: [["chrome-mac-arm64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"],
               ["chrome-mac-x64", "Google Chrome for Testing.app", "Contents", "MacOS", "Google Chrome for Testing"],
               ["chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"]],
    chromium_headless_shell: [["chrome-headless-shell-mac-arm64", "chrome-headless-shell"],
                              ["chrome-headless-shell-mac-x64", "chrome-headless-shell"]],
  },
};

// Every Playwright Chromium executable in the cache that could be ours, best first: the full browser before the headless
// shell (the old headless mode), the newest revision first within each.
export function playwrightChromes({ env = process.env, platform = process.platform, home = homedir() } = {}) {
  const layouts = PLAYWRIGHT_BINARIES[platform];
  if (!layouts) return [];
  const cache = playwrightCache({ env, platform, home });
  let names = [];
  try { names = readdirSync(cache); } catch { return []; }
  const out = [];
  for (const [kind, binaries] of Object.entries(layouts)) {
    const revisions = names.map((n) => new RegExp(`^${kind}-(\\d+)$`).exec(n)).filter(Boolean)
      .sort((a, b) => Number(b[1]) - Number(a[1]));
    for (const m of revisions) for (const parts of binaries) out.push(join(cache, m[0], ...parts));
  }
  return out;
}

function onPath(names, env = process.env) {
  for (const dir of String(env.PATH || "").split(delimiter).filter(Boolean)) {
    for (const name of names) if (isExecutable(join(dir, name))) return join(dir, name);
  }
  return null;
}

// The Chrome this driver launches (see WHICH CHROME), or null when there is none to be found. A plan's chrome and
// $AKASHIC_CHROME are taken as given; the CLI says so when the file is not there.
export function resolveChrome(plan = {}, { env = process.env, platform = process.platform, home = homedir() } = {}) {
  if (plan && plan.chrome) return plan.chrome;
  if (env[CHROME_ENV]) return env[CHROME_ENV];
  if (platform === "win32") return CHROME_EXE;
  if (platform === "darwin" && isExecutable(MAC_CHROME_EXE)) return MAC_CHROME_EXE;
  return playwrightChromes({ env, platform, home }).find(isExecutable) || onPath(PATH_CHROMES, env);
}

// pid and every descendant of it, breadth first, from [pid, ppid] rows (a process table read before anything dies).
export function processTree(rootPid, rows) {
  const kids = new Map();
  for (const [pid, ppid] of rows) {
    if (!kids.has(ppid)) kids.set(ppid, []);
    kids.get(ppid).push(pid);
  }
  const out = [rootPid];
  const seen = new Set(out);
  for (let i = 0; i < out.length; i++) {
    for (const kid of kids.get(out[i]) || []) if (!seen.has(kid)) { seen.add(kid); out.push(kid); }
  }
  return out;
}

// End a browser this script spawned and everything it started, and nothing else (see KILLING ONLY OUR CHROME).
// Windows: taskkill /T /F. Elsewhere the tree is read from ps first, since a child whose parent has died is re-parented
// and could no longer be found; then the browser goes first (it cannot restart a child it lost) and its descendants
// after it, all with SIGKILL, as /F is.
export function killTree(pid) {
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(pid), "/T", "/F"], { windowsHide: true, timeout: 15_000 });
    return;
  }
  const ps = spawnSync("ps", ["-A", "-o", "pid=,ppid="], { encoding: "utf8", timeout: 15_000 });
  const rows = String(ps.stdout || "").split("\n").map((line) => line.trim().split(/\s+/).map(Number))
    .filter((row) => row.length === 2 && row.every(Number.isInteger));
  for (const p of processTree(pid, rows)) {
    try { process.kill(p, "SIGKILL"); } catch { /* already gone */ }
  }
}
export const KILL_HOW = process.platform === "win32" ? "taskkill-tree" : "sigkill-tree";

// A renderer string names SwiftShader and no hardware. arsenal/shaders/render.py keeps the Python twin of this rule
// (judge_renderer); tests/test_shader_harness.py runs both over the same strings.
export const HARDWARE_HINT = /\b(amd|radeon|ati|nvidia|geforce|quadro|rtx|intel|iris|uhd graphics|adreno|mali|apple m\d|direct3d\d*|d3d\d+)\b/i;
export function judgeRenderer(renderer) {
  if (typeof renderer !== "string" || !renderer.trim()) {
    return { ok: false, reason: "no renderer string (no WebGL2 context, or WEBGL_debug_renderer_info is unavailable)" };
  }
  if (!/swiftshader/i.test(renderer)) return { ok: false, reason: `the renderer is not SwiftShader: ${renderer}` };
  const hw = HARDWARE_HINT.exec(renderer);
  if (hw) return { ok: false, reason: `the renderer names hardware (${hw[0]}): ${renderer}` };
  return { ok: true, reason: null };
}

// ---------------------------------------------------------------------------------------- what a plan may ask
// The real path of p's deepest existing ancestor, plus the rest: a junction under state/ cannot lead out of it.
function realish(p) {
  let head = p;
  const rest = [];
  while (!existsSync(head)) {
    const up = dirname(head);
    if (up === head) break;
    rest.unshift(basename(head));
    head = up;
  }
  try { head = realpathSync.native(head); } catch { /* keep it as written */ }
  return join(head, ...rest);
}

// True when target (a realish path) is root or lies under it (case-insensitive on Windows, as path.relative is there).
function within(root, target) {
  const rel = relative(root, target);
  return rel === "" || (!isAbsolute(rel) && rel.split(sep)[0] !== "..");
}

// True when p, as given, cannot be judged by its real path: a '..' in it (the OS climbs a link's target, path.resolve the
// link's name), a dangling link on it (realish keeps one as a plain name, Python's resolve follows it to its target, so
// the twins would judge different directories), or a part that cannot be read. Both twins then give the stricter
// answer. render.py links_unsure is the twin.
export function linksUnsure(p) {
  if (typeof p !== "string") return true;
  if (p.split(/[\\/]/).includes("..")) return true;
  const parts = [];
  for (let d = resolve(p); ; d = dirname(d)) {
    parts.unshift(d);
    if (dirname(d) === d) break;
  }
  for (const d of parts) {  // the root first
    let st;
    try { st = lstatSync(d); } catch (e) {
      if (e.code === "ENOENT" || e.code === "ENOTDIR") return false;  // nothing below a missing directory exists
      return true;
    }
    if (st.isSymbolicLink() && !existsSync(d)) return true;  // dangling, or a loop
  }
  return false;
}

// The nearest directory at or above p that holds a .git (a directory, a file, or a link, even a broken one), or null.
// Throws when a .git cannot be looked for.
function gitTop(p) {
  for (let d = p; ; d = dirname(d)) {
    try {
      lstatSync(join(d, ".git"));
      return d;
    } catch (e) {
      if (e.code !== "ENOENT" && e.code !== "ENOTDIR") throw e;
    }
    if (dirname(d) === d) return null;
  }
}

// True when dir, or a directory above it, holds a .git (a repository's directory, or the file a worktree or a submodule
// has; a link named .git counts, even a broken one): git could commit what lands there. linksUnsure paths (a dangling
// link, a '..') and paths that cannot be read count as inside. render.py in_git_tree is the twin, and its docstring
// names the known limit (a work tree with no .git above it, as a bare dotfiles repository with core.worktree=$HOME
// has); tests/test_shader_harness.py runs both over the same paths.
export function inGitTree(dir) {
  if (linksUnsure(dir)) return true;
  try { return gitTop(realish(resolve(dir))) !== null; } catch { return true; }  // cannot tell: the stricter answer
}

// True only when git itself says the file `file` is ignored by the work tree that holds it: `git check-ignore -q
// --no-index` at that tree's root (the nearest .git above), with the caller's GIT_* environment and its global excludes
// file (core.excludesFile) left out, since they belong to one user, not to the repository a commit is made from. The
// directory is resolved and the name kept, so a link at the probe's own name cannot answer for another place. False when
// there is no such tree, or git is missing, errors or times out: the caller refuses (fail closed). render.py git_ignores
// is the twin.
export function gitIgnores(file) {
  if (typeof file !== "string" || !isAbsolute(file) || linksUnsure(file)) return false;
  const p = join(realish(resolve(dirname(file))), basename(file));
  let top;
  try { top = gitTop(dirname(p)); } catch { return false; }
  if (top === null) return false;
  const env = Object.fromEntries(Object.entries(process.env).filter(([k]) => !k.toUpperCase().startsWith("GIT_")));
  const rel = relative(top, p).split(sep).join("/");
  const run = spawnSync("git", ["-c", "core.excludesFile=", "check-ignore", "-q", "--no-index", "--", `./${rel}`],
                        { cwd: top, env, stdio: "ignore", timeout: GIT_TIMEOUT_MS, windowsHide: true });  // ./: no :(magic)
  return !run.error && run.status === 0;
}

// Why r may not stand beside <repo>/state/ as a root pictures may land under, or null. The driver cannot compute the
// world's root render.py gives (core.paths), so it holds a plan's state_roots to this: absolute, named state (as given
// and once resolved), not a filesystem or drive root, not the user's home nor a directory above it, and not inside a
// git tree. render.py state_root_refusal is the twin, and render.py names only roots that pass it.
export function stateRootRefusal(r) {
  if (typeof r !== "string" || !isAbsolute(r)) return `a state root is an absolute path, not ${JSON.stringify(r)}`;
  if (linksUnsure(r)) return `a state root is a real directory (no '..', no dangling link): ${r}`;
  const given = resolve(r), real = realish(given), home = realish(resolve(homedir()));
  const named = (p) => (process.platform === "win32" ? basename(p).toLowerCase() : basename(p)) === "state";
  if (dirname(real) === real || dirname(given) === given) return `a state root is never a filesystem root: ${r}`;
  if (!named(given) || !named(real)) return `a state root is a directory named state: ${r}`;
  if (within(real, home)) return `a state root is never the user's home nor a directory above it: ${r}`;
  if (inGitTree(real)) return `a state root lies inside no git tree: ${r}`;
  return null;
}

// True for an absolute path that may hold pictures (real paths on both sides, so a link cannot lead out of a root):
//   * at or under <repo>/state/, while git ignores a file there (gitIgnores, asked of GIT_PROBE in it): .gitignore
//     re-includes parts of state/ (state/coord/**, state/drills/**, state/rewrites/), which git would commit;
//   * or under one of `roots` (the plan's state_roots: render.py extra_state_roots, the world's gitignored state/) that
//     passes stateRootRefusal, with no .git at or above it.
// A linksUnsure path (a '..', a dangling link) is refused. The driver accepts no root it was not given.
export function underState(dir, roots = []) {
  if (typeof dir !== "string" || !isAbsolute(dir) || linksUnsure(dir)) return false;
  const target = realish(resolve(dir));
  if (within(realish(resolve(STATE_ROOT)), target)) return !inGitTree(target) || gitIgnores(join(target, GIT_PROBE));
  return Array.isArray(roots) && roots.some((r) => stateRootRefusal(r) === null && within(realish(resolve(r)), target))
    && !inGitTree(target);
}

// The plan's state_roots (absolute paths render.py wrote), or why the list is not one. Absent means none. Each root is
// held to stateRootRefusal: a hand-edited plan cannot name '/', a home, or a directory not named state.
export function planStateRoots(plan) {
  const roots = plan?.state_roots ?? [];
  if (!Array.isArray(roots)) return { roots: [], why: "a plan's state_roots are a list of absolute paths" };
  for (const r of roots) {
    const why = stateRootRefusal(r);
    if (why) return { roots: [], why: `a plan's state_roots: ${why}` };
  }
  return { roots, why: null };
}

// The lab server's origin a job URL names, or a reason it names none we load: http, 127.0.0.1 spelled exactly, an
// explicit port that is not Daniel's, no credentials, and a page under /lab/.
function labOrigin(url) {
  let u;
  try { u = new URL(String(url)); } catch { return { why: `not a URL: ${url}` }; }
  if (u.protocol !== "http:" || u.hostname !== "127.0.0.1" || !u.port || u.username || u.password) {
    return { why: `a job loads the shader lab server on http://127.0.0.1:<port> only, not ${url}` };
  }
  if (FORBIDDEN_PORTS[Number(u.port)]) return { why: `port ${u.port} is refused: it is ${FORBIDDEN_PORTS[u.port]}` };
  if (!u.pathname.startsWith("/lab/")) return { why: `a job loads a lab page (/lab/...), not ${u.pathname}` };
  return { u };
}

// Why the driver will not run this job, or null. render.py writes every plan, so a refusal here means a plan that was
// hand-edited or written by something else: the driver answers it before the lock is taken or Chrome launched.
// `roots` are the plan's state_roots (planStateRoots): an out_dir under one of them counts as under state/.
export function jobRefusal(job, roots = []) {
  if (!job || typeof job !== "object") return "a job is an object";
  const { u, why } = labOrigin(job.url);
  if (why) return why;
  let corpus = false;
  for (const name of ["src", "media"]) {  // the page fetches these: from the same server, or not at all
    const value = u.searchParams.get(name);
    if (value === null) continue;
    let target;
    try { target = new URL(value, u); } catch { return `${name} is not a URL: ${value}`; }
    if (target.origin !== u.origin) return `${name} must come from the lab server (${u.origin}), not ${target.origin}`;
    // serve.py unquotes a path before it routes it, so /corpus%2F<id>/... or /%63orpus/... is the corpus too
    let path;
    try { path = decodeURIComponent(target.pathname); } catch { return `${name} is not a path the lab server reads: ${value}`; }
    if (name === "src" && path.startsWith("/corpus/")) corpus = true;
  }
  if (!PREFIX.test(String(job.prefix ?? ""))) return `a frame prefix is a plain name, not ${JSON.stringify(job.prefix)}`;
  if (underState(job.out_dir, roots)) return null;
  if (typeof job.out_dir !== "string" || !isAbsolute(job.out_dir)) return `out_dir must be an absolute path: ${job.out_dir}`;
  const where = [STATE_ROOT, ...roots].join(" or ");
  if (corpus) {
    return `out_dir ${job.out_dir} is outside ${where}: pictures of a corpus shader (someone else's work) stay under `
      + "state/, which git ignores, whatever the plan says";
  }
  if (job.allow_tracked === true) return null;  // render.py --allow-tracked, for a shader of our own
  return `out_dir ${job.out_dir} is outside ${where} (render.py's --allow-tracked lets a non-corpus shader out)`;
}

// Write one picture, judged again at the moment of writing: `refusal` reruns the job's check (jobRefusal), so an out_dir
// swapped for a link while the driver waited for the lock or rendered is refused here, not only at plan time. A link at
// the file's own name is removed, never followed, and "wx" refuses one that reappears before the open.
export function writePicture(file, bytes, refusal = () => null) {
  const why = refusal();
  if (why) throw new Error(`refused at write time: ${why}`);
  rmSync(file, { force: true });
  writeFileSync(file, bytes, { flag: "wx" });
}

export function planRefusal(plan) {
  if (!plan || !Array.isArray(plan.jobs)) return "a plan's jobs are a list";
  const { roots, why: badRoots } = planStateRoots(plan);
  if (badRoots) return badRoots;
  for (const job of plan.jobs) {
    const why = jobRefusal(job, roots);
    if (why) return `job ${job?.key ?? "?"}: ${why}`;
  }
  return null;
}

// ------------------------------------------------------------------------------------------- the burst's budget
// Whether job `index` may start `elapsedMs` into the burst, and its deadline. Every job, the first included, ends
// SHUTDOWN_MARGIN_MS before the burst does. A later job starts only if its whole requested timeout fits, so the budget
// never cuts one short; the first starts with whatever fits (a burst must make progress), unless that is under a second.
export function jobWindow({ index, elapsedMs, timeoutMs, burstMs = BURST_MS, marginMs = SHUTDOWN_MARGIN_MS }) {
  const roomMs = burstMs - marginMs - elapsedMs;
  if (roomMs < MIN_JOB_MS || (index > 0 && timeoutMs > roomMs)) return { start: false, deadlineMs: 0, roomMs };
  return { start: true, deadlineMs: Math.min(timeoutMs, roomMs), roomMs };
}

// A relaunch (after a timeout) begins only if even a slow one still leaves the shutdown its margin.
export const mayRelaunch = (elapsedMs, burstMs = BURST_MS) => elapsedMs + LAUNCH_RESERVE_MS + SHUTDOWN_MARGIN_MS <= burstMs;

// A job whose deadline the budget cut short tells its page so: the page's own watchdog (budget=) then fires 5 s before
// the driver's deadline, and the page reports what it has instead of being killed mid-draw.
export function clipBudget(url, deadlineMs) {
  const cap = Math.max(MIN_JOB_MS, deadlineMs - 5000);
  return String(url).replace(/([?&]budget=)(\d+)/, (_, key, value) => `${key}${Math.min(Number(value), cap)}`);
}

// ----------------------------------------------------------------------------------------------- the house lock
// How the driver asks for the lock. lock_wait_ms 0 means "do not wait" (one try, then one poll), never the default.
// signals: false, because gpu_lock.mjs's own signal handlers would release the lock before our Chrome is dead
// (they are prepended); guardHolder installs the driver's own, in the right order.
export function lockOptions(plan = {}) {
  const wait = plan.lock_wait_ms;
  return { label: plan.label || "shader render", maxHoldMs: LOCK_HOLD_MS, signals: false,
           maxWaitMs: typeof wait === "number" && Number.isFinite(wait) && wait >= 0 ? wait : DEFAULT_LOCK_WAIT_MS };
}

// On the way out our Chrome dies first and the lock goes second, so no waiter (a load-sensitive jam lane above all)
// takes the lock while our SwiftShader browser still burns CPU. The exit handler is PREPENDED, ahead of the one
// gpu_lock.mjs registered when the lock was taken. Returns the function that removes these handlers again.
export function guardHolder({ kill, release, exit = (code) => process.exit(code), proc = process }) {
  const handlers = {};
  const off = () => {
    proc.removeListener("exit", onExit);
    for (const [sig, fn] of Object.entries(handlers)) proc.removeListener(sig, fn);
  };
  const down = () => { off(); try { kill(); } finally { release(); } };
  const onExit = () => down();
  for (const [sig, code] of [["SIGINT", 130], ["SIGTERM", 143], ["SIGHUP", 129]]) {
    handlers[sig] = () => { down(); exit(code); };
    proc.on(sig, handlers[sig]);
  }
  proc.prependListener("exit", onExit);
  return off;
}

const stampNow = () => new Date().toISOString().slice(0, 19).replace(/[-:]/g, "").replace("T", "-");
const say = (...a) => console.log("[headless]", ...a);

function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([promise, new Promise((_, reject) => {
    timer = setTimeout(() => reject(Object.assign(new Error(`no answer within ${ms} ms: ${what}`), { timeout: true })), ms);
  })]).finally(() => clearTimeout(timer));
}

// The house CDP client (arsenal/lanes/chrome_verify.mjs), plus a close and a fail-fast when the socket drops.
function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    let nextId = 1;
    const pending = new Map();
    const listeners = [];
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const { res, rej } = pending.get(msg.id);
        pending.delete(msg.id);
        if (msg.error) rej(new Error(JSON.stringify(msg.error)));
        else res(msg.result);
      } else if (msg.method) {
        for (const listener of listeners) listener(msg);
      }
    };
    ws.onclose = () => { for (const { rej } of pending.values()) rej(new Error("DevTools socket closed")); pending.clear(); };
    ws.onopen = () => resolve({
      send: (method, params = {}) => new Promise((res, rej) => {
        if (ws.readyState !== 1) return rej(new Error("DevTools socket closed"));
        const id = nextId++;
        pending.set(id, { res, rej });
        ws.send(JSON.stringify({ id, method, params }));
      }),
      on: (fn) => listeners.push(fn),
      close: () => { try { ws.close(); } catch { /* already closed */ } },
    });
    ws.onerror = () => reject(new Error(`websocket error on ${wsUrl}`));
  });
}

// ------------------------------------------------------------------------------------------------------- Chrome
class Browser {
  constructor(exe) { this.exe = exe; this.proc = null; this.profile = null; this.cdp = null; this.page = null; this.events = []; }

  async launch() {
    const t0 = Date.now();
    this.profile = join(PROFILE_ROOT, `run-${stampNow()}-${process.pid}-${Math.random().toString(36).slice(2, 7)}`);
    mkdirSync(this.profile, { recursive: true });
    this.flags = chromeFlags(this.profile);
    this.proc = spawn(this.exe, this.flags, { stdio: "ignore", windowsHide: true });
    this.exited = new Promise((res) => this.proc.once("exit", res));
    this.proc.once("error", (e) => say("chrome failed to start:", e.message));
    const portFile = join(this.profile, "DevToolsActivePort");
    let port = null, path = null;
    while (Date.now() - t0 < DEVTOOLS_WAIT_MS && !port) {
      if (this.proc.exitCode !== null) throw new Error(`chrome exited at start (code ${this.proc.exitCode})`);
      try {
        const [p, w] = readFileSync(portFile, "utf8").split(/\r?\n/);
        if (p && w) { port = Number(p); path = w; }
      } catch { /* not written yet */ }
      if (!port) await delay(50);
    }
    if (!port) throw new Error(`chrome wrote no DevToolsActivePort within ${DEVTOOLS_WAIT_MS} ms`);
    this.port = port;
    this.cdp = await connect(`ws://127.0.0.1:${port}${path}`);
    this.version = (await this.cdp.send("Browser.getVersion")).product;
    const targets = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
    let target = targets.find((t) => t.type === "page");
    if (!target) {
      await this.cdp.send("Target.createTarget", { url: "about:blank" });
      target = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page");
    }
    this.page = await connect(target.webSocketDebuggerUrl);
    this.page.on((msg) => {
      const p = msg.params;
      if (msg.method === "Runtime.consoleAPICalled" && (p.type === "error" || p.type === "warning" || p.type === "assert")) {
        this.events.push({ type: p.type, text: p.args.map((a) => a.value ?? a.description ?? "").join(" ").slice(0, 600) });
      } else if (msg.method === "Runtime.exceptionThrown") {
        const d = p.exceptionDetails;
        this.events.push({ type: "exception", text: String(d.exception?.description || d.text).slice(0, 600), url: d.url, line: d.lineNumber });
      } else if (msg.method === "Log.entryAdded" && (p.entry.level === "error" || p.entry.level === "warning")) {
        this.events.push({ type: `log-${p.entry.level}`, text: String(p.entry.text).slice(0, 600), url: p.entry.url });
      }
    });
    for (const domain of ["Runtime", "Log", "Page"]) await this.page.send(`${domain}.enable`);
    this.startMs = Date.now() - t0;
    return this.startMs;
  }

  // The browser's own GPU report and a WebGL2 context's renderer string, judged before any job page loads.
  async assertSoftware() {
    let sys = null;
    try { sys = await withTimeout(this.cdp.send("SystemInfo.getInfo"), 15_000, "SystemInfo.getInfo"); } catch (e) { sys = { error: e.message }; }
    const gpu = sys?.gpu || {};
    const sysRenderer = gpu.auxAttributes?.glRenderer || null;
    const devices = (gpu.devices || []).map((d) => ({ vendor: d.vendorString, device: d.deviceString, driver: d.driverVendor }));
    const webgl = await this.evaluate(`(() => { const c = document.createElement("canvas"); const g = c.getContext("webgl2");
      if (!g) return null; const d = g.getExtension("WEBGL_debug_renderer_info");
      const out = { renderer: d ? g.getParameter(d.UNMASKED_RENDERER_WEBGL) : g.getParameter(g.RENDERER),
                    version: g.getParameter(g.VERSION), float_targets: !!g.getExtension("EXT_color_buffer_float") };
      const lose = g.getExtension("WEBGL_lose_context"); if (lose) lose.loseContext(); return out; })()`, 15_000);
    const judged = judgeRenderer(webgl?.renderer);
    const sysJudged = judgeRenderer(sysRenderer);
    const hwDevice = devices.find((d) => !judgeRenderer(`${d.device} ${d.vendor} ${d.driver}`).ok);
    const ok = judged.ok && sysJudged.ok && !hwDevice;
    return {
      ok, reason: ok ? null : (judged.reason || sysJudged.reason || `the GPU report lists a hardware device: ${JSON.stringify(hwDevice)}`),
      webgl, gl_renderer: sysRenderer, gl_implementation: gpu.auxAttributes?.glImplementationParts || null,
      devices, feature_status: gpu.featureStatus || null, sysinfo_error: sys?.error || null,
    };
  }

  async evaluate(expression, ms = 10_000) {
    const r = await withTimeout(this.page.send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }),
                                ms, expression.replace(/\s+/g, " ").slice(0, 100));
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
    return r.result.value;
  }

  // Browser.close, then killTree on our own pid if it is still running. Returns how it ended.
  async close() {
    if (!this.proc) return null;
    const pid = this.proc.pid;
    let how = "already-exited";
    if (this.proc.exitCode === null) {
      try { await withTimeout(this.cdp?.send("Browser.close") ?? Promise.resolve(), 3000, "Browser.close"); } catch { /* wedged */ }
      const exited = await Promise.race([this.exited.then(() => true), delay(4000).then(() => false)]);
      how = "browser-close";
      if (!exited) {
        killTree(pid);
        await Promise.race([this.exited, delay(3000)]);
        how = KILL_HOW;
      }
    }
    this.page?.close(); this.cdp?.close();
    for (let i = 0; i < 10; i += 1) {  // Chrome's children let go of the profile a moment after the browser exits
      try { rmSync(this.profile, { recursive: true, force: true }); break; } catch { await delay(300); }
    }
    this.proc = null;
    return { pid, how };
  }

  killSync() {  // the exit path: no awaits allowed
    if (this.proc && this.proc.exitCode === null) killTree(this.proc.pid);
  }
}

// Profiles a killed driver left behind (older than an hour; a live run's profile is minutes old at most).
function sweepProfiles() {
  try {
    for (const name of readdirSync(PROFILE_ROOT)) {
      const p = join(PROFILE_ROOT, name);
      if (name.startsWith("run-") && Date.now() - statSync(p).mtimeMs > 3_600_000) rmSync(p, { recursive: true, force: true });
    }
  } catch { /* best effort */ }
}

// ---------------------------------------------------------------------------------------------------------- jobs
const tName = (t) => `t${Number(t).toFixed(2)}`;

// `roots` are the plan's state_roots: every picture write judges job.out_dir against them again (writePicture).
export async function runJob(browser, job, deadlineAt, roots = []) {
  const t0 = Date.now();
  const nonce = `${process.pid}-${t0}-${Math.random().toString(36).slice(2, 8)}`;
  const url = `${job.url}${job.url.includes("?") ? "&" : "?"}job=${nonce}`;
  const row = { key: job.key, url: job.url, status: "error", render: null, frames: [], screenshot: null,
                console: [], error: null, wall_ms: 0, renderer_judged: null };
  browser.events = [];
  try {
    await browser.page.send("Emulation.setDeviceMetricsOverride", { width: job.w, height: job.h, deviceScaleFactor: 1, mobile: false });
    await browser.page.send("Page.navigate", { url });
    const probe = `(() => { if (!location.search.includes(${JSON.stringify("job=" + nonce)})) return "loading";
      const r = window.__render; return r ? (r.done ? "done" : "pending") : "no-render"; })()`;
    let state = "loading";
    while (Date.now() < deadlineAt) {
      try { state = await browser.evaluate(probe, Math.max(500, Math.min(5000, deadlineAt - Date.now()))); } catch (e) {
        state = e.timeout ? "unanswered (the page's main thread is busy)" : "loading";
      }
      if (state === "done") break;
      await delay(POLL_MS);
    }
    if (state !== "done") {
      row.status = "timeout";
      row.error = `the page did not finish within ${job.timeout_ms} ms (last state: ${state})`;
      try { row.render = await browser.evaluate(`(() => { const r = window.__render; if (!r) return null;
        return JSON.parse(JSON.stringify({ ...r, frames: (r.frames || []).map((f) => ({ t: f.t, ms: f.ms })) })); })()`, 1500); } catch { /* wedged */ }
      return row;
    }
    // The report first, without the PNGs; then each frame on its own, so no single DevTools message is huge. A frame's
    // other facts (render.js adds frame, frames walked, reset) ride along; a hole in the list stays a hole, so index i
    // here is index i in the page.
    row.render = await browser.evaluate(`(() => { const r = window.__render;
      return JSON.parse(JSON.stringify({ ...r, frames: (r.frames || []).map((f) => f ? ({ ...f, png: undefined,
        has_png: typeof f.png === "string" }) : { t: null, ms: null, has_png: false }) })); })()`, 10_000);
    const judged = judgeRenderer(row.render?.renderer ?? browser.renderer);
    row.renderer_judged = judged;
    if (!judged.ok) {
      row.status = "refused";
      row.error = `frames discarded: ${judged.reason}`;
      return row;
    }
    const again = () => jobRefusal(job, roots);  // the plan's check, rerun now that the lock wait and the render are over
    const moved = again();
    if (moved) throw new Error(`refused at write time: ${moved}`);
    mkdirSync(job.out_dir, { recursive: true });
    const used = new Set();  // two times that round to one name (t=1 and t=1.001) must not overwrite each other
    for (let i = 0; i < (row.render?.frames || []).length; i++) {
      const { has_png: hasPng, ...f } = row.render.frames[i];
      if (!hasPng) { row.frames.push({ ...f, file: null, bytes: 0 }); continue; }
      const dataUrl = await browser.evaluate(`window.__render.frames[${i}].png`, 20_000);
      const m = /^data:image\/png;base64,(.*)$/s.exec(String(dataUrl || ""));
      const bytes = m ? Buffer.from(m[1], "base64") : null;
      if (!bytes || bytes.length < 8 || bytes.readUInt32BE(0) !== 0x89504e47) {
        row.frames.push({ ...f, file: null, bytes: 0, error: "not a PNG data URL" });
        continue;
      }
      let stem = `${job.prefix || ""}${tName(f.t)}`;
      if (used.has(stem)) stem = `${stem}_${i}`;
      used.add(stem);
      const file = join(job.out_dir, `${stem}.png`);
      writePicture(file, bytes, again);
      row.frames.push({ ...f, file, bytes: bytes.length });
    }
    if (job.screenshot) {
      const shot = await withTimeout(browser.page.send("Page.captureScreenshot", { format: "png" }), 30_000, "screenshot");
      row.screenshot = join(job.out_dir, `${job.prefix || ""}screenshot.png`);
      writePicture(row.screenshot, Buffer.from(shot.data, "base64"), again);
    }
    row.status = "done";
    return row;
  } catch (e) {
    row.status = e.timeout ? "timeout" : "error";
    row.error = String(e && e.message || e);
    return row;
  } finally {
    row.console = browser.events.slice(0, 40);
    row.wall_ms = Date.now() - t0;
  }
}

// ---------------------------------------------------------------------------------------------------------- burst
async function burst(plan, resultPath, exe) {
  const t0 = Date.now();
  const result = {
    api: RESULT_API, plan_label: plan.label, started_at: new Date().toISOString(), finished_at: null,
    chrome: { exe, version: null, flags: null, launches: [], closes: [] },
    software: null, refused: null, lock: null, jobs: [], timings: {}, error: null,
  };
  const save = () => { mkdirSync(dirname(resultPath), { recursive: true }); writeFileSync(resultPath, JSON.stringify(result, null, 1)); };
  const burstMs = Math.min(Number(plan.burst_ms) || BURST_MS, BURST_MS);
  const jobs = plan.jobs || [];
  const { roots } = planStateRoots(plan);  // planRefusal has passed them

  const { acquireGpuLock } = await import("../lanes/gpu_lock.mjs");
  let gpu;
  const lockT0 = Date.now();
  try {
    gpu = await acquireGpuLock(lockOptions(plan));
  } catch (e) {
    result.error = e.message;
    result.jobs = jobs.map((j) => ({ key: j.key, url: j.url, status: "deferred", error: "GPU lock not taken" }));
    result.finished_at = new Date().toISOString();
    save();
    return EXIT.lock;
  }
  const heldFrom = Date.now();
  result.lock = { label: plan.label, acquired_at: gpu.owner.acquiredAt, inherited: gpu.inherited, wait_ms: heldFrom - lockT0 };
  const browser = new Browser(exe);
  const unguard = guardHolder({ kill: () => browser.killSync(), release: () => gpu.release() });
  let code = EXIT.ok;
  try {
    sweepProfiles();
    const start = async () => {
      const ms = await browser.launch();
      result.chrome.version = browser.version;
      result.chrome.flags = browser.flags.filter((f) => !f.startsWith("--user-data-dir="));
      const sw = await browser.assertSoftware();
      result.chrome.launches.push({ pid: browser.proc.pid, start_ms: ms, software: sw.ok });
      result.software = sw;
      browser.renderer = sw.webgl?.renderer || null;
      say(`chrome ${browser.version} pid ${browser.proc.pid} up in ${ms} ms; renderer: ${browser.renderer}`);
      return sw;
    };
    const sw = await start();
    if (!sw.ok) {
      result.refused = sw.reason;
      say("REFUSED:", sw.reason);
      result.jobs = jobs.map((j) => ({ key: j.key, url: j.url, status: "refused", error: sw.reason }));
      code = EXIT.refused;
      return code;
    }
    for (let i = 0; i < jobs.length; i++) {
      const job = jobs[i];
      const timeout = Math.min(Number(job.timeout_ms) || 60_000, burstMs);
      const windowNow = () => jobWindow({ index: i, elapsedMs: Date.now() - heldFrom, timeoutMs: timeout, burstMs });
      const defer = (why) => {
        for (const rest of jobs.slice(i)) result.jobs.push({ key: rest.key, url: rest.url, status: "deferred", error: why });
        say(`${why} after ${i} job(s); ${jobs.length - i} deferred to the next burst`);
      };
      let w = windowNow();
      if (!w.start) { defer("burst budget reached"); break; }
      if (!browser.proc) {  // relaunched after a timeout; the launch spends part of the burst, so judge it again after
        if (!mayRelaunch(Date.now() - heldFrom, burstMs)) { defer("burst budget: no room to relaunch Chrome"); break; }
        const again = await start();
        if (!again.ok) {
          result.refused = again.reason;
          for (const rest of jobs.slice(i)) result.jobs.push({ key: rest.key, url: rest.url, status: "refused", error: again.reason });
          code = EXIT.refused;
          break;
        }
        w = windowNow();
        if (!w.start) { defer("burst budget reached (after a relaunch)"); break; }
      }
      const row = await runJob(browser, { ...job, url: clipBudget(job.url, w.deadlineMs), timeout_ms: w.deadlineMs },
                               Date.now() + w.deadlineMs, roots);
      result.jobs.push(row);
      say(`${row.key}: ${row.status} in ${row.wall_ms} ms` + (row.error ? ` (${row.error.slice(0, 160)})` : ""));
      if (row.status === "timeout" || /socket closed/.test(row.error || "")) {
        result.chrome.closes.push({ reason: `${row.status} on ${row.key}`, ...(await browser.close()) });
      }
      save();
    }
  } catch (e) {
    result.error = String(e && e.stack || e);
    code = EXIT.error;
  } finally {
    const closed = await browser.close();  // our Chrome first, the lock second (as guardHolder does on a signal)
    if (closed) result.chrome.closes.push({ reason: "end of burst", ...closed });
    gpu.release();
    unguard();
    result.lock.held_ms = Date.now() - heldFrom;
    result.timings = { total_ms: Date.now() - t0, lock_wait_ms: result.lock.wait_ms, held_ms: result.lock.held_ms };
    result.finished_at = new Date().toISOString();
    save();
  }
  return code;
}

// ------------------------------------------------------------------------------------------------------------ CLI
if (process.argv[1] && resolve(process.argv[1]).toLowerCase() === fileURLToPath(import.meta.url).toLowerCase()) {
  const { values: opt } = parseArgs({
    options: { plan: { type: "string" }, result: { type: "string" }, "check-renderer": { type: "string" },
               "print-flags": { type: "boolean", default: false } },
  });
  if (opt["check-renderer"] !== undefined) {
    console.log(JSON.stringify(judgeRenderer(opt["check-renderer"])));
    process.exit(0);
  }
  if (opt["print-flags"]) {
    console.log(JSON.stringify(chromeFlags("<profile>")));
    process.exit(0);
  }
  if (!opt.plan || !opt.result) {
    console.log("usage: node arsenal/shaders/headless.mjs --plan <plan.json> --result <result.json>\n" +
                "       node arsenal/shaders/headless.mjs --check-renderer \"<renderer string>\" | --print-flags");
    process.exit(EXIT.usage);
  }
  const plan = JSON.parse(readFileSync(opt.plan, "utf8"));
  if (plan.api !== PLAN_API) { console.log(`not a plan (${plan.api}, want ${PLAN_API})`); process.exit(EXIT.usage); }
  const refusal = planRefusal(plan);
  if (refusal) {  // before the lock and before Chrome: nothing of this plan runs
    say("REFUSED:", refusal);
    const jobs = Array.isArray(plan.jobs) ? plan.jobs : [];
    const { roots, why: badRoots } = planStateRoots(plan);
    mkdirSync(dirname(opt.result), { recursive: true });
    writeFileSync(opt.result, JSON.stringify({
      api: RESULT_API, plan_label: plan.label ?? null, started_at: new Date().toISOString(),
      finished_at: new Date().toISOString(), chrome: { exe: resolveChrome(plan), version: null, flags: null, launches: [], closes: [] },
      software: null, refused: refusal, lock: null, timings: {}, error: null,
      jobs: jobs.map((j) => ({ key: j?.key ?? null, url: j?.url ?? null, status: "refused",
                              error: jobRefusal(j, roots) || badRoots || "another job in this plan was refused" })),
    }, null, 1));
    process.exit(EXIT.refused);
  }
  const exe = resolveChrome(plan);
  if (!exe) {
    console.log(`no Chrome found: set ${CHROME_ENV}, or install Playwright's Chromium (playwright install chromium), `
                + `or put one of ${PATH_CHROMES.join(", ")} on PATH`);
    process.exit(EXIT.error);
  }
  if (!existsSync(exe)) { console.log(`no Chrome at ${exe}`); process.exit(EXIT.error); }
  const code = await burst(plan, opt.result, exe);
  process.exit(code);
}
