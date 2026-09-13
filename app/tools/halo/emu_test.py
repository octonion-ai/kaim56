"""Geraeteseite gegen den Halo-Emulator des Herstellers.

Anders als tools/halo/test_frame_app.lua (eigene Attrappe) laeuft hier unser
unveraendertes katagent.lua auf der Emulation von Brilliant Labs: echte
Lua-5.4-Laufzeit, die vollstaendige frame.*-API, ein virtuelles 256x256-Display,
dessen Bildpuffer man auslesen kann. Eingespeist werden genau die Pakete, die
Halo.kt erzeugt — abzueglich des fuehrenden 0x01, das der Bluetooth-Stapel
abschneidet, bevor der Lua-Handler sie sieht.

Aufruf (Python liegt mit den Abhaengigkeiten nur im Container):
  tools/halo/run-emu-tests.sh
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

from halo_emulator import HaloEmulator

ASSETS = Path(__file__).resolve().parents[2] / "app/src/main/assets/halo"

# Codes wie in HaloSession.Code
TEXT, CLEAR, AUDIO_START, AUDIO_STOP, PHOTO = 0x12, 0x10, 0x30, 0x31, 0x0D

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok     {name}")
    else:
        failures.append(name)
        print(f"  FEHLER {name}" + (f"  -> {detail}" if detail else ""))


def packets(code, payload, max_data=241):
    """Wie Halo.packets(), aber ohne das 0x01 — so kommt es beim Geraet an."""
    chunk = max_data - 1
    out, sent, first = [], 0, True
    while True:
        rest = len(payload) - sent
        take = min(rest, chunk - 2) if first else min(rest, chunk)
        head = bytes([code, len(payload) >> 8, len(payload) & 0xFF]) if first else bytes([code])
        out.append(head + payload[sent:sent + take])
        sent += take
        first = False
        if sent >= len(payload):
            return out


def text_payload(s, x=1, y=1, color=1, spacing=4):
    return bytes([x >> 8, x & 0xFF, y >> 8, y & 0xFF, color, spacing]) + s.encode()


def deliver(emu, code, payload, max_data=241):
    for p in packets(code, payload, max_data):
        emu.inject_bluetooth_data(p)
    time.sleep(0.35)


def lit_pixels(emu):
    """Anzahl und groesste x-Position der gesetzten Pixel im Bildpuffer."""
    img = emu.get_framebuffer()
    w, h = img.size
    px = img.load()
    count, max_x = 0, -1
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if r + g + b > 30:
                count += 1
                max_x = max(max_x, x)
    return count, max_x, w


def main():
    sandbox = Path(tempfile.mkdtemp(prefix="halo-emu-"))
    for f in ASSETS.glob("*.lua"):
        shutil.copy(f, sandbox)

    prints = []
    emu = HaloEmulator(sandbox_dir=sandbox, print_handler=prints.append)
    emu.start("katagent.lua")
    time.sleep(0.8)                      # Module laden, Schleife anlaufen lassen

    err = emu.get_error()
    check("katagent.lua laeuft auf der Emulation", err is None, repr(err))
    if err is not None:
        emu.stop()
        return 1
    check("die App meldet sich beim Start",
          any("KatAgent" in p for p in prints), "; ".join(prints[:3]))

    # ---- Text ------------------------------------------------------------
    emu.clear_bluetooth_sent()
    deliver(emu, TEXT, text_payload("Hallo von KatAgent"))
    count, max_x, width = lit_pixels(emu)
    check("Text erscheint wirklich auf dem Display", count > 0, f"{count} Pixel gesetzt")

    # ---- Quittungen: die zentrale Annahme des Bluetooth-Teils -------------
    sent = emu.get_bluetooth_sent()
    check("die Brille quittiert jedes Paket mit 01 00 00",
          any(s == b"\x01\x00\x00" for s in sent),
          f"gesendet: {[s[:4] for s in sent][:5]}")

    # ---- Mehrere Pakete --------------------------------------------------
    deliver(emu, CLEAR, b"")
    time.sleep(0.2)
    long_text = "Zeile eins ist lang genug fuer mehrere Pakete. " * 6
    deliver(emu, TEXT, text_payload(long_text), max_data=48)
    count_long, _, _ = lit_pixels(emu)
    check("ueber viele Pakete zusammengesetzter Text wird angezeigt", count_long > 0,
          f"{count_long} Pixel")

    # ---- Loeschen --------------------------------------------------------
    deliver(emu, CLEAR, b"")
    count_after, _, _ = lit_pixels(emu)
    check("Loeschen raeumt das Display", count_after == 0, f"{count_after} Pixel uebrig")

    # ---- Mikrofon --------------------------------------------------------
    emu.clear_bluetooth_sent()
    deliver(emu, AUDIO_START, bytes([0x1F, 0x40, 16]))       # 8000 Hz, 16 Bit
    emu.inject_microphone_data(b"\x01\x02" * 64)
    time.sleep(0.5)
    audio = [s for s in emu.get_bluetooth_sent() if s[:1] == b"\x05"]
    check("Mitschnitt geht mit 0x05 an das Telefon", len(audio) > 0,
          f"Stuecke: {len(audio)}")

    emu.clear_bluetooth_sent()
    deliver(emu, AUDIO_STOP, b"")
    time.sleep(0.5)
    final = [s for s in emu.get_bluetooth_sent() if s[:1] == b"\x06"]
    check("Stopp schickt das Schlussstueck 0x06", len(final) > 0)

    # ---- Displaybreite: wie viele Zeichen passen wirklich in eine Zeile? --
    deliver(emu, CLEAR, b"")
    fits = 0
    for n in range(1, 60):
        deliver(emu, TEXT, text_payload("M" * n))
        _, max_x, width = lit_pixels(emu)
        if max_x < 0:
            continue
        if max_x >= width - 1:            # laeuft rechts an oder ueber den Rand
            break
        fits = n
        deliver(emu, CLEAR, b"")
    print(f"\n  gemessen: {fits} Zeichen 'M' passen in eine Zeile bei {width} px Breite")
    check("die angenommene Zeilenbreite (32) ist nicht zu gross", fits >= 32,
          f"es passen nur {fits}")

    # ---- Displayhoehe: wie viele Zeilen passen bei 20 px Abstand? --------
    deliver(emu, CLEAR, b"")
    rows = 0
    for n in range(1, 16):
        deliver(emu, CLEAR, b"")
        deliver(emu, TEXT, text_payload("\n".join(["Zeile"] * n)))
        img = emu.get_framebuffer()
        px = img.load()
        lit_rows = {y for y in range(img.size[1]) for x in range(img.size[0])
                    if sum(px[x, y][:3]) > 30}
        # Zeilen zaehlen: zusammenhaengende Bloecke gesetzter Bildzeilen
        blocks, prev = 0, -5
        for y in sorted(lit_rows):
            if y - prev > 1:
                blocks += 1
            prev = y
        if blocks < n:
            break
        rows = n
    print(f"  gemessen: {rows} Zeilen passen bei 20 px Zeilenabstand auf das Display")
    check("die angenommene Zeilenzahl (8) passt auf das Display", rows >= 8,
          f"es passen nur {rows}")

    emu.stop()
    print("\n" + ("ALLE TESTS GRUEN" if not failures else f"{len(failures)} FEHLER: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
