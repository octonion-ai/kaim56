-- kAIm56 KatAgent -- Geraeteseite fuer die Brilliant-Brille (Halo/Frame)
-- Copyright (C) 2026 Ulrich Neidel
-- SPDX-License-Identifier: AGPL-3.0-or-later
--
-- Gegenstueck zu Halo.kt: nimmt die Nachrichten des Telefons entgegen, zeigt
-- Text an, schaltet Mikrofon und Kamera. Die Bausteine (data, plain_text,
-- audio, camera, code) stammen unveraendert aus dem Brilliant SDK
-- (BSD-3-Clause, siehe LICENSE.brilliant_sdk); deshalb halten wir uns bei
-- Text und Loeschen an deren Nachrichtenformat.
--
-- NUR ASCII in dieser Datei: die Lua-Laufzeit liest den Quelltext als
-- latin-1, ein Gedankenstrich im Kommentar bricht bereits das Laden.
--
-- Testbar ohne Brille: die Schleife steckt in step(), und app_loop() startet
-- nur, wenn KATAGENT_TEST nicht gesetzt ist (siehe tools/halo/test_frame_app.lua).

local data = require('data.min')
local plain_text = require('plain_text.min')
local audio = require('audio.min')
local camera = require('camera.min')
local code = require('code.min')

-- Telefon -> Brille (muss zu HaloSession.Code in Halo.kt passen)
TEXT_MSG = 0x12
CLEAR_MSG = 0x10
AUDIO_START_MSG = 0x30
AUDIO_STOP_MSG = 0x31
PHOTO_MSG = 0x0d

-- laeuft das Mikrofon gerade? Die Schleife schaufelt dann Audio ans Telefon.
streaming = false

local parsers = {}
parsers[TEXT_MSG] = plain_text.parse_plain_text
parsers[CLEAR_MSG] = code.parse_code
parsers[AUDIO_STOP_MSG] = code.parse_code
parsers[PHOTO_MSG] = code.parse_code
-- Mikrofonstart bringt Abtastrate (16 Bit) und Bittiefe mit.
parsers[AUDIO_START_MSG] = function(d)
	return {
		sample_rate = string.byte(d, 1) << 8 | string.byte(d, 2),
		bit_depth = string.byte(d, 3),
	}
end

function clear_display()
	if frame.HARDWARE_VERSION == "Frame" then
		frame.display.text(" ", 1, 1)
		frame.display.show()
	else
		frame.display.clear(0x000000)
	end
end

-- Antwort des Agenten anzeigen. Zeilenumbrueche werden zu Zeilen; die Brille
-- hat keinen Umbruch von sich aus, das Telefon bricht den Text also vor.
function print_text(parsed)
	local i = 0
	for line in parsed.string:gmatch("([^\n]*)\n?") do
		if line ~= "" then
			if frame.HARDWARE_VERSION == "Frame" then
				frame.display.text(line, 1, i * 60 + 1)
			else
				frame.display.text(line, 1, i * 20 + 1, parsed.color)
			end
			i = i + 1
		end
	end
	if frame.HARDWARE_VERSION == "Frame" then
		frame.display.show()
	end
end

local handlers = {}

handlers[TEXT_MSG] = function(parsed)
	if parsed.string ~= nil then
		print_text(parsed)
	end
end

handlers[CLEAR_MSG] = function(_)
	clear_display()
end

handlers[AUDIO_START_MSG] = function(parsed)
	audio.start({ sample_rate = parsed.sample_rate, bit_depth = parsed.bit_depth })
	streaming = true
end

handlers[AUDIO_STOP_MSG] = function(_)
	-- NUR das Mikrofon anhalten. streaming bleibt an, bis die Schleife einmal
	-- mehr liest: read_and_send_audio() bekommt dann nil, schickt dem Telefon
	-- das Schlussstueck (0x06) und erst danach ruht der Mitschnitt. Wer hier
	-- streaming sofort abschaltet, verschluckt das Schlussstueck: das Telefon
	-- wartet dann ewig auf das Ende der Aufnahme.
	audio.stop()
end

handlers[PHOTO_MSG] = function(_)
	camera.capture_and_send({})
end

-- Ein Durchlauf der Hauptschleife. Ausgelagert, damit der Test ihn einzeln
-- aufrufen kann, ohne in einer Endlosschleife zu haengen.
function step()
	local items = data.process_raw_items()
	for i = 1, #items do
		local flag = items[i][1]
		local raw = items[i][2]
		if parsers[flag] then
			local parsed = parsers[flag](raw)
			if handlers[flag] then
				handlers[flag](parsed)
			end
		end
	end
	if streaming then
		-- nil = das Mikrofon wurde gestoppt und das Schlussstueck ist raus
		if audio.read_and_send_audio() == nil then
			streaming = false
		end
	end
end

function app_loop()
	clear_display()
	print("KatAgent bereit")
	while true do
		local rc, err = pcall(step)
		if rc == false then
			print(err)
			clear_display()
			frame.sleep(0.04)
			break
		end
		-- Waehrend eines Mitschnitts eng takten, sonst genuegsam bleiben.
		frame.sleep(streaming and 0.001 or 0.1)
	end
end

if not KATAGENT_TEST then
	app_loop()
end
