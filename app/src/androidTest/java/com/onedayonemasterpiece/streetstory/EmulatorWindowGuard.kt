package com.onedayonemasterpiece.streetstory

import android.content.Context
import android.util.Log
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
        val productVisible = device.wait(Until.hasObject(By.pkg(context.packageName)), 3000)
        if (!productVisible || device.currentPackageName != context.packageName) {
            capture(context, device, "$evidencePrefix-window-blocked")
            error("Street Story must own the active accessibility window before interaction; actual=${device.currentPackageName}")
        }
    }

    private fun capture(context: Context, device: UiDevice, prefix: String) {
        device.dumpWindowHierarchy(File(context.getExternalFilesDir(null), "$prefix.xml"))
        check(device.takeScreenshot(File(context.getExternalFilesDir(null), "$prefix.png"))) {
            "Could not retain the blocking emulator window"
        }
    }
}
