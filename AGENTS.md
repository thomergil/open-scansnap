# AGENTS.md

Notes for AI agents working on open-scansnap. Keep project knowledge here, not in agent memory. The README covers install and use; this file covers what the code cannot tell you.

## Goal and owner rules

Replace ScanSnap Home for a ScanSnap iX500 on Wi-Fi: a PDF in `~/Downloads` that looks like ScanSnap's "Document" output (cropped, deskewed, upright, blank sides dropped, black and white for text pages, searchable). The owner wants it "as good as ScanSnap software or better".

- **Use existing software.** Research before building.
- **No custom image processing.** A hand-written OpenCV crop/flatten/levels pipeline was rejected.
- **No settings tuned on one sheet.** Each page's treatment must come from that page: established algorithms, objective scores (Tesseract), or boundaries with a stated physical or published basis. No LLMs.
- Work through `/principal:code`.
- **No firmware updates.** They stay with ScanSnap Home: a failed or wrong update can leave the scanner unusable, and none can be tested safely (owner, 2026-11-22). The iX500's latest firmware is 0U00 (PFU's firmware page); this scanner has it.
- **Anything that changes the scanner's settings** (Wi-Fi network, the computers allowed on Wi-Fi) is sent only by an explicit user action in the app, after the user sees what it changes.
- **License: MIT** for open-scansnap's own code. Push it with fresh history: the upstream clone's commits (Bram Heerink, Axel Lodyga) are GPL-2.0. Its code is gone; only protocol facts, which are not copyrightable, were kept. Do not add GPL code to this repository. SANE stays GPL (our `fujitsu.c` changes must); its linking exception lets MIT code call it. Avoid Ghostscript (AGPL): `pypdfium2` rasterizes for OCRmyPDF. A bundled app must carry every component's license and point to the SANE fork's source; img2pdf (LGPL-3.0) must stay replaceable, so bundle it as plain `.py` files.

## Layout

- `sane-backends/`: clone of https://gitlab.com/sane-project/backends, branch `vens`, not yet published or committed. It holds the Wi-Fi transport in the `fujitsu` backend: `backend/fujitsu-vens.{c,h}` (packets), Wi-Fi command definitions at the end of `backend/fujitsu-scsi.h`, the `vens_*` functions and hooks in `backend/fujitsu.c`, docs in `doc/sane-fujitsu.man` and `doc/descriptions/fujitsu.desc`. Users configure `wifi HOST` with `option wifi-hostid` and `option wifi-password`; device names are `fujitsu:wifi:HOST`. "VENS" (the packet magic) names only the internals. Built into `sane-prefix/` (see README).
- `openscansnap/`:
  - scanning: `sane.py` (SANE configuration and `scanimage`), `saneapi.py` (libsane through ctypes, for the sensors), `monitor.py` (the thread that owns the scanner, and the PDF thread), `job.py` (a batch: scan, then make its PDF);
  - finding the scanner: `discovery.py` (UDP search, this Mac's HostID), `scanner.py` (set up, follow to a new address, password; its `CONFIGURING` lock);
  - images: `sheet.py` (crop and straighten), `showthrough.py`, `pages.py` (per-page decisions), `pdf.py` (img2pdf and OCRmyPDF);
  - settings: `options.py` (the scan's choices), `config.py` with `defaults.toml`, `files.py` (whole-file replacement);
  - the app: `menubar.py`, `wifiwindow.py` (Wi-Fi setup over USB), `firmware.py` (the newest firmware per model), `appicon.py`, `updates.py` (Sparkle), `bundle.py`, `selftest.py`; `__main__.py` is the CLI.
  - `packaging/macos/languages.txt`: the OCR languages bundled besides English (tessdata_fast at a pinned commit, SHA-256 per file; the build checks them). Owner's choice (2026-11-22): the languages of most scanned documents, bundled, rather than a language menu with English only.
- `tests/`: one `test_<module>.py` per module, `test_cli.py` for the CLI; `test_wifi.py` with `fake_ix500.py` (the scanner's Wi-Fi firmware on localhost, driven by the real `scanimage`; strict about READ order) also answers the search.

Development setup: an iX500 with firmware 0U00 (the newest) and a Silex Wi-Fi module. SANE config in `~/.config/open-scansnap/sane.d/`.

## Protocol facts (verified against the scanner, 2026-11-22)

Ports: 52217/udp registration and search, 53219/tcp reservation, 53218/tcp commands, 55265/tcp trigger port the host announces ("Scan button notice" in PFU's docs).

Reservation (128 bytes on 53219), as built by ScanSnap Home's `+[SsSvcPCWIFITelReserveScanner initCreateSendData:…]`, big-endian:

| Offset | Field |
|---|---|
| 0 | Length (128) |
| 4 | `"VENS"` |
| 8 | Command 0x11 |
| 16 | Client MAC |
| 32 | Interface version, u16 (6) |
| 34 | Timeout, u8 (30 s) |
| 36 | Reserve mode, u32 |
| 40 | Password-present flag, u32 |
| 44 | Client IPv4 |
| 48 | Trigger port, u32 |
| 52 | Pairing key |
| 100 | Year u16, month, day, hour, minute, second |
| 108 | HostID, u64 |
| 116 | Negated GMT offset in seconds, i32 |

- **Pairing key:** the scanner password through `codePFUCipher`: `"%03d" % (ord("pFusCANsNapFiPfu"[i]) + ord(ch) + 11)` per character. Default password: last 4 characters of the serial.
- **HostID:** must be the one ScanSnap Home paired with. ScanSnap Home computes it as the Mac's MAC shifted left 16 bits, from interface en1 (else the first other `en1*`, else en0) (`_getHostId` in `SshResident`; verified: the value computed from the development Mac's en1 equals the plist's). It caches it in `~/Library/Preferences/com.fujitsu.pfu.ScanSnapHomeSetting.plist` (`Common` → `Wifi Connected Scanner` → `AutoConnectInfo_ScannerInfo` → `HostID`). `scanner.this_hostid` takes the plist's, else computes it.
- **Search** (verified 2026-11-22): ScanSnap Home's three 32-byte packets (`VENS`, `ssNR`, `V2ss`) to UDP 52217 at the broadcast address find every scanner. The scanner ignores a search without the client's IPv4 address at byte 8, but answers one without the client's MAC at 12, so `discovery` sends none (no identifier broadcast). Bytes 20–23 are the port to answer (ScanSnap Home binds 55264; any port works). The iX500's 132-byte reply: IPv4 at 0x10, ports at 0x14, scanner MAC at 0x1C, `model-serial` at 0x28, product at 0x68, a host IPv4 at 0x78 and HostID at 0x7C. Those two named the development Mac while the app was connected; whether they name the paired host when none is connected is not yet seen, so setup prefers a scanner naming this Mac's HostID but does not reject others. Nothing authenticates a reply; `discovery` keeps only replies from another host inside a searched network that name their sender's address and a `model-serial` name, and following searches only the configured scanner's network, where an answer from the configured address beats a look-alike.
- **Replies** (i32 at byte 8): 0 OK, -3 wrong key, -4 busy, -7 wrong HostID.
- **Reservations** are keyed to the client MAC; another MAC gets -4. They lapse 25–30 s after the last activity. **Release:** 32 bytes on 53219, command 0x12, MAC at 16; only the holder's release counts (`SsSvcPCWIFITelReleaseScanner`). The D6 command on 53218 ends the job, not the reservation.
- **Registration** replies are lost while the scanner's Wi-Fi wakes from sleep: send a round, wait, repeat.
- **53218 is a SCSI tunnel:** a 32-byte envelope (`len`, `"VENS"`, 1, 0, MAC, 10 zero bytes), `>IIII(cdb_len, read_len, write_len, 0)`, the CDB padded to 16 bytes, write data. Reply: 16-byte header with the SCSI status at bytes 12–15, 24 unused bytes, data.
- **The Wi-Fi firmware rejects** (sense 5/0x20) TEST UNIT READY, SET/GET WINDOW, SCAN, OBJECT POSITION, SEND, MODE SELECT/SENSE, SEND/READ DIAGNOSTIC, SCANNER CONTROL and ENDORSER. It accepts INQUIRY (and VPD 0xF0), REQUEST SENSE, GET HW STATUS, READ and ScanSnap Home's vendor commands. Their names, from `SsSvcPCWIFITel*` classes: d5 StartJob, d8 CancelRead, e9 SetPreReadMode, d4 SetParams (job settings and a window descriptor), e0 StartPaper, d6 EndJob, e6 PowerOffTime, e7 GetScannerInfo. A side is one READ (byte 3 = 0x02, window id 0x80 for the back, sequence number at byte 10, 12-byte CDB); it returns a 300 dpi color JPEG, then CHECK CONDITION with EOM/ILI. An empty feeder answers e0 with sense 3/0x80/0x03.
- macOS masks the MAC as `02:00:00:00:00:00` for programs using `getifaddrs` (so psutil too): the same value on every Mac. SystemConfiguration's `SCNetworkInterfaceGetHardwareAddressString` (and `ifconfig`) give the real one; `discovery.hardware_addresses` uses it for the HostID. The scanner accepts any client MAC, so the backend sends a locally administered MAC derived from the host UUID instead (`host_mac` in `fujitsu-vens.c`); otherwise two Macs would share one reservation.
- ScanSnap Home's helpers (`SshResident` and others) must be quit, or the scanner reports busy.

## Wi-Fi setup over USB (static analysis of ScanSnap Home's PfuSsWifiTool, 2026-11-22)

Verified on this iX500 over USB (2026-11-22): reading the allowed computers (`GET HOSTID LEN`/`GET HOSTID DATA`: one entry, this Mac), the network setup version (opcode 0xA: "02.02", so opcodes 6 and 7), the network it is set up for (opcode 7) and the networks it sees (opcode 6, signal in dBm). The session needs the scanner's Wi-Fi switch on: with it off, `SETUP NET START` answers 0x81. Not yet sent to the scanner: joining (0x1006), allowing a computer (`CLR`/`REG HOSTID`). The command layouts are in `fujitsu-scsi.h` (`NET_*`, `SD_wifi_*`) and the backend's `wifi_*` functions.

Two separate settings: the Wi-Fi network the scanner joins, and the computers allowed to scan over Wi-Fi (HostIDs). Phones (the ScanSnap iOS app) use the scanner's password, not that list. The app shows only the network; setting up Wi-Fi also adds this Mac to the list when it is not on it, as ScanSnap Home's setup does, and says so only when that removes another computer (owner, 2026-11-22). The scanner's WPS button joins a network without any app.

### Details

- On the iX500, pairing and Wi-Fi settings work over **USB only**: `PFUScannerIF scn_WifiConnectPC_Register:`, `scn_Wifi_Setting` and `scn_IP_Setting` do nothing over the network. They are SCSI SEND DIAGNOSTIC (`1D 00 00 LL LL 00`) with a 16-byte ASCII command, then RECEIVE DIAGNOSTIC RESULTS (`1C`).
- Pairing: `"CLR HOSTID ADATA"`, then `"REG HOSTID DATA "` + HostID (8 bytes, big-endian) + 8 zero bytes; reply byte 0 & 0x7f == 0 is success. PfuSsWifiTool (`CLR` then `REG`) and ScanSnap Home (`DoSetHostID:withDeviceHostID:`, delete-all then register) both clear the table first, so pairing evicts every other computer.
- Wi-Fi: a `"SETUP NET START "` … `"SETUP NET END   "` session of `"SETUP NET CMD   "` commands (opcode 0x1006 SSID and security, 0x1003 IP, 0x1002 name and password, 3 the access points it sees); the passphrase goes through the same cipher as the pairing key, with the iX500's table `aQweSrTywuIoppAsFdfGuhjkslZxCcvBAnmqNweRstyuNiopaAsdPfghajKlPzxc`.
- Over Wi-Fi, ScanSnap Home sends its HostID operation (tunnel CDB `DC`) only to model code 0x32, never to the iX500.
- Anything that changes the scanner's settings is sent only with the owner's go-ahead.

## How ScanSnap Home makes a "document"

All on the Mac, in `SsSvcImgFrmwk.framework` (bundles OpenCV and ONNX models); the scanner only scans in color. The "Document" profile sets Color Mode Auto, Mono Level 138, Blank Skip on. Steps: `PaperCrop`/`AutoSize`, `EraseEdgeShadow`, `DeskewImageWithIPP`, `DetectOrientation3`, `JudgeBlankPage`, `RemovalBleedThrough`, `IsColorMode`, `Gray2Mono`. Ghidra 12.2 (Homebrew formula `ghidra`, with OpenJDK 25) and REA (`npx rea-agents`, run with Homebrew's Node 26 because asdf pins Node 22.11) decompile `SsSvcPCWIFIFrmwk`.

## Design decisions

- **SANE does the scanner; `openscansnap.sheet` does the geometry.** The fujitsu backend already supports the iX500 over USB. Over Wi-Fi only the commands differ: `set_window`, `object_position`, `start_scan` and `read_from_scanner` branch to `vens_*` functions, and each decoded JPEG lands in `s->buffers[side]` in the packed-RGB layout the iX500's USB path produces, so everything downstream is unchanged. `vens_init_model()` switches off features whose commands are rejected; `has_cmd_scanner_ctl` matters most (otherwise every batch stalls 60 s retrying lamp-on).
- `check_status()` alone applies the busy/REQUEST SENSE/short-read rule for USB and Wi-Fi. Sense data may only shorten the received count: a scanner-supplied residual once wrapped the length and crashed `scanimage` (test: `test_bogus_residual_is_ignored`).
- A side is one READ that must hold a complete JPEG; `vens_decode` accepts only a baseline color JPEG within the scanner's area at the scan resolution, and treats libjpeg warnings (corrupt or missing data) as errors.
- Each `wifi` line becomes a `struct fujitsu_wifi_line` with the options in effect for it and its last attach status; `attach_one` gets the line, never the file-wide option globals. `sane_open` retries a failed line with that line's own options and returns the real status (busy, access denied) instead of "Invalid argument"; `openscansnap.sane` turns it into an instruction. Never attach a name the caller supplies: that would copy an unbounded string and send the stored password to whatever host the caller names.
- The transport's exported functions carry SANE's required `sanei_` prefix (`doc/backend-writing.txt`, `make libcheck`).
- A reservation lapses 25–30 s after the last command, so `vens_set_window` reconnects when the connection has been idle longer than `VENS_IDLE_MAX`. Cancelling over Wi-Fi ends the job (`OP_Halt`), since SCANNER CONTROL is rejected.
- `vens_init_model` also switches off the MODE SENSE and SEND DIAGNOSTIC probes at attach; `test_duplex_scan_uses_only_wifi_commands` asserts that the firmware rejects no command.
- The backend never logs the scanner's serial number or a config line's arguments: the serial's last 4 characters are the default password.
- The config keyword is `wifi`, not SANE's usual `net`: fujitsu's networked fi-series scanners use a different protocol, so `net` would be ambiguous in this backend.
- Credentials: the SANE config is written through `sane.write_private` (directory 0700, files 0600, replaced whole by `files.replace_file`, which replaces a link instead of following it); the password is asked with `getpass` or the menu's secure field, never taken from the command line.
- The SANE config has two writers: the monitor thread (automatic setup, following the scanner, learning its MAC) and the menu (the password). `scanner.update` re-reads and writes under `scanner.CONFIGURING`, so neither loses the other's change. Automatic setup runs only when the file is absent; a damaged file is reported (`sane.configured` raises), never overwritten.
- `fujitsu.c` defines `DEBUG_NOT_STATIC` so `fujitsu-vens.c` can log; without it `DBG` there jumps to address 0.
- **Page decisions are per page** (`openscansnap/pages.py`). The blank cutoffs `NO_INK`, `INK` and `PICTURE_SHARE` rest on one real iX500 sheet (printed front, blank back with show-through) and synthetic sides (paper noise, a pencil line, a block photo); the values are in the comment above them. Between `NO_INK` and `INK` a side is kept if it has readable text that does not read better mirrored, or, when nothing reads as text, if its darker class covers at least `PICTURE_SHARE` of it. Measure more real sheets before changing these.
- On black-and-white pages, `pages.clear_edge_marks` clears only marks that touch the page edge and lie within `EDGE_BAND_MM` of it: the iX500's edge shadow (a 0.9 mm bar along the edge) and dust. Two rules failed: removing every shape that touches the band (`skimage.segmentation.clear_border`) erased table rules that run off the edge; clearing the whole band cut the top off a letterhead seal 2-3 mm from the edge (2026-11-22).
- The app's settings (scans folder, window toggle, debug, scan options) are in `~/.config/open-scansnap/config.toml` over `openscansnap/defaults.toml`, the only home of their defaults (`config.default_options`); the menu writes them with `config.save`, which refuses what `config.load` would.
- A failed batch keeps its work: `sane.ScanStopped` carries the sides scanned before the failure, and the scan command saves them as the PDF. Scanned sides live in a temporary folder outside any automatic cleanup until they are in the PDF; if the PDF cannot be written, they move next to it, or stay put if that folder is the problem. A fallback must not depend on what just failed.
- Scans are taken whole (no `--swcrop`/`--swdeskew`); `sheet.straighten_and_crop` finds the sheet. SANE's `sanei_magic_findEdges` places the top from where both side edges first show, which on the iX500's light gray backing (≈234-238 against white paper) and its soft leading edge (a 1.7 mm ramp on the front sensor) lands up to 7 mm inside the paper; how much depends on which face and end go in first. ImageMagick `-trim` needs a tolerance tuned to the backing; `pschichtel/auto-crop` found no paper on the front side (2026-11-22). `sheet.py`: the backing's tones and noise come from the scan's first and last rows (the scanner always captures backing before and after a sheet); paper is brighter than every backing tone; each sample line's edge is the half-way crossing between backing and paper levels; the Theil-Sen estimator (median of pairwise slopes) fits one line per side, robust to stray points and to a wavy, curled leading edge, which a strict fit (RANSAC within 0.2 mm) rejected; the tilt comes from the left and right sides, which run along the feed; edges found again crop. A side whose samples disagree is not cropped (never cut paper).
- Binarization is Otsu's global threshold on the page as scanned or background-normalized (`pages.flatten`: paper brightness from paper pixels, interpolated under the ink at the page's own scale), chosen by Tesseract. Sauvola's local threshold turned fold creases (paper 227-247 against 254) into solid black lines; do not bring it back.
- Color: a page is color when any ink mark at least 6 pt in size has median CIELAB chroma C* >= 15, or Hasler's colorfulness says so (photos). Measured: black print, creases and show-through at most C* 8 on the iX500; blue ballpoint 20 and up. Pixel-level chroma rules failed: paper tint along creases and the colored ink itself kept contaminating the reference (2026-11-22).
- Show-through (`showthrough.clean`, rule in its docstring) runs per sheet in `job.assemble`, after both sides are cropped: the back is the front mirrored left to right (offset 3-10 px on the iX500), aligned by phase correlation. Rules that failed on real sheets (2026-11-22): a darkness cut from the sheet's measured pass-through, and a fixed spread around the other side's ink (leftover specks reached 2-3 px past it, and the spread varied 3-10 px per side). What separates copies from real marks is the share on the other side's ink: copies 73-100%, real marks crossing it at most 41%. "Lighter than the other side" keeps a side's own print when the other side shows only that print coming through (letter, 2026-11-22). Sides arrive front, back, front, back (`SIDES_PER_SHEET`).
- Debug mode (menu, `debug` setting) keeps every scan's raw sides in `~/Library/Logs/open-scansnap/raw/<scan>/` and logs at DEBUG. Use those raws to reproduce a bad scan offline; that is how the crop, crease and color fixes were found.
- Scanning and PDF making are separate: `job.scan` (scanner) and `job.finish` (PDF); the monitor's second thread makes PDFs so the scanner is free at once. Quit waits for PDFs in progress.
- Updates: Sparkle 2 (MIT). `updates.updater()` loads `Sparkle.framework` from the app bundle; the feed is the newest GitHub release's `appcast.xml` (`SUFeedURL`); `scripts/build-macos.sh` writes it with Sparkle's `generate_appcast`, signing with the Keychain key of account `open-scansnap` (public key `SUPublicEDKey` in the spec). Publish `dist/OpenScanSnap-<version>.dmg` and `dist/appcast.xml` as assets of GitHub release `v<version>`.
- Only `pages.upright()` rotates pages; OCRmyPDF does not rotate.

## Dead ends (do not retry)

- SANE's standard scan commands over Wi-Fi: rejected by the firmware (see above).
- Hand-tuned thresholds or a fixed edge band from one sheet: the owner rejected them.
- SANE `--swskip`: an absolute darkness percentage, so paper tone decides blankness.
- Otsu's effectiveness measure η alone for blank detection: a uniform tone spread scores exactly 0.75, and grey backing in frame looks like ink.
- Tesseract confidence 14 (OCRmyPDF's default) as a fixed orientation cutoff: sparse pages fall below it; comparing reading before and after the turn works.
- unpaper (through OCRmyPDF or alone): leaves the edge-shadow bar; its mask scan shifts content.
- ImageMagick `-trim`: fails on the noisy backing.
- ScanTailor Advanced: no command line, no Homebrew formula. NAPS2's command line: no blank-page option.
- Python VENS client (upstream bramheerink/scansnap, removed): superseded by the SANE transport.

## Later: menu bar app and scan button

Follow the owner's `~/src/cannonball` pattern (`cannonball/menubar.py`, `status.py`, `localnet.py`, `services.py`, `packaging/macos/`, `.architecture/history/2026-11-20-macos-local-network.md`): plain PyObjC pinned by hash (not `rumps`); one `LSUIElement` app with the worker on a thread, no launchd agent (macOS denied an agent Local Network access without a prompt); a frozen status snapshot with one writer; a minimal menu; trigger the Local Network prompt with a UDP connect; `SMAppService.mainAppService` for login; `NSLocalNetworkUsageDescription` and `en.lproj/InfoPlist.strings` in the bundle. Testing from Terminal proves nothing about Local Network access: macOS grants it to Terminal, not to the program Terminal starts.

Scan button (verified 2026-11-22): SANE's `scan` hardware sensor (GET HARDWARE STATUS) works over Wi-Fi. A press of any length shows as `scan = 1` for one reading at 0.2 s polling, so react to its 0 → 1 edge. `page-loaded` goes 0 → 1 when a sheet is put in the feeder. Polling also keeps the reservation alive. ScanSnap Home's push route (trigger port 55265, `SsSvcPCWIFISCTriggerController`) is not needed. Read the sensors in-process through a `ctypes` binding to `sane-prefix`'s libsane (as the 2026-11-22 probe did): `python-sane` is published only as a source distribution, and this project installs wheels only. Close the handle before `scanimage` scans: both reserve the scanner.

Owner's choices for the app: menu bar icon is a Futura Bold "S" drawn as a template image (different from ScanSnap's slanted S), with a Wi-Fi mark or a USB plug (as large as the letter) for how the scanner is connected, dimmed while it is off; no window pops up when the scanner opens. Menu items are in sentence case ("Show window when paper is loaded", "this Mac"), not title case (owner, 2026-11-22). The menu has a status line (no "Last scan" line), Scan, the scans folder, Scan mode, Quality, Processing, while on USB the scanner's Wi-Fi network and "Set up Wi-Fi…", Show window when paper is loaded, Debug mode, About, Check for updates and Quit. The app uses the scanner on USB when it is plugged in, else over Wi-Fi (`monitor.Connector`; the backend opens `fujitsu:usb` without reaching for Wi-Fi scanners). The window is a non-activating floating panel with a large Scan button and the scanner state.

Owner's choices for the scan settings (2026-11-22): the most capable result by default, every step switchable. No master "post-processing off" switch: As Scanned is that, and one mode menu leaves no contradictory combination. Quality names rise plainly: Draft, Standard, High (not ScanSnap's names, whose "Best" sat below "Excellent"). No quality above the scanner's 300 dpi until a scan proves the iX500 sends more over Wi-Fi: an upsampled scan is not finer. A step a mode rules out shows off and disabled (`Options.in_effect`), so the menu never shows a setting that is not in effect. A scan reads the settings when it starts and keeps them until its PDF is written.
