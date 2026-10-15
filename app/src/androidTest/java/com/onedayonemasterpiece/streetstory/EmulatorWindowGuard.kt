package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry
import androidx.test.runner.lifecycle.Stage
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import java.io.File

/** Retained emulator evidence identifies this one external launcher failure. */
internal object EmulatorWindowGuard {
    private val closedForBoundary = mutableSetOf<String>()

    fun requireStoryWindow(context: Context, device: UiDevice, evidencePrefix: String) {
        val launcher = By.res("android", "alertTitle").text("Pixel Launcher isn't responding")
        if (device.hasObject(launcher)) {
            if (!closedForBoundary.add(evidencePrefix)) {
                capture(context, device, "$evidencePrefix-window-blocked")
                error("Pixel Launcher ANR recurred after one close action; retained blocked window")
            }
            capture(context, device, "$evidencePrefix-launcher-anr")
            Log.w("StreetStoryPlatformE2E", "boundary=$evidencePrefix external_pixel_launcher_anr retained=true action=close_once")
            // Match the observed system title and close resource. Never close
            // a Street Story ANR, an unknown dialog, or retry a product tap.
            requireNotNull(device.findObject(By.res("android", "aerr_close").text("Close app"))) {
                "The named external launcher ANR has no scoped close action"
            }.click()
            check(device.wait(Until.gone(launcher), 3000)) {
                "Pixel Launcher ANR did not close after its single close action"
            }
        }
        val instrumentation = InstrumentationRegistry.getInstrumentation()
        val power = context.getSystemService(PowerManager::class.java)
        val deadline = SystemClock.elapsedRealtime() + 3000
        var focused = false
        // Accessibility can retain a populated hierarchy while the emulator
        // shows a black frame and has no focused input window. Observe the
        // resumed Activity's actual window before injecting the one product tap.
        do {
            instrumentation.runOnMainSync {
                focused = ActivityLifecycleMonitorRegistry.getInstance()
                    .getActivitiesInStage(Stage.RESUMED).any { activity ->
                        activity.packageName == context.packageName &&
                            activity.hasWindowFocus() && activity.window.decorView.isShown
                    }
            }
            if (focused && power.isInteractive && device.currentPackageName == context.packageName) return
            SystemClock.sleep(50)
        } while (SystemClock.elapsedRealtime() < deadline)
        if (!focused || !power.isInteractive || device.currentPackageName != context.packageName) {
            capture(context, device, "$evidencePrefix-window-blocked")
            error("Street Story needs a focused resumed window and interactive display before interaction; " +
                "actual=${device.currentPackageName} focused=$focused interactive=${power.isInteractive}")
        }
    }

    private fun capture(context: Context, device: UiDevice, prefix: String) {
        device.dumpWindowHierarchy(File(context.getExternalFilesDir(null), "$prefix.xml"))
        check(device.takeScreenshot(File(context.getExternalFilesDir(null), "$prefix.png"))) {
            "Could not retain the blocking emulator window"
        }
    }
}
