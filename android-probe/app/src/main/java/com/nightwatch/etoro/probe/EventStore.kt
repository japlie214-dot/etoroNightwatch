package com.nightwatch.etoro.probe

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * The whole point of the multi-night test is worthless if results can't be read the next
 * morning (task requirement 7). Every event this probe cares about — alarm fired, FCM received
 * with its priority, network latency, boot re-register, permission changes, tzdata results — is
 * persisted here with a timestamp, independent of process death, and readable/exportable from
 * the UI.
 */
class EventStore(context: Context) :
    SQLiteOpenHelper(context.applicationContext, DB_NAME, null, DB_VERSION) {

    private val appContext = context.applicationContext

    companion object {
        private const val DB_NAME = "nightwatch_probe_events.db"
        private const val DB_VERSION = 1
        internal const val RETENTION_DAYS = 90
        private var retentionPruned = false

        internal fun retentionCutoff(nowEpochMs: Long): Long =
            nowEpochMs - RETENTION_DAYS * 24L * 60 * 60 * 1000

        private val TS_FORMAT =
            SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS z", Locale.US)

        @Volatile
        private var instance: EventStore? = null

        internal fun redactedDetail(type: String, detail: String, redact: (String) -> String?): String {
            // Unknown commands contain arbitrary FCM values, not diagnostic labels.
            if (type == "COMMAND_UNKNOWN") return "details omitted"
            return try { redact(detail) ?: "" } catch (_: Exception) { "" }
        }

        fun get(context: Context): EventStore =
            instance ?: synchronized(this) {
                instance ?: EventStore(context).also { instance = it }
            }
    }

    init {
        TS_FORMAT.timeZone = TimeZone.getDefault()
    }

    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL(
            """
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_epoch_ms INTEGER NOT NULL,
                type TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """.trimIndent()
        )
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        db.execSQL("DROP TABLE IF EXISTS events")
        onCreate(db)
    }

    @Synchronized
    fun log(type: String, detail: String) {
        val db = writableDatabase
        val now = System.currentTimeMillis()
        synchronized(Companion) {
            if (!retentionPruned) {
                db.delete("events", "ts_epoch_ms < ?", arrayOf(retentionCutoff(now).toString()))
                retentionPruned = true
            }
        }
        val cv = ContentValues().apply {
            put("ts_epoch_ms", now)
            put("type", type)
            put("detail", redactedDetail(type, detail) { PyBridge.redactDiagnostic(appContext, it) })
        }
        db.insert("events", null, cv)
    }

    data class Event(val tsEpochMs: Long, val type: String, val detail: String) {
        fun formatted(): String = "${TS_FORMAT.format(Date(tsEpochMs))}  [$type]  $detail"
    }

    @Synchronized
    fun recent(limit: Int = 500): List<Event> {
        val out = mutableListOf<Event>()
        readableDatabase.rawQuery(
            "SELECT ts_epoch_ms, type, detail FROM events ORDER BY id DESC LIMIT ?",
            arrayOf(limit.toString())
        ).use { c ->
            while (c.moveToNext()) {
                out.add(Event(c.getLong(0), c.getString(1), c.getString(2)))
            }
        }
        return out
    }

    @Synchronized
    fun exportAsText(): String {
        val all = mutableListOf<Event>()
        readableDatabase.rawQuery(
            "SELECT ts_epoch_ms, type, detail FROM events ORDER BY id ASC",
            null
        ).use { c ->
            while (c.moveToNext()) {
                all.add(Event(c.getLong(0), c.getString(1), c.getString(2)))
            }
        }
        val header = "etoroNightwatch minimal proof APK — event log export\n" +
            "Generated ${TS_FORMAT.format(Date())}\n" +
            "${all.size} events\n" +
            "=".repeat(60) + "\n"
        return header + all.joinToString("\n") { it.formatted() }
    }
}
