"""core.comm.runner_lib -- shared hardening for OpenAI-compatible seat transports (K0, 2026-07-18).

The rule-of-three extraction's second seam (fence: deepseek counter sec 1, accepted): the
G4/L0 anti-wedge client factory as GENUS -- explicit parameters only, no env reads here.
Each seat module (deepseek_chat, kimi_chat, ...) wraps this with its OWN env-var conventions
and defaults; that keeps every seat's tuning surface local and greppable, per the fence.

The Agent/loop layer deliberately does NOT live here ("premature generalization" ruling):
chat-completions loops stay species-specific until two of them stabilize side by side.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys

# ---------------------------------------------------------------- stop signals
# WHY: systemd (KillMode=control-group) and ManagedChild.terminate() stop a runner with SIGTERM.
# Python's default SIGTERM action kills the process on the spot, so the runner's `finally` never
# ran: no retire_seat, no runner_lock.release, no exit summary. Each restart left a dead holder on
# the runner lock (the next daemon idled ~20 s on "a foreign runner holds the runner lock") and a
# 'running' roster card the doctor paged as HARD WEDGE. The handler below makes a stop signal take
# the same exit path as Ctrl-C always has.
#
# One-shot by design: the first SIGTERM/SIGINT sets BOTH to SIG_IGN before raising, so a second
# signal (systemd and the daemon both send one; humans press Ctrl-C twice) cannot raise into the
# cleanup it started. POSIX only: on Windows ManagedChild stops a child with TerminateProcess,
# which no handler can catch, and Ctrl-C keeps its default -- that behaviour is unchanged.
_STOP_SIGNAL_NAMES = ("SIGTERM", "SIGINT")
_STOP_STATE: dict = {"signum": None, "installed": False}


class StopSignal(KeyboardInterrupt):
    """The KeyboardInterrupt a stop signal raises. A subclass, so every runner's existing
    `except (KeyboardInterrupt, EOFError)` exit path catches it unchanged."""

    def __init__(self, signum: int):
        super().__init__(f"stop signal {signum}")
        self.signum = int(signum)


def _stop_signals() -> list:
    return [s for s in (getattr(signal, n, None) for n in _STOP_SIGNAL_NAMES) if s is not None]


def ignore_stop_signals() -> None:
    """Disarm: SIG_IGN for SIGTERM/SIGINT. Call it FIRST in the cleanup, so a stop signal that
    arrives while the cleanup runs cannot interrupt it. A no-op unless install_stop_signals()
    armed this process (so Windows, and any runner that never armed, keep their old behaviour).
    Never raises."""
    if _STOP_STATE.get("installed"):
        _ignore_stop_signals()


def _ignore_stop_signals() -> None:
    for s in _stop_signals():
        with contextlib.suppress(ValueError, OSError):
            signal.signal(s, signal.SIG_IGN)


def _on_stop_signal(signum, _frame):
    _STOP_STATE["signum"] = int(signum)
    _ignore_stop_signals()
    raise StopSignal(signum)


def install_stop_signals() -> bool:
    """Arm the one-shot stop handler: the first SIGTERM/SIGINT ignores both from then on and
    raises StopSignal (a KeyboardInterrupt) in the main thread, so the runner's `finally`
    (retire_seat, runner_lock.release) and its exit summary run. Install it right after the
    runner lock is won, immediately before the try whose finally releases that lock.

    Returns True when armed. False on Windows (deliberately unchanged) and when called off the
    main thread (signal.signal refuses there). Never raises."""
    if sys.platform == "win32":
        return False
    try:
        for s in _stop_signals():
            signal.signal(s, _on_stop_signal)
    except (ValueError, OSError):
        return False
    _STOP_STATE["installed"] = True
    return True


def stop_requested() -> bool:
    """True once a stop signal has arrived. A runner checks it at its loop top as a backstop:
    a broad `except BaseException` somewhere in a turn could swallow the StopSignal, and the
    handler has already set SIG_IGN, so without this check the runner would run on deaf to
    every later SIGTERM until a SIGKILL."""
    return _STOP_STATE.get("signum") is not None


def stop_exit_code(default: int = 0) -> int:
    """The exit code for a runner that stopped. SIGINT (Ctrl-C) keeps the old clean `default`.
    Any other stop signal exits 128+signum, the shell convention for "killed by" -- so a
    supervisor still sees an abnormal stop: before this handler a SIGTERM death read as -15, and
    ManagedChild respawned it; an exit 0 would instead read as a deliberate stay-down."""
    signum = _STOP_STATE.get("signum")
    if signum is None or signum == getattr(signal, "SIGINT", None):
        return default
    return 128 + int(signum)


def set_seat_agent(agent: str) -> str:
    """Declare which seat this runner process is, so its wire records are attributable.

    T160: BIFROST_AGENT was read in exactly one place and written in none, so 102 of 102 records
    in the live journal said 'unknown' and doctor's per-seat wire forensics matched nothing for
    as long as it had existed. Every runner already knows its own id from argv; nothing carried
    it the last few inches.

    Wrapped here rather than imported directly by four runners so the SAFETY NET has one
    definition: a runner must never fail to start because its instrumentation could not load.
    That is the same rule recording_http_client() follows, and for the same reason -- the
    blindness this cures is far cheaper than a seat that will not boot.
    """
    try:
        from scripts.wire_journal import set_seat_agent as _set

        return _set(agent)
    except Exception:
        return ""


def make_openai_compat_client(
    api_key: str,
    base_url: str,
    *,
    connect_timeout: float = 15.0,
    read_timeout: float = 120.0,
    max_retries: int = 1,
    record_wire: bool = True,
):
    """OpenAI-compatible client hardened against hung-stream wedges (G4/L0): a per-read
    streaming timeout turns a stalled model call into a caught httpx.ReadTimeout the caller's
    try/except revives from, and an explicit max_retries stops the SDK default (2) from
    tripling wall-clock before the wedge surfaces. Lazy imports keep this module import-cheap
    for callers that never build a client (e.g. spend-ledger-only tooling).

    T161: instrumented by default. kimi and gemini reach the wire ONLY through this factory, so
    one seam covers both -- and until now three of the four seats made model calls with no wire
    record at all, leaving system_fingerprint (the silent model-swap detector) unobservable for
    most of the fleet.

    THE TIMEOUT TRAVELS WITH THE CLIENT, and that is the trap this function has to avoid. Hand
    the SDK an http_client and it takes its timeout from THAT object, ignoring the one this
    factory would otherwise set -- so instrumenting is exactly how a seat silently loses the
    anti-wedge hardening this module exists to provide. The recorder is therefore built WITH the
    computed timeout, and every fallback below keeps it too. An instrumented but wedge-prone seat
    is a worse trade than a blind one, so if capture cannot be set up, capture is what we drop.
    """
    import httpx
    from openai import OpenAI

    timeout = httpx.Timeout(read_timeout, connect=connect_timeout)

    if record_wire:
        try:
            from scripts.wire_journal import recording_http_client

            http_client = recording_http_client(timeout=timeout)
            if http_client is not None:
                return OpenAI(api_key=api_key, base_url=base_url, http_client=http_client, max_retries=max_retries)
        except Exception:
            pass  # telemetry never blocks a launch -- fall through uninstrumented

    return OpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=max_retries)


def seat_session_id(agent: str, session=None) -> str:
    """The per-incarnation session id a runner beats the roster under (T147) -- ONE
    derivation, shared by the heartbeat thread and retire_seat(), so the card a runner
    retracts on exit is the card it wrote. Precedence is the heartbeat's historical one:
    an explicit --session, else BIFROST_INCARNATION, else "<pid>-<agent>" (the 2026-08-26
    receipt's deepseek#3708-dee is sid8 of "3708-deepseek")."""
    return str(session or os.environ.get("BIFROST_INCARNATION") or f"{os.getpid()}-{agent}")


def retire_seat(
    agent: str,
    session_id: str,
    *,
    stop_hb=None,
    hb_thread=None,
    hb_join_s: float = 6.0,
    ns=None,
    client=None,
    bare_phase=None,
) -> dict:
    """A planned exit retracts its own phase card, HONESTLY (defer 8c881ab628, amended).

    WHY: every runner's heartbeat thread beats {ns}:worklive:{agent}#{sid8} with phase='running'
    and a since_ts the roster preserves across beats, and until this landed the exit path stopped
    the thread, released the lock, and nothing else. The card outlived the process for 180s:
    non-idle, aged, dead pulse -- the doctor's HARD WEDGE signature -- so a drained deepseek paged
    after a clean exit 0. An emitter without a retraction path manufactures false pages.

    THE AMENDED GUARANTEE (the 09-08 adversarial REJECT, folded in): a planned exit retracts
    its card ONLY when the retraction is PROVEN sound, and reports the truth otherwise:

      1. stop_hb.set()             -- ask the beat thread to stop;
      2. hb_thread.join(hb_join_s) -- WAIT for it. An in-flight beat after the delete would
                                     re-create the card (discord's *_pulse* beats the BARE
                                     worklive plane via `beat(wl)`, the roster heartbeat beats
                                     the INCARNATION plane -- both must stop);
      3. THE KEY GUARD THE REJECT DEMANDED: only when the beat thread is CONFIRMED stopped
         (hb_thread is None, or joined, i.e. not alive after join) do we go_offline. If it is
         still alive, we do NOT delete the card -- we report retracted=False with the reason,
         because a delete here would be immediately resurrected and would fake a clean exit.
      4. roster.go_offline(...)    -- delete the incarnation card and stamp the seatseen
                                     witness phase='offline' + offline_ts. It honors ns/client
                                     (unlike the OLD bare-phase write, REJECT defect (2)).
      5. the BARE worklive key     -- stand its phase down via the SAME namespace/client the
                                     roster plane used, so a test client never touches the
                                     shared Redis. bare_phase=None skips it (probe/test callers).
    Never raises -- an exit path must not fail on its own bookkeeping. Reversible by
    construction (roster O3): a later beat under the same id is LIVE again."""
    out = {
        "ok": False,
        "agent": str(agent),
        "session_id": str(session_id or ""),
        "sid8": str(session_id or "")[:8],
        "hb_joined": None,
        "offline_ts": None,
        "retracted": False,
    }
    try:
        if stop_hb is not None:
            stop_hb.set()
        hb_joined = None
        if hb_thread is not None:
            try:
                if hb_thread.is_alive():
                    hb_thread.join(max(0.0, float(hb_join_s)))
                hb_joined = not hb_thread.is_alive()
            except Exception:
                hb_joined = False
        out["hb_joined"] = hb_joined

        # REJECT defect (1): do NOT go_offline unless the beat thread is provably stopped.
        # A delete while a beat is in flight (or still looping) is resurrected by that beat,
        # and reporting a clean retraction that does not hold is a false page in reverse.
        if hb_thread is not None and hb_joined is False:
            out["retracted"] = False
            out["reason"] = (
                "beat thread still alive after join -- card NOT deleted "
                "(a delete here would be resurrected by an in-flight beat)"
            )
            return out

        _ns = ns or os.environ.get("BIFROST_NAMESPACE", "bifrost")
        from core.comm import roster

        rep = roster.go_offline(_ns, str(agent), str(session_id or ""), client=client) or {}
        out["ok"] = bool(rep.get("ok"))
        out["offline_ts"] = rep.get("offline_ts")
        out["retracted"] = bool(rep.get("ok"))

        # REJECT defect (2): the bare worklive write MUST honor ns/client. The old path called
        # liveness.worklive(agent).set(phase) which writes the REAL shared Redis regardless of
        # the test client/namespace -- contamination under a probe. We instead write the bare
        # worklive key through the SAME ns/client the roster plane used, keying it by name
        # directly (not via _worklive_prefix(), which reads the env var and would ignore ns).
        if bare_phase is not None:
            try:
                if client is not None:
                    client.delete(f"{_ns}:worklive:{agent}")
                else:
                    from core.comm import liveness as _liveness

                    _liveness.worklive(str(agent)).set(str(bare_phase))
            except Exception:
                pass
    except Exception:
        pass
    return out
