plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val embeddedSourceSha = System.getenv("STREET_STORY_SOURCE_SHA")
    ?: providers.exec { commandLine("git", "rev-parse", "HEAD") }.standardOutput.asText.get().trim()
val prepareSharedLive by tasks.registering(Exec::class) {
    workingDir(rootProject.projectDir)
    commandLine("python3", "scripts/prepare_live_framework.py")
    inputs.file(rootProject.file("live-framework.lock.json"))
    inputs.file(rootProject.file("vendor/live-interaction-0.3.6-rc.1.tar.gz"))
    inputs.file(rootProject.file("scripts/prepare_live_framework.py"))
    outputs.dir(rootProject.file(".live-framework"))
}

android {
    namespace = "com.onedayonemasterpiece.streetstory"
    compileSdk = 36
    defaultConfig {
        applicationId = "com.onedayonemasterpiece.streetstory"
        minSdk = 29
        targetSdk = 35
        versionCode = (System.getenv("GITHUB_RUN_NUMBER") ?: "1").toInt()
        versionName = "0.1.${System.getenv("GITHUB_RUN_NUMBER") ?: "1"}"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "DEFAULT_BACKEND_URL", "\"${System.getenv("STREET_STORY_BACKEND_URL") ?: ""}\"")
        buildConfigField("String", "SOURCE_SHA", "\"$embeddedSourceSha\"")
    }
    buildTypes { release { isMinifyEnabled = false } }
    compileOptions { sourceCompatibility = JavaVersion.VERSION_17; targetCompatibility = JavaVersion.VERSION_17 }
    buildFeatures { buildConfig = true }
    sourceSets.getByName("main").java.srcDir(rootProject.file(".live-framework/android/src/main/java"))
    lint { abortOnError = true; checkReleaseBuilds = true }
    packaging { resources.excludes += setOf("META-INF/AL2.0", "META-INF/LGPL2.1") }
}
tasks.named("preBuild").configure { dependsOn(prepareSharedLive) }

dependencies {
    implementation("androidx.core:core-ktx:1.17.0")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("androidx.exifinterface:exifinterface:1.4.1")
    implementation("com.cloudflare.realtimekit.android-vad:webrtc:2.0.10-cf.4")
    implementation("com.google.code.gson:gson:2.13.1")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test:core:1.6.1")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test.uiautomator:uiautomator:2.3.0")
}
kotlin { jvmToolchain(17) }
