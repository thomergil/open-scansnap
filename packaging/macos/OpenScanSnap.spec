# PyInstaller spec for Open ScanSnap.app; built by scripts/build-macos.sh,
# which sets ICON, LICENSES and TESSERACT_PREFIX.
import os
import sys

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, SPECPATH)
from licenses import REPLACEABLE_MODULES  # noqa: E402
from openscansnap import APP_NAME, BUNDLE_ID, __version__, bundle  # noqa: E402

MIN_MACOS = "13.0"  # SMAppService, for opening at login
DEVELOPMENT_REGION = "en"
LOCAL_NETWORK_USAGE = f"{APP_NAME} talks to your ScanSnap scanner over the local network."
# Sparkle updates: the feed is the appcast of the newest GitHub release; the
# key's private half is in the release signer's login Keychain, account
# "open-scansnap" (Sparkle's generate_keys --account open-scansnap).
UPDATE_FEED = "https://github.com/thomergil/open-scansnap/releases/latest/download/appcast.xml"
UPDATE_PUBLIC_KEY = "NfcKGFO/rbE7NQkQpTA1ADi/wZsgp9M2Vlnqa8NlaUA="
TESSDATA = os.path.join(os.environ["TESSERACT_PREFIX"], "share", "tessdata")
LANGUAGES = ("eng", "osd")  # English text, and orientation detection
# the other languages: packaging/macos/languages.txt, checked by the build
LANGUAGE_DIR = os.environ["LANGUAGE_DIR"]

# In the same places inside the app as in the checkout (openscansnap/bundle.py)
binaries = [
    (str(bundle.SCANIMAGE), "sane-prefix/bin"),
    (str(bundle.LIBSANE), "sane-prefix/lib"),
    (str(bundle.SANE_BACKENDS / "libsane-fujitsu.1.so"), "sane-prefix/lib/sane"),
    (os.path.join(os.environ["TESSERACT_PREFIX"], "bin", "tesseract"), "tesseract/bin"),
]
datas = [
    (os.path.join(ROOT, "openscansnap", "defaults.toml"), "openscansnap"),
    *[(os.path.join(TESSDATA, f"{language}.traineddata"), "tesseract/share/tessdata") for language in LANGUAGES],
    # output formats (tsv, hocr, pdf) and the font of the PDF text layer
    (os.path.join(TESSDATA, "configs"), "tesseract/share/tessdata/configs"),
    (os.path.join(TESSDATA, "tessconfigs"), "tesseract/share/tessdata/tessconfigs"),
    (os.path.join(TESSDATA, "pdf.ttf"), "tesseract/share/tessdata"),
    *[
        (os.path.join(LANGUAGE_DIR, name), "tesseract/share/tessdata")
        for name in sorted(os.listdir(LANGUAGE_DIR))
        if name.endswith(".traineddata")
    ],
    (os.environ["LICENSES"], "Licenses"),
    *collect_data_files("ocrmypdf"),
]

a = Analysis(
    [os.path.join(SPECPATH, "entry.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    # OCRmyPDF loads its built-in plugins by name
    hiddenimports=["ServiceManagement", "PyObjCTools.AppHelper", *collect_submodules("ocrmypdf")],
    excludes=["tkinter"],
    module_collection_mode={module: "py" for module in REPLACEABLE_MODULES},
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=APP_NAME, console=False, argv_emulation=False,
          icon=os.environ["ICON"])
coll = COLLECT(exe, a.binaries, a.datas, name=APP_NAME)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    bundle_identifier=BUNDLE_ID,
    icon=os.environ["ICON"],
    version=__version__,
    info_plist={
        "CFBundleDevelopmentRegion": DEVELOPMENT_REGION,
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": __version__,
        "CFBundleVersion": __version__,
        "LSUIElement": True,  # menu bar only, no Dock icon
        "LSMinimumSystemVersion": MIN_MACOS,
        "NSLocalNetworkUsageDescription": LOCAL_NETWORK_USAGE,
        "NSHumanReadableCopyright": "MIT License",
        "SUFeedURL": UPDATE_FEED,
        "SUPublicEDKey": UPDATE_PUBLIC_KEY,
        "SUEnableAutomaticChecks": True,
    },
)

# A standard localization: macOS's Local Network privacy reads the app's name
# and usage description through it, and fails for a bundle without one.
strings = {
    "CFBundleName": APP_NAME,
    "CFBundleDisplayName": APP_NAME,
    "NSLocalNetworkUsageDescription": LOCAL_NETWORK_USAGE,
}
lproj = os.path.join(DISTPATH, f"{APP_NAME}.app", "Contents", "Resources", f"{DEVELOPMENT_REGION}.lproj")
os.makedirs(lproj, exist_ok=True)
with open(os.path.join(lproj, "InfoPlist.strings"), "w", encoding="utf-8") as f:
    f.writelines(f'"{key}" = "{value}";\n' for key, value in strings.items())
