-- Test der Geraeteseite (app/src/main/assets/halo/katagent.lua) OHNE Brille.
--
-- Statt der Brille steht hier eine Attrappe der `frame`-API. Eingespeist wird
-- genau das, was Halo.kt auf die Leitung legt — abzueglich des fuehrenden
-- 0x01, das der Bluetooth-Stapel der Brille abschneidet, bevor der Lua-Handler
-- die Daten sieht. Geprueft wird: Zusammensetzen ueber mehrere Pakete,
-- Verteilung an die Handler, Anzeige, Mikrofon und der Abschluss des
-- Mitschnitts.
--
-- Aufruf (Lua liegt nicht auf dem Host, deshalb im Container):
--   docker run --rm -v /home/ulrich/katagent:/w -w /w alpine:latest \
--     sh -c "apk add --no-cache lua5.4 >/dev/null && lua5.4 tools/halo/test_frame_app.lua"

KATAGENT_TEST = true

-- Die Brille speichert die Module flach als "data.min.lua" und laedt sie mit
-- require('data.min'); Standard-Lua wuerde daraus "data/min.lua" machen.
-- Deshalb legen wir sie hier direkt unter dem gepunkteten Namen ab — dieselbe
-- Aufloesung wie auf dem Geraet, ohne die Dateinamen zu verbiegen.
local HALO = "app/src/main/assets/halo/"
local function preload(name)
        package.loaded[name] = dofile(HALO .. name .. ".lua")
end

-- ---- Attrappe der Brille --------------------------------------------------
local sent = {}            -- was die Brille ans Telefon geschickt haette
local drawn = {}           -- Zeilen auf dem Display
local cleared = 0
local mic = { running = false, rate = nil, depth = nil, queue = {} }
local receive_cb = nil

frame = {
	HARDWARE_VERSION = "Halo",
	sleep = function(_) end,
	bluetooth = {
		max_length = function() return 241 end,
		send = function(s) sent[#sent + 1] = s; return true end,
		receive_callback = function(cb) receive_cb = cb end,
	},
	display = {
		text = function(s, x, y, color) drawn[#drawn + 1] = { s = s, x = x, y = y, color = color } end,
		show = function() end,
		clear = function(_) cleared = cleared + 1 end,
	},
	microphone = {
		start = function(args) mic.running = true; mic.rate = args.sample_rate; mic.depth = args.bit_depth end,
		stop = function() mic.running = false end,
		read = function(_)
			if not mic.running then return nil end
			if #mic.queue == 0 then return '' end
			return table.remove(mic.queue, 1)
		end,
	},
	camera = {
		-- capture_and_send() des SDK ruft mehrere dieser Funktionen; fuer den
		-- Test genuegt, dass sie existieren und nichts tun.
		capture = function(_) end,
		read = function(_) return nil end,
		auto = function(_) end,
		image_ready = function() return true end,
	},
	file = { open = function() return { write = function() end, close = function() end } end },
	FILE = 0,
}

-- Erst nach der frame-Attrappe laden: data.min registriert beim Laden seinen
-- Empfangs-Callback ueber frame.bluetooth.receive_callback.
for _, m in ipairs({ "data.min", "plain_text.min", "audio.min", "camera.min", "code.min" }) do
        preload(m)
end
dofile(HALO .. "katagent.lua")

-- ---- Hilfen ---------------------------------------------------------------
local failures = 0
local function check(name, cond, detail)
	if cond then
		print("  ok     " .. name)
	else
		failures = failures + 1
		print("  FEHLER " .. name .. (detail and ("  -> " .. detail) or ""))
	end
end

-- Baut die Pakete wie Halo.kt (packets()), liefert sie aber schon ohne das
-- 0x01: genau so kommen sie beim Lua-Handler an.
local function packets(code, payload, max_data)
	max_data = max_data or 241
	local chunk = max_data - 1
	local out, sent_bytes, first = {}, 0, true
	repeat
		local rest = #payload - sent_bytes
		local take = first and math.min(rest, chunk - 2) or math.min(rest, chunk)
		local head
		if first then
			head = string.char(code, (#payload >> 8) & 0xFF, #payload & 0xFF)
		else
			head = string.char(code)
		end
		out[#out + 1] = head .. string.sub(payload, sent_bytes + 1, sent_bytes + take)
		sent_bytes = sent_bytes + take
		first = false
	until sent_bytes >= #payload
	return out
end

local function deliver(code, payload, max_data)
	for _, p in ipairs(packets(code, payload, max_data)) do
		receive_cb(p)
	end
end

local function text_payload(s, x, y, color, spacing)
	return string.char((x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF, color, spacing) .. s
end

-- ---- Tests ----------------------------------------------------------------
print("Geraeteseite (katagent.lua)")

check("der Datenhandler ist registriert", receive_cb ~= nil)

-- 1) Text in einem Paket
deliver(0x12, text_payload("Hallo Brille", 1, 1, 1, 4))
step()
check("kurzer Text landet auf dem Display", #drawn == 1 and drawn[1].s == "Hallo Brille",
	drawn[1] and drawn[1].s or "nichts gezeichnet")

-- 2) Mehrzeiliger Text -> mehrere Zeilen, y waechst
drawn = {}
deliver(0x12, text_payload("Zeile eins\nZeile zwei\nZeile drei", 1, 1, 1, 4))
step()
check("Zeilenumbrueche werden zu Displayzeilen", #drawn == 3)
check("die Zeilen ruecken nach unten", #drawn == 3 and drawn[2].y > drawn[1].y,
	#drawn == 3 and (drawn[1].y .. " -> " .. drawn[2].y) or "zu wenige Zeilen")

-- 3) Langer Text ueber viele Pakete: kommt vollstaendig an
drawn = {}
local long = string.rep("A", 1200)
deliver(0x12, text_payload(long, 1, 1, 1, 4), 64)
step()
check("langer Text wird aus vielen Paketen zusammengesetzt",
	#drawn == 1 and #drawn[1].s == 1200,
	#drawn == 1 and ("Laenge " .. #drawn[1].s) or ("Zeilen: " .. #drawn))

-- 4) Loeschen
cleared = 0
deliver(0x10, "")
step()
check("Loeschen raeumt das Display", cleared == 1, "cleared=" .. cleared)

-- 5) Mikrofon an, mit den Werten des Telefons
deliver(0x30, string.char(0x1F, 0x40, 16))          -- 8000 Hz, 16 Bit
step()
check("Mikrofon laeuft mit 8000 Hz / 16 Bit",
	mic.running and mic.rate == 8000 and mic.depth == 16,
	tostring(mic.rate) .. "/" .. tostring(mic.depth))

-- 6) Aufgenommenes wandert mit dem Fortschritts-Flag ans Telefon
sent = {}
mic.queue = { "abc", "def" }
step(); step()
local audio_chunks = 0
for _, s in ipairs(sent) do
	if string.byte(s, 1) == 0x05 then audio_chunks = audio_chunks + 1 end
end
check("Audio geht mit 0x05 an das Telefon", audio_chunks == 2, "Stuecke: " .. audio_chunks)

-- 7) Stopp: Schlussstueck 0x06, danach ruht der Mitschnitt
sent = {}
deliver(0x31, "")
step()
local final = false
for _, s in ipairs(sent) do
	if string.byte(s, 1) == 0x06 then final = true end
end
check("Stopp schickt das Schlussstueck 0x06", final)
check("nach dem Stopp laeuft nichts mehr", mic.running == false)

-- 8) Unbekannter Code darf die Schleife nicht umbringen
local ok = pcall(function() deliver(0x77, "irgendwas"); step() end)
check("unbekannte Nachrichten werden still verworfen", ok)

print(failures == 0 and "ALLE TESTS GRUEN" or (failures .. " FEHLER"))
os.exit(failures == 0 and 0 or 1)
