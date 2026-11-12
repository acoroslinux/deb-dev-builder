#!/usr/bin/env python3
"""Render the actual Calamares slideshow without starting an installation.

Requires PyQt6 and the QML slideshow module shipped by Calamares. The latter
can also be supplied from an extracted Calamares package with --qml-import-path.
"""
import argparse
import os
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qml-import-path', type=Path, default=Path('/usr/share/calamares/qml'))
    parser.add_argument('--size', default='720x440')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    width, height = map(int, args.size.split('x'))
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    os.environ.setdefault('QT_QUICK_BACKEND', 'software')
    try:
        from PyQt6.QtCore import QEventLoop, QMetaObject, QObject, QTimer, QUrl
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtQuick import QQuickItem, QQuickView
    except ImportError:
        parser.error('Preview requires PyQt6, including its QtQuick bindings; use a separate development environment.')
    app = QGuiApplication(sys.argv[:1])
    view = QQuickView()
    view.engine().addImportPath(str(args.qml_import_path.resolve()))
    view.setResizeMode(QQuickView.ResizeMode.SizeRootObjectToView)
    view.resize(width, height)
    source = Path(__file__).resolve().parents[1] / 'configs/custom_files/calamares/branding/deb-dev-modern/show.qml'
    view.setSource(QUrl.fromLocalFile(str(source)))
    if view.status() == QQuickView.Status.Error:
        raise RuntimeError('\n'.join(error.toString() for error in view.errors()))
    view.show()
    root = view.rootObject()
    QMetaObject.invokeMethod(root, 'onActivate')
    slides = root.property('slides').toVariant()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for index, slide in enumerate(slides):
        root.setProperty('currentSlide', index)
        wait = QEventLoop()
        QTimer.singleShot(700, wait.quit)
        wait.exec()
        copy = slide.findChild(QQuickItem, 'slideCopy')
        assert copy is not None, 'Slide content was not created'
        assert copy.y() >= 0 and copy.y() + copy.height() <= height - 52, 'Slide text overlaps the footer'
        image = view.grabWindow()
        if image.isNull():
            raise RuntimeError('Qt did not render a slideshow frame')
        path = args.output_dir / ('slide-%02d.png' % (index + 1))
        if not image.save(str(path)):
            raise RuntimeError('Cannot save ' + str(path))
        print(path)
    timer = root.findChild(QObject, 'slideTimer')
    QMetaObject.invokeMethod(root, 'onLeave')
    assert not timer.property('running'), 'Slideshow timer continues after leaving installation'
    assert not root.property('activatedInCalamares')
    view.close()
    app.processEvents()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
