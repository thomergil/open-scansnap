"""The app icon: an iX500 seen from the front, its paper chute open, and a
Wi-Fi fan, in outline on the macOS rounded square, in the colors of the
owner's Cannonball app.

The running app sets it as its icon; the macOS build turns it into the
bundle's .icns. python -m openscansnap.appicon PREVIEW.png writes a preview.
"""

import sys
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSCalibratedRGBColorSpace,
    NSColor,
    NSGradient,
    NSGraphicsContext,
    NSImage,
    NSLineCapStyleRound,
    NSLineJoinStyleMiter,
    NSLineJoinStyleRound,
    NSPNGFileType,
    NSShadow,
)
from Foundation import NSMakePoint, NSMakeRect, NSMakeSize

# All in points of a 1024-point canvas, y up.
CANVAS = 1024
BODY = NSMakeRect(100, 100, 824, 824)  # macOS icon grid: 824 pt rounded square
CORNER = 185
BACKGROUND = ((0.20, 0.27, 0.40), (0.07, 0.09, 0.15))  # top, bottom
LINE = (0.96, 0.97, 0.99)
STROKE = 34

# The scanner from the front, after PFU's product photos: a boxy body with
# sharp corners, the open chute standing behind it, the output tray as a
# flat lip in front, and the blue light bar at the front right
CHUTE = [(285, 470), (615, 470), (615, 735), (285, 735)]
BODY_FACE = [(215, 300), (685, 300), (665, 485), (235, 485)]
TRAY = [(170, 255), (730, 255), (700, 300), (200, 300)]
LIGHT = NSMakeRect(545, 400, 95, 24)
LIGHT_COLOR = (0.30, 0.62, 1.00)  # the iX500's front light glows blue on Wi-Fi

BADGE_CENTER = (790, 690)  # a background disc behind the fan
BADGE_RADIUS = 135
FAN_COLOR = (0.98, 0.62, 0.15)
FAN_DOT = 26
FAN_RADII = (78, 130)
FAN_SPAN = (45, 135)  # degrees: the fan opens upward
FAN_CENTER = (790, 615)


def rgb(color, alpha=1.0) -> NSColor:
    return NSColor.colorWithCalibratedRed_green_blue_alpha_(*color, alpha)


def circle(center, radius: float) -> NSBezierPath:
    x, y = center
    return NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(x - radius, y - radius, 2 * radius, 2 * radius)
    )


def polyline(points, closed: bool = False) -> NSBezierPath:
    path = NSBezierPath.bezierPath()
    path.moveToPoint_(NSMakePoint(*points[0]))
    for point in points[1:]:
        path.lineToPoint_(NSMakePoint(*point))
    if closed:
        path.closePath()
    return path


def stroke(path: NSBezierPath, color=LINE, sharp: bool = False) -> None:
    """Stroke path; sharp keeps its corners square (the scanner is boxy)."""
    path.setLineWidth_(STROKE)
    path.setLineCapStyle_(NSLineCapStyleRound)
    path.setLineJoinStyle_(NSLineJoinStyleMiter if sharp else NSLineJoinStyleRound)
    rgb(color).set()
    path.stroke()


def gradient(path: NSBezierPath) -> None:
    """Fill path with the background, aligned to the whole icon."""
    NSGraphicsContext.saveGraphicsState()
    path.addClip()
    NSGradient.alloc().initWithStartingColor_endingColor_(
        rgb(BACKGROUND[0]), rgb(BACKGROUND[1])
    ).drawInRect_angle_(BODY, -90)
    NSGraphicsContext.restoreGraphicsState()


def background(body: NSBezierPath) -> None:
    shadow = NSShadow.alloc().init()
    shadow.setShadowColor_(rgb((0, 0, 0), 0.35))
    shadow.setShadowOffset_(NSMakeSize(0, -12))
    shadow.setShadowBlurRadius_(28)
    NSGraphicsContext.saveGraphicsState()
    shadow.set()
    rgb(BACKGROUND[1]).set()
    body.fill()
    NSGraphicsContext.restoreGraphicsState()
    gradient(body)


def scanner() -> None:
    for outline in (CHUTE, BODY_FACE, TRAY):  # back to front
        shape = polyline(outline, closed=True)
        gradient(shape)  # hides what is behind it
        stroke(shape, sharp=True)
    rgb(LIGHT_COLOR).set()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        LIGHT, LIGHT.size.height / 2, LIGHT.size.height / 2
    ).fill()


def fan() -> None:
    gradient(circle(BADGE_CENTER, BADGE_RADIUS))  # hides lines behind the fan
    rgb(FAN_COLOR).set()
    circle(FAN_CENTER, FAN_DOT).fill()
    for radius in FAN_RADII:
        arc = NSBezierPath.bezierPath()
        arc.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_(
            NSMakePoint(*FAN_CENTER), radius, *FAN_SPAN
        )
        stroke(arc, FAN_COLOR)


def bitmap() -> NSBitmapImageRep:
    """The icon at CANVAS pixels square."""
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, CANVAS, CANVAS, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
    )
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(
        NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    )
    body = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(BODY, CORNER, CORNER)
    background(body)
    NSGraphicsContext.saveGraphicsState()
    body.addClip()
    scanner()
    fan()
    NSGraphicsContext.restoreGraphicsState()
    NSGraphicsContext.restoreGraphicsState()
    return rep


def image() -> NSImage:
    icon = NSImage.alloc().initWithSize_(NSMakeSize(CANVAS, CANVAS))
    icon.addRepresentation_(bitmap())
    return icon


def write_png(path: Path) -> None:
    png = bitmap().representationUsingType_properties_(NSPNGFileType, {})
    png.writeToFile_atomically_(str(path), True)


if __name__ == "__main__":
    write_png(Path(sys.argv[1]))
