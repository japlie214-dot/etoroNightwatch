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
            binding.txtStatus.text = "Test alarm scheduled for epoch $at (in $minutes min)"
        }

        binding.btnPickTime.setOnClickListener { showTimePicker() }

        binding.btnScheduleNightly.setOnClickListener {
            val days = dayToggles.filter { it.first.isChecked }.map { it.second }.toSet()
            if (days.isEmpty()) {
                binding.txtStatus.text = "Select at least one day"
            } else {
                val at = AlarmScheduler.scheduleNightly(this, pickedHour, pickedMinute, days)
                refreshStatus()
                binding.txtStatus.text = "Nightly alarm scheduled for epoch $at"
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

        binding.btnShareLog.setOnClickListener { shareLog() }

        setupLogFilterSpinner()
        fetchFcmToken()
        refreshStatus()
        refreshEventList()
        startCountdownLoop()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        refreshEventList()
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
            binding.txtNextFireAbsolute.text = "Next fire: (no nightly schedule set)"
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
