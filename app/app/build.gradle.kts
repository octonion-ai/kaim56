plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

android {
    namespace = "de.kat56.agent"
    compileSdk = 34

    defaultConfig {
        applicationId = "de.kat56.agent"
        minSdk = 26
        targetSdk = 34
        versionCode = 87
        versionName = "5.37"
        ndk {
            // Xiaomi 15 = arm64-v8a. Nur diese ABI -> deutlich kleinere APK.
            abiFilters += "arm64-v8a"
        }
    }

    signingConfigs {
        // Fester Schluessel -> jede APK hat dieselbe Signatur -> Update-in-place,
        // App-Speicher (Modell) bleibt erhalten. Bewusst simples Dev-Passwort.
        create("stable") {
            storeFile = file("../keystore/katagent.jks")
            storePassword = "katagent"
            keyAlias = "katagent"
            keyPassword = "katagent"
        }
    }
    buildTypes {
        getByName("debug") {
            // R8 an: strippt ungenutzte MediaPipe-Vision-Klassen -> kleinere APK.
            isMinifyEnabled = true
            isShrinkResources = true
            signingConfig = signingConfigs.getByName("stable")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }
    buildFeatures {
        compose = true
    }
    // Echo canceller for barge-in: vendored speexdsp + JNI, see src/main/cpp.
    ndkVersion = "26.1.10909125"
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    packaging {
        resources.excludes += "/META-INF/{AL2.0,LGPL2.1}"
        // Native Libs KOMPRIMIERT in die APK (statt unkomprimiert) -> APK-Datei
        // deutlich kleiner (unter dem 30-MB-Sendelimit). Werden beim Install
        // entpackt (etwas mehr Speicher/langsamerer Erststart, sonst unkritisch).
        jniLibs {
            useLegacyPackaging = true
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.2")
    implementation(platform("androidx.compose:compose-bom:2024.06.00"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    // Ein-/Ausblenden der Vollbilder und des Anhang-Blatts (AnimatedVisibility).
    implementation("androidx.compose.animation:animation")
    implementation("androidx.compose.material3:material3")
    // Vollständige Google-Material-Icons (R8 entfernt ungenutzte -> kaum Größenzuwachs).
    implementation("androidx.compose.material:material-icons-extended")
    implementation("net.java.dev.jna:jna:5.14.0@aar")
    implementation("com.google.android.material:material:1.12.0")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    // On-Device-LLM via LiteRT-LM (laedt .litertlm-Modelle inkl. Gemma 4; multimodal).
    implementation("com.google.ai.edge.litertlm:litertlm-android:0.14.0")
    // Nur fuer JVM-Tests der Brillen-Protokollschicht (Halo.kt) — laeuft ohne
    // Geraet und ohne Emulator: gradle testDebugUnitTest
    testImplementation("junit:junit:4.13.2")
}
