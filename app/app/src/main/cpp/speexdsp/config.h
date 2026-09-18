/* speexdsp 1.2.1 build configuration for the kAIm56 Android app.
 * Floating point on arm64, KISS FFT (bundled, BSD), C99 variable-size arrays,
 * no symbol export (the library is linked statically into libkatecho.so). */
#ifndef SPEEXDSP_CONFIG_H
#define SPEEXDSP_CONFIG_H
#define FLOATING_POINT 1
#define USE_KISS_FFT 1
#define VAR_ARRAYS 1
#define HAVE_STDINT_H 1
#define EXPORT
#endif
