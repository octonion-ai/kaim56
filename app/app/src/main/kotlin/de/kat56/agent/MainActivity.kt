// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import android.Manifest
import androidx.core.content.ContextCompat
import android.content.pm.PackageManager
import android.media.MediaRecorder
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import androidx.core.app.NotificationCompat
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.ui.draw.alpha
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.slideInHorizontally
import androidx.compose.animation.slideInVertically
import androidx.compose.animation.slideOutHorizontally
import androidx.compose.animation.slideOutVertically
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.itemsIndexed
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.ArrowForward
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.outlined.Description
import androidx.compose.material.icons.filled.Cloud
import androidx.compose.material.icons.filled.HourglassEmpty
import androidx.compose.material.icons.filled.Menu
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Shield
import androidx.compose.material.icons.filled.StopCircle
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.Terminal
import androidx.compose.material.icons.outlined.Checklist
import androidx.compose.material.icons.outlined.Flag
import androidx.compose.material.icons.outlined.CloudOff
import androidx.compose.material.icons.outlined.ChatBubbleOutline
import androidx.compose.material.icons.outlined.Shield
import androidx.compose.material.icons.outlined.VolumeUp
import androidx.compose.material.icons.outlined.DeleteOutline
import androidx.compose.material.icons.outlined.DeleteSweep
import androidx.compose.material.icons.outlined.Download
import androidx.compose.material.icons.outlined.PhotoCamera
import androidx.compose.material.icons.outlined.PhotoLibrary
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

// Speech-pause detection, everything in milliseconds. VAD_HANG is the one value
// you feel: too short and it cuts off mid-sentence, too long and you wait after
// every sentence. 1.8 s leaves room for a breath mid-sentence, without the end
// of the recording feeling like a hang.
private const val VAD_TICK = 100L
private const val VAD_HANG = 3500L   // longer pauses for thought allowed (natural speech)
private const val VAD_LEAD = 6000L      // never said anything -> abort
private const val VAD_MAX = 120_000L    // emergency brake against an endless recording

class MainActivity : ComponentActivity() {
    /** Counter instead of a flag: on the second assistant call the value is
     *  already set, a Boolean would not trigger a second time. */
    private val assistCalls = mutableStateOf(0)
    /** Target link of a tapped system notification ("missions" | "chat:x"). */
    private val notifNav = mutableStateOf("")
    private val notifNavCalls = mutableStateOf(0)

    private fun isAssist(i: Intent?) =
        i?.action == Intent.ACTION_ASSIST || i?.action == Intent.ACTION_VOICE_COMMAND

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Write uncaught exceptions to a file, so crashes can be inspected in
        // Settings > Diagnostics without a PC/adb.
        run {
            val prev = Thread.getDefaultUncaughtExceptionHandler()
            Thread.setDefaultUncaughtExceptionHandler { t, e ->
                try {
                    val sw = java.io.StringWriter()
                    e.printStackTrace(java.io.PrintWriter(sw))
                    val ver = try { packageManager.getPackageInfo(packageName, 0).versionName } catch (x: Exception) { "?" }
                    val txt = "kAIm56 " + ver + " @ " + java.util.Date() + "\nThread " + t.name + "\n\n" + sw
                    java.io.File(filesDir, "crash.log").writeText(txt)
                    // Also to external files + public Downloads (Android/data is
                    // blocked on Android 15; Downloads is retrievable by any file manager).
                    try { (getExternalFilesDir(null))?.let { java.io.File(it, "crash.log").writeText(txt) } } catch (_: Throwable) {}
                    try { IrohNet.writeToDownloads(applicationContext, "kaim-crash.txt", txt) } catch (_: Throwable) {}
                } catch (_: Throwable) {}
                prev?.uncaughtException(t, e)
            }
        }
        val prefs = Prefs(this)
        IrohNet.register(this)   // install the iroh:// transport to the manager
        val gemma = LocalGemma(this)
        val store = ChatStore(this)
        store.migrate(prefs)   // migrate the v1.0 model, if present
        if (isAssist(intent)) assistCalls.value++
        intent?.getStringExtra("notifLink")?.takeIf { it.isNotBlank() }?.let {
            notifNav.value = it; notifNavCalls.value++
        }
        setContent {
            // The prototype is drawn in only one theme (dark="true"),
            // so no more isSystemInDarkTheme().
            MaterialTheme(
                colorScheme = KatColors,
                typography = KatTypography,
                shapes = KatShapes,
            ) {
                KatAgentApp(prefs, gemma, store, assistCalls.value,
                    notifNav.value, notifNavCalls.value)
            }
        }
    }

    /** If the app is already running, the assistant call arrives here instead of
     *  in onCreate — with singleTask no second copy is started. */
    override fun onNewIntent(i: Intent) {
        super.onNewIntent(i)
        setIntent(i)
        if (isAssist(i)) assistCalls.value++
        i.getStringExtra("notifLink")?.takeIf { it.isNotBlank() }?.let {
            notifNav.value = it; notifNavCalls.value++
        }
    }
}

/** A model suggestion from the prototype (name, description, tag, size, URL). */
// Separate reasoning/thinking from the message text. The agent wraps it in
// visible Unicode brackets (see agent.py). streaming=true -> the block is
// still open (the model is currently thinking).
private data class Thought(val think: String, val answer: String, val streaming: Boolean)
private val MD_INLINE = Regex("\\*\\*(.+?)\\*\\*|`([^`]+)`|\\*(.+?)\\*|_(.+?)_")

// Light markdown -> AnnotatedString: bold, italic, code, #-headings
// (bold) and bullet lists. Deliberately simple.
private fun mdAnnotated(src: String): AnnotatedString = buildAnnotatedString {
    val lines = src.split("\n")
    lines.forEachIndexed { li, raw ->
        var line = raw
        var boldLine = false
        Regex("^(#{1,6})\\s+(.*)").find(line)?.let { line = it.groupValues[2]; boldLine = true }
        Regex("^(\\s*)[-*]\\s+(.*)").find(line)?.let { append(it.groupValues[1] + "\u2022  "); line = it.groupValues[2] }
        if (boldLine) pushStyle(SpanStyle(fontWeight = FontWeight.Bold))
        var i = 0
        for (m in MD_INLINE.findAll(line)) {
            if (m.range.first > i) append(line.substring(i, m.range.first))
            val g = m.groupValues
            when {
                g[1].isNotEmpty() -> { pushStyle(SpanStyle(fontWeight = FontWeight.Bold)); append(g[1]); pop() }
                g[2].isNotEmpty() -> { pushStyle(SpanStyle(fontFamily = PlexMono)); append(g[2]); pop() }
                g[3].isNotEmpty() -> { pushStyle(SpanStyle(fontStyle = FontStyle.Italic)); append(g[3]); pop() }
                g[4].isNotEmpty() -> { pushStyle(SpanStyle(fontStyle = FontStyle.Italic)); append(g[4]); pop() }
            }
            i = m.range.last + 1
        }
        if (i < line.length) append(line.substring(i))
        if (boldLine) pop()
        if (li < lines.lastIndex) append("\n")
    }
}

/** Tool status lines the agent streams ("🔧 web_search …", plus the
 *  heartbeat dots while it runs) — shown collapsed under "Tools", not in
 *  the answer text (where Markdown also turned the underscores into italics). */
private data class ToolSplit(val tools: List<String>, val rest: String)
private fun splitTools(s: String): ToolSplit {
    val tools = ArrayList<String>(); val rest = StringBuilder()
    for (line in s.split('\n')) {
        val t = line.trimStart()
        if (t.startsWith("\uD83D\uDD27")) {
            val name = t.substring(2).trim().trimEnd('\u2026', '\u00B7', ' ', '.').trim()
            if (name.isNotEmpty()) tools.add(name)
        } else rest.append(line).append('\n')
    }
    return ToolSplit(tools, rest.toString().trim('\n'))
}

private fun splitThink(s: String): Thought {
    val a = "\u27E6think\u27E7"; val b = "\u27E6/think\u27E7"
    val th = StringBuilder(); val ans = StringBuilder(); var open = false; var i = 0
    while (true) {
        val p = s.indexOf(a, i)
        if (p < 0) { ans.append(s.substring(i)); break }
        ans.append(s.substring(i, p))
        val q = s.indexOf(b, p + a.length)
        if (q < 0) { th.append(s.substring(p + a.length)); open = true; break }
        th.append(s.substring(p + a.length, q)); i = q + b.length
    }
    return Thought(th.toString().trim(), ans.toString(), open)
}


// Slash commands for the autocomplete above the input row.
// arg=true -> takes arguments (insert command + space); arg=false ->
// send directly. Order = display; filtered by prefix.
// Show a push notification from the agent as an Android system notification.
private fun showAgentNotification(ctx: Context, id: String, title: String, body: String,
                                  link: String = "") {
    val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    if (Build.VERSION.SDK_INT >= 26) {
        nm.createNotificationChannel(
            NotificationChannel("kaim56_agent", "Agent notifications",
                NotificationManager.IMPORTANCE_HIGH))
    }
    // Tapping the notification leads to the action: missions screen or the
    // chat of the triggering instance (the link comes from the manager).
    val tap = android.app.PendingIntent.getActivity(
        ctx, id.hashCode(),
        Intent(ctx, MainActivity::class.java)
            .putExtra("notifLink", link)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        android.app.PendingIntent.FLAG_UPDATE_CURRENT or android.app.PendingIntent.FLAG_IMMUTABLE)
    val n = NotificationCompat.Builder(ctx, "kaim56_agent")
        .setSmallIcon(android.R.drawable.ic_dialog_info)
        .setContentTitle(if (title.isBlank()) "kAIm56" else title)
        .setContentText(body)
        .setStyle(NotificationCompat.BigTextStyle().bigText(body))
        .setContentIntent(tap)
        .setAutoCancel(true)
        .setPriority(NotificationCompat.PRIORITY_HIGH)
        .build()
    nm.notify(id.hashCode(), n)
}

private data class SlashCmd(val cmd: String, val desc: String, val arg: Boolean = false)
private val SLASH_CMDS = listOf(
    SlashCmd("/task", "Background task (e.g. every 30m …)", arg = true),
    SlashCmd("/reasoning", "Toggle reasoning (low · medium · high · off)", arg = true),
    SlashCmd("/goal", "Set a goal; the answer is refined against a judge (off = disable)", arg = true),
    SlashCmd("/model", "Switch model (e.g. orcarouter:anthropic/claude-sonnet-4.6)", arg = true),
    SlashCmd("/steps", "Max. tool steps per turn: 30 or unlimited", arg = true),
    SlashCmd("/fresh", "One-off request in a throwaway context (history untouched)", arg = true),
    SlashCmd("/aside", "Open an aside — a follow-up beside the main thread"),
    SlashCmd("/back", "Close the aside and summarise it (/back drop = discard)"),
    SlashCmd("/reset", "Reset conversation context"),
    SlashCmd("/tools", "List the tools the agent can use right now"),
    SlashCmd("/agents", "Open agent management"),
    SlashCmd("/help", "Show commands"),
)


private data class Preset(
    val name: String, val desc: String, val tag: String, val size: String, val url: String,
)

private val PRESETS = listOf(
    Preset("Gemma-4 E4B", "Text · image · audio, best quality", "multimodal", "3.7 GB",
        "https://huggingface.co/litert-community/gemma-4-E4B-it-litert-lm/resolve/main/gemma-4-E4B-it.litertlm"),
    Preset("Gemma-4 E2B", "Text · image · audio, more economical", "multimodal", "2.6 GB",
        "https://huggingface.co/litert-community/gemma-4-E2B-it-litert-lm/resolve/main/gemma-4-E2B-it.litertlm"),
    Preset("Gemma-3n E2B", "Proven, int4-quantized", "text", "3.0 GB",
        "https://huggingface.co/litert-community/Gemma-3n-E2B-it-litert-lm/resolve/main/gemma-3n-E2B-it-int4.litertlm"),
    Preset("Qwen2.5 1.5B", "Fast, good for simple tasks", "text", "1.6 GB",
        "https://huggingface.co/litert-community/Qwen2.5-1.5B-Instruct/resolve/main/Qwen2.5-1.5B-Instruct_multi-prefill-seq_q8_ekv1280.litertlm"),
    Preset("Qwen2 0.5B", "Minimal, runs on weak hardware", "small", "0.6 GB",
        "https://huggingface.co/litert-community/Qwen2-0.5B-Instruct/resolve/main/Qwen2-0.5B-Instruct_multi-prefill-seq_q8_ekv1280.litertlm"),
)

private fun nowHm(): String = SimpleDateFormat("HH:mm", Locale.GERMANY).format(Date())

/** Short name of a chat's agent — in the prototype the line under the title. */
private fun agentOf(c: Conversation): String =
    if (c.mode == "server") c.instance.ifBlank { "server" } else "device"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun KatAgentApp(prefs: Prefs, gemma: LocalGemma, store: ChatStore, assistCalls: Int = 0,
                notifNav: String = "", notifNavCalls: Int = 0) {
    val scope = rememberCoroutineScope()
    val context = LocalContext.current
    val mainHandler = remember { Handler(Looper.getMainLooper()) }

    val conversations = remember { mutableStateListOf<Conversation>().also { it.addAll(store.load()) } }
    val tombs = remember { store.loadTombs() }   // delete tombstones {id -> deletedAt}
    if (conversations.isEmpty()) conversations.add(Conversation(mode = prefs.mode))
    var currentId by remember {
        mutableStateOf(prefs.currentChatId.takeIf { id -> conversations.any { it.id == id } } ?: conversations.first().id)
    }
    LaunchedEffect(currentId) { prefs.currentChatId = currentId }
    val current = conversations.firstOrNull { it.id == currentId } ?: conversations.first()
    val lastStreamSave = remember { longArrayOf(0L) }

    val drawerState = rememberDrawerState(DrawerValue.Closed)
    var input by remember { mutableStateOf("") }
    var busy by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf("") }
    // Status messages must not stay up forever (a DNS error used to stick in
    // the UI permanently): errors expire after 8 s, normal hints after 4 s.
    LaunchedEffect(status) {
        if (status.isNotBlank()) {
            delay(if (status.startsWith("⚠")) 8000 else 4000)
            status = ""
        }
    }
    // The prototype has three full screens: Chat, Tasks, Settings.
    var screen by remember { mutableStateOf<String?>(null) }
    var showAgents by remember { mutableStateOf(false) }
    var menuOpen by remember { mutableStateOf(false) }
    var attachOpen by remember { mutableStateOf(false) }
    var pendingImage by remember { mutableStateOf<Bitmap?>(null) }
    // Angehaengtes Dokument: der Manager hat den Text schon extrahiert; in die
    // Nachricht wandert NUR der Text, nie die Binaerdatei.
    var pendingDoc by remember { mutableStateOf<ManagerSync.Extracted?>(null) }
    // Vorschau des extrahierten Texts, per Tipp auf den Chip: VOR dem Senden
    // sehen, was die Extraktion wirklich hergibt (PDF-Extraktion ohne Poppler
    // ist nicht perfekt — besser hier pruefen als das Modell raten lassen).
    var docPreviewOpen by remember { mutableStateOf(false) }
    var web by remember { mutableStateOf(prefs.webAccess) }
    var bargeIn by remember { mutableStateOf(prefs.bargeIn) }

    // ── Self-update from GitHub Releases ──────────────────────────────────
    var autoUpdate by remember { mutableStateOf(prefs.autoUpdate) }
    var updateInfo by remember { mutableStateOf<AppUpdate.Release?>(null) }
    var updateBusy by remember { mutableStateOf(false) }
    var updateReady by remember { mutableStateOf<java.io.File?>(null) }   // downloaded, waiting for the installer
    val installedVersion = remember {
        try { context.packageManager.getPackageInfo(context.packageName, 0).versionName ?: "" } catch (e: Exception) { "" }
    }

    fun installUpdate() {
        val f = updateReady ?: return
        if (!AppUpdate.canInstall(context)) {
            status = "Allow installs from KatAgent, then tap Install again"
            AppUpdate.openInstallPermission(context); return
        }
        if (!AppUpdate.install(context, f)) status = "⚠️ Could not start the installer"
    }

    /** Ask GitHub; with auto-update (or [manual]) download and hand over to the installer. */
    fun checkUpdate(manual: Boolean) {
        if (updateBusy) return
        updateBusy = true
        scope.launch {
            val r = withContext(Dispatchers.IO) { AppUpdate.fetch(prefs.updateRepo) }
            prefs.lastUpdateCheck = System.currentTimeMillis()
            if (r == null) { updateBusy = false; if (manual) status = "Update check failed (offline?)"; return@launch }
            if (!AppUpdate.isNewer(installedVersion, r.version)) {
                updateBusy = false; updateInfo = null
                if (manual) status = "KatAgent $installedVersion is up to date"
                return@launch
            }
            updateInfo = r
            val f = AppUpdate.updateFile(context, r.version)
            if (!f.exists() && (manual || prefs.autoUpdate)) {
                status = "Downloading KatAgent ${r.version}…"
                val ok = withContext(Dispatchers.IO) { AppUpdate.download(r.apkUrl, f) }
                if (!ok) { updateBusy = false; status = "⚠️ Update download failed"; return@launch }
            }
            updateBusy = false
            if (f.exists()) {
                updateReady = f
                status = "KatAgent ${r.version} ready to install"
                if (manual || prefs.autoUpdate) installUpdate()
            }
        }
    }
    LaunchedEffect(Unit) {
        delay(4000)
        if (prefs.autoUpdate && System.currentTimeMillis() - prefs.lastUpdateCheck > 6 * 3600_000L) checkUpdate(false)
    }
    var instances by remember { mutableStateOf<List<AgentInstance>>(emptyList()) }
    // Header and settings show the sync state ("Syncing …" / "Synced · N chats").
    var syncing by remember { mutableStateOf(false) }
    var lastSync by remember { mutableStateOf("") }
    var online by remember { mutableStateOf(false) }
    val listState = rememberLazyListState()

    fun loadInstances() {
        if (prefs.serverUrl.isBlank()) return
        scope.launch {
            val j = withContext(Dispatchers.IO) { ManagerSync.listInstances(prefs.serverUrl, prefs.user, prefs.pass) }
            if (j != null) instances = ManagerSync.parseInstances(j)
        }
    }
    // Load at startup and again after the agent management is closed.
    LaunchedEffect(showAgents) { if (!showAgents) loadInstances() }
    // The prototype no longer has a refresh button in the chip row; the list
    // is instead reloaded when the drawer is opened.
    LaunchedEffect(drawerState.currentValue) {
        if (drawerState.currentValue == DrawerValue.Open) loadInstances()
    }

    val notifPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { }
    LaunchedEffect(Unit) { if (Build.VERSION.SDK_INT >= 33) notifPerm.launch(Manifest.permission.POST_NOTIFICATIONS) }

    // Prompt templates from the manager -> appear in the slash picker (/daily …).
    var promptCmds by remember { mutableStateOf(listOf<SlashCmd>()) }
    LaunchedEffect(Unit) {
        while (prefs.serverUrl.isBlank()) delay(3000)
        while (true) {
            withContext(Dispatchers.IO) {
                ManagerSync.listPrompts(prefs.serverUrl, prefs.user, prefs.pass)
            }.let { promptCmds = it.map { (n, t) -> SlashCmd("/$n", t.take(60), arg = true) } }
            delay(60000)
        }
    }

    // Notifications: polls /api/notifications and raises an Android system
    // notification for new, unread entries (since app start). Its own loop, so a
    // slow chat poll doesn't block the notifications.
    LaunchedEffect(Unit) {
        val startTs = System.currentTimeMillis() / 1000
        var nrev = 0L
        val seen = HashSet<String>()
        while (prefs.serverUrl.isBlank()) delay(3000)
        while (true) {
            val res = withContext(Dispatchers.IO) {
                ManagerSync.pollNotifications(prefs.serverUrl, prefs.user, prefs.pass, nrev, 25)
            }
            if (res == null) { delay(5000); continue }
            nrev = res.rev
            res.items?.forEach { n ->
                if (seen.add(n.id) && !n.read && n.ts >= startTs) {
                    showAgentNotification(context, n.id, n.title, n.body, n.link)
                }
            }
        }
    }

    // Observe the background download.
    val dl by DownloadBus.state.collectAsState()
    LaunchedEffect(dl) {
        when (val s = dl) {
            is Dl.Progress -> status = if (s.total > 0)
                "Download ${s.done * 100 / s.total}% (${s.done / 1_000_000}/${s.total / 1_000_000} MB)"
                else "Download ${s.done / 1_000_000} MB…"
            is Dl.Done -> {
                status = "Loading model…"
                withContext(Dispatchers.IO) { try { gemma.load(s.path) } catch (e: Exception) { status = "⚠️ ${e.message}" } }
                if (gemma.isReady()) status = "Model loaded ✅"
            }
            is Dl.Error -> status = "⚠️ ${s.msg}"
            Dl.Idle -> {}
        }
    }

    val picker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri != null) scope.launch {
            status = "Copying model…"
            val path = withContext(Dispatchers.IO) { copyModel(context, store.modelsDir, uri) }
            if (path != null) {
                prefs.activeModel = File(path).name
                status = "Loading model…"
                withContext(Dispatchers.IO) { try { gemma.load(path) } catch (e: Exception) { status = "⚠️ ${e.message}" } }
                if (gemma.isReady()) status = "Model loaded ✅"
            } else status = "⚠️ Copy failed"
        }
    }
    val docPicker = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri: Uri? ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            status = "Extracting…"
            val r = withContext(Dispatchers.IO) {
                runCatching {
                    val name = context.contentResolver.query(uri, null, null, null, null)?.use { c ->
                        val i = c.getColumnIndex(android.provider.OpenableColumns.DISPLAY_NAME)
                        if (c.moveToFirst() && i >= 0) c.getString(i) else null
                    } ?: uri.lastPathSegment ?: "upload"
                    val bytes = context.contentResolver.openInputStream(uri)?.use { it.readBytes() }
                        ?: return@runCatching null
                    if (bytes.size > 50 * 1024 * 1024) null
                    else ManagerSync.extract(prefs.serverUrl, prefs.user, prefs.pass, name, bytes)
                }.getOrNull()
            }
            if (r != null) { pendingDoc = r; docPreviewOpen = false; status = "" }
            else status = "⚠️ ${ManagerSync.lastStatus}"
        }
    }
    val imagePicker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri: Uri? ->
        if (uri != null) scope.launch {
            val bmp = withContext(Dispatchers.IO) { loadBitmap(context, uri) }
            if (bmp != null) pendingImage = bmp else status = "⚠️ Image could not be loaded"
        }
    }
    // The prototype's attachment sheet: "Camera" takes a preview (no
    // FileProvider needed), send() only handles bitmaps anyway.
    val camera = rememberLauncherForActivityResult(ActivityResultContracts.TakePicturePreview()) { bmp: Bitmap? ->
        if (bmp != null) pendingImage = bmp
    }

    // Debounced background push to the manager, so new/changed chats land on the
    // server live (and via it in the web UI) — not only at the next manual sync.
    // The manager MERGES server-side, so it overwrites nothing.
    val pushJob = remember { arrayOfNulls<kotlinx.coroutines.Job>(1) }
    fun pushChats() {
        if (prefs.serverUrl.isBlank()) return
        pushJob[0]?.cancel()
        pushJob[0] = scope.launch {
            delay(1200)
            withContext(Dispatchers.IO) {
                ManagerSync.push(prefs.serverUrl, prefs.user, prefs.pass, store.toPushJson(conversations, tombs))
            }
        }
    }

    fun persist() {
        current.updatedAt = System.currentTimeMillis()
        if (current.title == "New Chat") {
            current.messages.firstOrNull { it.user }?.text?.trim()?.take(40)?.let {
                if (it.isNotBlank()) current.title = it
            }
        }
        store.save(conversations)
        pushChats()
    }

    fun newChat() {
        val c = Conversation(mode = prefs.mode)
        conversations.add(0, c)
        currentId = c.id
        store.save(conversations)
    }

    // Switch agent = jump to THIS agent's history (one thread per agent), instead
    // of silently rehoming the open chat. If the current chat is still empty, it
    // is simply reassigned (no new empty thread).
    fun switchToAgent(mode: String, instance: String) {
        prefs.mode = mode; prefs.instance = instance
        if (current.messages.isEmpty()) {
            current.mode = mode; current.instance = instance; persist(); return
        }
        val existing = conversations
            .filter { it.mode == mode && (mode == "local" || it.instance == instance) }
            .maxByOrNull { it.updatedAt }
        currentId = existing?.id ?: Conversation(mode = mode, instance = instance)
            .also { conversations.add(0, it); store.save(conversations) }.id
    }

    fun deleteChat(c: Conversation) {
        tombs[c.id] = System.currentTimeMillis()   // propagate the deletion (web + other devices)
        store.saveTombs(tombs)
        conversations.remove(c)
        if (conversations.isEmpty()) conversations.add(Conversation(mode = prefs.mode))
        if (currentId == c.id) currentId = conversations.first().id
        store.save(conversations)
        pushChats()
    }

    fun selectModel(f: File) {
        prefs.activeModel = f.name
        status = "Loading model…"
        scope.launch {
            withContext(Dispatchers.IO) { try { gemma.load(f.absolutePath) } catch (e: Exception) { status = "⚠️ ${e.message}" } }
            if (gemma.isReady()) status = "Model loaded ✅"
        }
    }

    fun sync() {
        if (prefs.serverUrl.isBlank()) { status = "⚠️ Server URL missing (Settings)"; return }
        scope.launch {
            syncing = true
            status = "Sync…"
            val remoteJson = withContext(Dispatchers.IO) { ManagerSync.pull(prefs.serverUrl, prefs.user, prefs.pass) }
            if (remoteJson == null) {
                syncing = false; online = false
                status = "⚠️ Sync: server unreachable"; return@launch
            }
            val byId = LinkedHashMap<String, Conversation>()
            for (c in conversations) byId[c.id] = c
            for (r in store.fromJson(remoteJson)) {
                if (tombs[r.id]?.let { r.updatedAt <= it } == true) continue   // tombstoned locally
                val local = byId[r.id]
                if (local == null || r.updatedAt > local.updatedAt) byId[r.id] = r
            }
            val merged = byId.values.sortedByDescending { it.updatedAt }
            conversations.clear(); conversations.addAll(merged)
            if (conversations.none { it.id == currentId }) {
                if (conversations.isEmpty()) conversations.add(Conversation(mode = prefs.mode))
                currentId = conversations.first().id
            }
            store.save(conversations)
            val ok = withContext(Dispatchers.IO) { ManagerSync.push(prefs.serverUrl, prefs.user, prefs.pass, store.toPushJson(conversations, tombs)) }
            syncing = false; online = ok; lastSync = nowHm()
            status = if (ok) "" else "⚠️ Push failed"
        }
    }

    // Live sync with the manager: do one full reconcile, then stay on the
    // long-poll permanently (/api/chats?since=&wait=). The manager answers as soon
    // as the web UI or another device writes — so new messages appear here within
    // fractions of a second, without constant polling and without a restart.
    // While a reply is streaming (busy) nothing is merged, otherwise the partial
    // text would be overwritten.
    val chatsRev = remember { longArrayOf(0L) }
    LaunchedEffect(Unit) {
        while (prefs.serverUrl.isBlank()) delay(3000)
        sync()
        while (true) {
            val res = withContext(Dispatchers.IO) {
                ManagerSync.pollChats(prefs.serverUrl, prefs.user, prefs.pass, chatsRev[0], 25)
            }
            if (res == null) { online = false; delay(5000); continue }   // offline / old manager
            online = true
            chatsRev[0] = res.rev
            // Apply incoming delete tombstones (even without new chats)
            res.tombstones?.let { ts ->
                try {
                    val o = JSONObject(ts)
                    var tchanged = false
                    o.keys().forEach { id ->
                        val dat = o.optLong(id)
                        if ((tombs[id] ?: 0) < dat) tombs[id] = dat
                        val idx = conversations.indexOfFirst { it.id == id }
                        if (idx >= 0 && conversations[idx].updatedAt <= dat) {
                            if (currentId == conversations[idx].id) currentId =
                                conversations.firstOrNull { it.id != id }?.id ?: currentId
                            conversations.removeAt(idx); tchanged = true
                        }
                    }
                    store.saveTombs(tombs)
                    if (tchanged) {
                        if (conversations.isEmpty()) conversations.add(Conversation(mode = prefs.mode))
                        if (conversations.none { it.id == currentId }) currentId = conversations.first().id
                        store.save(conversations)
                    }
                } catch (_: Exception) {}
            }
            val remote = res.chats ?: continue           // timeout, nothing new
            var waited = 0
            while (busy && waited++ < 120) delay(500)
            // IMPORTANT: existing Conversation objects are FILLED, not replaced.
            // send() holds a reference to current.messages and streams the reply
            // there — if you swap the object out, question and answer land in a
            // detached list: invisible, unsaved, never pushed. The open
            // conversation also stays untouched while a turn is running.
            val byId = LinkedHashMap<String, Conversation>()
            for (c in conversations) byId[c.id] = c
            var changed = false
            for (r in store.fromJson(remote)) {
                if (tombs[r.id]?.let { r.updatedAt <= it } == true) continue   // tombstoned -> do not resurrect
                val local = byId[r.id]
                if (local == null) {                        // genuinely new
                    byId[r.id] = r; changed = true
                    continue
                }
                if (r.updatedAt <= local.updatedAt) continue   // local is newer
                if (busy && local.id == currentId) continue    // turn in progress
                // Only APPEND messages. Replacing would wipe out a just-typed,
                // not-yet-pushed question - exactly the case where the other side
                // (web/manager) has a newer clock. Only if the local list is a
                // prefix of the remote one are we sure nothing of our own is lost;
                // otherwise the next push reconciles it.
                val lm = local.messages
                val rm = r.messages
                val isPrefix = rm.size >= lm.size && lm.indices.all { lm[it] == rm[it] }
                if (isPrefix) {
                    if (rm.size > lm.size) { for (i in lm.size until rm.size) lm.add(rm[i]); changed = true }
                } else if (rm.size >= lm.size) {
                    // DIVERGENCE (no prefix), remote is newer (checked above) and not
                    // shorter -> adopt the server state as the merge point, instead of
                    // letting the sync hang forever. Fill in place (don't swap the object!).
                    lm.clear(); lm.addAll(rm); changed = true
                } else continue    // local is longer -> keep it, our own push reconciles
                if (local.title != r.title && r.title.isNotBlank()) { local.title = r.title; changed = true }
                if (local.instance != r.instance && r.instance.isNotBlank()) local.instance = r.instance
                local.updatedAt = r.updatedAt
            }
            if (!changed) continue
            lastSync = nowHm()
            val merged = byId.values.sortedByDescending { it.updatedAt }
            conversations.clear()
            conversations.addAll(merged)
            if (conversations.isNotEmpty() && conversations.none { it.id == currentId })
                currentId = conversations.first().id
            store.save(conversations)                    // local only, no push
        }
    }

    // true  = the app handled the command itself (nothing to the agent).
    // false = pass through: the message goes to the agent as normal text
    //         (so its own commands like /reset take effect).
    fun handleSlash(text: String, msgs: androidx.compose.runtime.snapshots.SnapshotStateList<Msg>): Boolean {
        val body = text.removePrefix("/").trim()
        val cmd = body.substringBefore(' ').lowercase()
        val rest = body.substringAfter(' ', "").trim()
        when (cmd) {
            "help", "" -> msgs.add(Msg(false,
                "App commands:\n" +
                "/task <text> – background task on the current agent\n" +
                "/task every 30m <text> – recurring (also: daily 08:00, hourly)\n" +
                "/agents – open agent management\n" +
                "/help – this help\n" +
                "Other /-commands (e.g. /reset) go to the agent."))
            "task" -> {
                val inst = current.instance.ifBlank { prefs.instance }
                if (inst.isBlank()) { msgs.add(Msg(false, "⚠️ No server agent selected (tap a chip above).")); return true }
                val m = Regex("^(every\\s+\\d+[mhd]|daily\\s+\\d{1,2}:\\d{2}|hourly)\\s+(.*)", RegexOption.IGNORE_CASE).find(rest)
                val schedule = m?.groupValues?.get(1)?.trim() ?: ""
                val message = (m?.groupValues?.get(2) ?: rest).trim()
                if (message.isBlank()) { msgs.add(Msg(false, "⚠️ Usage: /task <text>")); return true }
                scope.launch {
                    val r = withContext(Dispatchers.IO) { ManagerSync.createTask(prefs.serverUrl, prefs.user, prefs.pass, inst, message, schedule) }
                    msgs.add(Msg(false, if (r != null)
                        "✅ Task created on @$inst${if (schedule.isNotBlank()) " ($schedule)" else " (background)"}. Drawer → Tasks."
                        else "⚠️ ${ManagerSync.lastStatus}"))
                    persist()
                }
            }
            "agents" -> { msgs.add(Msg(false, "Opening server agents…")); showAgents = true }
            // /login is moot: claudy signs in at boot via the host, and headless
            // (claude -p) there is no interactive login. Intercept it instead of
            // sending it to the agent, where it would only run into nothing.
            "login" -> msgs.add(Msg(false, "No login needed – the agent is signed in via the host."))
            else -> return false   // pass through to the agent
        }
        return true
    }

    // ── Voice control ──────────────────────────────────────────────────────
    // Record on the device, recognize and speak on the manager (Parakeet/Piper).
    // Only what was asked by voice is read aloud — reading a long explanation
    // aloud unprompted would be a nuisance.
    var recording by remember { mutableStateOf(false) }
    var transcribing by remember { mutableStateOf(false) }
    var voiceIn by remember { mutableStateOf(false) }
    // send() is declared further down; in Kotlin you can't call a local function
    // before it. This flag bridges that.
    var pendingVoiceSend by remember { mutableStateOf(false) }
    // Which message is currently being spoken — the bubble shows it and stops via
    // it. -1 means: no one is speaking.
    var speakingIdx by remember { mutableStateOf(-1) }
    val recorder = remember { arrayOfNulls<MediaRecorder>(1) }
    val recFile = remember { arrayOfNulls<java.io.File>(1) }
    val player = remember { arrayOfNulls<TtsPlayer>(1) }
    // Barge-in: the echo-cancelled microphone that listens while the reply is spoken.
    val echoMic = remember { arrayOfNulls<EchoMic>(1) }
    var bargeListening by remember { mutableStateOf(false) }
    // Counter instead of a flag: speech synthesis runs over the network, and a
    // reply that trickles in after cancellation must not still blare out.
    val speakGen = remember { intArrayOf(0) }
    // Turn generation: another mic press increments it -> the running send is
    // ignored and chatStream aborts (correcting the previous statement).
    val turnGen = remember { intArrayOf(0) }
    val cancelHandle = remember { arrayOfNulls<ServerAgent.CancelHandle>(1) }
    var traceOpen by remember { mutableStateOf<String?>(null) }     // turn id whose trace is shown

    fun stopSpeak() {
        speakGen[0]++
        runCatching { player[0]?.stop() }
        player[0] = null
        speakingIdx = -1
    }

    fun stopBargeMic() {
        echoMic[0]?.stop(); echoMic[0] = null
        bargeListening = false
    }

    /** Recognize on the manager and send as a voice turn (hands-free). */
    fun transcribeAndSend(audio: ByteArray, mime: String) {
        transcribing = true
        scope.launch {
            val text = withContext(Dispatchers.IO) {
                ManagerSync.stt(prefs.serverUrl, prefs.user, prefs.pass, audio, mime)
            }
            transcribing = false
            if (text.isNullOrBlank()) { status = "Didn't catch that (${ManagerSync.lastStatus})"; return@launch }
            input = text
            voiceIn = true
            pendingVoiceSend = true      // hands-free: send right away
        }
    }

    /** [bargeIn]: keep listening while speaking; talking over the reply cuts it
     *  off and becomes the next input (voice turns only, never for read-aloud). */
    fun speakText(text: String, idx: Int = -1, bargeIn: Boolean = false) {
        if (text.isBlank() || prefs.serverUrl.isBlank()) return
        stopSpeak()                       // never two voices at once
        stopBargeMic()
        val gen = speakGen[0]
        speakingIdx = idx
        scope.launch {
            val wav = withContext(Dispatchers.IO) {
                ManagerSync.tts(prefs.serverUrl, prefs.user, prefs.pass, text.take(4000))
            }
            if (gen != speakGen[0]) return@launch          // cancelled in the meantime
            if (wav == null) {
                speakingIdx = -1
                status = "⚠️ Speech: ${ManagerSync.lastStatus}"; return@launch
            }
            withContext(Dispatchers.IO) {
                runCatching {
                    if (gen != speakGen[0]) return@runCatching
                    val pcm = Wav.parse(wav)
                    var tp: TtsPlayer? = null
                    // The player reports on the main looper — safe for the Compose state.
                    tp = TtsPlayer(onDone = {
                        if (player[0] === tp) { player[0] = null; speakingIdx = -1 }
                        echoMic[0]?.playbackEnded()
                    })
                    player[0]?.stop()
                    player[0] = tp
                    // Barge-in: the mic listens while we speak, with our own voice
                    // taken out by the echo canceller (EchoMic / libkatecho).
                    var mic: EchoMic? = null
                    if (bargeIn && prefs.bargeIn && pcm != null && !recording &&
                        ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
                        PackageManager.PERMISSION_GRANTED) {
                        // Each callback checks it still is THE mic: a stopped one may
                        // report a frame later, after the next reply started.
                        mic = EchoMic(pcm.rate,
                            onBargeIn = { if (echoMic[0] === mic) { stopSpeak(); bargeListening = true; status = "Listening…" } },
                            onUtterance = { bytes -> if (echoMic[0] === mic) { echoMic[0] = null; bargeListening = false; transcribeAndSend(bytes, "audio/wav") } },
                            onIdle = { if (echoMic[0] === mic) { echoMic[0] = null; bargeListening = false } })
                        if (!mic.start()) mic = null
                    }
                    echoMic[0] = mic
                    val farEnd: ((ShortArray, Int) -> Unit)? = mic?.let { m -> { buf, n -> m.feedFarEnd(buf, n) } }
                    if (!tp.play(wav, farEnd)) {
                        mic?.stop(); echoMic[0] = null; player[0] = null
                        mainHandler.post { speakingIdx = -1; status = "⚠️ Speech: unsupported audio" }
                    }
                }
            }
        }
    }

    fun stopRec() {
        val r = recorder[0] ?: return
        recorder[0] = null; recording = false
        // stop() throws if stopped too early (recording too short) — then there
        // is simply nothing to recognize.
        val ok = runCatching { r.stop() }.isSuccess
        runCatching { r.release() }
        val f = recFile[0]; recFile[0] = null
        if (!ok || f == null || !f.exists() || f.length() < 2000) {
            status = "Too short — try again"; f?.delete(); return
        }
        transcribing = true
        scope.launch {
            val bytes = withContext(Dispatchers.IO) { f.readBytes().also { f.delete() } }
            transcribeAndSend(bytes, "audio/mp4")
        }
    }

    fun startRec() {
        if (prefs.serverUrl.isBlank()) { status = "⚠️ Server URL missing (Settings)"; return }
        stopSpeak()                       // speaking over it means: the output is done
        stopBargeMic()
        val f = java.io.File(context.cacheDir, "rec.m4a")
        val r = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(context) else @Suppress("DEPRECATION") MediaRecorder()
        val ok = runCatching {
            r.setAudioSource(MediaRecorder.AudioSource.MIC)
            r.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            r.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            r.setAudioSamplingRate(16000)      // recognition needs no more
            r.setAudioChannels(1)
            r.setAudioEncodingBitRate(32000)
            r.setOutputFile(f.absolutePath)
            r.prepare(); r.start()
        }.isSuccess
        if (!ok) { runCatching { r.release() }; status = "⚠️ Cannot record"; return }
        recorder[0] = r; recFile[0] = f; recording = true
        status = "Listening… stops on its own"

        // Stop by itself when it goes quiet. The threshold comes from the first
        // tenths of a second of room noise: a fixed value won't do, a train is
        // louder than an office. Stopping only happens after something was actually
        // spoken — otherwise it would cut off the pause for thought at the start.
        scope.launch {
            // Measure the noise floor from the first ~0.6 s as a MINIMUM (not max):
            // so it isn't skewed if the user starts talking immediately — otherwise
            // the speech threshold would be unreachable and the recording would cut
            // off MID-speech (exactly the bug). Additionally capped.
            var floor = Int.MAX_VALUE; var probes = 0
            var spoke = false; var quiet = 0L; var total = 0L
            while (recorder[0] === r) {
                delay(VAD_TICK)
                total += VAD_TICK
                val amp = runCatching { r.maxAmplitude }.getOrDefault(0)
                if (probes < 6) { floor = minOf(floor, amp); probes++; continue }
                val base = (if (floor == Int.MAX_VALUE) 0 else floor).coerceAtMost(3000)
                // Hysteresis: the START of speech needs a clear margin, but once
                // speaking IS happening a much lower threshold keeps the recording
                // alive. This way the short amplitude dips between words/syllables do
                // NOT count as silence — exactly what cut the recording off mid
                // fluent speech after a few seconds.
                val loud = if (spoke) amp > base + 350 else amp > base + 1500
                if (loud) { spoke = true; quiet = 0L } else if (spoke) quiet += VAD_TICK
                val done = (spoke && quiet >= VAD_HANG) ||
                    (!spoke && total >= VAD_LEAD) ||       // said nothing at all
                    total >= VAD_MAX                       // emergency brake
                if (done) { if (recorder[0] === r) stopRec(); return@launch }
            }
        }
    }

    val micPerm = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) startRec() else status = "⚠️ Microphone permission needed"
    }

    // ── Security gateway ───────────────────────────────────────────────────
    // Per chat, state on the manager. Filtering happens there too — the app only
    // sends its chat identifier along, so the manager knows which chat is meant.
    // A filter on the device would be useless as soon as the same chat is served
    // from the web.
    var gwIds by remember { mutableStateOf(setOf<String>()) }
    var gwAvailable by remember { mutableStateOf(false) }
    var gwChars by remember { mutableStateOf(mapOf<String, Int>()) }
    var gwImgs by remember { mutableStateOf(mapOf<String, Int>()) }
    val gwOn = current.id in gwIds

    fun gwLoad() {
        if (prefs.serverUrl.isBlank()) return
        scope.launch {
            val g = withContext(Dispatchers.IO) {
                ManagerSync.gatewayGet(prefs.serverUrl, prefs.user, prefs.pass)
            } ?: return@launch
            gwIds = g.on; gwAvailable = g.available; gwChars = g.chars; gwImgs = g.images
        }
    }

    fun gwToggle() {
        val id = current.id
        val on = id !in gwIds
        gwIds = if (on) gwIds + id else gwIds - id      // visible immediately
        scope.launch {
            withContext(Dispatchers.IO) {
                ManagerSync.gatewaySet(prefs.serverUrl, prefs.user, prefs.pass, id, on)
            }
            gwLoad()                                     // and then the real state
        }
    }

    fun cancelTurn() {
        // Discard the running reply/send: increment the generation (the stream
        // aborts, late chunks are ignored), turn off auto-send, mark the half
        // reply as aborted.
        turnGen[0]++
        runCatching { cancelHandle[0]?.cancel() }   // drop the running stream immediately
        pendingVoiceSend = false
        val msgs = current.messages
        val li = msgs.lastIndex
        if (li >= 0 && !msgs[li].user && msgs[li].text.isBlank())
            msgs[li] = msgs[li].copy(text = "_(aborted)_")
        busy = false
        stopSpeak()
        stopBargeMic()
        persist()
    }

    fun micToggle() {
        if (recording) { stopRec(); return }
        if (bargeListening) { stopBargeMic(); status = ""; return }   // abort the barge-in capture
        // Another press during reply/auto-send: cancel and record ANEW (correcting
        // the previous statement), instead of continuing the old send.
        if (busy || pendingVoiceSend) cancelTurn()
        if (speakingIdx >= 0) stopSpeak()   // first the output, then the ear
        stopBargeMic()
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (granted) startRec() else micPerm.launch(Manifest.permission.RECORD_AUDIO)
    }

    fun send() {
        val typed = input.trim()
        if ((typed.isEmpty() && pendingImage == null && pendingDoc == null) || busy) return
        val img = pendingImage
        val imgB64 = img?.let { bitmapToBase64(it) }
        // Angehaengtes Dokument: der extrahierte Text wird VOR die Frage
        // gestellt, klar markiert — das Modell bekommt Inhalt + Frage in einem
        // Turn. In der Blase erscheint nur eine kompakte Kennzeichnung, nicht
        // die 80k Zeichen (die Nachricht ans Modell traegt den Volltext).
        val doc = pendingDoc
        val text = if (doc != null)
            "[Attached document: ${doc.name}]\n${doc.text}\n[End of document]\n\n" +
                typed.ifBlank { "Please read the attached document and summarize it." }
        else typed
        val msgs = current.messages
        msgs.add(Msg(true, if (doc != null)
            "\uD83D\uDCC4 ${doc.name}" + (if (typed.isNotBlank()) "\n$typed" else "")
        else typed, image = imgB64))
        input = ""; pendingImage = null; pendingDoc = null
        if (typed.startsWith("/") && handleSlash(typed, msgs)) { persist(); return }
        busy = true
        persist()

        if (current.mode == "server") {
            val botMsg = Msg(false, "")
            msgs.add(botMsg)
            val botKey = botMsg.key           // address by key, NOT by index (interrupt/sync-safe)
            val inst = current.instance.ifBlank { prefs.instance }
            val myGen = ++turnGen[0]
            val ch = ServerAgent.CancelHandle(); cancelHandle[0] = ch
            fun appendBot(chunk: String) {
                val i = msgs.indexOfFirst { it.key == botKey }
                if (i >= 0) msgs[i] = msgs[i].copy(text = msgs[i].text + chunk)
            }
            scope.launch {
                val err = withContext(Dispatchers.IO) {
                    ServerAgent.chatStream(prefs.serverUrl, inst, prefs.user, prefs.pass, text, imgB64,
                        chatId = current.id, cancel = ch,
                        onTurn = { t -> mainHandler.post {
                            val i = msgs.indexOfFirst { it.key == botKey }
                            if (i >= 0) msgs[i] = msgs[i].copy(turn = t)
                        } }) { chunk ->
                        if (myGen != turnGen[0]) return@chatStream
                        mainHandler.post {
                            if (myGen == turnGen[0]) {
                                appendBot(chunk)
                                val t = System.currentTimeMillis()
                                if (t - lastStreamSave[0] > 800) { lastStreamSave[0] = t; current.updatedAt = t; store.save(conversations) }
                            }
                        }
                    }
                }
                if (myGen != turnGen[0]) return@launch          // aborted -> do nothing more
                if (err != null) mainHandler.post { appendBot("\n$err") }
                busy = false; persist(); listState.animateScrollToItem(msgs.size)
                if (voiceIn) {
                    voiceIn = false
                    val bi = msgs.indexOfFirst { it.key == botKey }
                    if (bi >= 0) speakText(splitThink(msgs[bi].text).answer, bi, bargeIn = true)
                }
            }
        } else {
            val botMsg = Msg(false, "")
            msgs.add(botMsg)
            val botKey = botMsg.key
            fun appendBot(chunk: String) {
                val i = msgs.indexOfFirst { it.key == botKey }
                if (i >= 0) msgs[i] = msgs[i].copy(text = msgs[i].text + chunk)
            }
            fun setBot(txt: String) {
                val i = msgs.indexOfFirst { it.key == botKey }
                if (i >= 0) msgs[i] = msgs[i].copy(text = txt)
            }
            val useWeb = web
            scope.launch {
                try {
                    withContext(Dispatchers.IO) {
                        if (!gemma.isReady() && prefs.activeModel.isNotEmpty()) {
                            try { gemma.load(store.modelFile(prefs.activeModel).absolutePath) } catch (_: Exception) {}
                        }
                        val prompt = if (useWeb && img == null) {
                            mainHandler.post { status = "🌐 Web research…" }
                            val ctx = try { WebSearch.buildContext(text) } catch (e: Exception) { "" }
                            mainHandler.post { status = "" }
                            if (ctx.isNotBlank())
                                "Answer the following question using this current web information. " +
                                "Cite the source (URL) if possible.\n\n$ctx\n\nQuestion: $text"
                            else text
                        } else text
                        gemma.generateStreaming(prompt, img) { d ->
                            mainHandler.post {
                                appendBot(d)
                                val t = System.currentTimeMillis()
                                if (t - lastStreamSave[0] > 800) { lastStreamSave[0] = t; current.updatedAt = t; store.save(conversations) }
                            }
                        }
                    }
                } catch (e: Exception) {
                    mainHandler.post { setBot("⚠️ ${e.message}") }
                } finally {
                    busy = false; persist(); listState.animateScrollToItem(msgs.size)
                if (voiceIn) { voiceIn = false; val bi = msgs.indexOfFirst { it.key == botKey }; if (bi >= 0) speakText(splitThink(msgs[bi].text).answer, bi, bargeIn = true) }
                }
            }
        }
    }

    // As in the prototype (componentDidUpdate): the list sticks to the bottom edge.
    LaunchedEffect(current.messages.size, currentId) {
        if (current.messages.isNotEmpty()) listState.animateScrollToItem(current.messages.size)
    }

    // ── derived labels ────────────────────────────────────────────────────────
    LaunchedEffect(pendingVoiceSend) {
        if (pendingVoiceSend) { pendingVoiceSend = false; send() }
    }

    // Call via the power button (assistant): listen immediately. The reply is
    // then spoken automatically — stopRec() sets voiceIn as soon as the input
    // really came by voice.
    LaunchedEffect(assistCalls) {
        if (assistCalls > 0) {
            val ai = prefs.assistInstance.trim()
            if (ai.isNotBlank() && !(current.mode == "server" && current.instance == ai)) {
                val existing = conversations.filter { it.mode == "server" && it.instance == ai }.maxByOrNull { it.updatedAt }
                currentId = existing?.id ?: Conversation(mode = "server", instance = ai)
                    .also { conversations.add(0, it); store.save(conversations) }.id
            }
            if (!recording && !busy) micToggle()
        }
    }
    // Tap on a system notification: navigate to the target.
    LaunchedEffect(notifNavCalls) {
        if (notifNavCalls <= 0) return@LaunchedEffect
        when {
            notifNav == "missions" -> screen = "missions"
            notifNav == "tasks" -> screen = "tasks"
            notifNav.startsWith("chat:") -> {
                val inst = notifNav.removePrefix("chat:")
                prefs.mode = "server"; prefs.instance = inst; screen = null
                // Open an existing chat instead of an empty window: prefer the
                // task chat (where task results land), otherwise the most recent
                // chat with this instance.
                val target = conversations.firstOrNull { it.id == "task-$inst" }
                    ?: conversations.filter { it.instance == inst }
                        .maxByOrNull { it.updatedAt }
                if (target != null) currentId = target.id
            }
        }
    }

    // Fetch the gateway state: at startup, on server change and after every
    // finished reply (by then the counter has moved).
    LaunchedEffect(prefs.serverUrl) { gwLoad() }
    LaunchedEffect(busy) { if (!busy) gwLoad() }

    val serverModel = instances.firstOrNull { it.name == current.instance }?.model ?: ""
    val modelLabel = if (current.mode == "server")
        serverModel.ifBlank { current.instance.ifBlank { "server" } }
    else prefs.activeModel.ifBlank { "no model loaded" }
    val agentLabel = agentOf(current)
    // The counter visibly belongs here: being filtered silently is exactly what
    // you shouldn't let a filter get away with.
    val gwRemoved = (gwChars[current.id] ?: 0) + (gwImgs[current.id] ?: 0)
    val syncLabel = (if (syncing) "Syncing …" else "Synced · ${conversations.size} chats") +
        (if (gwOn && gwRemoved > 0) " · ${gwChars[current.id] ?: 0} stripped" +
            (if ((gwImgs[current.id] ?: 0) > 0) " · ${gwImgs[current.id]} img" else "") else "")

    BackHandler(screen != null || menuOpen || attachOpen) {
        when {
            menuOpen -> menuOpen = false
            attachOpen -> attachOpen = false
            else -> screen = null
        }
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        scrimColor = Kat.scrim,
        drawerContent = {
            KatDrawer(
                conversations.sortedByDescending { it.updatedAt },
                currentId,
                agentCount = instances.size,
                online = online,
                onSelect = { currentId = it; scope.launch { drawerState.close() } },
                onNew = { newChat(); scope.launch { drawerState.close() } },
                onDelete = { deleteChat(it) },
                onTasks = { screen = "tasks"; scope.launch { drawerState.close() } },
                onMissions = { screen = "missions"; scope.launch { drawerState.close() } },
                onSettings = { screen = "settings"; scope.launch { drawerState.close() } },
            )
        }
    ) {
        Box(Modifier.fillMaxSize().background(Kat.bg)) {
            Column(Modifier.fillMaxSize()) {
                // ── Header: menu, title + sync line, overflow menu ──────────
                Row(
                    Modifier.fillMaxWidth().padding(start = 8.dp, end = 8.dp, top = 8.dp, bottom = 4.dp),
                    horizontalArrangement = Arrangement.spacedBy(4.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    RoundIconButton({ scope.launch { drawerState.open() } }) {
                        Icon(Icons.Filled.Menu, "Chats", Modifier.size(20.dp), tint = Kat.textDim)
                    }
                    Column(Modifier.weight(1f).padding(start = 4.dp)) {
                        Text(
                            current.title, style = MaterialTheme.typography.titleMedium,
                            color = Kat.text, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                        Row(
                            horizontalArrangement = Arrangement.spacedBy(6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Box(Modifier.size(6.dp).clip(CircleShape)
                                .background(if (online) Kat.green else Kat.textGhost))
                            Text(
                                syncLabel, fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint,
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                    if (gwAvailable) RoundIconButton(
                        { gwToggle() },
                        background = if (gwOn) Kat.accent else Color.Transparent,
                    ) {
                        Icon(
                            if (gwOn) Icons.Filled.Shield else Icons.Outlined.Shield,
                            if (gwOn) "Security Gateway on" else "Security Gateway off",
                            Modifier.size(18.dp),
                            tint = if (gwOn) Kat.onAccent else Kat.textDim,
                        )
                    }
                    RoundIconButton(
                        { menuOpen = !menuOpen },
                        background = if (menuOpen) Kat.hover else Color.Transparent,
                    ) { Icon(Icons.Filled.MoreVert, "Menu", Modifier.size(18.dp), tint = Kat.textDim) }
                }

                // ── Agent chips ─────────────────────────────────────────────
                val chips = if (instances.isNotEmpty()) instances
                    else if (prefs.instance.isNotBlank()) listOf(AgentInstance(prefs.instance, false, "", "", "")) else emptyList()
                Row(
                    Modifier.fillMaxWidth().horizontalScroll(rememberScrollState())
                        .padding(start = 16.dp, end = 16.dp, top = 8.dp, bottom = 12.dp),
                    horizontalArrangement = Arrangement.spacedBy(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    AgentChip("Device", current.mode == "local", { switchToAgent("local", "") }) {
                        Icon(Icons.Filled.PhoneAndroid, null, Modifier.size(13.dp), tint = it)
                    }
                    chips.forEach { inst ->
                        AgentChip(
                            inst.name,
                            current.mode == "server" && current.instance == inst.name,
                            { switchToAgent("server", inst.name) },
                        ) {
                            Icon(
                                if (inst.running) Icons.Filled.Cloud else Icons.Outlined.CloudOff,
                                null, Modifier.size(14.dp), tint = it,
                            )
                        }
                    }
                }
                Hairline()

                // Feedback (download, model load errors, sync problems). Not in
                // the prototype like this, but the only place these messages
                // become visible at all.
                if (status.isNotEmpty()) Text(
                    status,
                    Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
                    fontSize = 12.sp, fontFamily = Plex,
                    color = if (status.startsWith("⚠️")) Kat.red else Kat.accentText,
                    maxLines = 2, overflow = TextOverflow.Ellipsis,
                )

                // ── Messages ────────────────────────────────────────────────
                val bubbleMax = (LocalConfiguration.current.screenWidthDp * 0.78f).dp
                LazyColumn(
                    state = listState,
                    modifier = Modifier.weight(1f).fillMaxWidth(),
                    contentPadding = PaddingValues(start = 16.dp, end = 16.dp, top = 16.dp, bottom = 8.dp),
                    verticalArrangement = Arrangement.spacedBy(14.dp),
                ) {
                    item {
                        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Center) {
                            Box(
                                Modifier
                                    .clip(RoundedCornerShape(14.dp))
                                    .background(Kat.wash)
                                    .border(1.dp, Kat.hairline, RoundedCornerShape(14.dp))
                                    .padding(horizontal = 12.dp, vertical = 5.dp)
                            ) {
                                Text(
                                    modelLabel, fontFamily = PlexMono, fontSize = 11.5.sp,
                                    color = Kat.textFaint, maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                            }
                        }
                    }
                    if (current.messages.isEmpty()) {
                        item { EmptyState(agentLabel, current.mode, Modifier.fillParentMaxHeight(0.72f)) }
                    } else {
                        itemsIndexed(current.messages) { i, m ->
                            Bubble(m, agentLabel, current.mode, bubbleMax,
                                working = busy && !m.user && i == current.messages.lastIndex,
                                speaking = speakingIdx == i,
                                onSpeak = { t -> speakText(t, i) },
                                onStopSpeak = { stopSpeak() },
                                onTrace = if (current.mode == "server") ({ t -> traceOpen = t }) else null)
                        }
                    }
                }

                // ── Attachment preview (not in the prototype, otherwise the
                //    attached snapshot would be invisible) ─────────────────────
                pendingDoc?.let { doc ->
                    Column(
                        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 6.dp)
                            .clip(RoundedCornerShape(12.dp)).background(Kat.surface)
                            .border(1.dp, Kat.hairlineStrong, RoundedCornerShape(12.dp))
                            .tap { docPreviewOpen = !docPreviewOpen }
                            .padding(horizontal = 12.dp, vertical = 8.dp),
                    ) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(10.dp),
                        ) {
                            Icon(Icons.Outlined.Description, null, tint = Kat.accentText,
                                modifier = Modifier.size(20.dp))
                            Column(Modifier.weight(1f)) {
                                Text(doc.name, fontSize = 13.sp, fontFamily = Plex,
                                    color = Kat.text, maxLines = 1, overflow = TextOverflow.Ellipsis)
                                Text("${doc.text.length} characters" +
                                    (if (doc.note.isNotBlank()) " · ${doc.note}" else "") +
                                    (if (docPreviewOpen) "" else " · tap to preview"),
                                    fontSize = 11.sp, fontFamily = Plex, color = Kat.textFaint)
                            }
                            RoundIconButton({ pendingDoc = null; docPreviewOpen = false }, size = 32.dp) {
                                Icon(Icons.Filled.Close, null, tint = Kat.textDim,
                                    modifier = Modifier.size(16.dp))
                            }
                        }
                        // Aufgeklappt: der ANFANG des extrahierten Texts, scrollbar.
                        // Genau das (plus Rest) bekommt das Modell zu sehen.
                        AnimatedVisibility(docPreviewOpen) {
                            Column {
                                Hairline(Modifier.padding(vertical = 8.dp))
                                Text(
                                    doc.text.take(4000) +
                                        (if (doc.text.length > 4000) "\n…" else ""),
                                    fontSize = 11.5.sp, fontFamily = PlexMono,
                                    color = Kat.textDim, lineHeight = 15.sp,
                                    modifier = Modifier
                                        .heightIn(max = 220.dp)
                                        .verticalScroll(rememberScrollState()),
                                )
                            }
                        }
                    }
                }
                pendingImage?.let { bmp ->
                    Row(
                        Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 4.dp),
                        verticalAlignment = Alignment.CenterVertically,
                        horizontalArrangement = Arrangement.spacedBy(10.dp),
                    ) {
                        Image(
                            bmp.asImageBitmap(), "Attachment",
                            Modifier.size(40.dp).clip(RoundedCornerShape(8.dp)),
                            contentScale = ContentScale.Crop,
                        )
                        Text("Image attached", fontSize = 13.sp, fontFamily = Plex,
                            color = Kat.textDim, modifier = Modifier.weight(1f))
                        RoundIconButton({ pendingImage = null }, size = 32.dp) {
                            Icon(Icons.Filled.Close, "Remove", Modifier.size(16.dp), tint = Kat.textSubtle)
                        }
                    }
                }

                // ── Slash command suggestions (only while typing the name) ──
                // All matches, scrollable: a bare "/" lists every command and
                // every prompt template, the list scrolls instead of being cut
                // to five (the templates alone were more than that).
                val slashMatches = if (input.startsWith("/") && !input.contains(' '))
                    (SLASH_CMDS + promptCmds).filter { it.cmd.startsWith(input, ignoreCase = true) }
                else emptyList()
                if (slashMatches.isNotEmpty()) {
                    Hairline()
                    Column(
                        Modifier.fillMaxWidth().background(Kat.bg)
                            .heightIn(max = 280.dp)
                            .verticalScroll(rememberScrollState())
                            .padding(horizontal = 8.dp, vertical = 4.dp),
                    ) {
                        slashMatches.forEach { c ->
                            Row(
                                Modifier.fillMaxWidth()
                                    .clip(RoundedCornerShape(10.dp))
                                    .tap {
                                        // Prepare argument commands, send arg-free ones directly.
                                        if (c.arg) input = c.cmd + " " else { input = c.cmd; send() }
                                    }
                                    .padding(horizontal = 12.dp, vertical = 9.dp),
                                verticalAlignment = Alignment.CenterVertically,
                                horizontalArrangement = Arrangement.spacedBy(10.dp),
                            ) {
                                Text(c.cmd, fontFamily = Plex, fontSize = 14.sp,
                                    fontWeight = FontWeight.Medium, color = Kat.accentText)
                                Text(c.desc, fontFamily = Plex, fontSize = 12.5.sp,
                                    color = Kat.textSubtle, maxLines = 1,
                                    overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f))
                            }
                        }
                    }
                }

                // ── Input card: text field on top, control row below (one
                //    rounded container instead of buttons side by side) ────────
                Hairline()
                Column(
                    Modifier.fillMaxWidth().background(Kat.bg)
                        .padding(start = 12.dp, end = 12.dp, top = 10.dp, bottom = 14.dp),
                ) {
                    Column(
                        Modifier.fillMaxWidth()
                            .clip(RoundedCornerShape(26.dp))
                            .background(Kat.surface)
                            .border(1.dp, Kat.border, RoundedCornerShape(26.dp))
                            .padding(start = 18.dp, end = 12.dp, top = 12.dp, bottom = 8.dp),
                    ) {
                        val style = TextStyle(fontFamily = Plex, fontSize = 15.sp, color = Kat.text)
                        BasicTextField(
                            value = input,
                            onValueChange = { input = it },
                            textStyle = style,
                            singleLine = false,
                            maxLines = 6,
                            cursorBrush = SolidColor(Kat.accentText),
                            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Send),
                            keyboardActions = KeyboardActions(onSend = { send() }),
                            modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
                            decorationBox = { inner ->
                                if (input.isEmpty())
                                    Text("Message …", style = style.copy(color = Kat.textSubtle), maxLines = 1)
                                inner()
                            },
                        )
                        // Control row: + on the left, mic + send on the right.
                        Row(
                            Modifier.fillMaxWidth().padding(top = 6.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            RoundIconButton({ attachOpen = true }, enabled = !busy) {
                                Icon(Icons.Filled.Add, "Attach", Modifier.size(20.dp), tint = Kat.textMuted)
                            }
                            Spacer(Modifier.weight(1f))
                            RoundIconButton(
                                { micToggle() }, enabled = !transcribing,
                                background = if (recording || bargeListening) Kat.accent else Kat.tile,
                            ) {
                                Icon(
                                    if (transcribing) Icons.Filled.HourglassEmpty else Icons.Filled.Mic,
                                    if (recording || bargeListening) "Stop recording" else "Speak",
                                    Modifier.size(18.dp),
                                    tint = if (recording || bargeListening) Kat.onAccent else Kat.textMuted,
                                )
                            }
                            Spacer(Modifier.width(8.dp))
                            val canSend = (input.isNotBlank() || pendingImage != null || pendingDoc != null) || !busy
                            fun sendOrSteer() {
                                if (busy && input.isNotBlank()) {
                                    // Steering: call into the running turn instead of waiting.
                                    val t = input.trim(); input = ""
                                    current.messages.add(current.messages.size - 1,
                                        Msg(user = true, text = t))
                                    scope.launch {
                                        val ok = withContext(Dispatchers.IO) {
                                            ManagerSync.steer(prefs.serverUrl, prefs.user, prefs.pass,
                                                prefs.instance, t)
                                        }
                                        if (!ok) status = "No running turn — please send normally."
                                    }
                                } else send()
                            }
                            RoundIconButton(
                                { sendOrSteer() }, enabled = canSend && (input.isNotBlank() || pendingImage != null || pendingDoc != null || !busy),
                                background = if (canSend) Kat.accent else Kat.tile,
                            ) {
                                Icon(
                                    Icons.AutoMirrored.Filled.Send, "Send", Modifier.size(18.dp),
                                    tint = if (canSend) Kat.onAccent else Kat.textSubtle,
                                )
                            }
                        }
                    }
                }
            }

            // ── Overflow menu ───────────────────────────────────────────────
            if (menuOpen) {
                Box(Modifier.fillMaxSize().tap { menuOpen = false })
                Column(
                    Modifier
                        .align(Alignment.TopEnd)
                        .padding(top = 56.dp, end = 10.dp)
                        .width(238.dp)
                        .clip(RoundedCornerShape(14.dp))
                        .background(Kat.surface)
                        .border(1.dp, Kat.border, RoundedCornerShape(14.dp))
                        .padding(6.dp),
                ) {
                    Kicker("Model", Modifier.padding(start = 12.dp, end = 12.dp, top = 8.dp, bottom = 4.dp))
                    if (current.mode == "local") {
                        val models = remember(menuOpen) { store.models() }
                        if (models.isEmpty()) Text(
                            "No model loaded", Modifier.padding(horizontal = 12.dp, vertical = 9.dp),
                            fontSize = 12.5.sp, fontFamily = PlexMono, color = Kat.textMuted,
                        )
                        models.forEach { f ->
                            MenuModelRow(f.name, f.name == prefs.activeModel) {
                                selectModel(f); menuOpen = false
                            }
                        }
                    } else {
                        // A server agent's model is set by the manager, not the
                        // app — the row shows it but doesn't switch it.
                        MenuModelRow(modelLabel, true, null)
                    }
                    Box(Modifier.fillMaxWidth().padding(horizontal = 4.dp, vertical = 6.dp)
                        .height(1.dp).background(Kat.hairlineStrong))
                    MenuRow("Clear conversation", Kat.textDim, Icons.Outlined.DeleteSweep) {
                        current.messages.clear(); menuOpen = false; persist()
                    }
                    MenuRow("Delete chat", Kat.red, Icons.Outlined.DeleteOutline) {
                        menuOpen = false; deleteChat(current)
                    }
                }
            }

            // ── Attachment sheet ────────────────────────────────────────────
            AnimatedVisibility(attachOpen, enter = fadeIn(), exit = fadeOut()) {
                Box(Modifier.fillMaxSize().background(Kat.scrim).tap { attachOpen = false })
            }
            AnimatedVisibility(
                attachOpen,
                modifier = Modifier.align(Alignment.BottomCenter),
                enter = slideInVertically { it }, exit = slideOutVertically { it },
            ) {
                Column(
                    Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(topStart = 20.dp, topEnd = 20.dp))
                        .background(Kat.elevated)
                        .padding(start = 16.dp, end = 16.dp, top = 10.dp, bottom = 28.dp),
                ) {
                    Box(
                        Modifier.align(Alignment.CenterHorizontally).padding(bottom = 10.dp)
                            .size(36.dp, 4.dp).clip(RoundedCornerShape(2.dp))
                            .background(Color(0x26FFFFFF))
                    )
                    Kicker("Attach", Modifier.padding(start = 4.dp, bottom = 12.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                        AttachTile("Camera", Icons.Outlined.PhotoCamera, Modifier.weight(1f)) {
                            attachOpen = false; camera.launch(null)
                        }
                        AttachTile("Gallery", Icons.Outlined.PhotoLibrary, Modifier.weight(1f)) {
                            attachOpen = false; imagePicker.launch("image/*")
                        }
                        AttachTile("File", Icons.Outlined.Description, Modifier.weight(1f)) {
                            attachOpen = false
                            docPicker.launch(arrayOf(
                                "application/pdf",
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                "application/vnd.oasis.opendocument.text",
                                "text/plain", "text/markdown", "text/csv", "text/html"))
                        }
                    }
                }
            }

            // ── Full screens Tasks / Settings ───────────────────────────────
            AnimatedVisibility(
                screen == "missions",
                enter = slideInHorizontally { it }, exit = slideOutHorizontally { it },
            ) {
                MissionsScreen(prefs, onClose = { screen = null }, onStatus = { status = it })
            }

            AnimatedVisibility(
                screen == "tasks",
                enter = slideInHorizontally { it }, exit = slideOutHorizontally { it },
            ) {
                TasksScreen(
                    prefs, instances,
                    onClose = { screen = null },
                    onStatus = { status = it },
                    onOpenChat = { instance ->
                        // The manager keeps ONE task history per instance under the
                        // fixed id "task-<instance>" (chat_log_append, kind="task").
                        // Use the same id locally, so the sync merges both sides
                        // instead of duplicating.
                        val cid = "task-$instance"
                        if (conversations.none { it.id == cid }) {
                            conversations.add(0, Conversation(
                                id = cid, title = "Tasks · $instance",
                                mode = "server", instance = instance,
                                updatedAt = System.currentTimeMillis(),
                            ))
                            store.save(conversations)
                        }
                        currentId = cid
                        prefs.mode = "server"
                        prefs.instance = instance
                        screen = null
                    },
                )
            }
            AnimatedVisibility(
                screen == "settings",
                enter = slideInHorizontally { it }, exit = slideOutHorizontally { it },
            ) {
                SettingsScreen(
                    prefs, store, dl, instances = instances,
                    web = web, onWeb = { web = it; prefs.webAccess = it },
                    bargeIn = bargeIn, onBargeIn = { bargeIn = it; prefs.bargeIn = it },
                    autoUpdate = autoUpdate, onAutoUpdate = { autoUpdate = it; prefs.autoUpdate = it },
                    update = updateInfo, updateReady = updateReady != null, updateBusy = updateBusy,
                    onCheckUpdate = { checkUpdate(true) }, onInstallUpdate = { installUpdate() },
                    syncing = syncing, lastSync = lastSync, online = online,
                    onClose = { screen = null },
                    onSelectModel = { selectModel(it) },
                    onDeleteModel = { f -> f.delete(); if (prefs.activeModel == f.name) { prefs.activeModel = ""; gemma.close() } },
                    onPickModel = { picker.launch(arrayOf("application/octet-stream", "*/*")) },
                    onDownload = { url, token ->
                        DownloadService.start(context, url, token); status = "Download starting… (background)"
                    },
                    onManageAgents = { showAgents = true },
                    onSync = { sync() },
                )
            }
        }
    }

    traceOpen?.let { t ->
        TraceDialog(prefs, current.instance.ifBlank { prefs.instance }, t, onDismiss = { traceOpen = null })
    }
    if (showAgents) {
        ServerAgentsDialog(prefs, onDismiss = { showAgents = false }, onStatus = { status = it })
    }
}

// ── Chat building blocks ────────────────────────────────────────────────────

@Composable
private fun AgentChip(
    label: String,
    selected: Boolean,
    onClick: () -> Unit,
    icon: @Composable (Color) -> Unit,
) {
    val fg = if (selected) Kat.accentBright else Kat.textMuted
    Row(
        Modifier
            .clip(RoundedCornerShape(18.dp))
            .background(if (selected) Kat.chipSel else Color.Transparent)
            .border(1.dp, if (selected) Kat.borderFocus else Kat.border, RoundedCornerShape(18.dp))
            .tap { onClick() }
            .padding(horizontal = 14.dp, vertical = 7.dp),
        horizontalArrangement = Arrangement.spacedBy(7.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        icon(fg)
        Text(
            label, fontSize = 13.5.sp, fontFamily = Plex,
            fontWeight = if (selected) FontWeight.SemiBold else FontWeight.Medium,
            color = fg, maxLines = 1,
        )
    }
}

@Composable
private fun MenuModelRow(label: String, selected: Boolean, onClick: (() -> Unit)?) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .then(if (onClick != null) Modifier.tap { onClick() } else Modifier)
            .padding(horizontal = 12.dp, vertical = 9.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            label, Modifier.weight(1f), fontSize = 12.5.sp, fontFamily = PlexMono,
            color = if (selected) Kat.accentBright else Kat.textMuted,
            maxLines = 1, overflow = TextOverflow.Ellipsis,
        )
        if (selected) Icon(Icons.Filled.Check, null, Modifier.size(14.dp), tint = Kat.accentText)
    }
}

@Composable
private fun MenuRow(
    label: String,
    color: Color,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    onClick: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(9.dp))
            .tap { onClick() }
            .padding(horizontal = 12.dp, vertical = 10.dp),
        horizontalArrangement = Arrangement.spacedBy(10.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, null, Modifier.size(16.dp), tint = color)
        Text(label, fontSize = 14.sp, fontFamily = Plex, color = color, maxLines = 1)
    }
}

@Composable
private fun AttachTile(
    label: String,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    modifier: Modifier = Modifier,
    onClick: () -> Unit,
) {
    Column(
        modifier
            .clip(RoundedCornerShape(16.dp))
            .background(Kat.surface)
            .border(1.dp, Kat.border, RoundedCornerShape(16.dp))
            .tap { onClick() }
            .padding(horizontal = 8.dp, vertical = 16.dp),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Icon(icon, null, Modifier.size(24.dp), tint = Kat.accentText)
        Text(label, fontSize = 13.sp, fontFamily = Plex, fontWeight = FontWeight.Medium, color = Kat.textDim)
    }
}

/** 28-dp tile to the left of the agent message. */
@Composable
private fun AgentAvatar(mode: String, size: androidx.compose.ui.unit.Dp = 28.dp) {
    Box(
        Modifier
            .size(size)
            .clip(RoundedCornerShape(size / 3.5f))
            .background(Kat.tile)
            .border(1.dp, Kat.tileBorder, RoundedCornerShape(size / 3.5f)),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            if (mode == "server") Icons.Filled.Cloud else Icons.Filled.PhoneAndroid,
            null, Modifier.size(size / 2), tint = Kat.accentText,
        )
    }
}

@Composable
fun EmptyState(agent: String, mode: String, modifier: Modifier = Modifier) {
    Column(
        modifier.fillMaxWidth(),
        horizontalAlignment = Alignment.CenterHorizontally,
        verticalArrangement = Arrangement.spacedBy(12.dp, Alignment.CenterVertically),
    ) {
        Box(
            Modifier.size(52.dp).clip(RoundedCornerShape(16.dp)).background(Kat.tile)
                .border(1.dp, Kat.tileBorder, RoundedCornerShape(16.dp)),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                if (mode == "server") Icons.Filled.Cloud else Icons.Filled.PhoneAndroid,
                null, Modifier.size(24.dp), tint = Kat.accentText,
            )
        }
        Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(5.dp)) {
            Text("New conversation with", fontSize = 14.sp, fontFamily = Plex, color = Kat.textSubtle)
            Text(agent, fontSize = 13.sp, fontFamily = PlexMono, color = Kat.accentText,
                maxLines = 1, overflow = TextOverflow.Ellipsis)
        }
    }
}

/** Three dots pulsing in sequence plus the seconds since the turn started —
 *  the app's "working in the background" cue while no token has arrived yet. */
@Composable
fun WorkingIndicator() {
    val t = rememberInfiniteTransition(label = "dots")
    var secs by remember { mutableStateOf(0) }
    LaunchedEffect(Unit) {
        val start = System.currentTimeMillis()
        while (true) { delay(1000); secs = ((System.currentTimeMillis() - start) / 1000).toInt() }
    }
    Row(verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(6.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
            repeat(3) { i ->
                val a by t.animateFloat(
                    initialValue = 0.25f, targetValue = 1f,
                    animationSpec = infiniteRepeatable(
                        animation = tween(900, delayMillis = i * 180, easing = LinearEasing),
                        repeatMode = RepeatMode.Reverse), label = "dot$i")
                Box(Modifier.size(7.dp).alpha(a).clip(RoundedCornerShape(4.dp)).background(Kat.accentText))
            }
        }
        if (secs >= 2) Text("working · ${secs}s", fontSize = 11.5.sp, fontFamily = Plex, color = Kat.textSubtle)
    }
}


@Composable
fun Bubble(
    m: Msg, agentLabel: String, mode: String, maxWidth: androidx.compose.ui.unit.Dp,
    working: Boolean = false,      // the model is still producing this reply
    speaking: Boolean = false,
    onSpeak: (String) -> Unit = {},
    onStopSpeak: () -> Unit = {},
    onTrace: ((String) -> Unit)? = null,     // opens the turn's trace (server chats)
) {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = if (m.user) Arrangement.End else Arrangement.Start,
        verticalAlignment = Alignment.Top,
    ) {
        if (!m.user) {
            AgentAvatar(mode)
            Spacer(Modifier.width(10.dp))
        }
        Column(
            horizontalAlignment = if (m.user) Alignment.End else Alignment.Start,
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            val th = splitThink(m.text)
            val tl = if (m.user) ToolSplit(emptyList(), m.text) else splitTools(th.answer)
            val body = if (m.user) m.text else tl.rest
            if (!m.user && th.think.isNotBlank()) {
                var open by remember { mutableStateOf(false) }
                val show = th.streaming || open
                Row(
                    Modifier.clip(RoundedCornerShape(10.dp)).tap { open = !open }
                        .padding(horizontal = 6.dp, vertical = 3.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(5.dp),
                ) {
                    Text(if (show) "\u25BE" else "\u25B8", fontSize = 11.sp, color = Kat.textSubtle)
                    Text(if (th.streaming) "Thinking \u2026" else "Thinking", fontSize = 11.5.sp,
                        fontFamily = Plex, color = Kat.textSubtle)
                }
                if (show) SelectionContainer {
                    Text(th.think, fontSize = 13.sp, lineHeight = 19.sp, fontFamily = Plex,
                        color = Kat.textMuted,
                        modifier = Modifier.widthIn(max = maxWidth).padding(start = 6.dp, bottom = 2.dp))
                }
            }
            if (!m.user && tl.tools.isNotEmpty()) {
                var open by remember { mutableStateOf(false) }
                val last = tl.tools.last()
                Row(
                    Modifier.clip(RoundedCornerShape(10.dp)).tap { open = !open }
                        .padding(horizontal = 6.dp, vertical = 3.dp),
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(5.dp),
                ) {
                    Text(if (open) "\u25BE" else "\u25B8", fontSize = 11.sp, color = Kat.textSubtle)
                    Text("Tools \u00B7 ${tl.tools.size}" + (if (!open) "  \u00B7  $last" else ""),
                        fontSize = 11.5.sp, fontFamily = Plex, color = Kat.textSubtle,
                        maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                if (open) Column(Modifier.widthIn(max = maxWidth).padding(start = 6.dp, bottom = 2.dp),
                    verticalArrangement = Arrangement.spacedBy(2.dp)) {
                    tl.tools.forEach { t ->
                        Text("\uD83D\uDD27 $t", fontSize = 12.sp, lineHeight = 17.sp, fontFamily = PlexMono,
                            color = Kat.textMuted)
                    }
                }
            }
            val shape = if (m.user)
                RoundedCornerShape(topStart = 18.dp, topEnd = 18.dp, bottomEnd = 4.dp, bottomStart = 18.dp)
            else
                RoundedCornerShape(topStart = 4.dp, topEnd = 18.dp, bottomEnd = 18.dp, bottomStart = 18.dp)
            Box(
                Modifier
                    .widthIn(max = maxWidth)
                    .clip(shape)
                    .background(if (m.user) Kat.accent else Kat.surface)
                    // While speaking, the whole bubble is the stop button — the
                    // 15-dp speaker icon is hard to hit when you want to get rid
                    // of the read-aloud quickly.
                    // While speaking: whole bubble = stop button. Otherwise NO tap
                    // on the bubble -> the text stays selectable (speaking runs via
                    // the speaker icon below anyway).
                    .then(
                        if (speaking) Modifier.border(1.dp, Kat.accent, shape).tap { onStopSpeak() }
                        else if (m.user) Modifier
                        else Modifier.border(1.dp, Kat.hairlineStrong, shape)
                    )
                    .padding(horizontal = 16.dp, vertical = 11.dp)
            ) {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    if (m.user && m.image != null) {
                        val thumb = remember(m.image) {
                            runCatching {
                                val b = android.util.Base64.decode(m.image, android.util.Base64.DEFAULT)
                                BitmapFactory.decodeByteArray(b, 0, b.size)
                            }.getOrNull()
                        }
                        thumb?.let {
                            Image(
                                it.asImageBitmap(), "Image",
                                Modifier.heightIn(max = 180.dp).widthIn(max = maxWidth).clip(RoundedCornerShape(10.dp)),
                                contentScale = ContentScale.Fit,
                            )
                        }
                    }
                    val hasImg = m.user && m.image != null
                    if (!m.user && working && body.isBlank()) {
                        // Nothing has streamed yet: a local model chews on the
                        // prompt for half a minute before the first token, so a
                        // static "…" reads as dead. Animated dots + a live
                        // elapsed counter show it is working in the background.
                        WorkingIndicator()
                    } else if (!hasImg || body.isNotBlank()) SelectionContainer {
                        if (m.user) Text(
                            if (hasImg) body else body.ifEmpty { "…" },
                            fontSize = 15.sp, lineHeight = 22.5.sp, fontFamily = Plex, color = Kat.onAccent,
                        ) else Text(
                            if (body.isEmpty()) AnnotatedString("…") else mdAnnotated(body),
                            fontSize = 15.sp, lineHeight = 22.5.sp, fontFamily = Plex,
                            color = if (body.isEmpty()) Kat.textFaint else Kat.textStrong,
                        )
                    }
                }
            }
            // The prototype puts "time · agent" here. Msg carries no time (and
            // must not get one, otherwise the chat sync breaks), so the agent
            // name stays.
            if (!m.user) Row(
                Modifier.padding(horizontal = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(agentLabel, fontSize = 11.sp, fontFamily = Plex,
                    color = Kat.textSubtle, maxLines = 1)
                if (th.answer.isNotBlank()) Icon(
                    if (speaking) Icons.Filled.StopCircle else Icons.Outlined.VolumeUp,
                    if (speaking) "Stop speaking" else "Read aloud",
                    Modifier.size(15.dp).tap { if (speaking) onStopSpeak() else onSpeak(tl.rest) },
                    tint = if (speaking) Kat.accent else Kat.textGhost,
                )
                // The turn's trace (LLM calls, tool calls, durations) — the
                // bridge named the turn in a response header, the manager has it.
                if (m.turn != null && onTrace != null && !th.streaming) Text(
                    "⟲ trace", fontSize = 11.sp, fontFamily = Plex, color = Kat.textSubtle,
                    modifier = Modifier.clip(RoundedCornerShape(6.dp)).tap { onTrace(m.turn) }
                        .padding(horizontal = 4.dp, vertical = 1.dp),
                )
            }
        }
    }
}

// ── Drawer ──────────────────────────────────────────────────────────────────

@Composable
fun KatDrawer(
    conversations: List<Conversation>,
    currentId: String,
    agentCount: Int,
    online: Boolean,
    onSelect: (String) -> Unit,
    onNew: () -> Unit,
    onDelete: (Conversation) -> Unit,
    onTasks: () -> Unit,
    onMissions: () -> Unit,
    onSettings: () -> Unit,
) {
    // From the package rather than BuildConfig: this way it always shows the
    // version actually installed — just like in the settings.
    val ctx = LocalContext.current
    val appVersion = remember {
        try { ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "" } catch (e: Exception) { "" }
    }
    ModalDrawerSheet(
        modifier = Modifier.fillMaxWidth(0.82f),
        drawerShape = RoundedCornerShape(0.dp),
        drawerContainerColor = Kat.elevated,
    ) {
        Row(
            Modifier.fillMaxWidth().padding(start = 20.dp, end = 20.dp, top = 20.dp, bottom = 16.dp),
            horizontalArrangement = Arrangement.spacedBy(14.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Box(
                Modifier.size(44.dp).clip(RoundedCornerShape(12.dp))
                    .background(Brush.linearGradient(listOf(Color(0xFF2D5C96), Color(0xFF1E3D63)))),
                contentAlignment = Alignment.Center,
            ) {
                Text("K", fontSize = 19.sp, fontWeight = FontWeight.Bold, fontFamily = Plex, color = Color(0xFFEAF2FB))
            }
            Column(Modifier.weight(1f)) {
                Row(
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                    verticalAlignment = Alignment.Bottom,
                ) {
                    Text("KatAgent", style = MaterialTheme.typography.titleLarge, color = Kat.text)
                    // Smaller and muted: the version should be readable without
                    // competing with the name for the line.
                    if (appVersion.isNotBlank()) Text(
                        appVersion, fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint,
                        modifier = Modifier.padding(bottom = 2.dp),
                    )
                }
                Text(
                    "$agentCount agents · ${if (online) "connected" else "offline"}",
                    fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint,
                )
            }
        }
        Box(Modifier.padding(start = 16.dp, end = 16.dp, bottom = 8.dp)) {
            FilledPill(
                "New chat", onNew, Modifier.fillMaxWidth(), height = 46.dp,
                leading = { Icon(Icons.Filled.Add, null, Modifier.size(17.dp), tint = Kat.onAccent) },
            )
        }
        Kicker("History", Modifier.padding(start = 20.dp, end = 20.dp, top = 16.dp, bottom = 6.dp))
        LazyColumn(
            Modifier.weight(1f).padding(horizontal = 8.dp),
            verticalArrangement = Arrangement.spacedBy(2.dp),
        ) {
            items(conversations, key = { it.id }) { c ->
                Row(
                    Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(12.dp))
                        .background(if (c.id == currentId) Kat.rowSel else Color.Transparent)
                        .tap { onSelect(c.id) }
                        .padding(horizontal = 12.dp, vertical = 10.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Column(Modifier.weight(1f)) {
                        Text(
                            c.title, fontSize = 14.5.sp, fontFamily = Plex, fontWeight = FontWeight.Medium,
                            color = Kat.textStrong, maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                        val a = agentOf(c)
                        Text(
                            a, Modifier.padding(top = 2.dp), fontSize = 12.sp, fontFamily = PlexMono,
                            color = Kat.agent(a), maxLines = 1, overflow = TextOverflow.Ellipsis,
                        )
                    }
                    RoundIconButton({ onDelete(c) }, size = 32.dp) {
                        Icon(Icons.Outlined.DeleteOutline, "Delete", Modifier.size(16.dp), tint = Kat.textSubtle)
                    }
                }
            }
        }
        Hairline()
        Column(Modifier.padding(8.dp), verticalArrangement = Arrangement.spacedBy(1.dp)) {
            DrawerAction("Tasks", Icons.Outlined.Checklist, onTasks)
            DrawerAction("Missions", Icons.Outlined.Flag, onMissions)
            DrawerAction("Settings", Icons.Outlined.Settings, onSettings)
        }
    }
}

@Composable
private fun DrawerAction(
    label: String,
    icon: androidx.compose.ui.graphics.vector.ImageVector,
    onClick: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .tap { onClick() }
            .padding(horizontal = 12.dp, vertical = 11.dp),
        horizontalArrangement = Arrangement.spacedBy(14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(icon, null, Modifier.size(18.dp), tint = Kat.textDim)
        Text(label, fontSize = 14.5.sp, fontFamily = Plex, fontWeight = FontWeight.Medium, color = Kat.textDim)
    }
}

/** Header of the two full screens: back arrow + title + hairline. */
@Composable
private fun ScreenHeader(title: String, onClose: () -> Unit) {
    Column {
        Row(
            Modifier.fillMaxWidth().padding(start = 8.dp, end = 8.dp, top = 8.dp, bottom = 4.dp),
            horizontalArrangement = Arrangement.spacedBy(4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            RoundIconButton(onClose) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back", Modifier.size(20.dp), tint = Kat.textDim)
            }
            Text(
                title, Modifier.weight(1f).padding(start = 4.dp, top = 8.dp, bottom = 8.dp),
                style = MaterialTheme.typography.titleMedium, color = Kat.text,
            )
        }
        Hairline()
    }
}

// ── Tasks ───────────────────────────────────────────────────────────────────

@Composable
private fun MissionsScreen(
    prefs: Prefs,
    onClose: () -> Unit,
    onStatus: (String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    var missions by remember { mutableStateOf<List<ManagerSync.Mission>>(emptyList()) }
    var reload by remember { mutableStateOf(0) }
    var mExpanded by remember { mutableStateOf("") }

    LaunchedEffect(reload) {
        withContext(Dispatchers.IO) { ManagerSync.listMissions(prefs.serverUrl, prefs.user, prefs.pass) }
            ?.let { missions = it }
            ?: onStatus("⚠️ Could not load missions: ${ManagerSync.lastStatus}")
    }
    LaunchedEffect(Unit) { while (true) { delay(5000); reload++ } }

    @Composable
    fun MissionCard(m: ManagerSync.Mission) {
        val total = m.steps.size.coerceAtLeast(1)
        val done = m.steps.count { it.status == "done" }
        val cur = m.steps.firstOrNull { it.status == "doing" }
            ?: m.steps.firstOrNull { it.status == "open" }
        val closed = m.status == "done" || m.status == "failed"
        Column(
            Modifier.fillMaxWidth()
                .clip(RoundedCornerShape(14.dp))
                .background(Kat.surface)
                .border(1.dp, Kat.hairlineStrong, RoundedCornerShape(14.dp))
                .tap { mExpanded = if (mExpanded == m.id) "" else m.id }
                .padding(horizontal = 16.dp, vertical = 14.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Text(m.goal, fontSize = 14.5.sp, fontFamily = Plex,
                    fontWeight = FontWeight.Medium,
                    color = if (closed) Kat.textDim else Kat.text,
                    maxLines = 2, overflow = TextOverflow.Ellipsis,
                    modifier = Modifier.weight(1f))
                if (m.instance.isNotBlank())
                    Text(m.instance, fontSize = 11.sp, fontFamily = Plex, color = Kat.textFaint)
                Text(when (m.status) {
                    "paused" -> "paused"; "done" -> "done"; "failed" -> "failed"
                    else -> "active"
                }, fontSize = 11.sp, fontFamily = Plex,
                    color = if (m.status == "active") Kat.accentText else Kat.textFaint)
            }
            Row(verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                Box(Modifier.weight(1f).height(5.dp).clip(RoundedCornerShape(3.dp))
                    .background(Kat.tile)) {
                    Box(Modifier.fillMaxHeight()
                        .fillMaxWidth(done.toFloat() / total)
                        .background(if (m.status == "failed") Kat.textFaint else Kat.accent))
                }
                Text("$done/$total", fontSize = 11.5.sp, fontFamily = Plex, color = Kat.textFaint)
            }
            if (!closed) cur?.let {
                val on = if (it.target.isNotBlank()) " · on ${it.target}" else ""
                Text("Step ${it.n}: ${it.text}$on", fontSize = 12.5.sp,
                    fontFamily = Plex, color = Kat.textDim,
                    maxLines = if (mExpanded == m.id) 4 else 1,
                    overflow = TextOverflow.Ellipsis)
            }
            if (m.summary.isNotBlank())
                Text(m.summary, fontSize = 12.sp, fontFamily = Plex, color = Kat.textDim,
                    maxLines = if (mExpanded == m.id) 6 else 2, overflow = TextOverflow.Ellipsis)
            if (mExpanded == m.id) {
                m.steps.forEach { st ->
                    val mark = when (st.status) {
                        "done" -> "✓"; "doing" -> "◔"; "failed" -> "✕"; else -> "○"
                    }
                    val on = if (st.target.isNotBlank()) "  @${st.target}" else ""
                    Text("$mark  ${st.n}. ${st.text}$on", fontSize = 12.sp, fontFamily = Plex,
                        color = if (st.status == "done") Kat.textFaint else Kat.textDim)
                }
                if (m.lastLog.isNotBlank())
                    Text(m.lastLog, fontSize = 11.sp, fontFamily = Plex, color = Kat.textFaint)
                if (!closed) Row(horizontalArrangement = Arrangement.spacedBy(16.dp)) {
                    fun act(a: String) {
                        scope.launch {
                            withContext(Dispatchers.IO) {
                                ManagerSync.missionAction(prefs.serverUrl, prefs.user, prefs.pass,
                                    m.id, a, m.instance)
                            }
                            reload++
                        }
                    }
                    if (m.status == "active")
                        Text("Pause", fontSize = 12.5.sp, fontFamily = Plex,
                            color = Kat.accentText, modifier = Modifier.tap { act("pause") })
                    if (m.status == "paused")
                        Text("Resume", fontSize = 12.5.sp, fontFamily = Plex,
                            color = Kat.accentText, modifier = Modifier.tap { act("resume") })
                    Text("Cancel", fontSize = 12.5.sp, fontFamily = Plex,
                        color = Kat.textFaint, modifier = Modifier.tap { act("abort") })
                }
            }
        }
    }

    Column(Modifier.fillMaxSize().background(Kat.bg)) {
        ScreenHeader("Missions", onClose)
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            val open = missions.filter { it.status == "active" || it.status == "paused" }
            val closed = missions.filter { it.status == "done" || it.status == "failed" }.takeLast(5)
            if (missions.isEmpty()) Text(
                "No missions. Any agent creates them itself for multi-step jobs " +
                "— e.g. via chat: \"… — as a mission\".",
                fontSize = 13.sp, fontFamily = Plex, color = Kat.textFaint,
            )
            open.forEach { MissionCard(it) }
            if (closed.isNotEmpty()) {
                Kicker("Recently completed")
                closed.reversed().forEach { MissionCard(it) }
            }
        }
    }
}

@Composable
fun TasksScreen(
    prefs: Prefs,
    instances: List<AgentInstance>,
    onClose: () -> Unit,
    onStatus: (String) -> Unit,
    onOpenChat: (String) -> Unit,
) {
    val scope = rememberCoroutineScope()
    var tasks by remember { mutableStateOf<List<AgentTask>>(emptyList()) }
    var loading by remember { mutableStateOf(false) }
    var reload by remember { mutableStateOf(0) }
    var expanded by remember { mutableStateOf("") }
    val targets = if (instances.isNotEmpty()) instances.map { it.name }
        else if (prefs.instance.isNotBlank()) listOf(prefs.instance) else emptyList()
    var target by remember { mutableStateOf(prefs.instance.ifBlank { targets.firstOrNull() ?: "" }) }
    var message by remember { mutableStateOf("") }
    var schedule by remember { mutableStateOf("") }

    LaunchedEffect(reload) {
        loading = true
        val j = withContext(Dispatchers.IO) { ManagerSync.listTasks(prefs.serverUrl, prefs.user, prefs.pass) }
        loading = false
        if (j == null) onStatus("⚠️ Could not load tasks: ${ManagerSync.lastStatus}")
        else { tasks = ManagerSync.parseTasks(j); onStatus("") }
    }
    LaunchedEffect(Unit) { while (true) { delay(5000); reload++ } }

    Column(Modifier.fillMaxSize().background(Kat.bg)) {
        ScreenHeader("Tasks", onClose)
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp, vertical = 12.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            // The prototype only shows the list. But without this field no task
            // could be created in the app anymore (only via /task).
            KatCard {
                Kicker("New task")
                if (targets.isEmpty()) {
                    Text("No server agent available.", fontSize = 13.sp, fontFamily = Plex, color = Kat.textFaint)
                } else {
                    Row(
                        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                        horizontalArrangement = Arrangement.spacedBy(8.dp),
                    ) {
                        targets.forEach { n ->
                            AgentChip(n, target == n, { target = n }) {
                                Icon(Icons.Filled.Cloud, null, Modifier.size(14.dp), tint = it)
                            }
                        }
                    }
                    KatField(message, { message = it }, placeholder = "Task")
                    KatField(schedule, { schedule = it }, placeholder = "Schedule (empty = one-off)", mono = true)
                    FilledPill(
                        if (schedule.isBlank()) "Start in background" else "Create schedule",
                        {
                            val m = message.trim()
                            if (m.isNotBlank() && target.isNotBlank()) {
                                onStatus("Creating task…")
                                scope.launch {
                                    val r = withContext(Dispatchers.IO) {
                                        ManagerSync.createTask(prefs.serverUrl, prefs.user, prefs.pass, target, m, schedule.trim())
                                    }
                                    message = ""
                                    onStatus(r?.let { "" } ?: "⚠️ ${ManagerSync.lastStatus}")
                                    reload++
                                }
                            }
                        },
                        Modifier.fillMaxWidth(),
                        enabled = message.isNotBlank() && target.isNotBlank(),
                    )
                }
            }

            if (tasks.isEmpty() && !loading) Text(
                "No tasks yet.", Modifier.padding(top = 4.dp),
                fontSize = 13.sp, fontFamily = Plex, color = Kat.textFaint,
            )
            tasks.forEach { t ->
                val done = t.status == "done"
                Column(
                    Modifier
                        .fillMaxWidth()
                        .clip(RoundedCornerShape(14.dp))
                        .background(Kat.surface)
                        .border(1.dp, Kat.hairlineStrong, RoundedCornerShape(14.dp))
                        // First tap expands, the second jumps into the instance's
                        // task chat — where the result can be discussed further
                        // directly.
                        .tap { if (expanded == t.id) onOpenChat(t.instance) else expanded = t.id }
                        .padding(horizontal = 16.dp, vertical = 14.dp),
                ) {
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(14.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        // Display, not a toggle: the manager has no "check off",
                        // only the status of the run.
                        Box(
                            Modifier.size(24.dp).clip(CircleShape)
                                .background(if (done) Kat.green else Color.Transparent)
                                .border(2.dp, if (done) Kat.green else Kat.textGhost, CircleShape),
                            contentAlignment = Alignment.Center,
                        ) {
                            if (done) Icon(Icons.Filled.Check, null, Modifier.size(12.dp), tint = Kat.bg)
                        }
                        Column(Modifier.weight(1f)) {
                            Text(
                                t.message, fontSize = 14.5.sp, fontFamily = Plex, fontWeight = FontWeight.Medium,
                                color = if (done) Kat.textSubtle else Kat.textStrong,
                                textDecoration = if (done) TextDecoration.LineThrough else TextDecoration.None,
                                maxLines = 2, overflow = TextOverflow.Ellipsis,
                            )
                            Text(
                                t.instance + " · " + t.schedule.ifBlank { "one-off" },
                                Modifier.padding(top = 3.dp),
                                fontSize = 12.sp, fontFamily = PlexMono, color = Kat.agent(t.instance),
                                maxLines = 1, overflow = TextOverflow.Ellipsis,
                            )
                        }
                        val (bg, fg, bd) = when {
                            done -> Triple(Color(0x1A4CC38A), Kat.green, Color(0x404CC38A))
                            t.status == "error" -> Triple(Color(0x1AE06C75), Kat.red, Color(0x40E06C75))
                            t.status == "running" || t.status == "pending" ->
                                Triple(Color(0x1A7FB0E8), Kat.accentText, Color(0x4D7FB0E8))
                            else -> Triple(Kat.hover, Kat.textMuted, Color(0x1AFFFFFF))
                        }
                        StatusBadge(t.status, bg, fg, bd)
                        RoundIconButton({
                            scope.launch {
                                withContext(Dispatchers.IO) { ManagerSync.deleteTask(prefs.serverUrl, prefs.user, prefs.pass, t.id) }
                                reload++
                            }
                        }, size = 32.dp) {
                            Icon(Icons.Outlined.DeleteOutline, "Delete", Modifier.size(16.dp), tint = Kat.textSubtle)
                        }
                    }
                    if (expanded == t.id) {
                        // Above, the task is truncated to two lines — expanded it
                        // belongs in full.
                        if (t.message.length > 90) Text(
                            t.message, Modifier.padding(top = 10.dp),
                            fontSize = 13.sp, fontFamily = Plex, color = Kat.textSubtle,
                        )
                        if (t.result.isNotBlank()) Text(
                            t.result, Modifier.padding(top = 10.dp),
                            fontSize = 13.sp, fontFamily = Plex, color = Kat.textFaint,
                        )
                        Row(
                            Modifier.padding(top = 12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(6.dp),
                        ) {
                            Icon(Icons.Outlined.ChatBubbleOutline, null,
                                Modifier.size(14.dp), tint = Kat.accentText)
                            Text(
                                "Tap again: continue in the chat with @${t.instance}",
                                fontSize = 12.sp, fontFamily = Plex, color = Kat.accentText,
                            )
                        }
                    }
                }
            }
        }
    }
}

// ── Settings ────────────────────────────────────────────────────────────────

@Composable
fun SettingsScreen(
    prefs: Prefs,
    store: ChatStore,
    dl: Dl,
    instances: List<AgentInstance>,
    web: Boolean,
    onWeb: (Boolean) -> Unit,
    bargeIn: Boolean,
    onBargeIn: (Boolean) -> Unit,
    autoUpdate: Boolean,
    onAutoUpdate: (Boolean) -> Unit,
    update: AppUpdate.Release?,
    updateReady: Boolean,
    updateBusy: Boolean,
    onCheckUpdate: () -> Unit,
    onInstallUpdate: () -> Unit,
    syncing: Boolean,
    lastSync: String,
    online: Boolean,
    onClose: () -> Unit,
    onSelectModel: (File) -> Unit,
    onDeleteModel: (File) -> Unit,
    onPickModel: () -> Unit,
    onDownload: (String, String) -> Unit,
    onManageAgents: () -> Unit,
    onSync: () -> Unit,
) {
    val ctx = LocalContext.current
    val clipboard = LocalClipboardManager.current
    val ver = remember {
        try { ctx.packageManager.getPackageInfo(ctx.packageName, 0).versionName ?: "" } catch (e: Exception) { "" }
    }
    var url by remember { mutableStateOf(prefs.serverUrl.removePrefix("iroh://")) }
    var myNodeId by remember { mutableStateOf("") }
    LaunchedEffect(Unit) { myNodeId = withContext(Dispatchers.IO) { IrohNet.myNodeId(prefs.appContext) } }
    var instance by remember { mutableStateOf(prefs.instance) }
    var assistInstance by remember { mutableStateOf(prefs.assistInstance) }
    var user by remember { mutableStateOf(prefs.user) }
    var pass by remember { mutableStateOf(prefs.pass) }
    var token by remember { mutableStateOf(prefs.hfToken) }
    var modelUrl by remember { mutableStateOf(prefs.modelUrl) }
    var models by remember { mutableStateOf(store.models()) }
    var copied by remember { mutableStateOf(false) }
    // The prototype has no save button: the fields write directly.
    LaunchedEffect(copied) { if (copied) { delay(2000); copied = false } }
    // After a finished download the model appears in the list.
    LaunchedEffect(dl) { models = store.models() }

    Column(Modifier.fillMaxSize().background(Kat.bg)) {
        ScreenHeader("Settings", onClose)
        Column(
            Modifier.weight(1f).verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(20.dp),
        ) {
            // ── Models (on-device) ─────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("Models (on-device)", Modifier.padding(horizontal = 4.dp))
                KatCard(padding = PaddingValues(4.dp), spacing = 0.dp) {
                    if (models.isEmpty()) Text(
                        "No model loaded yet.", Modifier.padding(horizontal = 12.dp, vertical = 14.dp),
                        fontSize = 13.sp, fontFamily = Plex, color = Kat.textFaint,
                    )
                    models.forEach { f ->
                        val sel = prefs.activeModel == f.name
                        Row(
                            Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
                                .tap { onSelectModel(f) }
                                .padding(horizontal = 12.dp, vertical = 11.dp),
                            horizontalArrangement = Arrangement.spacedBy(12.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Box(
                                Modifier.size(20.dp).clip(CircleShape)
                                    .border(2.dp, if (sel) Kat.accentText else Kat.textGhost, CircleShape),
                                contentAlignment = Alignment.Center,
                            ) {
                                if (sel) Box(Modifier.size(10.dp).clip(CircleShape).background(Kat.accentText))
                            }
                            Column(Modifier.weight(1f)) {
                                Text(f.name, fontSize = 13.sp, fontFamily = PlexMono, color = Kat.textStrong)
                                Text(
                                    "${f.length() / 1_000_000} MB", Modifier.padding(top = 2.dp),
                                    fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint,
                                )
                            }
                            RoundIconButton({ onDeleteModel(f); models = store.models() }, size = 36.dp) {
                                Icon(Icons.Outlined.DeleteOutline, "Delete", Modifier.size(17.dp), tint = Kat.textSubtle)
                            }
                        }
                    }
                    (dl as? Dl.Progress)?.let { p ->
                        val pct = if (p.total > 0) (p.done * 100 / p.total).toInt() else 0
                        Column(
                            Modifier.padding(horizontal = 12.dp, vertical = 11.dp),
                            verticalArrangement = Arrangement.spacedBy(7.dp),
                        ) {
                            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                                Text(
                                    modelUrl.substringAfterLast('/'), Modifier.weight(1f),
                                    fontSize = 12.sp, fontFamily = PlexMono, color = Kat.textFaint,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis,
                                )
                                Text("$pct%", fontSize = 12.sp, fontFamily = PlexMono, color = Kat.textFaint)
                            }
                            Box(Modifier.fillMaxWidth().height(5.dp).clip(RoundedCornerShape(3.dp)).background(Kat.bg)) {
                                Box(Modifier.fillMaxWidth(pct / 100f).height(5.dp)
                                    .clip(RoundedCornerShape(3.dp)).background(Kat.accent))
                            }
                        }
                    }
                }
            }

            // ── Server connection ──────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("Server connection", Modifier.padding(horizontal = 4.dp))
                KatCard {
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        LabeledField("Manager node-id", url, {
                            url = it
                            val id = it.trim().removePrefix("iroh://").lowercase()
                            prefs.serverUrl = if (id.isBlank()) "" else "iroh://$id"
                        }, Modifier.weight(1f), mono = true)
                        StatusBadge(
                            if (online) "connected" else "offline",
                            if (online) Color(0x1A4CC38A) else Kat.hover,
                            if (online) Kat.green else Kat.textMuted,
                            if (online) Color(0x404CC38A) else Color(0x1AFFFFFF),
                            Modifier.padding(top = 20.dp),
                        )
                    }
                    Text(
                        "Paste the manager node-id from the web UI (Sharing → App transport). " +
                            "This device: ${if (myNodeId.isBlank()) "…" else myNodeId} — add it to the manager's allowlist to pair.",
                        fontSize = 11.sp, fontFamily = PlexMono, color = Kat.textFaint,
                        modifier = Modifier.padding(horizontal = 4.dp),
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        LabeledField("User", user, { user = it; prefs.user = it.trim() }, Modifier.weight(1f))
                        LabeledField("Password", pass, { pass = it; prefs.pass = it }, Modifier.weight(1f), password = true)
                    }
                    LabeledField("Active instance", instance, { instance = it; prefs.instance = it.trim() }, mono = true)
                    Column(verticalArrangement = Arrangement.spacedBy(5.dp)) {
                        Text("Assist button opens instance", fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint)
                        var assistMenu by remember { mutableStateOf(false) }
                        Box {
                            Row(
                                Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).background(Kat.tile)
                                    .tap { assistMenu = true }.padding(horizontal = 14.dp, vertical = 13.dp),
                                horizontalArrangement = Arrangement.SpaceBetween,
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                Text(
                                    assistInstance.ifBlank { "\u2014 active instance \u2014" },
                                    fontSize = 13.sp, fontFamily = PlexMono,
                                    color = if (assistInstance.isBlank()) Kat.textFaint else Kat.textStrong,
                                    maxLines = 1, overflow = TextOverflow.Ellipsis, modifier = Modifier.weight(1f),
                                )
                                Text("\u25be", fontSize = 15.sp, fontFamily = Plex, color = Kat.textSubtle)
                            }
                            DropdownMenu(expanded = assistMenu, onDismissRequest = { assistMenu = false }) {
                                DropdownMenuItem(
                                    text = { Text("\u2014 active instance \u2014", fontFamily = Plex, fontSize = 13.sp) },
                                    onClick = { assistInstance = ""; prefs.assistInstance = ""; assistMenu = false },
                                )
                                instances.forEach { inst ->
                                    DropdownMenuItem(
                                        text = { Text(inst.name, fontFamily = PlexMono, fontSize = 13.sp) },
                                        onClick = { assistInstance = inst.name; prefs.assistInstance = inst.name; assistMenu = false },
                                    )
                                }
                            }
                        }
                    }
                    FilledPill(
                        "Manage server agents", onManageAgents, Modifier.fillMaxWidth(),
                        trailing = {
                            Icon(Icons.AutoMirrored.Filled.ArrowForward, null, Modifier.size(15.dp), tint = Kat.onAccent)
                        },
                    )
                    Text(
                        "List all agents, select the active one, create, start/stop/delete.",
                        fontSize = 11.5.sp, lineHeight = 17.sp, fontFamily = Plex, color = Kat.textSubtle,
                    )
                    Hairline(color = Kat.hairline)
                    Row(
                        Modifier.fillMaxWidth().tap { onSync() },
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text("Auto-sync", fontSize = 14.5.sp, fontFamily = Plex,
                                fontWeight = FontWeight.Medium, color = Kat.text)
                            Text(
                                "Last sync " + lastSync.ifBlank { "—" }, Modifier.padding(top = 2.dp),
                                fontSize = 12.5.sp, fontFamily = Plex, color = Kat.textFaint,
                            )
                        }
                        Text(
                            if (syncing) "running …" else "up to date",
                            fontSize = 12.5.sp, fontFamily = Plex, color = Kat.textFaint,
                        )
                    }
                }
            }

            // ── Diagnostics ─────────────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("Diagnostics", Modifier.padding(horizontal = 4.dp))
                var crash by remember { mutableStateOf(
                    runCatching { java.io.File(ctx.filesDir, "crash.log").takeIf { it.exists() }?.readText() }.getOrNull() ?: "") }
                KatCard(padding = PaddingValues(12.dp), spacing = 8.dp) {
                    if (crash.isBlank()) {
                        Text("No crash logged.", fontSize = 13.sp, fontFamily = Plex, color = Kat.textFaint)
                    } else {
                        Text(crash.take(4000), fontSize = 11.sp, fontFamily = PlexMono,
                            color = Kat.textStrong, maxLines = 16, overflow = TextOverflow.Ellipsis)
                        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            FilledPill("Copy", { clipboard.setText(AnnotatedString(crash)) }, height = 36.dp)
                            FilledPill("Delete", {
                                runCatching { java.io.File(ctx.filesDir, "crash.log").delete() }; crash = ""
                            }, height = 36.dp)
                        }
                    }
                }
            }

            // ── Brille (Halo) ──────────────────────────────────────────────
            // Trockenlauf ohne Hardware: HaloDryLink nimmt dieselben Pakete
            // entgegen wie die echte Brille und zeigt, was auf dem Display
            // stuende. Damit laesst sich die ganze Kette vor dem Auspacken
            // durchklicken — nur der Bluetooth-Weg fehlt dann.
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("Glasses (Halo)", Modifier.padding(horizontal = 4.dp))
                var haloStatus by remember { mutableStateOf("not connected") }
                var haloScreen by remember { mutableStateOf(listOf<String>()) }
                var dry by remember { mutableStateOf<HaloDryLink?>(null) }
                var ble by remember { mutableStateOf<HaloBle?>(null) }
                var ctrl by remember { mutableStateOf<HaloController?>(null) }
                val scope = rememberCoroutineScope()

                fun luaFromAssets(name: String): String =
                    ctx.assets.open("halo/$name.lua").bufferedReader().use { it.readText() }

                fun newController(link: HaloLink, fakeHeard: String?): HaloController {
                    val c = HaloController(
                        luaSource = ::luaFromAssets,
                        transcribe = { wav ->
                            fakeHeard ?: ManagerSync.stt(prefs.serverUrl, prefs.user, prefs.pass, wav, "audio/wav")
                        },
                        ask = { question ->
                            ServerAgent.chat(prefs.serverUrl, prefs.instance, prefs.user, prefs.pass, question)
                        },
                        askWithImage = { question, jpeg ->
                            // Derselbe Weg wie beim Foto aus der App: Base64-JPEG
                            // in den Chat-Strom, Antwort einsammeln.
                            val b64 = android.util.Base64.encodeToString(jpeg, android.util.Base64.NO_WRAP)
                            val sb = StringBuilder()
                            val err = ServerAgent.chatStream(prefs.serverUrl, prefs.instance,
                                prefs.user, prefs.pass, question, b64) { sb.append(it) }
                            err ?: sb.toString()
                        },
                        awaitPhoto = { t -> ble?.awaitPhoto(t) ?: dry?.photo },
                        onStatus = { haloStatus = it.ifBlank { "ready" } },
                    )
                    c.attach(link)
                    return c
                }

                val blePerm = rememberLauncherForActivityResult(
                    ActivityResultContracts.RequestMultiplePermissions()) { granted ->
                    if (granted.values.any { !it }) haloStatus = "permission denied"
                }

                KatCard(padding = PaddingValues(12.dp), spacing = 10.dp) {
                    Text(haloStatus, fontSize = 13.sp, fontFamily = Plex, color = Kat.textDim)
                    // Vorschau des Brillendisplays — im Trockenlauf das Einzige,
                    // was man zu sehen bekommt.
                    if (haloScreen.isNotEmpty()) Column(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(8.dp))
                            .background(Kat.tile).padding(10.dp),
                        verticalArrangement = Arrangement.spacedBy(2.dp),
                    ) {
                        haloScreen.forEach {
                            Text(it, fontSize = 12.sp, fontFamily = PlexMono, color = Kat.accentText)
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilledPill("Connect", {
                            val b = HaloBle(ctx)
                            val missing = b.missingPermissions()
                            if (missing.isNotEmpty()) { blePerm.launch(missing.toTypedArray()); return@FilledPill }
                            haloStatus = "searching…"
                            scope.launch {
                                val err = withContext(Dispatchers.IO) {
                                    b.connect(listener = object : HaloBle.Listener {
                                        override fun onDisconnected(reason: String) { haloStatus = reason }
                                        override fun onText(text: String) { haloStatus = text.take(120) }
                                    })
                                }
                                if (err != null) { haloStatus = "⚠️ $err"; return@launch }
                                ble = b
                                withContext(Dispatchers.IO) { ctrl = newController(b, null) }
                            }
                        }, height = 36.dp)
                        FilledPill("Dry run", {
                            val l = HaloDryLink()
                            dry = l; haloScreen = emptyList()
                            scope.launch {
                                withContext(Dispatchers.IO) { ctrl = newController(l, "Wie ist das Wetter?") }
                                haloScreen = l.display.toList()
                                haloStatus = "dry run: ${l.uploaded.size} modules loaded"
                            }
                        }, height = 36.dp)
                        if (ctrl != null) FilledPill("Disconnect", {
                            ctrl?.detach(); ctrl = null
                            ble?.disconnect(); ble = null; dry = null
                            haloScreen = emptyList(); haloStatus = "not connected"
                        }, height = 36.dp)
                    }
                    if (ctrl != null) Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        FilledPill("Listen", {
                            ctrl?.startListening()
                            dry?.let { haloScreen = it.display.toList() }
                        }, height = 36.dp)
                        FilledPill("Photo", {
                            scope.launch {
                                withContext(Dispatchers.IO) { ctrl?.photoAndAsk() }
                                dry?.let { haloScreen = it.display.toList() }
                            }
                        }, height = 36.dp)
                        FilledPill("Ask", {
                            scope.launch {
                                // Im Trockenlauf gibt es keinen Mitschnitt — die
                                // erfundene Frage geht trotzdem an die echte Instanz.
                                val wav = withContext(Dispatchers.IO) {
                                    ble?.recordingAsWav() ?: ByteArray(0)
                                }
                                withContext(Dispatchers.IO) { ctrl?.stopAndAsk(wav) }
                                dry?.let { haloScreen = it.display.toList() }
                            }
                        }, height = 36.dp)
                    }
                }
            }

            // ── Chat ───────────────────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("Chat", Modifier.padding(horizontal = 4.dp))
                KatCard(padding = PaddingValues(4.dp), spacing = 0.dp) {
                    Row(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
                            .tap { onWeb(!web) }.padding(12.dp),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text("Web access", fontSize = 14.5.sp, fontFamily = Plex,
                                fontWeight = FontWeight.Medium, color = Kat.text)
                            Text(
                                "Search results as context for the device model", Modifier.padding(top = 2.dp),
                                fontSize = 12.5.sp, fontFamily = Plex, color = Kat.textFaint,
                            )
                        }
                        KatSwitch(web, { onWeb(!web) })
                    }
                    Row(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
                            .tap { onBargeIn(!bargeIn) }.padding(12.dp),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text("Barge-in", fontSize = 14.5.sp, fontFamily = Plex,
                                fontWeight = FontWeight.Medium, color = Kat.text)
                            Text(
                                "Talking over the spoken reply cuts it off — the mic stays open, the assistant's own voice is cancelled on the device",
                                Modifier.padding(top = 2.dp), fontSize = 12.5.sp, fontFamily = Plex, color = Kat.textFaint,
                            )
                        }
                        KatSwitch(bargeIn, { onBargeIn(!bargeIn) })
                    }
                }
            }

            // ── App update ─────────────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("App update", Modifier.padding(horizontal = 4.dp))
                KatCard(padding = PaddingValues(4.dp), spacing = 0.dp) {
                    Row(
                        Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
                            .tap { onAutoUpdate(!autoUpdate) }.padding(12.dp),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text("Automatic updates", fontSize = 14.5.sp, fontFamily = Plex,
                                fontWeight = FontWeight.Medium, color = Kat.text)
                            Text(
                                "Installed $ver — new releases from GitHub are downloaded at start and handed to the installer",
                                Modifier.padding(top = 2.dp), fontSize = 12.5.sp, fontFamily = Plex, color = Kat.textFaint,
                            )
                        }
                        KatSwitch(autoUpdate, { onAutoUpdate(!autoUpdate) })
                    }
                    Row(
                        Modifier.fillMaxWidth().padding(12.dp),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Column(Modifier.weight(1f)) {
                            Text(
                                when {
                                    updateBusy -> "Checking…"
                                    update != null && updateReady -> "KatAgent ${update.version} ready to install"
                                    update != null -> "KatAgent ${update.version} available"
                                    else -> "No newer release known"
                                },
                                fontSize = 13.sp, fontFamily = Plex, color = Kat.text,
                            )
                        }
                        Text(
                            if (update != null && updateReady) "Install" else if (update != null) "Download" else "Check now",
                            Modifier.clip(RoundedCornerShape(8.dp))
                                .background(if (update != null) Kat.accent else Kat.tile)
                                .tap { if (!updateBusy) { if (update != null && updateReady) onInstallUpdate() else onCheckUpdate() } }
                                .padding(horizontal = 12.dp, vertical = 8.dp),
                            fontSize = 13.sp, fontFamily = Plex, fontWeight = FontWeight.Medium,
                            color = if (update != null) Kat.onAccent else Kat.textMuted,
                        )
                    }
                }
            }

            // ── Download model ─────────────────────────────────────────────
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Kicker("Download model", Modifier.padding(horizontal = 4.dp))
                KatCard {
                    LabeledField("HuggingFace-Token", token, { token = it; prefs.hfToken = it.trim() },
                        mono = true, password = true)
                    LabeledField("Model URL (.litertlm)", modelUrl, { modelUrl = it; prefs.modelUrl = it.trim() },
                        mono = true, fontSize = 12f)
                    Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                        Row(verticalAlignment = Alignment.CenterVertically,
                            horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            Text(
                                "Model catalog (litert-community)", Modifier.weight(1f),
                                fontSize = 12.sp, fontFamily = Plex, color = Kat.textSubtle,
                            )
                            Text(
                                if (copied) "Link copied ✓" else "Copy catalog link",
                                Modifier.tap {
                                    clipboard.setText(AnnotatedString("https://huggingface.co/models?library=litert-lm"))
                                    copied = true
                                },
                                fontSize = 11.5.sp, fontFamily = Plex, color = Kat.accentText,
                            )
                        }
                        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            PRESETS.forEach { p ->
                                val sel = modelUrl == p.url
                                Row(
                                    Modifier.fillMaxWidth().clip(RoundedCornerShape(10.dp))
                                        .background(if (sel) Kat.chipSel else Color.Transparent)
                                        .border(1.dp, if (sel) Kat.borderFocus else Kat.border, RoundedCornerShape(10.dp))
                                        .tap { modelUrl = p.url; prefs.modelUrl = p.url }
                                        .padding(horizontal = 11.dp, vertical = 9.dp),
                                    horizontalArrangement = Arrangement.spacedBy(10.dp),
                                    verticalAlignment = Alignment.CenterVertically,
                                ) {
                                    Column(Modifier.weight(1f)) {
                                        Text(
                                            p.name, fontSize = 12.5.sp, fontFamily = PlexMono,
                                            color = if (sel) Kat.accentBright else Kat.textDim,
                                        )
                                        Text(
                                            p.desc, Modifier.padding(top = 1.dp),
                                            fontSize = 11.5.sp, fontFamily = Plex, color = Kat.textSubtle,
                                        )
                                    }
                                    val (tbg, tfg) = when (p.tag) {
                                        "multimodal" -> Color(0x1F7FB0E8) to Kat.accentText
                                        "small" -> Color(0x1F4CC38A) to Kat.green
                                        else -> Kat.hairline to Kat.textMuted
                                    }
                                    MiniTag(p.tag, tbg, tfg)
                                    Text(p.size, fontSize = 11.5.sp, fontFamily = PlexMono, color = Kat.textFaint)
                                }
                            }
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                        FilledPill(
                            "Download",
                            { onDownload(modelUrl.trim(), token.trim()) },
                            Modifier.weight(1f),
                            enabled = modelUrl.isNotBlank(),
                            leading = { Icon(Icons.Outlined.Download, null, Modifier.size(15.dp), tint = Kat.onAccent) },
                        )
                        OutlinePill("Choose file", onPickModel, Modifier.weight(1f))
                    }
                }
            }

            Text(
                "KatAgent ${ver.ifBlank { "" }} · de.kat56.agent",
                Modifier.fillMaxWidth().padding(top = 8.dp, bottom = 4.dp),
                fontSize = 12.sp, fontFamily = Plex, color = Kat.textGhost,
                textAlign = androidx.compose.ui.text.style.TextAlign.Center,
            )
        }
    }
}

// ── Server agents (not drawn in the prototype — carried over unchanged) ──────

/** One turn as a span tree: the turn row, its LLM calls and its tool calls,
 *  each with duration — fetched from /api/trace/<instance>?turn=. */
@Composable
fun TraceDialog(prefs: Prefs, instance: String, turn: String, onDismiss: () -> Unit) {
    var data by remember { mutableStateOf<org.json.JSONObject?>(null) }
    var loading by remember { mutableStateOf(true) }
    LaunchedEffect(turn) {
        loading = true
        data = withContext(Dispatchers.IO) { ManagerSync.trace(prefs.serverUrl, prefs.user, prefs.pass, instance, turn) }
        loading = false
    }
    fun ms(v: Any?): String {
        val n = (v as? Number)?.toLong() ?: return "–"
        return if (n >= 1000) String.format("%.1f s", n / 1000.0) else "$n ms"
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Kat.surface,
        confirmButton = { TextButton(onDismiss) { Text("Close") } },
        title = { Text("Trace $turn", fontFamily = PlexMono, fontSize = 15.sp) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                val d = data
                if (!loading && d == null) Text("No trace from the manager.", style = MaterialTheme.typography.bodySmall)
                if (d != null) {
                    val t = d.optJSONObject("turn")
                    if (t != null) {
                        val head = buildString {
                            append(t.optString("kind", "turn")); append(" · "); append(ms(t.opt("ms")))
                            append(" · "); append(t.optInt("steps")); append(" steps")
                            append(" · "); append(t.optInt("llm_calls")); append(" LLM")
                            append(" · "); append(t.optInt("in")); append("/"); append(t.optInt("out")); append(" tok")
                            val oc = t.optString("outcome", ""); if (oc.isNotEmpty() && oc != "ok") { append(" · "); append(oc) }
                        }
                        Text(head, fontSize = 12.5.sp, fontFamily = Plex, color = Kat.text)
                    } else Text("turn row missing (still running or older than 30 days)",
                        fontSize = 12.sp, fontFamily = Plex, color = Kat.textFaint)
                    val llm = d.optJSONArray("llm"); val tools = d.optJSONArray("tools")
                    if (llm != null && llm.length() > 0) {
                        Text("LLM calls", fontSize = 11.sp, fontFamily = Plex, color = Kat.textSubtle,
                            modifier = Modifier.padding(top = 4.dp))
                        for (i in 0 until llm.length()) {
                            val x = llm.getJSONObject(i)
                            val ok = x.optBoolean("ok", true)
                            Text("#${x.optInt("step")}  ${ms(x.opt("ms"))}  ${x.optInt("in")}/${x.optInt("out")} tok" +
                                (if (!ok) "  ✗ " + x.optString("err").take(80) else ""),
                                fontSize = 12.sp, fontFamily = PlexMono,
                                color = if (ok) Kat.textDim else Kat.accentText)
                        }
                    }
                    if (tools != null && tools.length() > 0) {
                        Text("Tool calls", fontSize = 11.sp, fontFamily = Plex, color = Kat.textSubtle,
                            modifier = Modifier.padding(top = 4.dp))
                        for (i in 0 until tools.length()) {
                            val x = tools.getJSONObject(i)
                            val ok = x.optBoolean("ok", true)
                            Column {
                                Text("${x.optString("tool")}  ${ms(x.opt("ms"))}" + (if (!ok) "  ✗" else ""),
                                    fontSize = 12.sp, fontFamily = PlexMono,
                                    color = if (ok) Kat.text else Kat.accentText)
                                val tgt = x.optString("target", ""); val err = x.optString("err", "")
                                if (tgt.isNotEmpty()) Text(tgt.take(120), fontSize = 11.sp, fontFamily = PlexMono,
                                    color = Kat.textFaint, maxLines = 2, overflow = TextOverflow.Ellipsis)
                                if (!ok && err.isNotEmpty()) Text(err.take(160), fontSize = 11.sp, fontFamily = Plex,
                                    color = Kat.textDim, maxLines = 3, overflow = TextOverflow.Ellipsis)
                            }
                        }
                    } else if (llm != null) Text("no tool calls", fontSize = 11.5.sp, fontFamily = Plex, color = Kat.textFaint)
                }
            }
        },
    )
}


@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun ServerAgentsDialog(prefs: Prefs, onDismiss: () -> Unit, onStatus: (String) -> Unit) {
    val scope = rememberCoroutineScope()
    val ctx = LocalContext.current
    var instances by remember { mutableStateOf<List<AgentInstance>>(emptyList()) }
    var active by remember { mutableStateOf(prefs.instance) }
    var loading by remember { mutableStateOf(false) }
    var busyName by remember { mutableStateOf("") }
    var name by remember { mutableStateOf("") }
    var template by remember { mutableStateOf("openrouter") }
    var model by remember { mutableStateOf("google/gemma-4-26b-a4b-it:free") }
    var personas by remember { mutableStateOf<List<Persona>>(emptyList()) }
    var persona by remember { mutableStateOf("") }

    fun refresh() {
        scope.launch {
            loading = true
            val j = withContext(Dispatchers.IO) { ManagerSync.listInstances(prefs.serverUrl, prefs.user, prefs.pass) }
            loading = false
            if (j == null) { onStatus("⚠️ Server unreachable: ${ManagerSync.lastStatus}"); return@launch }
            instances = ManagerSync.parseInstances(j)
        }
    }
    LaunchedEffect(Unit) {
        refresh()
        val pj = withContext(Dispatchers.IO) { ManagerSync.listPersonas(prefs.serverUrl, prefs.user, prefs.pass) }
        personas = ManagerSync.parsePersonas(pj)
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = Kat.surface,
        confirmButton = { TextButton(onDismiss) { Text("Close") } },
        dismissButton = { TextButton({ refresh() }, enabled = !loading) { Text("Refresh") } },
        title = { Text("Server agents") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(6.dp)) {
                if (loading) LinearProgressIndicator(Modifier.fillMaxWidth())
                if (!loading && instances.isEmpty()) Text("No instances found.", style = MaterialTheme.typography.bodySmall)
                instances.forEach { inst ->
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        RadioButton(active == inst.name, {
                            active = inst.name; prefs.instance = inst.name; onStatus("Active: ${inst.name}")
                        })
                        Column(Modifier.weight(1f).padding(end = 4.dp)) {
                            Text(inst.name, style = MaterialTheme.typography.bodyMedium, maxLines = 1,
                                overflow = TextOverflow.Ellipsis)
                            val sub = buildString {
                                append(if (inst.running) "● running" else "○ off")
                                if (inst.template.isNotEmpty()) append(" · ${inst.template}")
                            }
                            Text(sub, style = MaterialTheme.typography.labelMedium,
                                color = if (inst.running) Kat.green else Kat.textFaint)
                            if (inst.model.isNotEmpty()) Text(inst.model,
                                fontSize = 12.sp, fontFamily = PlexMono, color = Kat.textFaint,
                                maxLines = 1, overflow = TextOverflow.Ellipsis)
                        }
                        if (busyName == inst.name) {
                            CircularProgressIndicator(Modifier.size(20.dp), strokeWidth = 2.dp)
                        } else {
                            if (inst.running) {
                                IconButton({
                                    val base = prefs.serverUrl.trimEnd('/')
                                    ctx.startActivity(
                                        Intent(ctx, TerminalActivity::class.java)
                                            .putExtra("url", "$base/i/${inst.name}/term/")
                                            .putExtra("title", "Terminal · ${inst.name}")
                                    )
                                }) { Icon(Icons.Filled.Terminal, "Terminal", Modifier.size(20.dp)) }
                            }
                            TextButton({
                                val act = if (inst.running) "stop" else "start"
                                busyName = inst.name
                                scope.launch {
                                    withContext(Dispatchers.IO) { ManagerSync.action(prefs.serverUrl, prefs.user, prefs.pass, inst.name, act) }
                                    busyName = ""; refresh()
                                }
                            }, contentPadding = PaddingValues(horizontal = 10.dp)) {
                                Text(if (inst.running) "Stop" else "Start", maxLines = 1)
                            }
                            IconButton({
                                busyName = inst.name
                                scope.launch {
                                    withContext(Dispatchers.IO) { ManagerSync.action(prefs.serverUrl, prefs.user, prefs.pass, inst.name, "delete") }
                                    busyName = ""
                                    if (active == inst.name) { active = ""; prefs.instance = "" }
                                    refresh()
                                }
                            }) { Icon(Icons.Outlined.DeleteOutline, "Delete", Modifier.size(20.dp)) }
                        }
                    }
                }
                HorizontalDivider(Modifier.padding(vertical = 4.dp), color = Kat.hairline)
                Text("Create new agent", style = MaterialTheme.typography.labelLarge)
                OutlinedTextField(name, { name = it }, label = { Text("Name (e.g. gemma4)") },
                    singleLine = true, modifier = Modifier.fillMaxWidth())
                FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                    listOf("openrouter", "prime", "pi", "claude").forEach { tpl ->
                        FilterChip(template == tpl, { template = tpl }, { Text(tpl, maxLines = 1) })
                    }
                }
                if (template != "claude") {
                    OutlinedTextField(model, { model = it }, label = { Text("Model") },
                        singleLine = true, modifier = Modifier.fillMaxWidth())
                }
                if (personas.isNotEmpty()) {
                    Text("Persona (system prompt)", style = MaterialTheme.typography.labelMedium)
                    FlowRow(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        FilterChip(persona == "", { persona = "" }, { Text("Default", maxLines = 1) })
                        personas.forEach { p ->
                            FilterChip(persona == p.name, { persona = p.name }, { Text(p.name, maxLines = 1) })
                        }
                    }
                }
                Button(
                    onClick = {
                        val n = name.trim()
                        if (n.isBlank()) return@Button
                        busyName = n; onStatus("Creating agent '$n'…")
                        scope.launch {
                            val cfg = JSONObject().put("TRANSPORT", "web")
                            when (template) {
                                "openrouter" -> cfg.put("OPENROUTER_MODEL", model.trim())
                                "pi" -> cfg.put("PI_MODEL", model.trim())
                            }
                            personas.firstOrNull { it.name == persona }?.let { cfg.put("AGENT_SYSTEM", it.prompt) }
                            val res = withContext(Dispatchers.IO) {
                                ManagerSync.createAndStart(prefs.serverUrl, prefs.user, prefs.pass, n, template, cfg)
                            }
                            prefs.instance = n; active = n; name = ""; onStatus(res); busyName = ""; refresh()
                        }
                    },
                    enabled = name.isNotBlank() && busyName.isEmpty(),
                ) { Text("Create & start") }
            }
        }
    )
}

private fun copyModel(context: Context, dir: File, uri: Uri): String? {
    return try {
        val name = "model-selected.litertlm"
        val out = File(dir.apply { mkdirs() }, name)
        context.contentResolver.openInputStream(uri)?.use { inp -> out.outputStream().use { o -> inp.copyTo(o, 1 shl 20) } }
        out.absolutePath
    } catch (e: Exception) { null }
}

private fun bitmapToBase64(bmp: Bitmap): String {
    val out = java.io.ByteArrayOutputStream()
    bmp.compress(Bitmap.CompressFormat.JPEG, 85, out)
    return android.util.Base64.encodeToString(out.toByteArray(), android.util.Base64.NO_WRAP)
}

private fun loadBitmap(context: Context, uri: Uri): Bitmap? {
    return try {
        val raw = context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it) } ?: return null
        val max = 768
        val w = raw.width; val h = raw.height
        val scaled = if (w <= max && h <= max) raw else {
            val s = max.toFloat() / maxOf(w, h)
            Bitmap.createScaledBitmap(raw, (w * s).toInt().coerceAtLeast(1), (h * s).toInt().coerceAtLeast(1), true)
        }
        if (scaled.config == Bitmap.Config.ARGB_8888) scaled else scaled.copy(Bitmap.Config.ARGB_8888, false)
    } catch (e: Exception) { null }
}
