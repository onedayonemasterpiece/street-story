package com.onedayonemasterpiece.streetstory

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.ExecutorService

@RunWith(AndroidJUnit4::class)
class LiveBudgetStatusInstrumentedTest {
    @Test fun capabilityHistoryWaitDoesNotAdmitSpeechOrPretendToListen() {
        val controller = LiveSessionController(ApplicationProvider.getApplicationContext<Context>())
        val type = LiveSessionController::class.java
        val handle = type.getDeclaredMethod("handleEvent", Integer.TYPE, LiveEventWire::class.java)
            .apply { isAccessible = true }
        val wait = type.getDeclaredField("waitStarted").apply { isAccessible = true }
        type.getDeclaredField("state").apply { isAccessible = true }
            .set(controller, LiveUiState(active = true, status = "Слушаю"))
        fun emit(kind: String) = handle.invoke(controller, 0, LiveEventWire().apply { this.type = kind })
        try {
            // Exact failed-phone order: handoff ACK, old turn completion,
            // fresh setup/history budget wait, then more owner speech.
            emit("capability_transition_acknowledged")
            assertTrue(controller.shouldSuppressMicrophoneInput())
            val epoch = controller.microphoneInputSuppression().epoch
            emit("turn_complete")
            assertFalse(controller.snapshot().status == "Слушаю")
            emit("capability_transition_started")
            assertEquals(epoch, controller.microphoneInputSuppression().epoch)
            emit("resource_budget_wait")
            val started = wait.getLong(controller)
            repeat(1000) { controller.submitPcm(ShortArray(480) { 1000 }) }
            assertFalse(controller.snapshot().inputActive)
            assertEquals(started, wait.getLong(controller))
            assertTrue(controller.snapshot().active)
            assertEquals(null, controller.snapshot().error)
            emit("resource_budget_ready")
            assertTrue("A grant is not a ready capability", controller.shouldSuppressMicrophoneInput())
            emit("capability_ready")
            assertFalse(controller.shouldSuppressMicrophoneInput())
            assertTrue(controller.microphoneInputSuppression().epoch > epoch)
            controller.submitPcm(ShortArray(480) { 1000 })
            assertTrue(controller.snapshot().inputActive)
            emit("capability_transition_acknowledged")
            controller.stopLocal(false)
            emit("capability_ready") // Stale callback cannot restart after Stop.
            assertFalse(controller.snapshot().active)
            assertFalse(controller.snapshot().inputActive)
        } finally {
            controller.stopLocal(false)
            for (name in listOf("network", "playback", "clocks")) {
                (type.getDeclaredField(name).apply { isAccessible = true }
                    .get(controller) as ExecutorService).shutdownNow()
            }
        }
    }

    @Test fun providerWaitEventsShowTheWaitAndDoNotOverwriteAReplyAfterReady() {
        val controller = LiveSessionController(ApplicationProvider.getApplicationContext<Context>())
        try {
            val controllerType = LiveSessionController::class.java
            val handle = controllerType.getDeclaredMethod("handleEvent", Integer.TYPE, LiveEventWire::class.java)
                .apply { isAccessible = true }
            val waitStarted = controllerType.getDeclaredField("waitStarted").apply { isAccessible = true }
            controllerType.getDeclaredField("state").apply { isAccessible = true }
                .set(controller, LiveUiState(active = true, status = "Слушаю"))
            fun emit(kind: String, message: String? = null) {
                handle.invoke(controller, 0, LiveEventWire().apply { type = kind; text = message })
            }
            for (ready in listOf("resource_budget_ready", "capability_ready")) {
                emit("resource_budget_wait")
                assertEquals("Ожидаю доступный лимит", controller.snapshot().status)
                assertTrue(waitStarted.getLong(controller) > 0L)
                val firstWait = maxOf(1L, waitStarted.getLong(controller) - 100L)
                waitStarted.setLong(controller, firstWait)
                emit("capability_budget_wait")
                assertEquals("Ожидаю доступный лимит", controller.snapshot().status)
                assertEquals(firstWait, waitStarted.getLong(controller))
                emit(ready)
                assertEquals("Думаю", controller.snapshot().status)
                emit("resource_budget_wait")
                emit("output_transcript", "Готово.")
                emit(ready)
                assertEquals("Мира отвечает", controller.snapshot().status)
                assertEquals("Готово.", controller.snapshot().assistantText)
                assertTrue(controller.snapshot().active)
            }
        } finally {
            try {
                controller.stopLocal(false)
            } finally {
                for (name in listOf("network", "playback", "clocks")) {
                    val executor = LiveSessionController::class.java.getDeclaredField(name)
                        .apply { isAccessible = true }.get(controller) as ExecutorService
                    executor.shutdownNow()
                }
            }
        }
    }
}
