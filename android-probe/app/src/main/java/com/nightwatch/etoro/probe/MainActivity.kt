package com.nightwatch.etoro.probe

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.ArrayAdapter
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import com.google.firebase.messaging.FirebaseMessaging
import com.nightwatch.etoro.probe.databinding.ActivityMainBinding
import kotlinx.coroutines.launch
import androidx.lifecycle.lifecycleScope
import java.io.File
import java.io.FileOutputStream

class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private val store by lazy { EventStore.get(this) }

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
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)

        NotificationHelper.ensureChannel(this)
        PyBridge.ensureStarted(this)

        binding.btnRequestExactAlarm.setOnClickListener { requestExactAlarmPermission() }
        binding.btnRequestBatteryExemption.setOnClickListener { requestBatteryExemption() }
        binding.btnRequestNotifications.setOnClickListener { requestNotificationPermission() }

        binding.btnScheduleTest.setOnClickListener {
            val minutes = binding.editFireMinutes.text.toString().toIntOrNull() ?: 2
            val at = AlarmScheduler.scheduleInMinutes(this, minutes)
            refreshStatus()
            binding.txtStatus.text = "Test alarm scheduled for epoch $at (in $minutes min)"
        }

        binding.btnScheduleNightly.setOnClickListener {
            val text = binding.editNightlyTime.text.toString().trim()
            val parts = text.split(":")
            val hour = parts.getOrNull(0)?.toIntOrNull()
            val minute = parts.getOrNull(1)?.toIntOrNull()
            if (hour == null || minute == null || hour !in 0..23 || minute !in 0..59) {
                binding.txtStatus.text = "Enter a valid HH:mm, e.g. 02:30"
            } else {
                val at = AlarmScheduler.scheduleNightly(this, hour, minute)
                refreshStatus()
                binding.txtStatus.text = "Nightly alarm scheduled for epoch $at"
            }
        }

        binding.btnRunChecksNow.setOnClickListener {
            val intent = Intent(this, WakeService::class.java)
                .putExtra(WakeService.EXTRA_TRIGGER, "manual")
            ContextCompat.startForegroundService(this, intent)
            binding.txtStatus.text = "Manual wake checks started — watch the event log below"
            lifecycleScope.launch {
                kotlinx.coroutines.delay(3000)
                refreshEventList()
            }
        }

        binding.btnShareLog.setOnClickListener { shareLog() }

        fetchFcmToken()
        refreshStatus()
        refreshEventList()
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
        refreshEventList()
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
    }

    private fun refreshEventList() {
        val events = store.recent(200).map { it.formatted() }
        binding.listEvents.adapter = ArrayAdapter(
            this, android.R.layout.simple_list_item_1, events
        )
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
