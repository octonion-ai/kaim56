// kAIm56 KatAgent — Android client for the kAIm56 agent platform
// Copyright (C) 2026 Ulrich Neidel
// SPDX-License-Identifier: AGPL-3.0-or-later
package de.kat56.agent

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AppUpdateTest {

    @Test
    fun `numeric version comparison, not string order`() {
        assertTrue(AppUpdate.isNewer("5.37", "5.38"))
        assertTrue(AppUpdate.isNewer("5.9", "5.10"))
        assertTrue(AppUpdate.isNewer("5.37", "6.0"))
        assertTrue(AppUpdate.isNewer("5.37", "5.37.1"))
        assertFalse(AppUpdate.isNewer("5.37", "5.37"))
        assertFalse(AppUpdate.isNewer("5.38", "5.37"))
        assertFalse(AppUpdate.isNewer("dev", "5.38"))
    }

    private val releases = """[
      {"tag_name":"v1.0.0","assets":[{"name":"katagent-5.35.apk","browser_download_url":"https://x/platform.apk"}]},
      {"tag_name":"app-v5.40","draft":true,"assets":[{"name":"katagent-5.40.apk","browser_download_url":"https://x/draft.apk"}]},
      {"tag_name":"app-v5.39","prerelease":true,"assets":[{"name":"katagent-5.39.apk","browser_download_url":"https://x/pre.apk"}]},
      {"tag_name":"app-v5.9","assets":[{"name":"katagent-5.9.apk","browser_download_url":"https://x/5.9.apk"}],"body":"old"},
      {"tag_name":"app-v5.38","assets":[{"name":"notes.txt"},{"name":"katagent-5.38.apk","browser_download_url":"https://x/5.38.apk"}],"body":"barge-in"},
      {"tag_name":"app-v5.41","assets":[]}
    ]"""

    @Test
    fun `the newest published app release with an APK wins`() {
        val r = AppUpdate.parseReleases(releases)!!
        assertEquals("5.38", r.version)
        assertEquals("app-v5.38", r.tag)
        assertEquals("https://x/5.38.apk", r.apkUrl)
        assertEquals("barge-in", r.notes)
    }

    @Test
    fun `platform releases and garbage give nothing`() {
        assertNull(AppUpdate.parseReleases("""[{"tag_name":"v1.0.0","assets":[{"name":"katagent-5.35.apk","browser_download_url":"u"}]}]"""))
        assertNull(AppUpdate.parseReleases("not json"))
        assertNull(AppUpdate.parseReleases("[]"))
    }
}
