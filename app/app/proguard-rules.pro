# LiteRT-LM (On-Device-LLM) — via JNI/Reflection genutzt, komplett behalten.
-keep class com.google.ai.edge.litertlm.** { *; }
-keep class com.google.ai.edge.litert.** { *; }
-dontwarn com.google.ai.edge.**

# Protobuf-lite (evtl. transitiv)
-keepclassmembers class * extends com.google.protobuf.GeneratedMessageLite {
    <fields>;
}
-dontwarn com.google.protobuf.**

# Build-Zeit-Only-Klassen (auto-value/javapoet) ignorieren
-dontwarn javax.annotation.processing.**
-dontwarn javax.lang.model.**
-dontwarn javax.annotation.**
-dontwarn com.google.auto.value.**
-dontwarn autovalue.shaded.**

-keepattributes *Annotation*, Signature, InnerClasses, EnclosingMethod

# JNA (com.sun.jna) — libjnidispatch.so calls back into these via JNI at runtime
# (e.g. Native.fromNative). R8 must not strip or rename them, or JNA's static
# init throws UnsatisfiedLinkError. UniFFI's generated bindings sit on top of JNA.
-dontwarn java.awt.**
-keep class com.sun.jna.** { *; }
-keepclassmembers class com.sun.jna.** { *; }
-keep class * extends com.sun.jna.Structure { *; }
-keepclassmembers class * extends com.sun.jna.Structure { *; }
-keep interface * extends com.sun.jna.Callback { *; }

# UniFFI generated bindings for the native iroh module — mapped by JNA via
# reflection; keep everything so R8 doesn't drop fields/methods it needs.
-keep class uniffi.kaim_iroh.** { *; }
-keepclassmembers class uniffi.kaim_iroh.** { *; }

# JNI (libkatecho, barge-in echo canceller): the native method names are the symbol names.
-keepclasseswithmembernames class de.kat56.agent.EchoMic { native <methods>; }
