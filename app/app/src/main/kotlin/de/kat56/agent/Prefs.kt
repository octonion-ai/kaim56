// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import android.content.Context

/** Simple settings persistence (SharedPreferences). */
class Prefs(context: Context) {
    val appContext: Context = context.applicationContext
    private val sp = appContext.getSharedPreferences("katagent", Context.MODE_PRIVATE)

    // Base URL for the manager. The app talks to the manager over iroh, so this
    // is "iroh://<manager-node-id>" (set from the Manager node-id in Settings);
    // empty until paired.
    var serverUrl: String
        get() = sp.getString("serverUrl", "") ?: ""
        set(v) = sp.edit().putString("serverUrl", v).apply()

    var instance: String
        get() = sp.getString("instance", "ortest") ?: ""
        set(v) = sp.edit().putString("instance", v).apply()

    // Which server instance the assistant button (ACTION_ASSIST) opens.
    // Empty = use the currently active instance.
    var assistInstance: String
        get() = sp.getString("assistInstance", "") ?: ""
        set(v) = sp.edit().putString("assistInstance", v).apply()

    var user: String
        get() = sp.getString("user", "admin") ?: ""
        set(v) = sp.edit().putString("user", v).apply()

    var pass: String
        get() = sp.getString("pass", "") ?: ""
        set(v) = sp.edit().putString("pass", v).apply()

    var modelPath: String
        get() = sp.getString("modelPath", "") ?: ""
        set(v) = sp.edit().putString("modelPath", v).apply()

    var mode: String
        get() = sp.getString("mode", "local") ?: "local"
        set(v) = sp.edit().putString("mode", v).apply()

    // Web access in device mode (the app fetches search/pages as context for Gemma).
    var webAccess: Boolean
        get() = sp.getBoolean("webAccess", false)
        set(v) = sp.edit().putBoolean("webAccess", v).apply()

    // Barge-in: talking over the spoken reply cuts it off (echo-cancelled mic).
    var bargeIn: Boolean
        get() = sp.getBoolean("bargeIn", true)
        set(v) = sp.edit().putBoolean("bargeIn", v).apply()

    // Active on-device model (file name in the models/ folder).
    var activeModel: String
        get() = sp.getString("activeModel", "") ?: ""
        set(v) = sp.edit().putString("activeModel", v).apply()

    // Last opened chat (restore after restart).
    var currentChatId: String
        get() = sp.getString("currentChatId", "") ?: ""
        set(v) = sp.edit().putString("currentChatId", v).apply()

    // On-Device-Download (HuggingFace)
    var hfToken: String
        get() = sp.getString("hfToken", "") ?: ""
        set(v) = sp.edit().putString("hfToken", v).apply()

    var modelUrl: String
        get() = sp.getString("modelUrl",
            "https://huggingface.co/litert-community/gemma-4-E4B-it-litert-lm/resolve/main/gemma-4-E4B-it.litertlm") ?: ""
        set(v) = sp.edit().putString("modelUrl", v).apply()
}

