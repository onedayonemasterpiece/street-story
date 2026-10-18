package com.onedayonemasterpiece.streetstory

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaMuxer
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteOrder
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.security.MessageDigest
import kotlin.math.roundToLong

class M4aChunkWriter(directory: File, val sessionId: String, val chunkIndex: Int, val audioStartMs: Long) {
    private val partFile: File
    private val targetFile: File
    private val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_AAC, AudioProfile.SAMPLE_RATE_HZ, AudioProfile.CHANNELS).apply {
        setInteger(MediaFormat.KEY_AAC_PROFILE, MediaCodecInfo.CodecProfileLevel.AACObjectLC)
        setInteger(MediaFormat.KEY_BIT_RATE, AudioProfile.BITRATE_BPS)
        setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, EfficientVad.FRAME_SAMPLES * 2)
    }
    private val encoder: MediaCodec
    private val selectedCodecName: String
    private val muxer: MediaMuxer
    private val bufferInfo = MediaCodec.BufferInfo()
    private var trackIndex = -1
    private var muxerStarted = false
    private var closed = false
    private var samplesQueued = 0L
    private var firstWallStartMs: Long? = null
    private var lastWallEndMs: Long? = null

    init {
        directory.mkdirs()
        val stem = "${sessionId}__${chunkIndex}__${audioStartMs}"
        partFile = File(directory, "$stem.m4a.part")
        targetFile = File(directory, "$stem.m4a")
        partFile.delete(); targetFile.delete()
        encoder = createStartedEncoder(format)
        selectedCodecName = encoder.name
        muxer = try { MediaMuxer(partFile.absolutePath, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4) }
        catch (exc: Exception) { runCatching { encoder.stop() }; encoder.release(); throw exc }
    }

    val durationMs: Long get() = samplesToMs(samplesQueued)
    val encodedBytes: Long get() = if (partFile.exists()) partFile.length() else 0L
    val codecName: String get() = selectedCodecName

    fun writeFrame(frame: ShortArray, wallStartMs: Long, wallEndMs: Long) {
        check(!closed); require(frame.isNotEmpty()); require(wallEndMs >= wallStartMs)
        var attempts = 0
        while (true) {
            val inputIndex = encoder.dequeueInputBuffer(INPUT_TIMEOUT_US)
            if (inputIndex >= 0) {
                val input = requireNotNull(encoder.getInputBuffer(inputIndex)).apply { clear(); order(ByteOrder.LITTLE_ENDIAN) }
                require(input.remaining() >= frame.size * 2)
                for (sample in frame) input.putShort(sample)
                val presentationUs = samplesQueued * 1_000_000L / AudioProfile.SAMPLE_RATE_HZ
                encoder.queueInputBuffer(inputIndex, 0, frame.size * 2, presentationUs, 0)
                samplesQueued += frame.size
                if (firstWallStartMs == null) firstWallStartMs = wallStartMs
                lastWallEndMs = wallEndMs
                drain(false)
                return
            }
            drain(false); attempts++; check(attempts < MAX_INPUT_ATTEMPTS)
        }
    }

    fun close(): ClosedChunk? {
        if (closed) return null
        closed = true
        if (samplesQueued == 0L) { releaseWithoutMuxerStop(); partFile.delete(); return null }
        try { queueEndOfStream(); drain(true) } finally {
            runCatching { encoder.stop() }; encoder.release()
            if (muxerStarted) runCatching { muxer.stop() }
            muxer.release()
        }
        check(partFile.isFile && partFile.length() > 0L)
        FileOutputStream(partFile, true).use { it.fd.sync() }
        atomicMove(partFile, targetFile)
        return ClosedChunk(sessionId, chunkIndex, audioStartMs, audioStartMs + durationMs,
            requireNotNull(firstWallStartMs), requireNotNull(lastWallEndMs), targetFile, sha256(targetFile), codecName)
    }

    private fun queueEndOfStream() {
        var attempts = 0
        while (true) {
            val inputIndex = encoder.dequeueInputBuffer(INPUT_TIMEOUT_US)
            if (inputIndex >= 0) {
                val presentationUs = samplesQueued * 1_000_000L / AudioProfile.SAMPLE_RATE_HZ
                encoder.queueInputBuffer(inputIndex, 0, 0, presentationUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                return
            }
            drain(false); attempts++; check(attempts < MAX_INPUT_ATTEMPTS)
        }
    }

    private fun drain(endOfStream: Boolean) {
        var idleRounds = 0
        while (true) {
            val outputIndex = encoder.dequeueOutputBuffer(bufferInfo, if (endOfStream) OUTPUT_TIMEOUT_US else 0L)
            when {
                outputIndex == MediaCodec.INFO_TRY_AGAIN_LATER -> { if (!endOfStream) return; idleRounds++; check(idleRounds < MAX_EOS_IDLE_ROUNDS) }
                outputIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> { check(!muxerStarted); trackIndex = muxer.addTrack(encoder.outputFormat); muxer.start(); muxerStarted = true }
                outputIndex >= 0 -> {
                    idleRounds = 0
                    val output = requireNotNull(encoder.getOutputBuffer(outputIndex))
                    if (bufferInfo.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG != 0) bufferInfo.size = 0
                    if (bufferInfo.size > 0) {
                        check(muxerStarted); output.position(bufferInfo.offset); output.limit(bufferInfo.offset + bufferInfo.size)
                        muxer.writeSampleData(trackIndex, output, bufferInfo)
                    }
                    val eos = bufferInfo.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM != 0
                    encoder.releaseOutputBuffer(outputIndex, false)
                    if (eos) return
                }
            }
        }
    }

    private fun releaseWithoutMuxerStop() { runCatching { encoder.stop() }; encoder.release(); muxer.release() }

    data class ClosedChunk(val sessionId: String, val chunkIndex: Int, val audioStartMs: Long, val audioEndMs: Long,
        val wallStartMs: Long, val wallEndMs: Long, val file: File, val sha256: String, val codecName: String)

    companion object {
        const val TARGET_SEGMENT_MS = 180_000L
        private const val INPUT_TIMEOUT_US = 10_000L
        private const val OUTPUT_TIMEOUT_US = 10_000L
        private const val MAX_INPUT_ATTEMPTS = 100
        private const val MAX_EOS_IDLE_ROUNDS = 300

        private fun createStartedEncoder(format: MediaFormat): MediaCodec {
            val candidates = MediaCodecList(MediaCodecList.REGULAR_CODECS).codecInfos
                .filter { it.isEncoder && it.supportedTypes.any { t -> t.equals(MediaFormat.MIMETYPE_AUDIO_AAC, true) } }
                .sortedWith(compareByDescending<MediaCodecInfo> { it.isHardwareAccelerated }.thenBy { it.isSoftwareOnly }.thenBy { it.name })
            for (candidate in candidates) {
                if (!runCatching { candidate.getCapabilitiesForType(MediaFormat.MIMETYPE_AUDIO_AAC).isFormatSupported(format) }.getOrDefault(false)) continue
                val codec = runCatching { MediaCodec.createByCodecName(candidate.name) }.getOrNull() ?: continue
                try { codec.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE); codec.start(); return codec }
                catch (_: Exception) { runCatching { codec.stop() }; codec.release() }
            }
            return MediaCodec.createEncoderByType(MediaFormat.MIMETYPE_AUDIO_AAC).also { it.configure(format, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE); it.start() }
        }

        private fun samplesToMs(samples: Long): Long = (samples.toDouble() * 1000.0 / AudioProfile.SAMPLE_RATE_HZ).roundToLong()
        private fun atomicMove(source: File, target: File) {
            runCatching { Files.move(source.toPath(), target.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING) }
                .getOrElse { Files.move(source.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING) }
        }
        private fun sha256(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input -> val buffer = ByteArray(DEFAULT_BUFFER_SIZE); while (true) { val count = input.read(buffer); if (count < 0) break; digest.update(buffer, 0, count) } }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
