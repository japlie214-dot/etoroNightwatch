package com.nightwatch.etoro.probe

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.BaseAdapter
import android.widget.TextView
import android.widget.ToggleButton
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.updatePadding
import androidx.lifecycle.lifecycleScope
import com.google.android.material.timepicker.MaterialTimePicker
import com.google.android.material.timepicker.TimeFormat
import com.google.firebase.messaging.FirebaseMessaging
import com.nightwatch.etoro.probe.databinding.ActivityMainBinding
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull
import java.io.File
import java.io.FileOutputStream
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val store by lazy { EventStore.get(this) }

    private var pickedHour = 2
    private var pickedMinute = 30
    private var currentFilter: String = "ALL"
    private var countdownRunning = false

    private val dayToggles: List<Pair<ToggleButton, Int>> by lazy {
        listOf(
            binding.toggleMon to Calendar.MONDAY,
            binding.toggleTue to Calendar.TUESDAY,
            binding.toggleWed to Calendar.WEDNESDAY,
            binding.toggleThu to Calendar.THURSDAY,
            binding.toggleFri to Calendar.FRIDAY,
            binding.toggleSat to Calendar.SATURDAY,
            binding.toggleSun to Calendar.SUNDAY
        )
    }

    private val requestNotificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        store.log("PERMISSION_RESULT", "POST_NOTIFICATIONS granted=$granted")
        refreshStatus()
    }

    private val batteryExemptionLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        store.log(
            "PERMISSION_RESULT",
            "battery exemption dialog closed; now=${PowerStatus.isIgnoringBatteryOptimizations(this)}"
        )
        refreshStatus()
    }

    private val exactAlarmSettingsLauncher = registerForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) {
        store.log(
            "PERMISSION_RESULT",
            "exact alarm settings closed; now=${PowerStatus.canScheduleExactAlarms(this)}"
        )
        refreshStatus()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge() // Android 15+ enforces this for targetSdk 35+; be explicit about it.
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        applyEdgeToEdgeInsets()

        NotificationHelper.ensureChannel(this)
        PyBridge.ensureStarted(this)

        restoreSavedSchedule()

        binding.btnRequestExactAlarm.setOnClickListener { requestExactAlarmPermission() }
        binding.btnRequestBatteryExemption.setOnClickListener { requestBatteryExemption() }
        binding.btnRequestNotifications.setOnClickListener { requestNotificationPermission() }

        binding.btnScheduleTest.setOnClickListener {
            val minutes = binding.editFireMinutes.text.toString().toIntOrNull() ?: 2
            val at = AlarmScheduler.scheduleInMinutes(this, minutes)
            refreshStatus()
            binding.txtStatus.text = if (at == null) "Test alarm not scheduled" else
                "Test alarm scheduled for epoch $at (in $minutes min)"
        }

        binding.btnPickTime.setOnClickListener { showTimePicker() }

        binding.btnScheduleNightly.setOnClickListener {
            val days = dayToggles.filter { it.first.isChecked }.map { it.second }.toSet()
            if (days.isEmpty()) {
                binding.txtStatus.text = "Select at least one day"
            } else {
                val at = AlarmScheduler.scheduleNightly(this, pickedHour, pickedMinute, days)
                refreshStatus()
                binding.txtStatus.text = if (at == null) "Nightly alarm not scheduled" else
                    "Nightly alarm scheduled for epoch $at"
            }
        }

        binding.btnScheduleMarket.setOnClickListener {
            binding.txtStatus.text = "Computing next market session..."
            lifecycleScope.launch {
                try {
                    val at = withContext(Dispatchers.IO) { AlarmScheduler.scheduleMarket(this@MainActivity) }
                    refreshStatus()
                    binding.txtStatus.text = if (at == null) "Market session not scheduled" else
                        "Market session scheduled for epoch $at"
                } catch (e: Exception) {
                    store.log("ALARM_MARKET_SCHEDULE_FAILED", "error=${e.javaClass.simpleName}")
                    binding.txtStatus.text = "Market session not scheduled"
                    NotificationHelper.postFailure(this@MainActivity, "Market alarm not scheduled", e.javaClass.simpleName)
                }
            }
        }

        binding.btnRunChecksNow.setOnClickListener {
            binding.txtStatus.text = "Running full check battery — watch the event log below"
            binding.txtTz.text = "tzdata: ${TzCheck.run()}"
            lifecycleScope.launch {
                val results = withContext(Dispatchers.IO) { CheckBattery.runAll(this@MainActivity) }
                results.forEach { r ->
                    store.log("CHECK_${r.name}", "trigger=manual ${r.asEventDetail()}")
                    if (!r.pass) {
                        NotificationHelper.postFailure(this@MainActivity, "Check ${r.name} failed", r.detail)
                    }
                }
                withContext(Dispatchers.IO) { SnapshotPusher.pushNow(this@MainActivity, results) }
                refreshEventList()
                refreshStatus()
            }
        }

        binding.btnSendTelegramTest.setOnClickListener {
            binding.txtStatus.text = "Sending Telegram test message..."
            lifecycleScope.launch {
                val result = withContext(Dispatchers.IO) { CheckBattery.telegramSend() }
                store.log("CHECK_TELEGRAM_SEND", "trigger=manual ${result.asEventDetail()}")
                if (!result.pass) NotificationHelper.postFailure(this@MainActivity, "Telegram send failed", result.detail)
                refreshEventList()
                binding.txtStatus.text = "Telegram test: ${if (result.pass) "sent" else "FAILED"} — ${result.detail}"
            }
        }

        binding.btnPushSnapshot.setOnClickListener {
            binding.txtStatus.text = "Pushing snapshot to Worker..."
            lifecycleScope.launch {
                withContext(Dispatchers.IO) { SnapshotPusher.pushNow(this@MainActivity) }
                refreshEventList()
                binding.txtStatus.text = "Snapshot push attempted — see event log"
            }
        }

        binding.btnRunLiveProbe.setOnClickListener { runLiveProbeNow() }

        binding.btnWindowBench.setOnClickListener { runWindowBenchNow() }

        binding.btnShareLog.setOnClickListener { shareLog() }

        setupLogFilterSpinner()
        fetchFcmToken()
        refreshStatus()
        refreshEventList()
        startCountdownLoop()

        maybeRunAdoptionFetchFromIntent(intent)
        maybeRunWindowBenchFromIntent(intent)
        maybeRunLiveProbeFromIntent(intent)
        maybeRunOrderLatencyFromIntent(intent)
        maybeRunEnvProbeFromIntent(intent)
        maybeRunDemoCleanupFromIntent(intent)
        maybeRunTakeProfitProbeFromIntent(intent)
        maybeRunPayloadGapsFromIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        // launchMode="singleTop": a second `adb shell am start` against an already-running
        // instance lands here, not in onCreate -- handle both so the adb-only trigger below
        // works regardless of whether the app was already open.
        maybeRunAdoptionFetchFromIntent(intent)
        maybeRunWindowBenchFromIntent(intent)
        maybeRunLiveProbeFromIntent(intent)
        maybeRunOrderLatencyFromIntent(intent)
        maybeRunEnvProbeFromIntent(intent)
        maybeRunDemoCleanupFromIntent(intent)
        maybeRunTakeProfitProbeFromIntent(intent)
        maybeRunPayloadGapsFromIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        refreshEventList()
    }

    // -- §4.7 adoption-migration dry run (2026-09-05): READ-ONLY on-device portfolio fetch,
    // triggered purely by adb intent extra rather than a tap -- this device's screen is locked
    // with a pattern the operator has not shared (see BenchReceiver.kt's docstring for the same
    // constraint on an earlier task), and task instructions forbid adding any new EXPORTED
    // component to reach it. MainActivity is already exported (MAIN/LAUNCHER) for its ordinary
    // purpose; this only adds intent-extra handling inside it, not a new component, and performs
    // no UI interaction of its own -- everything is visible in logcat ("ADOPTFETCH: ...") and in
    // the event log, and every produced file lands under outputDir for `adb pull`.
    //
    //   adb shell am start -n com.nightwatch.etoro/.MainActivity \
    //     -a android.intent.action.MAIN -c android.intent.category.LAUNCHER \
    //     --ez run_adopt_fetch true
    private fun maybeRunAdoptionFetchFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_adopt_fetch", false)) return
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_adopt_fetch_output").absolutePath
        android.util.Log.i("ADOPTFETCH", "starting: config_dir=$configDir output_dir=$outputDir")
        store.log("ADOPT_FETCH_START", "config_dir=$configDir output_dir=$outputDir")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(5 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runAdoptionFetch(this@MainActivity, configDir, outputDir)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runAdoptionFetch: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 5 minutes waiting for the adoption fetch to return."
            android.util.Log.i("ADOPTFETCH", "done: $summary")
            store.log("ADOPT_FETCH_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // -- Window bench (2026-09-05), adb-only route: mirrors maybeRunAdoptionFetchFromIntent
    // above -- runs the same full-history-vs-276-bar parse/compute benchmark as the
    // "Run window bench" button, but triggered purely by an intent extra so it can be fired
    // without unlocking the device's screen (this device's pattern lock was not shared). This
    // replaces an earlier BenchReceiver broadcast receiver that was android:exported="true" with
    // no permission guard and no input validation on the intent -- any other installed app could
    // have broadcast into it. MainActivity is already exported (MAIN/LAUNCHER); this only adds
    // intent-extra handling here, not a new exported component.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity \
    //     --ez run_window_bench true
    private fun maybeRunWindowBenchFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_window_bench", false)) return
        runWindowBenchNow()
    }

    // -- Live probe, adb-only route with a CALLER-CHOSEN section list (2026-09-11). Mirrors
    // maybeRunAdoptionFetchFromIntent above exactly: no new exported component (HANDOFF
    // 2026-09-06 §11 forbids an exported receiver outright), handling added inside the
    // already-exported MainActivity, everything visible in logcat ("LIVEPROBE: ...") and the
    // event log, every file under outputDir for `adb pull`.
    //
    // Why this exists: probe_runner.run_default hardcoded "A,C,D,E1,F", so section B -- which
    // needs an OPEN US market -- was unreachable from the device. The `sections` string extra
    // is optional; omitted, it behaves exactly as the old route did.
    //
    // SAFETY: section E2 places a REAL order and is NOT authorised. It is stripped on the Python
    // side by probe_runner._sanitize_sections whatever is passed here, and the copied probe's
    // separate `--e2` opt-in flag is never constructed anywhere in this app. There is
    // deliberately no intent extra that can reach it.
    //
    // Timeout: 60 minutes, NOT the adoption fetch's 5. Section B polls the market on its own
    // schedule for ~25 minutes; a 5-minute ceiling would truncate it mid-run and report a
    // timeout that was purely an artifact of this wrapper.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity \
    //     --ez run_live_probe true --es sections "A,C,D,E1,B"
    private fun maybeRunLiveProbeFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_live_probe", false)) return
        val sections = intent.getStringExtra("sections") ?: ""
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_live_probe_output").absolutePath
        android.util.Log.i("LIVEPROBE", "starting: config_dir=$configDir output_dir=$outputDir sections='$sections'")
        store.log("LIVE_PROBE_START", "config_dir=$configDir output_dir=$outputDir sections='$sections'")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(60 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runLiveProbeSections(this@MainActivity, configDir, outputDir, sections)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runLiveProbeSections: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 60 minutes waiting for the live probe to return."
            android.util.Log.i("LIVEPROBE", "done: $summary")
            store.log("LIVE_PROBE_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // -- Order-latency harness, adb-only route (2026-09-11; RUNBOOK_LIVE_WINDOW_2026-09-11 §8(b)).
    // Same pattern as the two handlers above -- no new exported component.
    //
    // SAFETY: this is the only route in this app that can place an order at all, and it is
    // fail-closed on the Python side. probe/order_latency_runner.py forwards no flag that could
    // weaken the harness's demo confirmation, and the harness refuses to place anything unless
    // the configured environment, the derived credential label, AND a live /balances signal all
    // POSITIVELY confirm demo. "Unable to confirm" is refused exactly like "confirmed real".
    // Against a device whose config says environment = "real" this prints every check it ran and
    // returns having traded nothing -- that refusal is the expected, correct outcome.
    //
    // Timeout: 60 minutes. The run shape is repeats(4) x 3 legs x up to 90s poll timeout, plus
    // order settle time, instrument resolution and the balances probe -- ~18 minutes of pure
    // poll ceiling in the worst case, so 60 leaves generous slack without being unbounded.
    //
    // The optional `repeats` int extra overrides the harness's default of 4 (RUNBOOK §8(b));
    // omitted or <= 0 means "use the harness default". It only changes how many times the same
    // demo-gated cycle runs -- it cannot widen what the harness is permitted to do.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity \
    //     --ez run_order_latency true --ei repeats 2
    private fun maybeRunOrderLatencyFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_order_latency", false)) return
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_order_latency_output").absolutePath
        val repeats = intent.getIntExtra("repeats", 0)
        android.util.Log.i("LATENCY", "starting: config_dir=$configDir output_dir=$outputDir repeats=$repeats")
        store.log("ORDER_LATENCY_START", "config_dir=$configDir output_dir=$outputDir repeats=$repeats")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(60 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runOrderLatency(this@MainActivity, configDir, outputDir, repeats)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runOrderLatency: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 60 minutes waiting for the order-latency harness to return."
            android.util.Log.i("LATENCY", "done: $summary")
            store.log("ORDER_LATENCY_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // -- DEMO-ONLY position reader/closer, adb-only route (2026-09-11). Added because the
    // _parse_order_result bug stranded demo positions that NO route in this app could close --
    // the latency harness only closes what it opened in the same process.
    //
    // SAFETY: probe/demo_cleanup.py reuses the latency harness's own demo gate verbatim.
    // `lookup_close_ids` invokes the dedicated close-order GET only and is mutually exclusive with
    // `close_ids`, which is the separate full-close path. Against a real config, the gate refuses.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity --ez run_demo_cleanup true --es lookup_close_ids "382354058"
    private fun maybeRunDemoCleanupFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_demo_cleanup", false)) return
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_demo_cleanup_output").absolutePath
        val closeIds = intent.getStringExtra("close_ids") ?: ""
        val lookupCloseIds = intent.getStringExtra("lookup_close_ids") ?: ""
        val lookupOpenIds = intent.getStringExtra("lookup_open_ids") ?: ""
        android.util.Log.i("CLEANUP", "starting: output_dir=$outputDir close_ids=$closeIds lookup_close_ids=$lookupCloseIds lookup_open_ids=$lookupOpenIds")
        store.log("DEMO_CLEANUP_START", "output_dir=$outputDir close_ids=$closeIds lookup_close_ids=$lookupCloseIds lookup_open_ids=$lookupOpenIds")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(15 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runDemoCleanup(this@MainActivity, configDir, outputDir, closeIds, lookupCloseIds, lookupOpenIds)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runDemoCleanup: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 15 minutes waiting for the demo cleanup."
            android.util.Log.i("CLEANUP", "done: $summary")
            store.log("DEMO_CLEANUP_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // -- READ-ONLY environment-payload capture, adb-only route (2026-09-11). Same pattern as the
    // handlers above: no new exported component, handling added inside the already-exported
    // MainActivity, everything visible in logcat ("ENVPROBE: ...") and the event log, every file
    // under outputDir for `adb pull`.
    //
    // Why: EtoroClient._detect_environment only ever looked for its marker keys at the TOP LEVEL
    // of the /balances body, which carries none of them -- so it returned null unconditionally and
    // verify_environment() has been a silent no-op on the REAL account as well as on demo. Writing
    // a correct detector needs the payload eToro actually sends, which this captures.
    //
    // SAFETY: probe/env_probe.py issues GETs only, with role="read" (the read-scoped credential),
    // and imports no order-placing/modifying/closing method at all. There is no order path from
    // this route, on either the demo or the real config.
    //
    // Timeout: 5 minutes, matching the adoption fetch -- this is a handful of GETs, not a poll loop.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity \
    //     --ez run_env_probe true
    private fun maybeRunEnvProbeFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_env_probe", false)) return
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_env_probe_output").absolutePath
        android.util.Log.i("ENVPROBE", "starting: config_dir=$configDir output_dir=$outputDir")
        store.log("ENV_PROBE_START", "config_dir=$configDir output_dir=$outputDir")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(5 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runEnvProbe(this@MainActivity, configDir, outputDir)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runEnvProbe: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 5 minutes waiting for the env probe to return."
            android.util.Log.i("ENVPROBE", "done: $summary")
            store.log("ENV_PROBE_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // -- T6: one explicitly authorized real $10 SCHG buy to inspect broker TP defaults.
    // This is only an intent route in the existing exported MainActivity; no component or UI
    // was added. The Python runner independently rejects every non-exact confirmation and
    // requires a one-use token from app-private files before verifying the live credential.
    // It allows one submit attempt per app-data directory, never retries the order, and leaves
    // any filled probe position open.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity \
    //     --ez run_take_profit_probe true --es confirmation 'PLACE REAL $10 SCHG ORDER'
    private fun maybeRunTakeProfitProbeFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_take_profit_probe", false)) return
        val confirmation = intent.getStringExtra("confirmation") ?: ""
        val approvalToken = intent.getStringExtra("approval_token") ?: ""
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_take_profit_probe_output").absolutePath
        android.util.Log.i("TPPROBE", "starting: output_dir=$outputDir confirmation_present=${confirmation.isNotEmpty()}")
        store.log("TP_PROBE_START", "output_dir=$outputDir confirmation_present=${confirmation.isNotEmpty()}")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(20 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runTakeProfitProbe(
                            this@MainActivity,
                            configDir,
                            outputDir,
                            confirmation,
                            approvalToken
                        )
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runTakeProfitProbe: ${e.javaClass.simpleName}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 20 minutes waiting for the TP probe."
            android.util.Log.i("TPPROBE", "done: $summary")
            store.log("TP_PROBE_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // T7 captures search, portfolio, and rejected-order payloads. Python reuses the
    // order-latency harness's positive demo gate and refuses before HTTP on a real config.
    // Its at-most-once marker and max_attempts=1 prevent a write retry.
    //
    //   adb shell am start -n com.nightwatch.etoro/com.nightwatch.etoro.probe.MainActivity \
    //     --ez run_payload_gaps true
    private fun maybeRunPayloadGapsFromIntent(intent: Intent?) {
        if (intent == null || !intent.getBooleanExtra("run_payload_gaps", false)) return
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_payload_gaps_output").absolutePath
        android.util.Log.i("PAYLOADGAPS", "starting: output_dir=$outputDir")
        store.log("PAYLOAD_GAPS_START", "output_dir=$outputDir")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(15 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runPayloadGaps(this@MainActivity, configDir, outputDir)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runPayloadGaps: ${e.javaClass.simpleName}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 15 minutes waiting for T7 payload capture."
            android.util.Log.i("PAYLOADGAPS", "done: $summary")
            store.log("PAYLOAD_GAPS_DONE", summary.take(4000))
            refreshEventList()
        }
    }

    // --- Edge-to-edge (task Problem 2): pad for systemBars() + displayCutout(), top and bottom ---
    private fun applyEdgeToEdgeInsets() {
        ViewCompat.setOnApplyWindowInsetsListener(binding.rootScroll) { view, insets ->
            val bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout()
            )
            view.updatePadding(left = bars.left, top = bars.top, right = bars.right, bottom = bars.bottom)
            insets
        }
    }

    private fun restoreSavedSchedule() {
        val (h, m) = AlarmScheduler.savedHourMinute(this)
        pickedHour = h
        pickedMinute = m
        binding.txtPickedTime.text = "Time: %02d:%02d (device-local)".format(h, m)
        val savedDays = AlarmScheduler.savedDays(this)
        dayToggles.forEach { (toggle, dow) -> toggle.isChecked = dow in savedDays }
    }

    private fun showTimePicker() {
        val picker = MaterialTimePicker.Builder()
            .setTimeFormat(TimeFormat.CLOCK_24H)
            .setHour(pickedHour)
            .setMinute(pickedMinute)
            .setTitleText("Nightly alarm time (device-local)")
            .build()
        picker.addOnPositiveButtonClickListener {
            pickedHour = picker.hour
            pickedMinute = picker.minute
            binding.txtPickedTime.text = "Time: %02d:%02d (device-local)".format(pickedHour, pickedMinute)
        }
        picker.show(supportFragmentManager, "nightwatch_time_picker")
    }

    // --- Live "fires in HH:mm:ss" countdown + absolute timestamp (task Problem 2) ---
    private fun startCountdownLoop() {
        if (countdownRunning) return
        countdownRunning = true
        lifecycleScope.launch {
            while (countdownRunning) {
                updateCountdown()
                kotlinx.coroutines.delay(1000)
            }
        }
    }

    private fun updateCountdown() {
        val nextEpoch = AlarmScheduler.currentNextFireEpochMs(this)
        if (nextEpoch == null) {
            binding.txtNextFireAbsolute.text = "Next fire: (not scheduled)"
            binding.txtCountdown.text = "Fires in: --"
            return
        }
        val fmt = SimpleDateFormat("yyyy-MM-dd HH:mm:ss z", Locale.US)
        binding.txtNextFireAbsolute.text = "Next fire: ${fmt.format(Date(nextEpoch))}"
        val remainingMs = nextEpoch - System.currentTimeMillis()
        binding.txtCountdown.text = if (remainingMs <= 0) {
            "Fires in: due now"
        } else {
            val totalSeconds = remainingMs / 1000
            val h = totalSeconds / 3600
            val m = (totalSeconds % 3600) / 60
            val s = totalSeconds % 60
            "Fires in: ${h}h ${m}m ${s}s"
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        countdownRunning = false
    }

    private fun requestExactAlarmPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            if (!PowerStatus.canScheduleExactAlarms(this)) {
                val intent = Intent(Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM).apply {
                    data = Uri.parse("package:$packageName")
                }
                exactAlarmSettingsLauncher.launch(intent)
            } else {
                binding.txtStatus.text = "Exact alarm permission already granted"
            }
        } else {
            binding.txtStatus.text = "Not required below API 31"
        }
    }

    private fun requestBatteryExemption() {
        if (!PowerStatus.isIgnoringBatteryOptimizations(this)) {
            val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:$packageName")
            }
            batteryExemptionLauncher.launch(intent)
        } else {
            binding.txtStatus.text = "Battery-optimization exemption already granted"
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            if (ContextCompat.checkSelfPermission(
                    this, Manifest.permission.POST_NOTIFICATIONS
                ) != PackageManager.PERMISSION_GRANTED
            ) {
                requestNotificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                binding.txtStatus.text = "Notification permission already granted"
            }
        } else {
            binding.txtStatus.text = "Not required below API 33"
        }
    }

    private fun fetchFcmToken() {
        FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
            if (task.isSuccessful) {
                val token = task.result
                TokenHolder.set(token)
                binding.txtFcmToken.text = "FCM token: $token"
                store.log("FCM_TOKEN_FETCHED", token)
            } else {
                binding.txtFcmToken.text = "FCM token: fetch failed — ${task.exception?.message}"
                store.log("FCM_TOKEN_FETCH_FAILED", "${task.exception}")
            }
        }
    }

    private fun refreshStatus() {
        binding.txtStatus.text = "power: ${PowerStatus.summary(this)}"
        TokenHolder.lastToken?.let { binding.txtFcmToken.text = "FCM token: $it" }
        binding.txtStandbyBucket.text = "Standby bucket: ${PowerStatus.standbyBucket(this)}"
        val fcmPriority = FcmPriorityHolder.lastPriority
        binding.txtFcmPriority.text = if (fcmPriority == null) {
            "FCM priority canary: (no message received yet)"
        } else if (fcmPriority == "PRIORITY_HIGH") {
            "FCM priority canary: $fcmPriority (OK)"
        } else {
            "FCM priority canary: $fcmPriority — DEMOTED, see DESIGN §8.2"
        }
        updateCountdown()
    }

    // --- Event log: newest first, monospace, filterable (task Problem 2) ---
    private fun setupLogFilterSpinner() {
        val types = listOf("ALL") + store.recent(2000).map { it.type }.distinct().sorted()
        binding.spinnerLogFilter.adapter = ArrayAdapter(
            this, android.R.layout.simple_spinner_dropdown_item, types
        )
        binding.spinnerLogFilter.setOnItemSelectedListener(
            object : android.widget.AdapterView.OnItemSelectedListener {
                override fun onItemSelected(
                    parent: android.widget.AdapterView<*>?, view: View?, position: Int, id: Long
                ) {
                    currentFilter = types.getOrElse(position) { "ALL" }
                    refreshEventList()
                }
                override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
            }
        )
    }

    private fun refreshEventList() {
        val all = store.recent(500) // already newest-first (EventStore orders by id DESC)
        val filtered = if (currentFilter == "ALL") all else all.filter { it.type == currentFilter }
        binding.listEvents.adapter = MonospaceLogAdapter(this, filtered.map { it.formatted() })
    }

    // --- Live device probe (doc/RUNBOOK_DEVICE_PROBE.md): sections A, C, D, E1, F + the sharpened
    // E1 fee audit. Read-only. Section B (needs an open market) and E2 (real money) are never
    // reachable from this button -- see PyBridge.runLiveProbe's own docstring. ---
    private fun runLiveProbeNow() {
        val configDir = File(filesDir, "nightwatch_config").absolutePath
        val outputDir = File(filesDir, "nightwatch_probe_output").absolutePath
        binding.txtLiveProbeStatus.text =
            "Live probe: RUNNING (this can take several minutes; do not close the app) -- " +
                "config_dir=$configDir output_dir=$outputDir"
        store.log("LIVE_PROBE_START", "config_dir=$configDir output_dir=$outputDir")
        lifecycleScope.launch {
            // Kotlin-side safety net on top of the Python side's own per-section thread
            // timeouts (run_section in probe_live_2026_09_11.py): this call runs on
            // Dispatchers.IO so the UI thread is never blocked regardless, and
            // withTimeoutOrNull guarantees this coroutine surfaces SOMETHING (a timeout
            // message, not silence) even if a bug somehow let a call escape every timeout on
            // the Python side. 25 minutes is generous: four ~300s-capped sections plus the E1+
            // fee audit's own network calls, run sequentially.
            val result = withTimeoutOrNull(25 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runLiveProbe(this@MainActivity, configDir, outputDir)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runLiveProbe: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result
                ?: "TIMED OUT after 25 minutes waiting for the live probe to return -- " +
                    "partial output (if any) is still in $outputDir; check the copied " +
                    "probe_live_2026_09_11.py's own per-section timeouts and this device's logcat."
            store.log("LIVE_PROBE_DONE", summary.take(2000))
            binding.txtLiveProbeStatus.text = "Live probe: DONE -- see event log below and " +
                "adb pull $outputDir (doc/RUNBOOK_DEVICE_PROBE.md)"
            if (result == null) {
                NotificationHelper.postFailure(this@MainActivity, "Live probe timed out", summary)
            } else {
                NotificationHelper.post(this@MainActivity, "Live probe finished", "See event log + $outputDir")
            }
            refreshEventList()
        }
    }

    // -- Window bench (2026-09-05): full-history vs 276-bar parse/compute cost on-device.
    // No network call -- both payload JSON files must already be pushed onto the device
    // (see doc, adb push into filesDir/nightwatch_bench_input/). Read-only, CPU/memory only. --
    private fun runWindowBenchNow() {
        val inputDir = File(filesDir, "nightwatch_bench_input")
        val payload276 = File(inputDir, "payload_276.json").absolutePath
        val payloadFull = File(inputDir, "payload_full.json").absolutePath
        val outputDir = File(filesDir, "nightwatch_bench_output").absolutePath
        binding.txtWindowBenchStatus.text = "Window bench: RUNNING -- output_dir=$outputDir"
        store.log("WINDOW_BENCH_START", "payload_276=$payload276 payload_full=$payloadFull output_dir=$outputDir")
        lifecycleScope.launch {
            val result = withTimeoutOrNull(5 * 60 * 1000L) {
                withContext(Dispatchers.IO) {
                    try {
                        PyBridge.runWindowBench(this@MainActivity, payload276, payloadFull, outputDir)
                    } catch (e: Exception) {
                        "EXCEPTION calling PyBridge.runWindowBench: ${e.javaClass.simpleName}: ${e.message}"
                    }
                }
            }
            val summary = result ?: "TIMED OUT after 5 minutes waiting for the window bench to return."
            store.log("WINDOW_BENCH_DONE", summary.take(4000))
            binding.txtWindowBenchStatus.text = "Window bench: DONE -- see event log + adb pull $outputDir"
            refreshEventList()
        }
    }

    private fun shareLog() {
        val text = store.exportAsText()
        val file = File(cacheDir, "nightwatch_probe_log.txt")
        FileOutputStream(file).use { it.write(text.toByteArray()) }
        val uri = androidx.core.content.FileProvider.getUriForFile(
            this, "$packageName.fileprovider", file
        )
        val intent = Intent(Intent.ACTION_SEND).apply {
            type = "text/plain"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
        startActivity(Intent.createChooser(intent, "Share Nightwatch Probe log"))
    }
}

/** Simple monospace, newest-first log row adapter (task Problem 2: "readable ... monospace"). */
private class MonospaceLogAdapter(
    private val context: Context,
    private val lines: List<String>
) : BaseAdapter() {
    override fun getCount(): Int = lines.size
    override fun getItem(position: Int): Any = lines[position]
    override fun getItemId(position: Int): Long = position.toLong()

    override fun getView(position: Int, convertView: View?, parent: ViewGroup?): View {
        val view = convertView ?: LayoutInflater.from(context)
            .inflate(R.layout.item_event_log, parent, false)
        view.findViewById<TextView>(R.id.txtEventLine).text = lines[position]
        return view
    }
}
