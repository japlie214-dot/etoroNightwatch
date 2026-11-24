plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
    id("com.google.gms.google-services")
}

// Authentication requires a trusted runtime provider; no credentials belong in BuildConfig.

android {
    namespace = "com.nightwatch.etoro.probe"
    // compileSdk/targetSdk 36, minSdk 24 — the exact combination proven in
    // RUNBOOK_INFRA.md Part G.7 to build cleanly with Chaquopy 17.0.0.
    compileSdk = 36

    defaultConfig {
        // Deliberately the REAL app's package name (DESIGN.md, prompt instructions): the probe
        // is meant to receive the exact same OS treatment (standby bucket, battery policy,
        // hibernation clock) that com.nightwatch.etoro will get once it exists for real.
        applicationId = "com.nightwatch.etoro"
        minSdk = 24
        targetSdk = 36
        // v2: upgrade over the v1 APK already installed on the phone (adb install -r).
        // Same applicationId + signing config as v1 — only the version bumps.
        versionCode = 7
        versionName = "0.7-probe"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            // Snapdragon 7 Gen 3 on the iQOO Z10 is arm64-v8a only (DESIGN §1.2, §7.1).
            // Chaquopy ships per-ABI native wheels; bundling anything else is pure bloat.
            abiFilters += listOf("arm64-v8a")
        }

        buildConfigField(
            "String",
            "WORKER_BASE_URL",
            "\"https://etoro-nightwatch-worker.japlie214.workers.dev\""
        )
    }

    signingConfigs {
        // Debug-signed only. This is a probe, not a release artifact.
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
        }
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources {
            excludes += "/META-INF/{AL2.0,LGPL2.1}"
        }
    }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        // Matches the buildPython host interpreter proven in the chaquopy-probe throwaway build.
        buildPython("C:/Users/10102721/AppData/Local/Programs/Python/Python313/python.exe")
        pip {
            // tzdata is included proactively: Python's zoneinfo on Android may not find the
            // system's APEX tzdata module the way it does on desktop Linux, and this is exactly
            // the kind of thing that silently produces a wrong trade time (DESIGN §1.3). Bundling
            // the pure-Python `tzdata` package (IANA data as a Python package) means
            // zoneinfo.ZoneInfo("America/New_York") resolves correctly on-device regardless of
            // whether Android's own system tzdata is reachable from Chaquopy's embedded
            // interpreter. See README.md and probe/checks.py for how this is verified/reported.
            install("tzdata")

            // v2 check 4: numpy/pandas were deliberately omitted from v1 (proven only at BUILD
            // time in the throwaway chaquopy-probe project, never at RUNTIME on this device, and
            // never bundled in an APK actually installed on the phone). The real strategy needs
            // both. Expect the APK to grow ~30 MB — that is fine and expected, not a regression.
            install("numpy")
            install("pandas")

            // Tonight's device-probe wiring (doc/RUNBOOK_DEVICE_PROBE.md, section A): the real
            // app's nightwatch.etoro.client uses httpx for every HTTP call, and
            // probe_live_2026_09_11.py's own section A imports exchange_calendars to prove it
            // resolves the XNYS calendar on-device. Neither was previously in this pip block —
            // section A existing to prove exactly these imports work at runtime would otherwise
            // fail on the very first line.
            install("httpx==0.27.2")
            // NOT 4.13.2 (this repo's desktop venv version, and requirements.txt's pin): a REAL
            // build attempt here failed --
            //   "Cannot install exchange-calendars==4.13.2 and numpy because these package
            //   versions have conflicting dependencies... exchange-calendars 4.13.2 depends on
            //   numpy>=1.26.4" -- while Chaquopy's own pypi index (chaquo.com/pypi-13.1) only
            //   ever offers a prebuilt numpy wheel at 1.26.2 for cp313/arm64-v8a (confirmed by
            //   listing that index directly). exchange_calendars tightened its numpy floor to
            //   >=1.26.4 starting at 4.12; 4.11.1 is the newest release before that tightening
            //   (confirmed against PyPI's own per-version requires_dist), and its other
            //   dependencies (pyluach, toolz, korean_lunar_calendar) are pure Python, resolvable
            //   from pypi.org directly. This makes the desktop venv (exchange_calendars==4.13.2,
            //   numpy 2.5.2) and this on-device pin (4.11.1, numpy 1.26.2) DIFFERENT VERSIONS on
            //   each side -- a real, reported divergence, not swept under the "keep them in sync"
            //   convention every other pin in this file follows. Revisit if Chaquopy ever ships
            //   a newer numpy wheel.
            install("exchange_calendars==4.11.1")
        }
    }
    // The real nightwatch package (this repo's app/src/main/python) is not otherwise part
    // of this APK's Chaquopy source set — there is no buildable Android project under
    // app/ yet, only this probe. Adding it as an extra python source dir makes
    // `import nightwatch...` work on-device without copying/duplicating the package.
    // probe_live_2026_09_11.py (a byte-for-byte copy of tools/probe_live_2026-09-11.py —
    // see that file's own docstring, which anticipates exactly this deployment) and
    // probe/probe_runner.py under this project's own src/main/python are unaffected;
    // this only adds nightwatch itself. (chaquopy's own sourceSets container, NOT
    // android.sourceSets — Chaquopy exposes ChaquopyExtension.sourceSets as a sibling of
    // defaultConfig, confirmed against the installed com.chaquo.python:gradle:17.0.0 plugin
    // classes since no doc page for this exact DSL shape was open to check against.)
    sourceSets {
        getByName("main") {
            srcDir("../../app/src/main/python")
        }
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20160810") // JVM runtime; android.jar's org.json is stubbed
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.activity:activity-ktx:1.9.2")
    implementation("androidx.sqlite:sqlite-ktx:2.4.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    implementation(platform("com.google.firebase:firebase-bom:33.5.1"))
    implementation("com.google.firebase:firebase-messaging-ktx")
}
