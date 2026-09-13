// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Bluetooth-Unterbau fuer die Brille: sucht, verbindet, handelt die MTU aus,
// abonniert die Benachrichtigungen und schreibt Pakete im Takt der Quittungen.
// Die Nachrichten selbst baut Halo.kt — diese Datei kennt nur Bytes.
//
// ACHTUNG, ungetestet: alles hier laesst sich erst mit echter Brille pruefen.
// Rahmung, Zusammensetzen und die Geraeteseite sind separat getestet
// (HaloTest.kt, tools/halo/test_frame_app.lua); was hier drinsteckt, ist der
// Rest, der ohne Hardware nicht zu beweisen ist.
package de.kat56.agent

import android.Manifest
import android.annotation.SuppressLint
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.ParcelUuid
import androidx.core.content.ContextCompat
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Verbindung zu einer Brilliant-Brille.
 *
 * Aufrufe von [write] und [writeString] BLOCKIEREN, bis die Brille quittiert
 * hat — sie gehoeren auf einen Hintergrund-Thread, nie auf den Haupt-Thread.
 * Das ist Absicht: die Brille gibt das Tempo vor (sie bestaetigt jedes Paket),
 * und ein blockierender Aufruf macht diesen Takt sichtbar, statt ihn hinter
 * einer Warteschlange zu verstecken.
 */
@SuppressLint("MissingPermission")   // Rechte werden in connect() geprueft
class HaloBle(private val ctx: Context) : HaloLink {

    interface Listener {
        /** verbunden und bereit (MTU ausgehandelt, Benachrichtigungen an). */
        fun onReady(isHalo: Boolean) {}
        fun onDisconnected(reason: String) {}
        /** Text aus dem Lua-Interpreter (print, Fehlermeldungen). */
        fun onText(text: String) {}
        /** Ein Stueck Mitschnitt; [done] = die Aufnahme ist beendet. */
        fun onAudio(pcm: ByteArray, done: Boolean) {}
        /** Ein Stueck Foto; [done] = das Bild ist vollstaendig. */
        fun onPhoto(part: ByteArray, done: Boolean) {}
    }

    private var gatt: BluetoothGatt? = null
    private var tx: BluetoothGattCharacteristic? = null
    private var rx: BluetoothGattCharacteristic? = null
    private var listener: Listener? = null

    @Volatile private var negotiatedMtu = 23      // BLE-Vorgabe, bis mehr ausgehandelt ist
    @Volatile private var halo = false
    @Volatile private var ready = false

    override val mtu: Int get() = negotiatedMtu
    override val isHalo: Boolean get() = halo

    /** Warteschlangen der Laenge 1: je Schreibvorgang ein Schritt. */
    private val writeDone = ArrayBlockingQueue<Boolean>(1)
    private val acks = ArrayBlockingQueue<Boolean>(1)
    private val connected = ArrayBlockingQueue<Boolean>(1)
    /** Fertige Fotos — [awaitPhoto] holt sie hier ab. */
    private val photos = ArrayBlockingQueue<ByteArray>(1)
    private val writeLock = Any()

    private val audio = Halo.AudioCollector()
    private val photo = Halo.PhotoCollector()

    // ---- Suchen und verbinden --------------------------------------------

    /** Fehlende Rechte als Liste — leer heisst: es kann losgehen. */
    fun missingPermissions(): List<String> {
        val need = if (Build.VERSION.SDK_INT >= 31)
            listOf(Manifest.permission.BLUETOOTH_SCAN, Manifest.permission.BLUETOOTH_CONNECT)
        else
            listOf(Manifest.permission.ACCESS_FINE_LOCATION)
        return need.filter {
            ContextCompat.checkSelfPermission(ctx, it) != PackageManager.PERMISSION_GRANTED
        }
    }

    /**
     * Sucht die naechste Brille und verbindet sich. Blockiert bis fertig.
     * Rueckgabe: null = verbunden, sonst der Grund des Scheiterns.
     */
    fun connect(timeoutMs: Long = 30000, listener: Listener): String? {
        this.listener = listener
        missingPermissions().let { if (it.isNotEmpty()) return "Rechte fehlen: ${it.joinToString()}" }
        val mgr = ctx.getSystemService(Context.BLUETOOTH_SERVICE) as? BluetoothManager
            ?: return "kein Bluetooth auf diesem Geraet"
        val adapter: BluetoothAdapter = mgr.adapter ?: return "kein Bluetooth-Adapter"
        if (!adapter.isEnabled) return "Bluetooth ist aus"

        val found = ArrayBlockingQueue<BluetoothDevice>(1)
        val scanner = adapter.bluetoothLeScanner ?: return "Scanner nicht verfuegbar"
        val cb = object : ScanCallback() {
            override fun onScanResult(type: Int, result: ScanResult) {
                result.device?.let { found.offer(it) }
            }
        }
        // Nur Geraete, die den Brillen-Dienst anbieten — sonst faengt man das
        // halbe Wohnzimmer ein.
        val filter = ScanFilter.Builder()
            .setServiceUuid(ParcelUuid(UUID.fromString(Halo.SERVICE))).build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        scanner.startScan(listOf(filter), settings, cb)
        val device = try {
            found.poll(timeoutMs, TimeUnit.MILLISECONDS)
        } finally {
            runCatching { scanner.stopScan(cb) }
        } ?: return "keine Brille gefunden"

        connected.clear()
        gatt = device.connectGatt(ctx, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        val ok = connected.poll(timeoutMs, TimeUnit.MILLISECONDS)
        return if (ok == true) null else "Verbindung nicht zustande gekommen"
    }

    fun disconnect() {
        ready = false
        runCatching { gatt?.disconnect() }
        runCatching { gatt?.close() }
        gatt = null
    }

    // ---- Senden ----------------------------------------------------------

    /**
     * Ein Paket schreiben und warten, bis die Brille es bestaetigt. Der Takt
     * kommt von ihr: ohne das Warten laeuft ihr Empfangspuffer ueber.
     */
    override fun write(packet: ByteArray) {
        val c = tx ?: throw IllegalStateException("nicht verbunden")
        require(ready) { "Verbindung noch nicht bereit" }
        require(packet.size <= Halo.maxPacket(Halo.maxDataLength(negotiatedMtu, halo))) {
            "Paket groesser als die ausgehandelte MTU zulaesst"
        }
        synchronized(writeLock) {
            writeDone.clear(); acks.clear()
            send(c, packet, withResponse = true)
            check(writeDone.poll(5, TimeUnit.SECONDS) == true) { "Brille bestaetigt den Schreibvorgang nicht" }
            val ack = acks.poll(5, TimeUnit.SECONDS)
                ?: throw IllegalStateException("keine Quittung der Brille")
            check(ack) { "Brille meldet einen Fehler beim Empfang" }
        }
    }

    /** Zeile an den Lua-Interpreter — ohne 0x01 und ohne Quittung. */
    override fun writeString(text: String) {
        val c = tx ?: throw IllegalStateException("nicht verbunden")
        val bytes = text.toByteArray(Charsets.UTF_8)
        require(bytes.size <= Halo.maxStringLength(negotiatedMtu, halo)) { "Zeile zu lang" }
        synchronized(writeLock) {
            writeDone.clear()
            send(c, bytes, withResponse = true)
            check(writeDone.poll(5, TimeUnit.SECONDS) == true) { "Zeile wurde nicht angenommen" }
        }
    }

    @Suppress("DEPRECATION")
    private fun send(c: BluetoothGattCharacteristic, data: ByteArray, withResponse: Boolean) {
        val g = gatt ?: throw IllegalStateException("nicht verbunden")
        val type = if (withResponse) BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
                   else BluetoothGattCharacteristic.WRITE_TYPE_NO_RESPONSE
        if (Build.VERSION.SDK_INT >= 33) {
            g.writeCharacteristic(c, data, type)
        } else {
            c.writeType = type
            c.value = data
            g.writeCharacteristic(c)
        }
    }

    // ---- Empfangen -------------------------------------------------------

    private fun onNotify(raw: ByteArray) {
        if (!Halo.isDataFrame(raw)) {
            // Kein Datenrahmen -> Ausgabe des Lua-Interpreters.
            listener?.onText(String(raw, Charsets.UTF_8))
            return
        }
        val frame = Halo.frameOf(raw)
        when {
            Halo.isAck(frame) -> acks.offer(!Halo.isAckError(frame))
            frame.isNotEmpty() && (frame[0] == Halo.AUDIO_CHUNK || frame[0] == Halo.AUDIO_FINAL) -> {
                val pcm = audio.feed(frame)
                listener?.onAudio(pcm, audio.complete)
            }
            frame.isNotEmpty() && (frame[0] == Halo.PHOTO_CHUNK || frame[0] == Halo.PHOTO_FINAL) -> {
                val done = photo.feed(frame)
                if (done) {
                    val jpeg = photo.jpeg()
                    photo.reset()
                    photos.offer(jpeg)          // wartender Aufrufer bekommt es
                    listener?.onPhoto(jpeg, true)
                } else {
                    listener?.onPhoto(frame.copyOfRange(1, frame.size), false)
                }
            }
        }
    }

    /**
     * Wartet auf das naechste vollstaendige Foto. Blockiert; null = es kam
     * keines rechtzeitig (Kamera aus, Verbindung weg, Auslesen haengt).
     */
    fun awaitPhoto(timeoutMs: Long = 20000): ByteArray? {
        photos.clear()                       // alte Aufnahme nicht zurueckgeben
        return photos.poll(timeoutMs, TimeUnit.MILLISECONDS)
    }

    /** Der Mitschnitt seit dem letzten Start, als WAV fuer /api/stt. */
    fun recordingAsWav(sampleRate: Int = 8000, bitsPerSample: Int = 16): ByteArray =
        Halo.wav(audio.pcm(), sampleRate, bitsPerSample)

    // ---- GATT ------------------------------------------------------------

    private val gattCallback = object : BluetoothGattCallback() {

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                g.discoverServices()
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                ready = false
                connected.offer(false)
                listener?.onDisconnected("Verbindung getrennt (Status $status)")
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val svc = g.getService(UUID.fromString(Halo.SERVICE))
            if (svc == null) { connected.offer(false); return }
            tx = svc.getCharacteristic(UUID.fromString(Halo.CHAR_TX))
            rx = svc.getCharacteristic(UUID.fromString(Halo.CHAR_RX))
            // Nur Halo hat den Audiokanal — daran erkennt man das Modell.
            halo = svc.getCharacteristic(UUID.fromString(Halo.CHAR_AUDIO_TX)) != null
            if (tx == null || rx == null) { connected.offer(false); return }
            g.requestMtu(Halo.MTU_REQUEST)
        }

        override fun onMtuChanged(g: BluetoothGatt, mtuValue: Int, status: Int) {
            negotiatedMtu = mtuValue
            // Erst jetzt die Benachrichtigungen einschalten: vorher koennte
            // schon etwas hereinkommen, das nicht mehr in ein Paket passt.
            val c = rx ?: return
            g.setCharacteristicNotification(c, true)
            val cccd = c.getDescriptor(
                UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"))
            if (cccd == null) { connected.offer(false); return }
            @Suppress("DEPRECATION")
            if (Build.VERSION.SDK_INT >= 33) {
                g.writeDescriptor(cccd, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE)
            } else {
                cccd.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
                g.writeDescriptor(cccd)
            }
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            ready = status == BluetoothGatt.GATT_SUCCESS
            connected.offer(ready)
            if (ready) listener?.onReady(halo)
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            writeDone.offer(status == BluetoothGatt.GATT_SUCCESS)
        }

        // Android 13+ liefert den Wert mit, davor steckt er in der Characteristic.
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic,
                                             value: ByteArray) = onNotify(value)

        @Suppress("DEPRECATION")
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic) {
            onNotify(c.value ?: return)
        }
    }
}
