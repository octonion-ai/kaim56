# Native part of KatAgent: the echo canceller for barge-in (vendored speexdsp,
# BSD) plus a thin JNI bridge (echo_jni.c). Linked statically into one
# libkatecho.so; the Kotlin side is EchoMic.kt.
cmake_minimum_required(VERSION 3.22)
project(katecho C)
set(SPX speexdsp/libspeexdsp)
add_library(katecho SHARED
    echo_jni.c
    ${SPX}/mdf.c ${SPX}/preprocess.c ${SPX}/filterbank.c ${SPX}/fftwrap.c
    ${SPX}/kiss_fft.c ${SPX}/kiss_fftr.c ${SPX}/resample.c)
target_include_directories(katecho PRIVATE speexdsp speexdsp/include ${SPX})
target_compile_definitions(katecho PRIVATE HAVE_CONFIG_H=1)
target_compile_options(katecho PRIVATE -O2 -fvisibility=hidden -Wno-unused-function -Wno-unused-variable)
find_library(log-lib log)
target_link_libraries(katecho ${log-lib})
