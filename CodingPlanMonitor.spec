# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for CodingPlanMonitor.

Goal: keep the exe small by including ONLY the Qt modules we actually use
(Core/Gui/Widgets) and excluding everything else (WebEngine, QML, Quick,
Multimedia, Pdf, Charts, 3D, Designer, translations, resources, etc.).
"""

import os
from PySide6 import __file__ as pyside_init

pyside_dir = os.path.dirname(os.path.abspath(pyside_init))

# Qt DLLs we DO need (Core/Gui/Widgets + their direct deps).
# Everything else is excluded by the excludes list + the prune step below.
keep_qt_dll_prefixes = (
    "Qt6Core", "Qt6Gui", "Qt6Widgets",
    "Qt6Network",  # used by Qt internals
    "Qt6Svg",      # QSvgWidget not used, but icon rendering may pull it
    "libssl", "libcrypto",  # requests needs ssl
)

# Heavy PySide6 submodules to drop entirely (we only use QtWidgets/QtCore/QtGui).
excludes = [
    # PySide6 modules we don't import
    "PySide6.Qt3DAnimation", "PySide6.Qt3DCore", "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput", "PySide6.Qt3DLogic", "PySide6.Qt3DRender",
    "PySide6.QtBluetooth", "PySide6.QtCharts", "PySide6.QtConcurrent",
    "PySide6.QtDataVisualization", "PySide6.QtDesigner",
    "PySide6.QtHelp", "PySide6.QtHttpServer", "PySide6.QtLocation",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
    "PySide6.QtNetworkAuth", "PySide6.QtNfc", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtPositioning", "PySide6.QtPrintSupport",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2", "PySide6.QtQuickWidgets",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSensors",
    "PySide6.QtSerialBus", "PySide6.QtSerialPort", "PySide6.QtShaderTools",
    "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtStateMachine",
    "PySide6.QtSvg", "PySide6.QtSvgWidgets", "PySide6.QtTest",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineQuick",
    "PySide6.QtWebEngineWidgets", "PySide6.QtWebSockets",
    "PySide6.QtWebView", "PySide6.QtWebViewQuick",
    "PySide6.QtXml", "PySide6.QtAxContainer",
    # stdlib / third-party we don't use
    "tkinter", "unittest", "pydoc", "pydoc_data", "xmlrpc",
]


def _prune_pyside_binaries(binaries):
    """Remove heavy Qt DLLs/resources that PyInstaller collects by default."""
    pruned = []
    for entry in binaries:
        # PyInstaller binaries are (dest, src, kind) tuples.
        dest, src = entry[0], entry[1]
        name = os.path.basename(src).lower()
        # Drop the giant WebEngine / Quick / 3D / multimedia DLLs.
        drop_prefixes = (
            "qt6webengine", "qt6quick", "qt6quick3d", "qt6qml",
            "qt6pdf", "qt6charts", "qt6datavis", "qt6designer",
            "qt6help", "qt6multimedia", "qt6virtualkeyboard",
            "qt6shadertools", "qt6scxml", "qt6sensors", "qt6serial",
            "qt6svg", "qt6test", "qt6uitools", "qt6webchannel",
            "qt6websockets", "qt6webview", "qt6xml", "qt6opengl",
            "qt6printsupport", "qt6sql", "qt6bluetooth", "qt6nfc",
            "qt6positioning", "qt6location", "qt6remoteobjects",
            "opengl32sw", "opengl32sw_",
            "qt6networkauth", "qt6httpserver", "qt6spatialaudio",
            "qt3d", "avcodec", "avformat", "avutil", "swscale", "swresample",
        )
        # Drop translation files, QML files, resources dumps.
        drop_dirs = ("translations", "qml", "resources", "metatypes",
                     "egl", "designer")
        if any(name.startswith(p) for p in drop_prefixes):
            continue
        if any(f"/{d}/" in src.replace("\\", "/").lower() or
               f"\\{d}\\" in src.lower() for d in drop_dirs):
            continue
        pruned.append(entry)
    return pruned


a = Analysis(
    ["monitor.py"],
    pathex=[],
    binaries=[],
    # Bundle the read-only icon assets (combo arrows, delete icon) into the
    # single-file exe. At runtime they are resolved via sys._MEIPASS
    # (see _resource_dir() in monitor.py).
    datas=[("icon", "icon")],
    hiddenimports=["PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    # Optimize: strip docstrings/asserts.
    optimize=2,
)
# Prune the binaries PyInstaller auto-collected from PySide6.
a.binaries = _prune_pyside_binaries(a.binaries)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CodingPlanMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # windowed, no console window
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Application icon (shows in Explorer / taskbar / desktop shortcut).
    icon="image/cpw_icon.ico",
)
