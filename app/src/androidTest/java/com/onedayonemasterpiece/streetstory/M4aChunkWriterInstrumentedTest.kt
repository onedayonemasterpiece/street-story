package com.onedayonemasterpiece.streetstory

import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class M4aChunkWriterInstrumentedTest {
    @Test fun pcmFramesBecomeDurableM4a(){
        val context=ApplicationProvider.getApplicationContext<android.content.Context>()
        val dir=File(context.cacheDir,"m4a-test").apply{deleteRecursively();mkdirs()}
        val writer=M4aChunkWriter(dir,"voice-test",0,0)
        repeat(100){index->
            val frame=ShortArray(EfficientVad.FRAME_SAMPLES){sample->(((sample%40)-20)*300).toShort()}
            writer.writeFrame(frame,index*30L,(index+1)*30L)
        }
        val closed=writer.close();assertNotNull(closed);requireNotNull(closed)
        assertTrue(closed.file.isFile);assertTrue(closed.file.length()>0);assertEquals(64,closed.sha256.length);assertTrue(closed.audioEndMs>=2900)
    }
}
