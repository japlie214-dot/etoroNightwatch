import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
    id("com.google.gms.google-services")
}

// local.properties is gitignored — holds the Android SDK path and PHONE_PUSH_SECRET.
// See local.properties.example / README.md for what must be filled in before building.
val localProps = Properties()
val localPropsFile = rootProject.file("local.properties")
if (localPropsFile.exists()) {
    localProps.load(FileInputStream(localPropsFile))
}
val phonePushSecret: String = localProps.getProperty("PHONE_PUSH_SECRET", "")

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
        versionCode = 1
        versionName = "0.1-probe"

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
        buildConfigField("String", "PHONE_PUSH_SECRET", "\"$phonePushSecret\"")
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
            // Deliberately NOT numpy/pandas here (proven separately in chaquopy-probe; 30 MB is
            // pointless for this probe, per the task brief).
            //
            // tzdata is included proactively: Python's zoneinfo on Android may not find the
            // system's APEX tzdata module the way it does on desktop Linux, and this is exactly
            // the kind of thing that silently produces a wrong trade time (DESIGN §1.3). Bundling
            // the pure-Python `tzdata` package (IANA data as a Python package) means
            // zoneinfo.ZoneInfo("America/New_York") resolves correctly on-device regardless of
            // whether Android's own system tzdata is reachable from Chaquopy's embedded
            // interpreter. See README.md and probe/checks.py for how this is verified/reported.
            install("tzdata")
        }
    }
}

dependencies {
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
