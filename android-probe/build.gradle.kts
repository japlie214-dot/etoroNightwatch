// etoroNightwatch minimal proof APK — root build script.
// Version combination pinned exactly to the known-good combo verified in
// doc/RUNBOOK_INFRA.md Part G.7 (chaquopy-probe throwaway build, 2026-09-03):
// Chaquopy 17.0.0, AGP 8.9.0, Kotlin 2.0.21, JDK 17, compileSdk/targetSdk 36, minSdk 24.
plugins {
    id("com.android.application") version "8.9.0" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
    id("com.chaquo.python") version "17.0.0" apply false
    id("com.google.gms.google-services") version "4.4.2" apply false
}
