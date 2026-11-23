<p align="center"><img src="docs/images/icon.png" width="128" alt=""></p>

# Open ScanSnap

Scan from a ScanSnap iX500 on your Mac, over Wi-Fi or USB, without ScanSnap
Home.

<p>
  <img src="docs/images/menu.png" width="250" alt="The menu">
  <img src="docs/images/menu-processing.png" width="460" alt="The Processing menu">
</p>

## Features

- Lives in the menu bar; press the scanner's Scan button, or choose Scan.
- Makes one searchable PDF per batch, in your Downloads folder (or a folder
  you choose).
- Straightens and crops each page, turns it upright, and leaves out blank
  pages.
- Stores text pages in black and white, and keeps color where there is color.
- Removes the other side's print showing through the paper, and the shadows
  of folds.
- Shows each scan's pages after scanning, so you can store any of them
  another way (in black and white, gray, color or as a photo, or not at all)
  and update the PDF.
- Recognizes each page's language, so its text can be searched.
- Works over Wi-Fi or with the USB cable, and sets up the scanner's Wi-Fi.
- Updates itself.

## Install

1. Make sure ScanSnap Home is not running: only one app can use the
   scanner. To remove it for good, as PFU describes for macOS:
   1. Quit ScanSnap Home, including its menu bar item.
   2. In Finder, open **Applications**, drag **ScanSnap Home** to the Trash,
      and empty the Trash.
   3. Then remove ScanSnap Online Update, which came with it: open
      **Applications ▸ ScanSnap Online Update ▸ Sub**, double-click
      **AOUUninstaller**, click **Continue**, then **Uninstall**, and type
      your password.
2. Download the newest `OpenScanSnap-*.dmg` from
   [Releases](https://github.com/thomergil/open-scansnap/releases).
3. Drag Open ScanSnap to Applications and open it.

Open ScanSnap needs a Mac with Apple silicon and macOS 13 or later.

## First use

- **On Wi-Fi:** if the scanner is already on your Wi-Fi network (set up by
  ScanSnap Home or with its WPS button), Open ScanSnap finds it by itself.
- **On USB:** plug the scanner in. To put it on your Wi-Fi network, choose
  **Set up Wi-Fi…** in the menu, pick the network, and type its password.

Put paper in the feeder and press the scanner's Scan button.

<p><img src="docs/images/menu-scan-mode.png" width="460" alt="The Scan mode menu"></p>

## FAQ

**A page came out wrong, or a page I need was left out.**
Choose **Review last scan…** in the menu, select the page (Shift-click or
⌘-click to select several), choose how to store it in **Store as** (the
preview shows the page stored that way), and click **Update PDF**.
The review window opens after each scan while **Show review after each scan**
is on in the menu; its checkbox **Open this window after each scan** turns
the same setting on or off.

**Where is the last scan kept, and for how long?**
To let you review it, Open ScanSnap keeps your latest scan as the scanner
sent it (about 7 MB per side, blank sides included) in
`~/Library/Application Support/open-scansnap/review`, readable only by you.
The next scan replaces it. If you delete its PDF, Open ScanSnap removes it
at the next scan or the next time it starts.

**Characters or words are missing from my scan.**
Turn off **Processing ▸ Remove show-through**, scan again, and please
[report it](https://github.com/thomergil/open-scansnap/issues/new) with the
scan if you can share it.

**The text in my PDF is not searchable in my language.**
Text language is Automatic unless you changed it: each page's language is
recognized. If it is recognized wrongly, choose your language in
**Processing ▸ Text language**.

**The scanner is not found.**
Turn it on (open its paper chute), check that it is on the same network as
your Mac, and allow Open ScanSnap in **System Settings ▸ Privacy & Security
▸ Local Network**.

**The menu says the scanner is in use by another app.**
Quit ScanSnap Home, including its menu bar item.

**Can Open ScanSnap update the scanner's firmware?**
No. Use ScanSnap Home for that (reinstall it from PFU's downloads, and quit
it again afterwards); the menu shows a ⚠ when newer firmware exists.
Choosing the scanner's line in the menu opens PFU's downloads.

**Which scanners work?**
The ScanSnap iX500. Others may work over USB but are untested.

## For developers

How it works, how to build it and run it from the command line:
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

## License

Open ScanSnap is MIT-licensed; see [LICENSE](LICENSE). The software it is
built on keeps its own licenses; their full texts are inside the app, in
`Open ScanSnap.app/Contents/Resources/Licenses`.

## Credits

[bramheerink/scansnap](https://github.com/bramheerink/scansnap) first worked
out the scanner's Wi-Fi protocol.

Open ScanSnap is built on this free software, all of it inside the app:

- **Scanning:** [SANE](https://gitlab.com/sane-project/backends)
  (sane-backends, GPL-2.0-or-later with the SANE exception; Open ScanSnap
  adds the ScanSnap's Wi-Fi to its fujitsu backend) and libusb.
- **Text recognition:** [Tesseract](https://github.com/tesseract-ocr/tesseract)
  with its [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast)
  language data, [Leptonica](http://www.leptonica.org/) (which also measures
  how far a page's text is turned), [OCRmyPDF](https://github.com/ocrmypdf/OCRmyPDF)
  and [py3langid](https://github.com/adbar/py3langid).
- **Images and PDFs:** [Pillow](https://python-pillow.org/),
  [NumPy](https://numpy.org/), [SciPy](https://scipy.org/),
  [scikit-image](https://scikit-image.org/),
  [img2pdf](https://gitlab.mister-muffin.de/josch/img2pdf),
  [pikepdf](https://github.com/pikepdf/pikepdf) with QPDF,
  [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) with PDFium,
  fpdf2, fontTools, uharfbuzz with HarfBuzz, pdfminer.six, imageio, tifffile,
  libjpeg-turbo, libpng, libtiff, giflib, WebP and OpenJPEG.
- **The app:** [Python](https://www.python.org/),
  [PyObjC](https://github.com/ronaldoussoren/pyobjc),
  [Sparkle](https://sparkle-project.org/) (updates),
  [PyInstaller](https://pyinstaller.org/) (the app bundle), psutil, tomli-w,
  and Tesseract's compression libraries libarchive, xz, zstd, lz4 and libb2.
- **Their Python dependencies:** annotated-types, attrs, cffi,
  charset-normalizer, cryptography, jsonschema, jsonschema-specifications,
  lazy-loader, lxml, markdown-it-py, mdurl, networkx, packaging, pluggy,
  pycparser, pydantic, pydantic-core, Pygments, referencing, rich, rpds-py,
  typing-extensions and typing-inspection.

ScanSnap and ScanSnap Home are trademarks of PFU Limited, a Ricoh company.
Open ScanSnap is not affiliated with or endorsed by PFU or Ricoh. Mac and
macOS are trademarks of Apple Inc.
