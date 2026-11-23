"""Every bundled language is detected as itself: Article 1 of the Universal
Declaration of Human Rights in each, drawn by macOS (which shapes Arabic,
Persian, Hindi and Bengali), read with the bundled language data."""

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image
from test_languages import languagelist

from openscansnap import languages, pages

ROOT = Path(__file__).resolve().parent.parent
DPI = 300
WIDTH, HEIGHT = 2550, 1600  # pixels: the top of a letter page
POINTS = 14
REPEATS = 2  # enough text for Tesseract to name the script
ARTICLE_1 = {
    "eng": "All human beings are born free and equal in dignity and rights. They are endowed with reason and conscience and should act towards one another in a spirit of brotherhood.",
    "spa": "Todos los seres humanos nacen libres e iguales en dignidad y derechos y, dotados como están de razón y conciencia, deben comportarse fraternalmente los unos con los otros.",
    "fra": "Tous les êtres humains naissent libres et égaux en dignité et en droits. Ils sont doués de raison et de conscience et doivent agir les uns envers les autres dans un esprit de fraternité.",
    "deu": "Alle Menschen sind frei und gleich an Würde und Rechten geboren. Sie sind mit Vernunft und Gewissen begabt und sollen einander im Geist der Brüderlichkeit begegnen.",
    "ita": "Tutti gli esseri umani nascono liberi ed eguali in dignità e diritti. Essi sono dotati di ragione e di coscienza e devono agire gli uni verso gli altri in spirito di fratellanza.",
    "por": "Todos os seres humanos nascem livres e iguais em dignidade e em direitos. Dotados de razão e de consciência, devem agir uns para com os outros em espírito de fraternidade.",
    "nld": "Alle mensen worden vrij en gelijk in waardigheid en rechten geboren. Zij zijn begiftigd met verstand en geweten, en behoren zich jegens elkander in een geest van broederschap te gedragen.",
    "swe": "Alla människor är födda fria och lika i värde och rättigheter. De har utrustats med förnuft och samvete och bör handla gentemot varandra i en anda av broderskap.",
    "dan": "Alle mennesker er født frie og lige i værdighed og rettigheder. De er udstyret med fornuft og samvittighed, og de bør handle mod hverandre i en broderskabets ånd.",
    "nor": "Alle mennesker er født frie og med samme menneskeverd og menneskerettigheter. De er utstyrt med fornuft og samvittighet og bør handle mot hverandre i brorskapets ånd.",
    "fin": "Kaikki ihmiset syntyvät vapaina ja tasavertaisina arvoltaan ja oikeuksiltaan. Heille on annettu järki ja omatunto, ja heidän on toimittava toisiaan kohtaan veljeyden hengessä.",
    "pol": "Wszyscy ludzie rodzą się wolni i równi pod względem swej godności i swych praw. Są oni obdarzeni rozumem i sumieniem i powinni postępować wobec innych w duchu braterstwa.",
    "ces": "Všichni lidé rodí se svobodní a sobě rovní co do důstojnosti a práv. Jsou nadáni rozumem a svědomím a mají spolu jednat v duchu bratrství.",
    "slk": "Všetci ľudia sa rodia slobodní a sú si rovní v dôstojnosti i právach. Sú obdarení rozumom a svedomím a majú spolu navzájom jednať v bratskom duchu.",
    "hun": "Minden emberi lény szabadon születik és egyenlő méltósága és joga van. Az emberek, ésszel és lelkiismerettel bírván, egymással szemben testvéri szellemben kell hogy viseltessenek.",
    "ron": "Toate ființele umane se nasc libere și egale în demnitate și în drepturi. Ele sunt înzestrate cu rațiune și conștiință și trebuie să se comporte unele față de altele în spiritul fraternității.",
    "hrv": "Sva ljudska bića rađaju se slobodna i jednaka u dostojanstvu i pravima. Ona su obdarena razumom i sviješću pa jedna prema drugima trebaju postupati u duhu bratstva.",
    "slv": "Vsi ljudje se rodijo svobodni in imajo enako dostojanstvo in enake pravice. Obdarjeni so z razumom in vestjo in bi morali ravnati drug z drugim kakor bratje.",
    "tur": "Bütün insanlar hür, haysiyet ve haklar bakımından eşit doğarlar. Akıl ve vicdana sahiptirler ve birbirlerine karşı kardeşlik zihniyeti ile hareket etmelidirler.",
    "vie": "Tất cả mọi người sinh ra đều được tự do và bình đẳng về nhân phẩm và quyền lợi. Mọi con người đều được tạo hóa ban cho lý trí và lương tâm và cần phải đối xử với nhau trong tình anh em.",
    "ind": "Semua orang dilahirkan merdeka dan mempunyai martabat dan hak-hak yang sama. Mereka dikaruniai akal dan hati nurani dan hendaknya bergaul satu sama lain dalam semangat persaudaraan.",
    "msa": "Semua manusia dilahirkan bebas dan samarata dari segi kemuliaan dan hak-hak. Mereka mempunyai pemikiran dan perasaan hati dan hendaklah bertindak di antara satu sama lain dengan semangat persaudaraan.",
    "rus": "Все люди рождаются свободными и равными в своем достоинстве и правах. Они наделены разумом и совестью и должны поступать в отношении друг друга в духе братства.",
    "ukr": "Всі люди народжуються вільними і рівними у своїй гідності та правах. Вони наділені розумом і совістю і повинні діяти у відношенні один до одного в дусі братерства.",
    "bul": "Всички хора се раждат свободни и равни по достойнство и права. Те са надарени с разум и съвест и следва да се отнасят помежду си в дух на братство.",
    "ell": "Όλοι οι άνθρωποι γεννιούνται ελεύθεροι και ίσοι στην αξιοπρέπεια και τα δικαιώματα. Είναι προικισμένοι με λογική και συνείδηση, και οφείλουν να συμπεριφέρονται μεταξύ τους με πνεύμα αδελφοσύνης.",
    "ara": "يولد جميع الناس أحرارًا متساوين في الكرامة والحقوق. وقد وهبوا عقلاً وضميرًا وعليهم أن يعامل بعضهم بعضًا بروح الإخاء.",
    "fas": "تمام افراد بشر آزاد به دنیا می‌آیند و از لحاظ حیثیت و حقوق با هم برابرند. همه دارای عقل و وجدان هستند و باید نسبت به یکدیگر با روح برادری رفتار کنند.",
    "heb": "כל בני האדם נולדו בני חורין ושווים בערכם ובזכויותיהם. כולם חוננו בתבונה ובמצפון, לפיכך חובה עליהם לנהוג איש ברעהו ברוח של אחווה.",
    "hin": "सभी मनुष्यों को गौरव और अधिकारों के मामले में जन्मजात स्वतन्त्रता और समानता प्राप्त है। उन्हें बुद्धि और अन्तरात्मा की देन प्राप्त है और परस्पर उन्हें भाईचारे के भाव से बर्ताव करना चाहिये।",
    "ben": "সমস্ত মানুষ স্বাধীনভাবে সমান মর্যাদা এবং অধিকার নিয়ে জন্মগ্রহণ করে। তাঁদের বিবেক এবং বুদ্ধি আছে; সুতরাং সকলেরই একে অপরের প্রতি ভ্রাতৃত্বসুলভ মনোভাব নিয়ে আচরণ করা উচিত।",
    "tha": "มนุษย์ทั้งหลายเกิดมามีอิสระและเสมอภาคกันในเกียรติศักดิ์และสิทธิ ต่างมีเหตุผลและมโนธรรม และควรปฏิบัติต่อกันด้วยเจตนารมณ์แห่งภราดรภาพ",
    "chi_sim": "人人生而自由，在尊严和权利上一律平等。他们赋有理性和良心，并应以兄弟关系的精神相对待。",
    "chi_tra": "人人生而自由，在尊嚴和權利上一律平等。他們賦有理性和良心，並應以兄弟關係的精神相對待。",
    "jpn": "すべての人間は、生まれながらにして自由であり、かつ、尊厳と権利とについて平等である。人間は、理性と良心とを授けられており、互いに同胞の精神をもって行動しなければならない。",
    "kor": "모든 인간은 태어날 때부터 자유로우며 그 존엄과 권리에 있어 동등하다. 인간은 천부적으로 이성과 양심을 부여받았으며 서로 형제애의 정신으로 행동하여야 한다.",
}
# read together: the identifier does not tell them apart
TOGETHER = {
    "chi_sim": "chi_sim+chi_tra",
    "chi_tra": "chi_sim+chi_tra",
    "ind": "ind+msa",
    "msa": "ind+msa",
}


def bundled_tessdata() -> Path | None:
    """The build's cache of the bundled language data, if it is there."""
    cache = ROOT / "build" / "cache" / f"tessdata-{languagelist.commit()}"
    return cache if cache.is_dir() else None


def page(text: str) -> Image.Image:
    """text drawn by macOS on a page at DPI."""
    from AppKit import (
        NSAttributedString,
        NSBitmapImageRep,
        NSCalibratedRGBColorSpace,
        NSColor,
        NSFont,
        NSFontAttributeName,
        NSGraphicsContext,
        NSMakeRect,
        NSRectFill,
        NSStringDrawingUsesLineFragmentOrigin,
    )

    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, WIDTH, HEIGHT, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    )
    NSColor.whiteColor().set()
    NSRectFill(NSMakeRect(0, 0, WIDTH, HEIGHT))
    string = NSAttributedString.alloc().initWithString_attributes_(
        " ".join([text] * REPEATS),
        {NSFontAttributeName: NSFont.systemFontOfSize_(POINTS * DPI / 72)},
    )
    margin = DPI  # an inch
    string.drawWithRect_options_(
        NSMakeRect(margin, margin, WIDTH - 2 * margin, HEIGHT - 2 * margin),
        NSStringDrawingUsesLineFragmentOrigin,
    )
    NSGraphicsContext.restoreGraphicsState()
    data = np.frombuffer(
        rep.bitmapData(), dtype=np.uint8, count=HEIGHT * rep.bytesPerRow()
    )
    rgba = data.reshape(HEIGHT, rep.bytesPerRow())[:, : WIDTH * 4].reshape(
        HEIGHT, WIDTH, 4
    )
    return Image.fromarray(rgba[..., :3].copy())


@unittest.skipUnless(importlib.util.find_spec("AppKit"), "needs macOS")
@unittest.skipUnless(shutil.which(pages.TESSERACT), "needs Tesseract")
@unittest.skipUnless(
    bundled_tessdata(), "needs the build's language data (scripts/build-macos.sh)"
)
class AllLanguagesTest(unittest.TestCase):
    def setUp(self):
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited

        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyProhibited
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        tessdata = Path(tmp.name)
        homebrew = (
            Path(shutil.which(pages.TESSERACT)).resolve().parent.parent
            / "share"
            / "tessdata"
        )
        for source in (bundled_tessdata(), homebrew):
            for data in source.glob("*.traineddata"):
                (tessdata / data.name).symlink_to(data)
        patcher = mock.patch.dict(os.environ, {"TESSDATA_PREFIX": str(tessdata)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.installed = set(pages.installed_languages())

    def test_every_bundled_language_is_detected_as_itself(self):
        self.assertEqual(self.installed - {"osd"}, set(ARTICLE_1))  # all covered
        for code, text in ARTICLE_1.items():
            expected = TOGETHER.get(code, code)
            with self.subTest(language=code):
                self.assertEqual(
                    languages.detect(pages.Detection(page(text), DPI), self.installed),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
