package com.onedayonemasterpiece.streetstory

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AcousticEchoCanceler
import android.media.audiofx.NoiseSuppressor
import android.os.Build
import android.os.IBinder
import androidx.core.content.ContextCompat
import java.io.File
import java.time.Duration
import java.time.OffsetDateTime
import java.util.ArrayDeque

class RecordingService : Service() {
    @Volatile private var captureRequested=false
    @Volatile private var audioRecord:AudioRecord?=null
    private var captureThread:Thread?=null
    private val store by lazy { AppGraph.store(this) }
    private val audioDirectory by lazy { File(filesDir,"audio") }
    private var sessionId:String?=null
    private var finishingCapture=false
    private var pendingStartIntent:Intent?=null
    private val runtime by lazy { RecordingRuntime(this) }

    override fun onCreate(){super.onCreate();createNotificationChannel()}
    override fun onBind(intent:Intent?):IBinder?=null
    override fun onStartCommand(intent:Intent?,flags:Int,startId:Int):Int{
        when(intent?.action){ACTION_START->startNewSession(intent);ACTION_PAUSE->pauseSession();ACTION_RESUME->resumeSession();ACTION_FINISH->finishSession();ACTION_TRANSPORT_FINISH->finishSession(false)}
        return START_NOT_STICKY
    }
    private fun startNewSession(intent:Intent){
        if(finishingCapture){pendingStartIntent=Intent(intent);return}
        if(store.activeVoiceSession()!=null)return
        val storyId=intent.getStringExtra(EXTRA_STORY_ID)?:return
        val kind=intent.getStringExtra(EXTRA_KIND)?:RecordingKind.INITIAL
        if(kind==RecordingKind.LIVE_ARCHIVE&&!AppGraph.live(this).isActiveFor(storyId)){broadcast("Live-сессия ещё не готова");return}
        val session=store.createVoiceSession(storyId,kind,"${Build.MANUFACTURER} ${Build.MODEL}".trim())
        sessionId=session.sessionId;enterForeground(if(kind==RecordingKind.LIVE_ARCHIVE)"Live · слушаю" else "Слушаю · тишина не записывается",false);beginCapture();broadcast()
    }
    private fun pauseSession(){val active=store.activeVoiceSession()?:return;sessionId=active.sessionId;if(active.captureState==CaptureState.RECORDING)stopCapture();store.beginManualPause(active.sessionId);val refreshed=store.voiceSession(active.sessionId)?:active;runtime.update(active.sessionId,refreshed.durationMs,elapsedFromStart(refreshed.startedAt),refreshed.autoSilenceSkippedMs,CaptureActivity.MANUAL_PAUSE);enterForeground("Пауза · микрофон остановлен",true);SyncScheduler.enqueue(this);broadcast()}
    private fun resumeSession(){val active=store.activeVoiceSession()?:return;sessionId=active.sessionId;store.endManualPause(active.sessionId);enterForeground("Слушаю · тишина не записывается",false);beginCapture();broadcast()}
    private fun finishSession(stopLive:Boolean=true){
        val active=store.activeVoiceSession()
        if(stopLive&&(active==null||active.kind==RecordingKind.LIVE_ARCHIVE))AppGraph.live(this).stopLocal()
        pendingStartIntent=null
        if(finishingCapture)return
        finishingCapture=true
        // Stop hardware admission immediately. Archive finalization must not
        // block the Activity/UI thread for the old ten-second join timeout.
        captureRequested=false
        runCatching{audioRecord?.stop()}
        val closingThread=captureThread
        Thread({
            closingThread?.join(10_000)
            android.os.Handler(mainLooper).post {
                if(closingThread?.isAlive==true){
                    finishingCapture=false
                    AppGraph.live(this).diagnostic("capture_stop_timeout")
                    broadcast("Микрофон остановлен; сохранение записи ещё завершается")
                    return@post
                }
                captureThread=null
                var message:String?=null
                if(active!=null){
                    if(active.captureState!=CaptureState.RECORDING)store.endManualPause(active.sessionId)
                    val refreshed=store.voiceSession(active.sessionId)
                    if(refreshed!=null){
                        if(refreshed.durationMs<MIN_SESSION_MS||refreshed.chunkCount==0){
                            store.discardVoiceSession(active.sessionId)
                            if(active.kind!=RecordingKind.LIVE_ARCHIVE)message="Слишком короткая запись удалена"
                        }else{
                            val ended=OffsetDateTime.now();val wall=elapsedBetween(refreshed.startedAt,ended)
                            val skipped=(wall-refreshed.manualPauseMs-refreshed.durationMs).coerceAtLeast(0)
                            store.finishVoiceSession(active.sessionId,ended.toString(),wall,skipped)
                            SyncScheduler.enqueue(this)
                        }
                    }
                    runtime.clear(active.sessionId)
                }
                finishingCapture=false
                val next=pendingStartIntent;pendingStartIntent=null
                if(next!=null){startNewSession(next)}else{stopForeground(STOP_FOREGROUND_REMOVE);stopSelf()}
                broadcast(message)
            }
        },"street-story-finish").start()
    }
    @Synchronized private fun beginCapture(){
        if(captureThread?.isAlive==true)return
        val id=sessionId?:store.activeVoiceSession()?.sessionId?:return
        if(ContextCompat.checkSelfPermission(this,Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){pauseForMicrophoneFailure(id,"Разрешение на микрофон отозвано");return}
        store.setCaptureState(id,CaptureState.RECORDING,CaptureActivity.AUTO_SILENCE);captureRequested=true;captureThread=Thread({captureLoop(id)},"street-story-capture").also{it.start()}
    }
    private fun captureLoop(id:String){
        val initial=store.voiceSession(id)?:return
        val live=if(initial.kind==RecordingKind.LIVE_ARCHIVE)AppGraph.live(this) else null
        val sessionStart=runCatching{OffsetDateTime.parse(initial.startedAt).toInstant().toEpochMilli()}.getOrElse{System.currentTimeMillis()}
        val manualPauseMs=initial.manualPauseMs
        val minBuffer=AudioRecord.getMinBufferSize(AudioProfile.SAMPLE_RATE_HZ,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT)
        if(minBuffer<=0){pauseForMicrophoneFailure(id,"Устройство не предоставило аудиобуфер");return}
        // The owner's VoIP capture trace was nearly silent before VAD.
        // Live gates playback locally; avoid redundant voice-call processing.
        val audioSource = MediaRecorder.AudioSource.VOICE_RECOGNITION
        val audioManager = getSystemService(AudioManager::class.java)
        val recorder=try{
            AudioRecord.Builder().setAudioSource(audioSource).setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(AudioProfile.SAMPLE_RATE_HZ).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build()).setBufferSizeInBytes(maxOf(minBuffer*2,EfficientVad.FRAME_SAMPLES*8)).build()
        }catch(exc:SecurityException){
            pauseForMicrophoneFailure(id,"Разрешение на микрофон недоступно");return
        }catch(exc:Exception){
            pauseForMicrophoneFailure(id,"Не удалось открыть микрофон: ${exc.message}");return
        }
        audioRecord=recorder
        val suppressor=if(NoiseSuppressor.isAvailable())runCatching{NoiseSuppressor.create(recorder.audioSessionId)?.also{it.enabled=live==null}}.getOrNull() else null
        val echoCanceler=if(live!=null&&AcousticEchoCanceler.isAvailable())runCatching{AcousticEchoCanceler.create(recorder.audioSessionId)?.also{it.enabled=false}}.getOrNull() else null
        live?.diagnostic(
            "capture_configured",
            mapOf(
                "audio_source" to audioSource,
                "sample_rate" to AudioProfile.SAMPLE_RATE_HZ,
                "frame_ms" to EfficientVad.FRAME_MS,
                "noise_suppressor_available" to NoiseSuppressor.isAvailable(),
                "noise_suppressor_enabled" to (suppressor?.enabled == true),
                "aec_available" to AcousticEchoCanceler.isAvailable(),
                "aec_enabled" to (echoCanceler?.enabled == true),
            ),
        )
        val admission=if(live!=null)LiveSpeechAdmission() else null
        val microphoneHealth = LiveMicrophoneHealth()
        var meterSamples=0L;var meterSquares=0.0;var lastMeterAt=android.os.SystemClock.elapsedRealtime()
        var wasPlaybackSuppressed=false
        var speechEpisodes=0
        val detector=EfficientVad(true,interactive=live!=null);val latch=SpeechLatch(if(live!=null)1 else LIVE_ATTACK_FRAMES,HANGOVER_FRAMES);val preRoll=ArrayDeque<FramePacket>();var writer:M4aChunkWriter?=null;var persisted=store.persistedDuration(id);var activity=CaptureActivity.AUTO_SILENCE;var lastActivity:String?=null;var lastRuntime=-1L;var lastStore=-1L;var silenceStart:Long?=null;val frame=ShortArray(EfficientVad.FRAME_SAMPLES)
        var signalSamples=0L;var signalSquares=0.0;var signalPeak=0;var lastSignalAt=android.os.SystemClock.elapsedRealtime()
        try{
            recorder.startRecording();check(recorder.recordingState==AudioRecord.RECORDSTATE_RECORDING)
            while(captureRequested){
                if(!readFrame(recorder,frame))continue
                val suppressForPlayback=live?.shouldSuppressMicrophoneInput()==true
                frame.forEach { sample ->
                    val value=sample.toInt()
                    val square=value.toDouble()*value
                    signalSquares+=square;meterSquares+=square
                    signalPeak=maxOf(signalPeak,kotlin.math.abs(value))
                }
                signalSamples+=frame.size;meterSamples+=frame.size
                val signalAt=android.os.SystemClock.elapsedRealtime()
                if(live!=null&&signalAt-lastMeterAt>=250){
                    val muted=runCatching{audioManager.isMicrophoneMute}.getOrDefault(false)
                    val silenced=runCatching{recorder.activeRecordingConfiguration?.isClientSilenced==true}.getOrDefault(false)
                    live.observeMicrophone(initial.storyId, microphoneHealth.observe(
                        signalAt,kotlin.math.sqrt(meterSquares/meterSamples.coerceAtLeast(1)),
                        muted,silenced,suppressForPlayback,
                    ))
                }
                if(signalAt-lastMeterAt>=250){
                    lastMeterAt=signalAt;meterSamples=0;meterSquares=0.0
                }
                if(signalAt-lastSignalAt>=5000){
                    val capture=runCatching{recorder.activeRecordingConfiguration}.getOrNull()
                    live?.diagnostic("capture_signal",mapOf(
                        "samples" to signalSamples,"rms" to kotlin.math.sqrt(signalSquares/signalSamples.coerceAtLeast(1)).toInt(),
                        "peak" to signalPeak,"vad_open" to latch.active,"speech_episodes" to speechEpisodes,
                        "vad_mode" to if(live!=null)3 else EfficientVad.MODE,
                        "audio_source" to audioSource,"actual_audio_source" to capture?.clientAudioSource,
                        "system_muted" to runCatching{audioManager.isMicrophoneMute}.getOrDefault(false),
                        "client_silenced" to capture?.isClientSilenced,
                        "route_type" to runCatching{recorder.routedDevice?.type}.getOrNull(),
                        "playback_suppressed" to suppressForPlayback,
                    ) + (admission?.metrics() ?: emptyMap()))
                    lastSignalAt=signalAt;signalSamples=0;signalSquares=0.0;signalPeak=0
                }
                val wallEnd=(System.currentTimeMillis()-sessionStart).coerceAtLeast(0)
                val wallStart=(wallEnd-EfficientVad.FRAME_MS).coerceAtLeast(0)
                val wasActive=latch.active
                if(wasPlaybackSuppressed&&!suppressForPlayback){
                    detector.resetAfterPlayback()
                    admission?.resetEvidence()
                    live?.diagnostic("vad_reset_after_playback",mapOf("native_reset" to true))
                }
                wasPlaybackSuppressed=suppressForPlayback
                if(detector.isUnavailable)throw IllegalStateException("LIVE_VAD_UNAVAILABLE")
                val active=if(suppressForPlayback){
                    if(wasActive)live?.endSpeech()
                    latch.reset()
                    preRoll.clear()
                    admission?.resetEvidence()
                    false
                }else{
                    val rawSpeech=detector.isSpeech(frame)
                    latch.onFrame(admission?.accept(rawSpeech,frameRms(frame)) ?: rawSpeech)
                }
                if(active){
                    silenceStart=null;writer=writer?:newWriter(id,persisted)
                    if(!wasActive){
                        speechEpisodes++
                        live?.diagnostic("speech_admission", (admission?.metrics() ?: emptyMap()) + mapOf("episode" to speechEpisodes,"frame_rms" to frameRms(frame).toInt()))
                        pushPreRoll(preRoll,frame,wallStart,wallEnd)
                        while(preRoll.isNotEmpty()){val buffered=preRoll.removeFirst();writer?.writeFrame(buffered.samples,buffered.wallStartMs,buffered.wallEndMs);live?.submitPcm(buffered.samples)}
                    }else{writer?.writeFrame(frame,wallStart,wallEnd);live?.submitPcm(frame)}
                    activity=if(detector.isFailOpen)CaptureActivity.FALLBACK_CONTINUOUS else CaptureActivity.VOICE
                    if((writer?.durationMs?:0)>=M4aChunkWriter.TARGET_SEGMENT_MS){persisted=persist(writer?.close(),persisted);writer=null}
                }else{
                    if(wasActive&&!suppressForPlayback){live?.endSpeech();admission?.resetEvidence()}
                    if(!suppressForPlayback)pushPreRoll(preRoll,frame,wallStart,wallEnd)
                    if(silenceStart==null)silenceStart=wallStart;activity=CaptureActivity.AUTO_SILENCE;val silenceMs=wallEnd-(silenceStart?:wallEnd);if(silenceMs>=LONG_SILENCE_CLOSE_MS&&(writer?.durationMs?:0)>=MIN_DURABLE_SEGMENT_MS){persisted=persist(writer?.close(),persisted);writer=null}
                }
                val recorded=persisted+(writer?.durationMs?:0);val skipped=(wallEnd-manualPauseMs-recorded).coerceAtLeast(0);val changed=activity!=lastActivity
                if(lastRuntime<0||wallEnd-lastRuntime>=RUNTIME_UPDATE_INTERVAL_MS||changed){runtime.update(id,recorded,wallEnd,skipped,activity);lastRuntime=wallEnd}
                if(lastStore<0||wallEnd-lastStore>=STORE_UPDATE_INTERVAL_MS||changed){store.updateCaptureProgress(id,recorded,wallEnd,manualPauseMs,skipped,activity);lastStore=wallEnd}
                if(changed){
                    updateNotification(activity)
                    lastActivity=activity
                    live?.diagnostic(
                        "capture_activity",
                        mapOf(
                            "activity" to activity,
                            "vad_fail_open" to detector.isFailOpen,
                            "recorded_ms" to recorded,
                            "wall_ms" to wallEnd,
                        ),
                    )
                    broadcast(if(activity==CaptureActivity.FALLBACK_CONTINUOUS)"Автопропуск недоступен · записываю всё" else null)
                }
            }
        }catch(exc:Exception){
            live?.diagnostic("capture_error",mapOf("type" to exc.javaClass.simpleName,"message" to (exc.message ?: "").take(240)))
            if(live!=null && detector.isUnavailable)live.stopLocal()
            if(captureRequested){captureRequested=false;store.beginManualPause(id);store.setLocalVoiceError(id,"Ошибка записи: ${exc.message}");enterForeground("Запись остановлена с ошибкой",true)}
        }finally{
            runCatching{recorder.stop()};recorder.release();audioRecord=null;suppressor?.release();echoCanceler?.release();detector.close();persisted=persist(writer?.close(),persisted)
            val finalActivity=if(captureRequested)activity else CaptureActivity.IDLE
            val final=store.voiceSession(id)
            if(final!=null){
                val wall=(System.currentTimeMillis()-sessionStart).coerceAtLeast(0)
                val skipped=(wall-final.manualPauseMs-persisted).coerceAtLeast(0)
                store.updateCaptureProgress(id,persisted,wall,final.manualPauseMs,skipped,finalActivity)
                runtime.update(id,persisted,wall,skipped,finalActivity)
            }
            live?.diagnostic("capture_stopped",mapOf("recorded_ms" to persisted,"activity" to finalActivity))
            broadcast()
        }
    }
    private fun readFrame(recorder:AudioRecord,target:ShortArray):Boolean{var offset=0;while(offset<target.size&&captureRequested){val count=recorder.read(target,offset,target.size-offset,AudioRecord.READ_BLOCKING);if(count==AudioRecord.ERROR_DEAD_OBJECT)throw IllegalStateException("Android audio service was restarted");if(count<0)throw IllegalStateException("AudioRecord read failed: $count");if(count==0)continue;offset+=count};return offset==target.size}
    private fun pushPreRoll(queue:ArrayDeque<FramePacket>,samples:ShortArray,wallStart:Long,wallEnd:Long){val reusable=if(queue.size>=PRE_ROLL_FRAMES)queue.removeFirst().samples else ShortArray(samples.size);samples.copyInto(reusable);queue.addLast(FramePacket(reusable,wallStart,wallEnd))}
    private fun newWriter(id:String,audioStart:Long)=M4aChunkWriter(audioDirectory,id,store.nextChunkIndex(id),audioStart)
    private fun persist(chunk:M4aChunkWriter.ClosedChunk?,previous:Long):Long{if(chunk==null)return previous;store.addChunk(chunk.sessionId,chunk.chunkIndex,chunk.audioStartMs,chunk.audioEndMs,chunk.wallStartMs,chunk.wallEndMs,chunk.file.absolutePath,chunk.sha256,AudioProfile.MIME_M4A);return chunk.audioEndMs}
    @Synchronized private fun stopCapture(){captureRequested=false;runCatching{audioRecord?.stop()};captureThread?.join(10_000);captureThread=null}
    private fun pauseForMicrophoneFailure(id:String,message:String){captureRequested=false;store.beginManualPause(id);store.setLocalVoiceError(id,message);enterForeground("Микрофон недоступен",true);broadcast(message)}
    private fun enterForeground(text:String,paused:Boolean){val n=notification(text,paused);if(Build.VERSION.SDK_INT>=Build.VERSION_CODES.Q)startForeground(NOTIFICATION_ID,n,ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)else startForeground(NOTIFICATION_ID,n)}
    private fun updateNotification(activity:String){val text=when(activity){CaptureActivity.VOICE->"Записываю голос";CaptureActivity.AUTO_SILENCE->"Слушаю · тишина не записывается";CaptureActivity.FALLBACK_CONTINUOUS->"Автопропуск недоступен · записываю всё";else->"Запись идёт"};getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID,notification(text,false))}
    @Suppress("DEPRECATION") private fun notification(text:String,paused:Boolean):Notification{val toggle=if(paused)ACTION_RESUME else ACTION_PAUSE;val label=if(paused)"Продолжить" else "Пауза";val open=PendingIntent.getActivity(this,0,Intent(this,MainActivity::class.java),PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE);return Notification.Builder(this,CHANNEL_ID).setSmallIcon(R.drawable.ic_mic).setContentTitle("Street Story").setContentText(text).setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true).addAction(0,label,serviceIntent(toggle,1)).addAction(0,"Завершить",serviceIntent(ACTION_FINISH,2)).build()}
    private fun serviceIntent(action:String,requestCode:Int)=PendingIntent.getService(this,requestCode,Intent(this,RecordingService::class.java).setAction(action),PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    private fun createNotificationChannel(){getSystemService(NotificationManager::class.java).createNotificationChannel(NotificationChannel(CHANNEL_ID,getString(R.string.notification_channel),NotificationManager.IMPORTANCE_LOW))}
    private fun broadcast(message:String?=null){sendBroadcast(Intent(ACTION_STATE_CHANGED).setPackage(packageName).putExtra(EXTRA_MESSAGE,message))}
    private fun elapsedFromStart(started:String)=runCatching{(System.currentTimeMillis()-OffsetDateTime.parse(started).toInstant().toEpochMilli()).coerceAtLeast(0)}.getOrDefault(0)
    private fun elapsedBetween(started:String,ended:OffsetDateTime)=runCatching{Duration.between(OffsetDateTime.parse(started),ended).toMillis().coerceAtLeast(0)}.getOrDefault(0)
    override fun onDestroy(){if(captureRequested){val current=sessionId?.let{store.voiceSession(it)};if(current?.kind==RecordingKind.LIVE_ARCHIVE)AppGraph.live(this).stopLocal();stopCapture();sessionId?.let{store.beginManualPause(it)}};super.onDestroy()}
    private data class FramePacket(val samples:ShortArray,val wallStartMs:Long,val wallEndMs:Long)
    companion object{
        const val ACTION_TRANSPORT_FINISH="com.onedayonemasterpiece.streetstory.TRANSPORT_FINISH"
        const val ACTION_START="com.onedayonemasterpiece.streetstory.START";const val ACTION_PAUSE="com.onedayonemasterpiece.streetstory.PAUSE";const val ACTION_RESUME="com.onedayonemasterpiece.streetstory.RESUME";const val ACTION_FINISH="com.onedayonemasterpiece.streetstory.FINISH";const val ACTION_STATE_CHANGED="com.onedayonemasterpiece.streetstory.STATE_CHANGED";const val EXTRA_MESSAGE="message";const val EXTRA_STORY_ID="story_id";const val EXTRA_KIND="kind"
        private const val CHANNEL_ID="street-story-recording";private const val NOTIFICATION_ID=7101;private const val MIN_SESSION_MS=5_000L;private const val PRE_ROLL_FRAMES=20;private const val LIVE_ATTACK_FRAMES=2;private const val HANGOVER_FRAMES=40;private const val RUNTIME_UPDATE_INTERVAL_MS=500L;private const val STORE_UPDATE_INTERVAL_MS=2_000L;private const val LONG_SILENCE_CLOSE_MS=15_000L;private const val MIN_DURABLE_SEGMENT_MS=10_000L
        fun start(context:Context,storyId:String,kind:String){context.startForegroundService(Intent(context,RecordingService::class.java).setAction(ACTION_START).putExtra(EXTRA_STORY_ID,storyId).putExtra(EXTRA_KIND,kind))}
        fun command(context:Context,action:String){val i=Intent(context,RecordingService::class.java).setAction(action);if(action==ACTION_RESUME)context.startForegroundService(i)else context.startService(i)}
    }
}
