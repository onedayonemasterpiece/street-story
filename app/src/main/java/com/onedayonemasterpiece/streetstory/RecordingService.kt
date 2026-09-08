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
import android.media.AudioRecord
import android.media.MediaRecorder
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
    private val runtime by lazy { RecordingRuntime(this) }

    override fun onCreate(){super.onCreate();createNotificationChannel()}
    override fun onBind(intent:Intent?):IBinder?=null
    override fun onStartCommand(intent:Intent?,flags:Int,startId:Int):Int{
        when(intent?.action){ACTION_START->startNewSession(intent);ACTION_PAUSE->pauseSession();ACTION_RESUME->resumeSession();ACTION_FINISH->finishSession()}
        return START_NOT_STICKY
    }
    private fun startNewSession(intent:Intent){
        if(store.activeVoiceSession()!=null)return
        val storyId=intent.getStringExtra(EXTRA_STORY_ID)?:return
        val kind=intent.getStringExtra(EXTRA_KIND)?:RecordingKind.INITIAL
        val session=store.createVoiceSession(storyId,kind,"${Build.MANUFACTURER} ${Build.MODEL}".trim())
        sessionId=session.sessionId;enterForeground("Слушаю · тишина не записывается",false);beginCapture();broadcast()
    }
    private fun pauseSession(){val active=store.activeVoiceSession()?:return;sessionId=active.sessionId;if(active.captureState==CaptureState.RECORDING)stopCapture();store.beginManualPause(active.sessionId);val refreshed=store.voiceSession(active.sessionId)?:active;runtime.update(active.sessionId,refreshed.durationMs,elapsedFromStart(refreshed.startedAt),refreshed.autoSilenceSkippedMs,CaptureActivity.MANUAL_PAUSE);enterForeground("Пауза · микрофон остановлен",true);SyncScheduler.enqueue(this);broadcast()}
    private fun resumeSession(){val active=store.activeVoiceSession()?:return;sessionId=active.sessionId;store.endManualPause(active.sessionId);enterForeground("Слушаю · тишина не записывается",false);beginCapture();broadcast()}
    private fun finishSession(){
        val active=store.activeVoiceSession()?:return;sessionId=active.sessionId
        if(active.captureState==CaptureState.RECORDING)stopCapture() else store.endManualPause(active.sessionId)
        val refreshed=store.voiceSession(active.sessionId)?:return
        if(refreshed.durationMs<MIN_SESSION_MS||refreshed.chunkCount==0){store.discardVoiceSession(active.sessionId);runtime.clear(active.sessionId);stopForeground(STOP_FOREGROUND_REMOVE);stopSelf();broadcast("Слишком короткая запись удалена");return}
        val ended=OffsetDateTime.now();val wall=elapsedBetween(refreshed.startedAt,ended);val skipped=(wall-refreshed.manualPauseMs-refreshed.durationMs).coerceAtLeast(0)
        store.finishVoiceSession(active.sessionId,ended.toString(),wall,skipped);runtime.clear(active.sessionId);SyncScheduler.enqueue(this);stopForeground(STOP_FOREGROUND_REMOVE);stopSelf();broadcast()
    }
    @Synchronized private fun beginCapture(){
        if(captureThread?.isAlive==true)return
        val id=sessionId?:store.activeVoiceSession()?.sessionId?:return
        if(ContextCompat.checkSelfPermission(this,Manifest.permission.RECORD_AUDIO)!=PackageManager.PERMISSION_GRANTED){pauseForMicrophoneFailure(id,"Разрешение на микрофон отозвано");return}
        store.setCaptureState(id,CaptureState.RECORDING,CaptureActivity.AUTO_SILENCE);captureRequested=true;captureThread=Thread({captureLoop(id)},"street-story-capture").also{it.start()}
    }
    private fun captureLoop(id:String){
        val initial=store.voiceSession(id)?:return
        val sessionStart=runCatching{OffsetDateTime.parse(initial.startedAt).toInstant().toEpochMilli()}.getOrElse{System.currentTimeMillis()}
        val manualPauseMs=initial.manualPauseMs
        val minBuffer=AudioRecord.getMinBufferSize(AudioProfile.SAMPLE_RATE_HZ,AudioFormat.CHANNEL_IN_MONO,AudioFormat.ENCODING_PCM_16BIT)
        if(minBuffer<=0){pauseForMicrophoneFailure(id,"Устройство не предоставило аудиобуфер");return}
        val recorder=try{
            AudioRecord.Builder().setAudioSource(MediaRecorder.AudioSource.VOICE_RECOGNITION).setAudioFormat(AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(AudioProfile.SAMPLE_RATE_HZ).setChannelMask(AudioFormat.CHANNEL_IN_MONO).build()).setBufferSizeInBytes(maxOf(minBuffer*2,EfficientVad.FRAME_SAMPLES*8)).build()
        }catch(exc:SecurityException){
            pauseForMicrophoneFailure(id,"Разрешение на микрофон недоступно");return
        }catch(exc:Exception){
            pauseForMicrophoneFailure(id,"Не удалось открыть микрофон: ${exc.message}");return
        }
        audioRecord=recorder
        val suppressor=if(NoiseSuppressor.isAvailable())runCatching{NoiseSuppressor.create(recorder.audioSessionId)?.also{it.enabled=true}}.getOrNull() else null
        val detector=EfficientVad(true);val latch=SpeechLatch(3,HANGOVER_FRAMES);val preRoll=ArrayDeque<FramePacket>();var writer:M4aChunkWriter?=null;var persisted=initial.durationMs;var activity=CaptureActivity.AUTO_SILENCE;var lastActivity:String?=null;var lastRuntime=-1L;var lastStore=-1L;var silenceStart:Long?=null;val frame=ShortArray(EfficientVad.FRAME_SAMPLES)
        try{
            recorder.startRecording();check(recorder.recordingState==AudioRecord.RECORDSTATE_RECORDING)
            while(captureRequested){
                if(!readFrame(recorder,frame))continue
                val wallEnd=(System.currentTimeMillis()-sessionStart).coerceAtLeast(0);val wallStart=(wallEnd-EfficientVad.FRAME_MS).coerceAtLeast(0);val wasActive=latch.active;val active=latch.onFrame(detector.isSpeech(frame))
                if(active){silenceStart=null;writer=writer?:newWriter(id,persisted);if(!wasActive){pushPreRoll(preRoll,frame,wallStart,wallEnd);while(preRoll.isNotEmpty()){val buffered=preRoll.removeFirst();writer?.writeFrame(buffered.samples,buffered.wallStartMs,buffered.wallEndMs)}}else writer?.writeFrame(frame,wallStart,wallEnd);activity=if(detector.isFailOpen)CaptureActivity.FALLBACK_CONTINUOUS else CaptureActivity.VOICE;if((writer?.durationMs?:0)>=M4aChunkWriter.TARGET_SEGMENT_MS){persisted=persist(writer?.close(),persisted);writer=null}}
                else{pushPreRoll(preRoll,frame,wallStart,wallEnd);if(silenceStart==null)silenceStart=wallStart;activity=CaptureActivity.AUTO_SILENCE;val silenceMs=wallEnd-(silenceStart?:wallEnd);if(silenceMs>=LONG_SILENCE_CLOSE_MS&&(writer?.durationMs?:0)>=MIN_DURABLE_SEGMENT_MS){persisted=persist(writer?.close(),persisted);writer=null}}
                val recorded=persisted+(writer?.durationMs?:0);val skipped=(wallEnd-manualPauseMs-recorded).coerceAtLeast(0);val changed=activity!=lastActivity
                if(lastRuntime<0||wallEnd-lastRuntime>=RUNTIME_UPDATE_INTERVAL_MS||changed){runtime.update(id,recorded,wallEnd,skipped,activity);lastRuntime=wallEnd}
                if(lastStore<0||wallEnd-lastStore>=STORE_UPDATE_INTERVAL_MS||changed){store.updateCaptureProgress(id,recorded,wallEnd,manualPauseMs,skipped,activity);lastStore=wallEnd}
                if(changed){updateNotification(activity);lastActivity=activity;broadcast(if(activity==CaptureActivity.FALLBACK_CONTINUOUS)"Автопропуск недоступен · записываю всё" else null)}
            }
        }catch(exc:Exception){if(captureRequested){captureRequested=false;store.beginManualPause(id);store.setLocalVoiceError(id,"Ошибка записи: ${exc.message}");enterForeground("Запись остановлена с ошибкой",true)}}finally{
            runCatching{recorder.stop()};recorder.release();audioRecord=null;suppressor?.release();detector.close();persisted=persist(writer?.close(),persisted);val final=store.voiceSession(id);if(final!=null){val wall=(System.currentTimeMillis()-sessionStart).coerceAtLeast(0);val skipped=(wall-final.manualPauseMs-persisted).coerceAtLeast(0);val finalActivity=if(captureRequested)activity else CaptureActivity.IDLE;store.updateCaptureProgress(id,persisted,wall,final.manualPauseMs,skipped,finalActivity);runtime.update(id,persisted,wall,skipped,finalActivity)};broadcast()
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
    override fun onDestroy(){if(captureRequested){stopCapture();sessionId?.let{store.beginManualPause(it)}};super.onDestroy()}
    private data class FramePacket(val samples:ShortArray,val wallStartMs:Long,val wallEndMs:Long)
    companion object{
        const val ACTION_START="com.onedayonemasterpiece.streetstory.START";const val ACTION_PAUSE="com.onedayonemasterpiece.streetstory.PAUSE";const val ACTION_RESUME="com.onedayonemasterpiece.streetstory.RESUME";const val ACTION_FINISH="com.onedayonemasterpiece.streetstory.FINISH";const val ACTION_STATE_CHANGED="com.onedayonemasterpiece.streetstory.STATE_CHANGED";const val EXTRA_MESSAGE="message";const val EXTRA_STORY_ID="story_id";const val EXTRA_KIND="kind"
        private const val CHANNEL_ID="street-story-recording";private const val NOTIFICATION_ID=7101;private const val MIN_SESSION_MS=5_000L;private const val PRE_ROLL_FRAMES=20;private const val HANGOVER_FRAMES=40;private const val RUNTIME_UPDATE_INTERVAL_MS=500L;private const val STORE_UPDATE_INTERVAL_MS=2_000L;private const val LONG_SILENCE_CLOSE_MS=15_000L;private const val MIN_DURABLE_SEGMENT_MS=10_000L
        fun start(context:Context,storyId:String,kind:String){context.startForegroundService(Intent(context,RecordingService::class.java).setAction(ACTION_START).putExtra(EXTRA_STORY_ID,storyId).putExtra(EXTRA_KIND,kind))}
        fun command(context:Context,action:String){val i=Intent(context,RecordingService::class.java).setAction(action);if(action==ACTION_RESUME)context.startForegroundService(i)else context.startService(i)}
    }
}
