package com.nightwatch.etoro.probe

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.Kwarg
import java.io.File
import java.util.concurrent.locks.ReentrantLock
import kotlin.concurrent.withLock
import com.chaquo.python.android.AndroidPlatform

/**
 * Thin bridge to the Chaquopy-embedded Python (task requirement 5: prove the embedded runtime
 * works after a cold wake, not just when the app is open). Mirrors DESIGN §7.1's host.py idea in
 * miniature — Kotlin calls named Python functions, nothing more.
 */
object PyBridge {
    private var started = false
    private var callCounter = 0
    private val sessionLock = ReentrantLock()
    private val bridgeLock = ReentrantLock()
    internal const val DEMO_BUY_BUSY = "Demo buy refused: session, adoption, probe or demo buy already running"

    fun isDemoManualBuyBusy(): Boolean = sessionLock.isLocked || bridgeLock.isLocked

    internal fun withDemoManualBuyLocks(block: () -> String): String {
        if (sessionLock.isHeldByCurrentThread || !sessionLock.tryLock()) return DEMO_BUY_BUSY
        try {
            if (bridgeLock.isHeldByCurrentThread || !bridgeLock.tryLock()) return DEMO_BUY_BUSY
            try {
                return block()
            } finally {
                bridgeLock.unlock()
            }
        } finally {
            sessionLock.unlock()
        }
    }

    /** No foreign text survives a missing config or unavailable sanitizer. */
    fun redactDiagnostic(context: Context, text: String): String? = try {
        ensureStarted(context)
        val py = Python.getInstance()
        val hostModule = py.getModule("nightwatch.host")
        val configModule = py.getModule("nightwatch.config")
        val sessionModule = py.getModule("nightwatch.engine.session")
        val homes = listOf("nightwatch_config", "nightwatch_demo")
            .map { File(context.filesDir, it) }.filter { it.isDirectory }
        check(homes.isNotEmpty())
        var safe = text
        for (home in homes) {
            val path = py.getModule("pathlib").callAttr("Path", home.absolutePath)
            val host = hostModule.callAttr("DesktopHost", path)
            val config = configModule.callAttr("load_config", Kwarg("host", host))
            safe = sessionModule.callAttr("_redact_reason", safe,
                Kwarg("config", config), Kwarg("fallback", "")).toString()
        }
        safe.takeIf { it.isNotEmpty() }
    } catch (_: Exception) {
        null
    }

    fun dashboardSnapshot(context: Context, home: String): String? = bridgeLock.withLock {
        try {
            ensureStarted(context)
            Python.getInstance().getModule("nightwatch.notify.dashboard_export")
                .callAttr("export_json", home).toString()
        } catch (_: Exception) {
            null
        }
    }

    // Separate monitor keeps probe methods usable during the wait. Python GIL/shared
    // interpreter contention remains: probes during a session may run slower.
    fun runSessionDry(context: Context, baseDir: String): String {
        ensureStarted(context)
        return sessionLock.withLock {
            Python.getInstance().getModule("nightwatch.android_entry")
                .callAttr("run_session_dry", baseDir).toString()
        }
    }

    fun runSessionArmedDemo(context: Context, baseDir: String): String {
        ensureStarted(context)
        return sessionLock.withLock {
            Python.getInstance().getModule("nightwatch.android_entry")
                .callAttr("run_session_armed_demo", baseDir).toString()
        }
    }

    /** Market scheduling callers must use a background dispatcher. */
    fun nextSessionWakeEpochMs(context: Context): Long {
        ensureStarted(context)
        return Python.getInstance().getModule("nightwatch.android_entry")
            .callAttr("next_session_wake_epoch_ms").toLong()
    }

    fun ensureStarted(context: Context) = bridgeLock.withLock {
        if (!started) {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(context.applicationContext))
            }
            started = true
        }
    }

    fun smoke(context: Context): String = bridgeLock.withLock {
        ensureStarted(context)
        callCounter++
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        module.callAttr("smoke", callCounter).toString()
    }

    fun tzCheck(context: Context): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        module.callAttr("tz_check").toString()
    }

    /** v2 check 4: numpy/pandas — 260-bar rolling std on synthetic data, the actual operation the
     * strategy performs (DESIGN §1.4 cone_5_z uses a 260-bar rolling stdev). */
    fun numpyPandasCheck(context: Context): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        module.callAttr("numpy_pandas_check").toString()
    }

    /** v2 check 5: Python's own sqlite3 module works inside Chaquopy (the real app's DB layer is
     * Python, DESIGN §2/§3). */
    fun pythonSqliteCheck(context: Context): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        module.callAttr("python_sqlite_check", context.cacheDir.absolutePath).toString()
    }

    /**
     * Tonight's device-probe wiring (doc/RUNBOOK_DEVICE_PROBE.md): runs
     * probe_live_2026_09_11.py's sections A, C, D, E1, F (read-only, market-independent) plus the
     * sharpened E1 per-trade fee audit (nightwatch.probes.fee_audit). Section B (needs an open
     * US market) is deliberately never requested from here; section E2 (places a real order) is
     * not reachable through this function at all -- see probe/probe_runner.py's own docstring.
     *
     * MUST be called from a background thread/coroutine (e.g. Dispatchers.IO) -- this can run for
     * several minutes (each of the four sections carries its own up-to-300s timeout on the Python
     * side, run sequentially) and must never be invoked directly on the UI thread.
     *
     * `configDir` must contain a `config/config.toml` and `config/.secrets.env` pushed onto the
     * device beforehand (see the runbook) -- this function never hardcodes or receives a raw
     * credential itself. `outputDir` is where every raw payload and report this run produces is
     * written, for a later `adb pull`.
     */
    fun runLiveProbe(context: Context, configDir: String, outputDir: String): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.probe_runner")
        module.callAttr("run_default", configDir, outputDir).toString()
    }

    /**
     * 2026-09-11: same as [runLiveProbe] but with the section list chosen by the caller instead
     * of probe_runner's hardcoded "A,C,D,E1,F". Added so section B -- which needs an OPEN US
     * market and was therefore unreachable from any on-device route until today -- can be run
     * during a live window.
     *
     * Section E2 is NOT reachable through this: probe_runner._sanitize_sections strips it from
     * whatever string arrives here, and the copied probe's separate `--e2` opt-in flag is never
     * constructed anywhere in the Kotlin or Python of this project. Passing `sections` does not
     * widen what this app can do with real money.
     *
     * MUST be called from a background thread/coroutine. Section B alone can run ~25 minutes;
     * callers must size their own timeout well above that (see MainActivity's
     * maybeRunLiveProbeFromIntent, which allows 60).
     */
    fun runLiveProbeSections(context: Context, configDir: String, outputDir: String, sections: String): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.probe_runner")
        module.callAttr("run_sections", configDir, outputDir, sections).toString()
    }

    /**
     * 2026-09-11 order-latency measurement harness (RUNBOOK_LIVE_WINDOW_2026-09-11.md §8(b)) --
     * the DEMO-only harness that times real order round-trips. See
     * android-probe/app/src/main/python/probe/order_latency_runner.py's own docstring.
     *
     * This path is FAIL-CLOSED on the Python side and this function adds no way around it: the
     * harness refuses to place any order unless the configured environment, the derived
     * credential label, AND a live /balances signal all positively confirm demo. Run against a
     * device whose config says environment = "real", it prints every check it performed and
     * returns without trading.
     *
     * MUST be called from a background thread/coroutine -- a full run is
     * repeats x 3 legs x up to pollTimeout seconds each, plus order settle time.
     *
     * `repeats` <= 0 means "use the harness's own documented default" (4, RUNBOOK §8(b)); any
     * positive value overrides it. Nothing here can widen what the harness is allowed to do --
     * repeats only changes how many times the same demo-gated cycle runs.
     */
    fun runOrderLatency(context: Context, configDir: String, outputDir: String, repeats: Int = 0): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.order_latency_runner")
        if (repeats > 0) {
            module.callAttr("run", configDir, outputDir, repeats).toString()
        } else {
            module.callAttr("run", configDir, outputDir).toString()
        }
    }

    /**
     * 2026-09-11 DEMO-ONLY position reader/closer. See
     * android-probe/app/src/main/python/probe/demo_cleanup.py's docstring.
     *
     * Reuses the order-latency harness's own fail-closed demo gate verbatim. `lookupCloseIds`
     * selects the dedicated close-order GET poll; it is mutually exclusive with `closeIds`,
     * the existing full-close path.
     *
     * MUST be called from a background thread/coroutine.
     */
    fun runDemoCleanup(
        context: Context,
        configDir: String,
        outputDir: String,
        closeIds: String,
        lookupCloseIds: String = "",
        lookupOpenIds: String = "",
    ): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.demo_cleanup")
        module.callAttr("run", configDir, outputDir, closeIds, 2.0, 90.0, lookupCloseIds, lookupOpenIds).toString()
    }

    /**
     * 2026-09-11 READ-ONLY environment-payload capture. Fetches the raw `/balances` body (plus a
     * handful of sibling GET endpoints) and writes them untouched to `outputDir` for an
     * `adb pull`, so that EtoroClient._detect_environment can be written against the payload
     * eToro actually sends instead of against a guess.
     *
     * See android-probe/app/src/main/python/probe/env_probe.py's own docstring: every call it
     * makes is a GET issued with role="read" (the read-scoped credential), it opens no database,
     * and no order-placing/modifying/closing method is imported anywhere in it. This function
     * cannot place an order.
     *
     * `configDir`/`outputDir` follow the same convention as [runAdoptionFetch] above.
     * MUST be called from a background thread/coroutine.
     */
    fun runEnvProbe(context: Context, configDir: String, outputDir: String): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.env_probe")
        module.callAttr("run", configDir, outputDir).toString()
    }

    /**
     * On-device measurement task (2026-09-05): the cost of parsing a FULL-HISTORY SPY payload
     * through the REAL market/window.py + strategy/cone.py code path, vs the previously-used
     * 276-bar window -- both payload files must already be on-device (pushed via adb beforehand;
     * this call makes no network request itself, isolating parse/compute cost from fetch
     * latency). See android-probe/app/src/main/python/probe/window_bench.py.
     */
    fun runWindowBench(context: Context, payload276Path: String, payloadFullPath: String, outputDir: String): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.window_bench")
        module.callAttr("run", payload276Path, payloadFullPath, outputDir).toString()
    }

    /**
     * §4.7 adoption-migration dry run (2026-09-05): READ-ONLY on-device portfolio fetch.
     * See android-probe/app/src/main/python/probe/adopt_fetch.py's own docstring -- this calls
     * ONLY EtoroClient.get_portfolio()/resolve_instrument_id()/get_instruments(), all GETs,
     * using role="read" (ETORO_READ_PRIVATE_KEY), and never opens a database. The actual
     * adoption resolution (nightwatch.market.adopt.run_adoption_backfill, dry_run=True) runs
     * separately on the PC against the JSON this saves -- the dev machine can reach Tiingo but
     * not eToro, so the two halves are deliberately split across processes/machines.
     *
     * `configDir`/`outputDir` follow the same convention as `runLiveProbe` above.
     */
    fun runAdoptionFetch(context: Context, configDir: String, outputDir: String): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.adopt_fetch")
        module.callAttr("run", configDir, outputDir).toString()
    }

    /** Operator confirmation only; refuse busy calls rather than queueing a later order. */
    fun runDemoManualBuy(context: Context, home: String, count: Int, usdEach: Double): String =
        withDemoManualBuyLocks {
            ensureStarted(context)
            Python.getInstance().getModule("probe.demo_manual_buy")
                .callAttr("run", home, count, usdEach).toString()
        }

    /** Alarm-only unattended demo seed (T080). Blocks behind a running session or adoption. */
    fun runDemoSeed(context: Context, home: String): String {
        ensureStarted(context)
        return sessionLock.withLock {
            bridgeLock.withLock {
                Python.getInstance().getModule("probe.demo_seed")
                    .callAttr("run", home).toString()
            }
        }
    }

    /**
     * Demo-only Refresh/Pause. Callers must have verified the dispatch HMAC first: this is the
     * only place `operator_write_authorized=True` is ever passed. Takes neither session nor
     * bridge lock on purpose -- a Pause must not queue behind a running session or a
     * minutes-long probe; Python's SQLite transaction serializes it against the final submission
     * guard. Returns null if Python could not be reached.
     */
    internal fun applyDemoControl(context: Context, home: String, requestJson: String, deviceId: String): String? =
        try {
            if (!Python.isStarted()) ensureStarted(context)
            Python.getInstance().getModule("nightwatch.android_entry").callAttr(
                "apply_demo_control", home, requestJson,
                Kwarg("device_id", deviceId), Kwarg("operator_write_authorized", true)).toString()
        } catch (_: Exception) {
            null
        }

    /**
     * T084 between-session Telegram poll. Skips (null) when a session or any probe holds a lock:
     * a session polls for itself, and a poll must never queue behind or ahead of one.
     */
    internal fun pollTelegram(context: Context, home: String): String? {
        if (!sessionLock.tryLock()) return null
        try {
            if (!bridgeLock.tryLock()) return null
            try {
                ensureStarted(context)
                return Python.getInstance().getModule("nightwatch.android_entry")
                    .callAttr("poll_telegram", home).toString()
            } catch (_: Exception) {
                return null
            } finally {
                bridgeLock.unlock()
            }
        } finally {
            sessionLock.unlock()
        }
    }

    /** Manual adoption only; share the session monitor to avoid changing its active host. */
    fun planAdoption(context: Context, home: String): String {
        ensureStarted(context)
        return sessionLock.withLock {
            bridgeLock.withLock {
                Python.getInstance().getModule("probe.adopt_apply")
                    .callAttr("plan", home).toString()
            }
        }
    }

    fun applyAdoption(context: Context, home: String, planId: String): String {
        ensureStarted(context)
        return sessionLock.withLock {
            bridgeLock.withLock {
                Python.getInstance().getModule("probe.adopt_apply")
                    .callAttr("apply", home, planId).toString()
            }
        }
    }

    /** One-off real $10 SCHG TP probe. Python enforces the exact confirmation and
     * positively verifies the real:write credential before its sole order submission. */
    fun runTakeProfitProbe(
        context: Context,
        configDir: String,
        outputDir: String,
        confirmation: String,
        approvalToken: String
    ): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.take_profit_probe")
        module.callAttr("run", configDir, outputDir, confirmation, approvalToken).toString()
    }

    /** T7 demo-only payload capture. Its Python runner refuses non-demo config/credentials and
     * never retries its single deliberately subminimum order write. */
    fun runPayloadGaps(context: Context, configDir: String, outputDir: String): String = bridgeLock.withLock {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.payload_gaps")
        module.callAttr("run", configDir, outputDir).toString()
    }
}
