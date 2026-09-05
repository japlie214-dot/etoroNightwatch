package com.nightwatch.etoro.probe

import android.content.Context
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Thin bridge to the Chaquopy-embedded Python (task requirement 5: prove the embedded runtime
 * works after a cold wake, not just when the app is open). Mirrors DESIGN §7.1's host.py idea in
 * miniature — Kotlin calls named Python functions, nothing more.
 */
object PyBridge {
    private var started = false
    private var callCounter = 0

    @Synchronized
    fun ensureStarted(context: Context) {
        if (!started) {
            if (!Python.isStarted()) {
                Python.start(AndroidPlatform(context.applicationContext))
            }
            started = true
        }
    }

    @Synchronized
    fun smoke(context: Context): String {
        ensureStarted(context)
        callCounter++
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        return module.callAttr("smoke", callCounter).toString()
    }

    @Synchronized
    fun tzCheck(context: Context): String {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        return module.callAttr("tz_check").toString()
    }

    /** v2 check 4: numpy/pandas — 260-bar rolling std on synthetic data, the actual operation the
     * strategy performs (DESIGN §1.4 cone_5_z uses a 260-bar rolling stdev). */
    @Synchronized
    fun numpyPandasCheck(context: Context): String {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        return module.callAttr("numpy_pandas_check").toString()
    }

    /** v2 check 5: Python's own sqlite3 module works inside Chaquopy (the real app's DB layer is
     * Python, DESIGN §2/§3). */
    @Synchronized
    fun pythonSqliteCheck(context: Context): String {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.checks")
        return module.callAttr("python_sqlite_check", context.cacheDir.absolutePath).toString()
    }

    /**
     * Tonight's device-probe wiring (doc/RUNBOOK_DEVICE_PROBE.md): runs
     * probe_live_2026_09_11.py's sections A, C, D, E1 (read-only, market-independent) plus the
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
    @Synchronized
    fun runLiveProbe(context: Context, configDir: String, outputDir: String): String {
        ensureStarted(context)
        val py = Python.getInstance()
        val module = py.getModule("probe.probe_runner")
        return module.callAttr("run_default", configDir, outputDir).toString()
    }
}
