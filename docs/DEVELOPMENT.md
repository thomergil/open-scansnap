# Open ScanSnap: development

How the app works, how to build it, and how to run it from the command
line. For using the app, see the [README](../README.md).

Scan from a Fujitsu ScanSnap iX500 over Wi-Fi into an upright, searchable
PDF, without ScanSnap Home: from a menu bar app (see The app below) or from
the command line. After Install and Set up below, run from the repository
root:

```sh
.venv/bin/python -m openscansnap scan     # writes ~/Downloads/scan_<date>_<time>.pdf
```

## How it works

Each step below runs as far as the scan's settings ask (see Settings).

1. **SANE** drives the scanner. The `fujitsu` backend already supports the
   iX500 over USB; `sane-backends/` (branch `vens`) adds its Wi-Fi
   connection. SANE scans both sides of every sheet in color.
2. **`openscansnap.sheet`** finds the sheet in each side: its four edges,
   estimated every 0.7 mm along each edge and fitted with straight lines
   (the Theil-Sen estimator), give its tilt and its outline; the side is straightened and
   cropped to them, whichever way the sheet went in.
3. **`openscansnap.showthrough`** paints out the other side's print where it
   shows or bleeds through the paper: the scanner reads both sides, so the
   other side is known (mirrored and aligned to this one).
4. **`openscansnap.pages`** decides, page by page, from measurements of
   that page:
   - drop the side if it is blank: nothing on it is clearly darker than the
     paper, or it has only faint marks that either Tesseract reads better
     mirrored (show-through from the other side) or, when they do not read
     as text, cover less than a fifth of the side;
   - turn it upright (Tesseract's orientation detection, kept only if the
     page then reads better);
   - keep color if any ink on it is colored (a pen stroke, a stamp: its
     CIELAB chroma) or the page as a whole is colorful (a photo);
   - otherwise make it black and white with Otsu's threshold, on the page
     as scanned or with the paper's own shading (folds, uneven light)
     divided out first, whichever Tesseract reads best, and clear marks that touch
     the paper edge and lie within 3 mm of it (the edge's shadow, dust);
     if neither yields readable text, as for a photo, keep it
     grayscale. Black and white is stored at twice the resolution of color
     and gray, so letter edges stay smooth.
5. **OCRmyPDF** adds the text layer. Black-and-white pages are stored
   lossless: CCITT G4, or JBIG2 if `jbig2enc` is installed. Color and
   grayscale pages are stored as JPEG.

If scanning fails partway through a batch, the PDF holds the sides scanned
before the failure, and the message says so. If the PDF cannot be written,
the scanned sides are
kept, and the message says where: in a folder next to where the PDF would
have gone, or, if that folder cannot be written, in a temporary folder.

## Install (macOS)

```sh
brew install autoconf automake autoconf-archive libtool pkgconf libusb \
    jpeg-turbo tesseract ghostscript poppler python@3.13
```

Build SANE into `sane-prefix/`. The Wi-Fi support is on the `vens` branch
of `sane-backends/`, which is not published yet, and `.gitignore` excludes
`sane-backends/`. A fresh clone of this repository therefore has no
`sane-backends/`; only a checkout that already holds it can build Wi-Fi
scanning:

```sh
cd sane-backends
autoreconf -fi
./configure --prefix="$PWD/../sane-prefix" BACKENDS=fujitsu \
    --disable-locking --without-snmp --without-avahi --disable-nls \
    --without-usb-record-replay \
    CPPFLAGS="-I$(brew --prefix jpeg-turbo)/include" \
    LDFLAGS="-L$(brew --prefix jpeg-turbo)/lib"
make && make install
cd ..
```

Check that `./configure` printed `checking for jpeg_start_decompress in
-ljpeg... yes`. Wi-Fi scanning needs SANE built with libjpeg-turbo (or
libjpeg 8 or later); without it, the backend does not list the scanner.

Install the Python dependencies, pinned by hash:

```sh
python3.13 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary :all: -r requirements.txt
```

## Set up

The scanner must be on your Wi-Fi network and paired with this Mac: it
accepts only the computer it was paired with, known by its HostID.
ScanSnap Home pairs it when it sets up the scanner's Wi-Fi.

The app then needs nothing more. It searches the network for the scanner
as ScanSnap Home does, uses this Mac's HostID as ScanSnap Home computes it,
and finds the scanner again when its IP address changes. If you changed
the scanner's password (by default the last 4 characters of its serial
number), the menu offers Scanner Password… when the scanner refuses.

From the command line, run:

```sh
.venv/bin/python -m openscansnap setup
```

`--host` (the scanner's IP address), `--hostid` (in decimal, or in
hexadecimal after `0x`) and `--password` (to be asked for it) set what the
search cannot find. The configuration, readable only by you, is in
`~/.config/open-scansnap/sane.d/`.

The scanner's protocol has no encryption and nothing proves which device
is the scanner: the password, in a reversible encoding, and the HostID cross
your network in the clear, and the app, like ScanSnap Home, uses whatever
answers its search as the scanner. Use the scanner only on a network you
trust.

## Scan

Quit ScanSnap Home, including its menu bar item, or the scanner reports
itself busy. Put the sheets in the feeder and run:

```sh
.venv/bin/python -m openscansnap -v scan
```

`-v` says what happened to each side. `--out-dir` chooses another folder;
`--mode`, `--quality` and `--language` override those settings for this
scan.

## The app

`scripts/build-macos.sh` builds `dist/Open ScanSnap.app`, signed, and a
DMG. The app lives in the menu bar: its S shows a Wi-Fi mark while the
scanner is on. Press the scanner's Scan button, or choose Scan in the menu,
and the PDF is made in the background while the next sheets are scanned.

## USB and Wi-Fi

The app uses the scanner over its USB cable when it is plugged in, else
over Wi-Fi; the menu bar S shows a plug or the Wi-Fi mark.

While the scanner is on USB, with its Wi-Fi switch on, the menu shows the
Wi-Fi network it is set up for, and Set up Wi-Fi… lists the networks the
scanner sees (2.4 GHz only): choose one, type its password, and the scanner
joins it. That also lets this Mac scan over Wi-Fi; the scanner lets only
the computers on its list do that (phones use the scanner's password
instead). Pressing the WPS button on the scanner and on the router joins a
network without any app.

The menu shows the scanner's model and firmware; choosing it opens PFU's
ScanSnap downloads, and a ⚠ marks firmware older than the newest release.
Firmware updates are left to ScanSnap Home.

## Settings

The app's menu sets them; they are saved in
`~/.config/open-scansnap/config.toml`, which `openscansnap/defaults.toml`
documents.

- **Scan mode**: Automatic (black and white, gray or color, whichever suits
  each page), Black and White Document, Gray Document, Color Document,
  Photo (color, without blank-page removal, show-through removal or
  clean-up), or As Scanned (the scanner's image, uncropped and unturned).
- **Quality**: Draft, Standard or High: color and gray pages at 150, 200 or
  300 dpi, black and white at twice that.
- **Processing**: Straighten and crop to the sheet, Turn pages upright,
  Remove blank pages, Remove show-through, Clean up creases and shadows,
  Make searchable, and the Text language. All are on by default.
- The scans folder, Show window when paper is loaded, and Debug mode, which
  keeps every scan's sides as the scanner sent them, in
  `~/Library/Logs/open-scansnap/raw`.

The app comes with Tesseract's data for 36 languages, those of most
scanned documents (`packaging/macos/languages.txt`). From the checkout,
each text language needs its Tesseract data (`brew install
tesseract-lang`); missing data is reported before any paper moves.

## Limits

- Tested with an iX500 only. Over Wi-Fi it sends 300 dpi color JPEG, so
  that is the scan resolution. A side must fit in one 16 MB transfer.
- ScanSnap Home must not be running while you scan.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

`tests/fake_ix500.py` emulates the scanner's Wi-Fi firmware on localhost, so
the SANE backend is tested without hardware. Tests skip, and say why, when
what they need is missing: the Wi-Fi tests need SANE built into
`sane-prefix/`; the page and PDF tests need Tesseract and `pdftotext`
(poppler).

## Credits

[bramheerink/scansnap](https://github.com/bramheerink/scansnap) first
reverse-engineered the Wi-Fi protocol; this repository started as its
clone. Built on [SANE](https://gitlab.com/sane-project/backends),
[OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF),
[Tesseract](https://github.com/tesseract-ocr/tesseract),
[scikit-image](https://scikit-image.org/) and
[img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf).

## License

open-scansnap is MIT-licensed, see [LICENSE](../LICENSE). It uses other
programs under their own licenses: notably SANE (GPL-2.0 or later, with the
SANE exception that lets programs under any license use it), libusb and
img2pdf (LGPL), and OCRmyPDF and pikepdf (MPL-2.0). The Wi-Fi support in
`sane-backends/` changes SANE's GPL code and is GPL with it.
